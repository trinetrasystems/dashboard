"""
Trinetra Systems Monitor — central Jetson backend.

Pipeline:
  Edge Jetsons --rsync--> /data/incoming/  --watchdog--> SQLite
                                                          ↓
                                                       FastAPI <--HTTP--> browser
                                                          ↓
                                                  loitering check loop
                                                          ↓
                                                  Telegram / Email alerts

Run locally:
  python3 app.py
"""
import asyncio
import csv
import uuid
import io
import json
import mimetypes
import random
import os
import re
import shutil
import smtplib
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import config
import notifications as notif
from notifications import NotificationService

# ─── Minimal valid 1×1 white JPEG (used by the test data generator) ──────────
# This is a real, parseable JPEG so Telegram and browsers accept it.
MINIMAL_JPEG = bytes([
    0xFF,0xD8,0xFF,0xE0,0x00,0x10,0x4A,0x46,0x49,0x46,0x00,0x01,0x01,0x00,
    0x00,0x01,0x00,0x01,0x00,0x00,0xFF,0xDB,0x00,0x43,0x00,0x08,0x06,0x06,
    0x07,0x06,0x05,0x08,0x07,0x07,0x07,0x09,0x09,0x08,0x0A,0x0C,0x14,0x0D,
    0x0C,0x0B,0x0B,0x0C,0x19,0x12,0x13,0x0F,0x14,0x1D,0x1A,0x1F,0x1E,0x1D,
    0x1A,0x1C,0x1C,0x20,0x24,0x2E,0x27,0x20,0x22,0x2C,0x23,0x1C,0x1C,0x28,
    0x37,0x29,0x2C,0x30,0x31,0x34,0x34,0x34,0x1F,0x27,0x39,0x3D,0x38,0x32,
    0x3C,0x2E,0x33,0x34,0x32,0xFF,0xC0,0x00,0x0B,0x08,0x00,0x01,0x00,0x01,
    0x01,0x01,0x11,0x00,0xFF,0xC4,0x00,0x1F,0x00,0x00,0x01,0x05,0x01,0x01,
    0x01,0x01,0x01,0x01,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x01,0x02,
    0x03,0x04,0x05,0x06,0x07,0x08,0x09,0x0A,0x0B,0xFF,0xC4,0x00,0x35,0x10,
    0x00,0x02,0x01,0x03,0x02,0x04,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x00,0x00,0x01,0x02,0x03,0x11,0x00,0x04,0x12,0x21,0x31,0x41,0x05,0x13,
    0x51,0x61,0x22,0x71,0x81,0x91,0xA1,0xB1,0xC1,0xD1,0xFF,0xDA,0x00,0x08,
    0x01,0x01,0x00,0x00,0x3F,0x00,0xF5,0x57,0xFF,0xD9,
])

# ════════════════════════════════════════════════════════════════════════════
# DATABASE
# ════════════════════════════════════════════════════════════════════════════

_db_lock = threading.Lock()  # SQLite is fine with WAL but we serialize writes


def db() -> sqlite3.Connection:
    """Open a SQLite connection with sane pragmas."""
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Apply schema.sql once. Idempotent. Also migrates older DBs."""
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    schema = Path(__file__).parent / "schema.sql"
    conn = db()
    try:
        # ── PRE-SCHEMA MIGRATIONS ────────────────────────────────────────────
        # These MUST run before executescript(). CREATE TABLE IF NOT EXISTS is a
        # no-op on a table that already exists, so a newly added column never
        # appears — and schema.sql then tries to index it and the whole startup
        # fails. Adding the column first makes the script safe on old and new
        # databases alike. (On a fresh DB the table doesn't exist yet, so the
        # guard below skips and executescript creates it complete.)
        ucols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if ucols and "firebase_uid" not in ucols:
            print("[migrate] adding firebase_uid column to users")
            conn.execute("ALTER TABLE users ADD COLUMN firebase_uid TEXT")
            conn.commit()

        conn.executescript(schema.read_text())

        # ── POST-SCHEMA MIGRATIONS ───────────────────────────────────────────
        cols = [r[1] for r in conn.execute("PRAGMA table_info(detections)").fetchall()]
        if "session_id" not in cols:
            print("[migrate] adding session_id column to detections")
            conn.execute("ALTER TABLE detections ADD COLUMN session_id INTEGER REFERENCES sessions(id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_det_session ON detections(session_id)")
        conn.commit()
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════════
# WEBSOCKET broadcaster — live updates to dashboards
# ════════════════════════════════════════════════════════════════════════════

class WSManager:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self.lock:
            self.clients.add(ws)

    async def disconnect(self, ws: WebSocket):
        async with self.lock:
            self.clients.discard(ws)

    async def broadcast(self, msg: dict):
        data = json.dumps(msg, default=str)
        dead = set()
        async with self.lock:
            for ws in self.clients:
                try:
                    await ws.send_text(data)
                except Exception:
                    dead.add(ws)
            self.clients -= dead


wsm = WSManager()
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def broadcast_sync(msg: dict):
    """Schedule a WebSocket broadcast from a non-async thread (file watcher)."""
    if _main_loop is None:
        return
    asyncio.run_coroutine_threadsafe(wsm.broadcast(msg), _main_loop)


# ════════════════════════════════════════════════════════════════════════════
# INGEST — turn a file into a detection + session update
# ════════════════════════════════════════════════════════════════════════════

def parse_filename(name: str):
    """Return (camera, badge, datetime) or None."""
    m = config.FILENAME_RE.match(name)
    if not m:
        return None
    try:
        # Filename timestamp is LOCAL time on the edge Jetson.
        # Convert to UTC for consistent storage (server stores everything in UTC).
        dt_local = datetime.strptime(m["ts"], "%Y%m%d-%H%M%S")
        # now()-utcnow() is a few microseconds short of the true offset (the two
        # calls don't happen at the same instant). Rounding to whole seconds
        # keeps stored timestamps exact — otherwise 10:00:00 is stored as
        # 09:59:59.999982 and every display truncates it back to 09:59:59.
        offset = datetime.now() - datetime.utcnow()
        local_offset = timedelta(seconds=round(offset.total_seconds()))
        dt = dt_local - local_offset
    except ValueError:
        return None
    return m["camera"], m["badge"], dt


def update_session(conn, badge: str, camera: str, ts: datetime, is_exit: bool):
    """
    Find open session for badge, extend it OR close it (on exit) OR open new one.
    Returns (session_id, detection_type, is_loitering, prev_camera)
      where type is 'entry' | 'sighting' | 'exit',
      is_loitering=True if the session already had alert_sent=1,
      and prev_camera is the camera this badge was last seen at (None on a
      brand-new session) — used to build movement alerts.
    """
    gap = timedelta(minutes=config.SESSION_GAP_MINUTES)

    row = conn.execute(
        "SELECT id, last_seen, cameras_json, alert_sent, first_seen, last_camera FROM sessions "
        "WHERE badge=? AND closed=0 ORDER BY id DESC LIMIT 1",
        (badge,)
    ).fetchone()

    if is_exit and row:
        # Close the session immediately on exit-gate detection.
        cams = json.loads(row["cameras_json"] or "{}")
        cams[camera] = cams.get(camera, 0) + 1
        conn.execute(
            "UPDATE sessions SET last_seen=?, last_camera=?, "
            "total_sightings=total_sightings+1, cameras_json=?, "
            "closed=1, closed_at=? WHERE id=?",
            (ts.isoformat(), camera, json.dumps(cams), ts.isoformat(), row["id"])
        )
        return row["id"], "exit", False, row["last_camera"]

    if row:
        last_seen = datetime.fromisoformat(row["last_seen"])
        if (ts - last_seen) < gap:
            # Extend existing session
            cams = json.loads(row["cameras_json"] or "{}")
            cams[camera] = cams.get(camera, 0) + 1
            conn.execute(
                "UPDATE sessions SET last_seen=?, last_camera=?, "
                "total_sightings=total_sightings+1, cameras_json=? WHERE id=?",
                (ts.isoformat(), camera, json.dumps(cams), row["id"])
            )
            already_loitering = bool(row["alert_sent"])
            return row["id"], "sighting", already_loitering, row["last_camera"]
        # Gap too long → close old session, open new one below
        conn.execute("UPDATE sessions SET closed=1, closed_at=? WHERE id=?",
                     (ts.isoformat(), row["id"]))

    if is_exit:
        # Exit on a badge with no open session — just log it, no session
        return None, "exit", False, None

    cur = conn.execute(
        "INSERT INTO sessions(badge, first_seen, last_seen, last_camera, cameras_json) "
        "VALUES (?,?,?,?,?)",
        (badge, ts.isoformat(), ts.isoformat(), camera, json.dumps({camera: 1}))
    )
    return cur.lastrowid, "entry", False, None


def camera_label(camera: Optional[str]) -> str:
    """'cam-electrical-bay' → 'Electrical Bay', for readable alert text."""
    if not camera:
        return "Unknown"
    return re.sub(r"^cam[-_]", "", camera).replace("-", " ").replace("_", " ").title()


def build_movement(badge: str, camera: str, prev_camera: Optional[str],
                   det_type: str) -> Optional[dict]:
    """Classify one detection as a movement event, or None if it isn't one.

    Movement is a change of location — so a repeat sighting at the SAME camera
    produces nothing, no matter how many frames arrive. Entry (first sighting of
    a visit) and exit-gate hits are reported because they are location changes
    too: from outside in, and from inside out.
    """
    to_label = camera_label(camera)

    if det_type == "entry" or not prev_camera:
        return {
            "event": "entry",
            "from_camera": None,
            "to_camera": camera,
            "statement": f"{badge} entered at {to_label}.",
        }

    from_label = camera_label(prev_camera)

    if det_type == "exit":
        return {
            "event": "exit",
            "from_camera": prev_camera,
            "to_camera": camera,
            "statement": (f"{badge} exited via {to_label}."
                          if prev_camera == camera else
                          f"{badge} moved from {from_label} and exited via {to_label}."),
        }

    if prev_camera != camera:
        return {
            "event": "move",
            "from_camera": prev_camera,
            "to_camera": camera,
            "statement": f"{badge} moved from {from_label} into {to_label}.",
        }

    return None   # same camera as last time — not a movement


def handle_new_file(path: Path):
    """Process one image file: parse → insert → update session → archive."""
    parsed = parse_filename(path.name)
    if not parsed:
        # Move bad filenames to dead-letter so they don't keep retriggering
        Path(config.DEAD_LETTER_DIR).mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(path), Path(config.DEAD_LETTER_DIR) / path.name)
        except FileNotFoundError:
            pass
        print(f"[ingest] bad filename → dead-letter: {path.name}")
        return

    camera, badge, ts = parsed
    session_id, det_type, is_loitering = None, None, False
    movement = None

    with _db_lock:
        conn = db()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO detections(filename, camera, badge, timestamp, type) "
                    "VALUES (?,?,?,?,'sighting')",
                    (path.name, camera, badge, ts.isoformat())
                )
                if cur.rowcount == 0:
                    return  # duplicate filename
                detection_id = cur.lastrowid
                is_exit = camera in config.EXIT_CAMERAS
                session_id, det_type, is_loitering, prev_camera = update_session(
                    conn, badge, camera, ts, is_exit
                )
                # Stamp the detection with its session (visit) ID and final type
                conn.execute(
                    "UPDATE detections SET type=?, session_id=? WHERE id=?",
                    (det_type, session_id, detection_id)
                )

                # Record a movement whenever the badge changed location.
                movement = build_movement(badge, camera, prev_camera, det_type)
                if movement:
                    movement.update({
                        "badge": badge,
                        "session_id": session_id,
                        "detection_id": detection_id,
                        "timestamp": ts.isoformat(),
                        "filename": path.name,
                    })
                    mcur = conn.execute(
                        "INSERT INTO movements(badge, session_id, detection_id, from_camera,"
                        " to_camera, event, statement, timestamp, filename) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (badge, session_id, detection_id, movement["from_camera"],
                         movement["to_camera"], movement["event"], movement["statement"],
                         ts.isoformat(), path.name)
                    )
                    movement["id"] = mcur.lastrowid
                # If this badge is already loitering, fetch session info for re-alert
                # but ONLY if the badge is not whitelisted (may have been added after first alert)
                is_whitelisted = conn.execute(
                    "SELECT 1 FROM whitelist WHERE badge=?", (badge,)
                ).fetchone() is not None

                if (config.LOITER_ALERTS and is_loitering and session_id
                        and det_type == "sighting" and not is_whitelisted):
                    sess_row = conn.execute(
                        "SELECT first_seen, last_seen, last_camera FROM sessions WHERE id=?",
                        (session_id,)
                    ).fetchone()
                    loiter_resight = {
                        "session_id": session_id,
                        "badge": badge,
                        "first_seen": sess_row["first_seen"],
                        "last_seen": sess_row["last_seen"],
                        "last_camera": sess_row["last_camera"],
                        "last_image": path.name,  # new image (will be archived below)
                        "duration_seconds": int(
                            (datetime.utcnow() - datetime.fromisoformat(sess_row["first_seen"])).total_seconds()
                        ),
                    }
                else:
                    loiter_resight = None
                    if is_loitering and is_whitelisted:
                        print(f"[loiter] re-alert suppressed: {badge} is now whitelisted")
        finally:
            conn.close()

    # Move image to archive (served at /images/<filename>)
    Path(config.ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(path), Path(config.ARCHIVE_DIR) / path.name)
    except FileNotFoundError:
        pass

    broadcast_sync({
        "type": "detection",
        "filename": path.name,
        "camera": camera,
        "badge": badge,
        "timestamp": ts.isoformat(),
        "session_id": session_id,
        "detection_type": det_type,
    })

    # Movement alert — also fired after archiving so the photo is readable.
    if movement and config.MOVEMENT_ALERTS and _main_loop:
        print(f"[movement] {movement['statement']}")
        asyncio.run_coroutine_threadsafe(fire_movement_alert(movement), _main_loop)
    elif movement:
        broadcast_sync({"type": "movement", **movement})

    # Fire re-alert AFTER archiving so the image file exists when Telegram reads it
    if loiter_resight and _main_loop:
        print(f"[loiter] re-alert: {badge} still loitering, new sighting at {camera}")
        asyncio.run_coroutine_threadsafe(
            fire_loiter_resight(loiter_resight), _main_loop
        )


# ════════════════════════════════════════════════════════════════════════════
# FILE WATCHER (inotify-based via watchdog)
# ════════════════════════════════════════════════════════════════════════════

class IncomingHandler(FileSystemEventHandler):
    def on_closed(self, event):
        if not event.is_directory and event.src_path.endswith(".jpg"):
            handle_new_file(Path(event.src_path))

    def on_moved(self, event):
        # rsync writes to .tmp then renames — this is what fires
        if not event.is_directory and event.dest_path.endswith(".jpg"):
            handle_new_file(Path(event.dest_path))


def scan_existing():
    """At startup, process any files left in incoming/ from a previous run."""
    incoming = Path(config.INCOMING_DIR)
    incoming.mkdir(parents=True, exist_ok=True)
    files = sorted(incoming.glob("*.jpg"))
    if files:
        print(f"[ingest] catching up on {len(files)} leftover file(s)")
    for f in files:
        handle_new_file(f)


def start_watcher() -> Observer:
    Path(config.INCOMING_DIR).mkdir(parents=True, exist_ok=True)
    obs = Observer()
    obs.schedule(IncomingHandler(), config.INCOMING_DIR, recursive=False)
    obs.start()
    print(f"[ingest] watching {config.INCOMING_DIR}")
    return obs


# ════════════════════════════════════════════════════════════════════════════
# MOVEMENT ALERT — fires on every camera-to-camera transition (no time threshold)
# ════════════════════════════════════════════════════════════════════════════

_MOVEMENT_STYLE = {
    "entry": ("🟢", "ENTERED"),
    "move":  ("🚗", "MOVEMENT"),
    "exit":  ("🔵", "EXITED"),
}


async def fire_movement_alert(movement: dict):
    """Notify that a badge changed camera location, and broadcast to dashboards."""
    icon, heading = _MOVEMENT_STYLE.get(movement["event"], ("🚗", "MOVEMENT"))

    ts_local = _utc_to_local(datetime.fromisoformat(movement["timestamp"]))
    route = (f"{camera_label(movement['from_camera'])} → {camera_label(movement['to_camera'])}"
             if movement["from_camera"] else camera_label(movement["to_camera"]))

    text = (
        f"{icon} {heading}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{movement['statement']}\n\n"
        f"🪪 Badge  : {movement['badge']}\n"
        f"📍 Route  : {route}\n"
        f"🕐 Time   : {ts_local.strftime('%d %b %Y  %H:%M:%S')}"
    )

    sent = False
    if config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_IDS:
        photo_path = Path(config.ARCHIVE_DIR) / movement["filename"] if movement.get("filename") else None
        has_photo = bool(photo_path and photo_path.exists() and photo_path.stat().st_size > 0)
        for chat_id in config.TELEGRAM_CHAT_IDS:
            try:
                if has_photo:
                    await _send_telegram_photo(chat_id, text, photo_path)
                else:
                    await _send_telegram(chat_id, text)
                sent = True
            except Exception as e:
                print(f"[movement] photo send failed for chat_id={chat_id}: {e} — falling back to text")
                try:
                    await _send_telegram(chat_id, text)
                    sent = True
                except Exception as e2:
                    print(f"[movement] text fallback also failed for chat_id={chat_id}: {e2}")

    if config.SMTP_USER and config.SMTP_TO and config.SMTP_PASS:
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, _send_email, f"Trinetra · {heading} · {movement['badge']}", text
            )
            sent = True
        except Exception as e:
            print(f"[movement] email failed: {e}")

    if sent and movement.get("id"):
        _mark_movement_notified(movement["id"])

    # FCM push — same event, delivered per-user to phones/browsers. Runs in a
    # worker thread because the Admin SDK is blocking. Every user with
    # notifications enabled is a recipient; per-user routing rules can replace
    # broadcast() here without touching the sender.
    await asyncio.get_event_loop().run_in_executor(
        None, NotificationService.broadcast, {
            "title": "Vehicle Movement Detected",
            "body": movement["statement"],
            "data": {
                "eventId": f"MOV_{movement.get('id', 0)}",
                "eventType": "BAY_MOVEMENT",
                "vehicleNumber": movement["badge"],
                "fromBay": (movement.get("from_camera") or ""),
                "toBay": movement["to_camera"],
                "sessionId": str(movement.get("session_id") or ""),
            },
            "link": f"/visits.html?visit={movement.get('session_id') or ''}",
        }
    )

    await wsm.broadcast({"type": "movement", **movement})


def _mark_movement_notified(movement_id: int):
    with _db_lock:
        conn = db()
        try:
            with conn:
                conn.execute("UPDATE movements SET notified=1 WHERE id=?", (movement_id,))
        finally:
            conn.close()


# ════════════════════════════════════════════════════════════════════════════
# LOITERING RE-ALERT — fires immediately on new detection for loitering badge
# ════════════════════════════════════════════════════════════════════════════

async def fire_loiter_resight(resight: dict):
    """Send a follow-up alert when a loitering badge is spotted again."""
    badge = resight["badge"]
    duration_min = resight["duration_seconds"] // 60
    duration_sec = resight["duration_seconds"] % 60

    local_offset = datetime.now() - datetime.utcnow()
    first_utc = datetime.fromisoformat(resight["first_seen"])
    first_local = first_utc + local_offset

    last_seen_str = "—"
    if resight.get("last_seen"):
        last_local = datetime.fromisoformat(resight["last_seen"]) + local_offset
        last_seen_str = last_local.strftime("%d %b %Y  %H:%M:%S")

    last_camera = resight.get("last_camera") or "unknown"

    text = (
        f"🔴 LOITERING RE-SIGHTED\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪪 Badge       : {badge}\n"
        f"⏱ Duration    : {duration_min}m {duration_sec:02d}s\n"
        f"🕐 Entry time  : {first_local.strftime('%d %b %Y  %H:%M:%S')}\n"
        f"🕒 Last seen   : {last_seen_str}\n"
        f"📷 Last camera : {last_camera}"
    )

    if config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_IDS:
        photo_path = None
        if resight.get("last_image"):
            photo_path = Path(config.ARCHIVE_DIR) / resight["last_image"]

        has_photo = photo_path and photo_path.exists() and photo_path.stat().st_size > 0
        for chat_id in config.TELEGRAM_CHAT_IDS:
            try:
                if has_photo:
                    await _send_telegram_photo(chat_id, text, photo_path)
                else:
                    await _send_telegram(chat_id, text)
                print(f"[loiter] re-alert sent to chat_id={chat_id} for badge={badge}")
            except Exception as e:
                print(f"[loiter] photo send failed for chat_id={chat_id}: {e} — falling back to text")
                try:
                    await _send_telegram(chat_id, text)
                    print(f"[loiter] text fallback sent to chat_id={chat_id}")
                except Exception as e2:
                    print(f"[loiter] text fallback also failed for chat_id={chat_id}: {e2}")

    await wsm.broadcast({"type": "loitering_resight", **resight})


# ════════════════════════════════════════════════════════════════════════════
# LOITERING CHECKER (runs every 30s)
# ════════════════════════════════════════════════════════════════════════════

async def loitering_loop():
    while True:
        try:
            await check_loitering()
        except Exception as e:
            print(f"[loiter] error: {e}")
        await asyncio.sleep(30)


async def check_loitering():
    alerts_to_send = []

    with _db_lock:
        conn = db()
        try:
            threshold = timedelta(minutes=config.LOITER_MINUTES)
            now = datetime.utcnow()

            # ── STEP 1: Loitering alert check (runs BEFORE stale-close) ──────
            # A session qualifies if first_seen is old enough, regardless of
            # how old last_seen is. This matters when a back-dated image file
            # is dropped: last_seen == ts (in the past), but first_seen also
            # shows the badge has been here long enough to loiter.
            #
            # Skipped entirely when LOITER_ALERTS is off. STEP 2 below still
            # runs — session housekeeping must not depend on this switch.
            rows = conn.execute("""
                SELECT id, badge, first_seen, last_seen, last_camera FROM sessions
                WHERE closed=0 AND alert_sent=0
                  AND badge NOT IN (SELECT badge FROM whitelist)
            """).fetchall() if config.LOITER_ALERTS else []

            for r in rows:
                first = datetime.fromisoformat(r["first_seen"])
                if (now - first) >= threshold:
                    duration = int((now - first).total_seconds())
                    with conn:
                        cur = conn.execute(
                            "INSERT INTO alerts(session_id, badge, triggered_at, duration_seconds) "
                            "VALUES (?,?,?,?)",
                            (r["id"], r["badge"], now.isoformat(), duration)
                        )
                        conn.execute("UPDATE sessions SET alert_sent=1 WHERE id=?", (r["id"],))
                        last_image_row = conn.execute(
                            "SELECT filename FROM detections WHERE session_id=? ORDER BY timestamp DESC LIMIT 1",
                            (r["id"],)
                        ).fetchone()
                        last_image = last_image_row["filename"] if last_image_row else None
                    alerts_to_send.append({
                        "alert_id": cur.lastrowid,
                        "session_id": r["id"],
                        "badge": r["badge"],
                        "first_seen": r["first_seen"],
                        "last_seen": r["last_seen"],
                        "last_camera": r["last_camera"],
                        "duration_seconds": duration,
                        "last_image": last_image,
                    })

            # ── STEP 2: Close stale sessions (runs AFTER loitering check) ────
            # A session is stale if last_seen is older than SESSION_AUTO_CLOSE_MINUTES
            # AND it has NOT triggered a loitering alert (alert_sent=0).
            #
            # Sessions with alert_sent=1 are intentionally kept open so the loitering
            # banner stays visible until the badge is explicitly seen at an exit camera.
            # This directly implements the use-case:
            #   "banner must stay visible until badge exits through exit camera".
            #
            # A back-dated file drop sets last_seen = file_ts (appears stale), but
            # STEP 1 above just set alert_sent=1 for it — so STEP 2 won't close it.
            stale_cutoff = (now - timedelta(minutes=config.SESSION_AUTO_CLOSE_MINUTES)).isoformat()
            with conn:
                conn.execute(
                    "UPDATE sessions SET closed=1, closed_at=? "
                    "WHERE closed=0 AND alert_sent=0 AND last_seen < ?",
                    (now.isoformat(), stale_cutoff)
                )
        finally:
            conn.close()

    for a in alerts_to_send:
        await fire_alert(a)


async def fire_alert(alert: dict):
    """Send notifications and broadcast over WebSocket."""
    badge = alert["badge"]
    duration_min = alert["duration_seconds"] // 60
    duration_sec = alert["duration_seconds"] % 60

    # Times are stored as UTC in DB — convert to local time for display
    local_offset = datetime.now() - datetime.utcnow()
    first_utc = datetime.fromisoformat(alert["first_seen"])
    first_local = first_utc + local_offset

    last_seen_str = "—"
    if alert.get("last_seen"):
        last_utc = datetime.fromisoformat(alert["last_seen"])
        last_local = last_utc + local_offset
        last_seen_str = last_local.strftime("%d %b %Y  %H:%M:%S")

    last_camera = alert.get("last_camera") or "unknown"

    text = (
        f"⚠️ LOITERING ALERT\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪪 Badge       : {badge}\n"
        f"⏱ Duration    : {duration_min}m {duration_sec:02d}s\n"
        f"🕐 Entry time  : {first_local.strftime('%d %b %Y  %H:%M:%S')}\n"
        f"🕒 Last seen   : {last_seen_str}\n"
        f"📷 Last camera : {last_camera}"
    )

    if config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_IDS:
        try:
            photo_path = None
            last_image = alert.get("last_image")
            print(f"[alert] last_image from DB: {last_image}")
            if last_image:
                photo_path = Path(config.ARCHIVE_DIR) / last_image
                print(f"[alert] photo_path: {photo_path} | exists: {photo_path.exists()} | size: {photo_path.stat().st_size if photo_path.exists() else 'N/A'}")

            has_photo = photo_path and photo_path.exists() and photo_path.stat().st_size > 0
            print(f"[alert] sending to {len(config.TELEGRAM_CHAT_IDS)} recipient(s): {config.TELEGRAM_CHAT_IDS}")
            for chat_id in config.TELEGRAM_CHAT_IDS:
                try:
                    if has_photo:
                        await _send_telegram_photo(chat_id, text, photo_path)
                    else:
                        await _send_telegram(chat_id, text)
                    print(f"[alert] sent to chat_id={chat_id}")
                except Exception as e:
                    print(f"[alert] photo send failed for chat_id={chat_id}: {e} — falling back to text")
                    try:
                        await _send_telegram(chat_id, text)
                        print(f"[alert] text fallback sent to chat_id={chat_id}")
                    except Exception as e2:
                        print(f"[alert] text fallback also failed for chat_id={chat_id}: {e2}")
            _mark_alert(alert["alert_id"], "telegram_sent")
        except Exception as e:
            print(f"[alert] telegram setup failed: {e}")

    if config.SMTP_USER and config.SMTP_TO and config.SMTP_PASS:
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, _send_email, "Trinetra Loitering Alert", text
            )
            _mark_alert(alert["alert_id"], "email_sent")
        except Exception as e:
            print(f"[alert] email failed: {e}")

    await wsm.broadcast({"type": "loitering_alert", **alert})


def _mark_alert(alert_id: int, column: str):
    with _db_lock:
        conn = db()
        try:
            with conn:
                conn.execute(f"UPDATE alerts SET {column}=1 WHERE id=?", (alert_id,))
        finally:
            conn.close()


async def _send_telegram(chat_id: str, text: str):
    token = config.TELEGRAM_TOKEN
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text
    }).encode()

    def _send():
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=5).read()

    await asyncio.get_event_loop().run_in_executor(None, _send)


async def _send_telegram_photo(chat_id: str, text: str, photo_path: Path):
    token = config.TELEGRAM_TOKEN
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    boundary = uuid.uuid4().hex
    headers = {'Content-type': f'multipart/form-data; boundary={boundary}'}

    with open(photo_path, 'rb') as f:
        photo_data = f.read()

    CRLF = b'\r\n'
    body = bytearray()

    def field(name, value):
        body.extend(f'--{boundary}'.encode() + CRLF)
        body.extend(f'Content-Disposition: form-data; name="{name}"'.encode() + CRLF)
        body.extend(CRLF)
        body.extend(value.encode('utf-8') if isinstance(value, str) else value)
        body.extend(CRLF)

    field('chat_id', chat_id)
    field('caption', text)

    filename = photo_path.name
    body.extend(f'--{boundary}'.encode() + CRLF)
    body.extend(f'Content-Disposition: form-data; name="photo"; filename="{filename}"'.encode() + CRLF)
    body.extend(b'Content-Type: image/jpeg' + CRLF)
    body.extend(CRLF)
    body.extend(photo_data)
    body.extend(CRLF)
    body.extend(f'--{boundary}--'.encode() + CRLF)

    def _send():
        req = urllib.request.Request(url, data=bytes(body), headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            print(f"[alert] Telegram sendPhoto response: {resp.status}")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode('utf-8', errors='replace')
            print(f"[alert] Telegram sendPhoto HTTP error {e.code}: {err_body}")
            raise

    await asyncio.get_event_loop().run_in_executor(None, _send)


def _send_email(subject: str, body: str):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_USER
    msg["To"] = config.SMTP_TO
    msg.set_content(body)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as s:
        s.starttls()
        s.login(config.SMTP_USER, config.SMTP_PASS)
        s.send_message(msg)


# ════════════════════════════════════════════════════════════════════════════
# HEARTBEAT STATUS — Jetson liveness via /data/heartbeats/*.txt mtime
# ════════════════════════════════════════════════════════════════════════════

def jetson_status() -> dict:
    """Inspect /data/heartbeats/ for hb_<jetson>.txt files."""
    Path(config.HEARTBEATS_DIR).mkdir(parents=True, exist_ok=True)
    now = time.time()
    seen = {}
    for f in Path(config.HEARTBEATS_DIR).glob("hb_*.txt"):
        name = f.stem.replace("hb_", "", 1)
        seen[name] = now - f.stat().st_mtime  # age in seconds

    jetsons = []
    for name in config.EXPECTED_JETSONS:
        age = seen.get(name)
        online = age is not None and age < config.HEARTBEAT_STALE_SECONDS
        jetsons.append({
            "name": name,
            "online": online,
            "last_heartbeat_age_seconds": age,
        })

    online_count = sum(1 for j in jetsons if j["online"])
    return {
        "online": online_count,
        "total": len(jetsons),
        "jetsons": jetsons,
    }


# ════════════════════════════════════════════════════════════════════════════
# IMAGE CLEANUP LOOP
# ════════════════════════════════════════════════════════════════════════════

def _delete_old_images():
    """Delete .jpg files from archive older than IMAGE_RETENTION_DAYS.
    DB records (detections/sessions/alerts) are intentionally NOT touched —
    has_image is computed live, so the dashboard shows NO IMAGE AVAILABLE."""
    if config.IMAGE_RETENTION_DAYS <= 0:
        return
    archive = Path(config.ARCHIVE_DIR)
    if not archive.exists():
        return
    cutoff = time.time() - config.IMAGE_RETENTION_DAYS * 86400  # days → seconds
    deleted = 0
    for f in archive.glob("*.jpg"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except FileNotFoundError:
            pass  # already gone
    if deleted:
        print(f"[cleanup] deleted {deleted} image(s) older than {config.IMAGE_RETENTION_DAYS} days")
    else:
        print(f"[cleanup] ran — no images older than {config.IMAGE_RETENTION_DAYS} days found")


async def image_cleanup_loop():
    """Run image cleanup once at startup (after 60s grace) then every 24 hours."""
    if config.IMAGE_RETENTION_DAYS <= 0:
        print("[cleanup] image cleanup disabled (TRINETRA_IMAGE_RETENTION_DAYS=0)")
        return
    print(f"[cleanup] image retention = {config.IMAGE_RETENTION_DAYS} days · first run in 60s")
    await asyncio.sleep(60)          # give server time to finish startup
    _delete_old_images()             # clean up any backlog from while server was down
    while True:
        await asyncio.sleep(86400)   # then repeat every 24 hours
        _delete_old_images()


# ════════════════════════════════════════════════════════════════════════════
# VISIT RECORDINGS — map a video file on local storage to a visit (session)
#
# Naming convention:  <badge>_<date>.<ext>   e.g. RED-CIRCLE_20260807.mp4
#
# The date is the visit's LOCAL start date — the same value the Visits table
# prints in its Date column — so what you see in the UI is what you name the
# file. Both 20260807 and 2026-08-07 are accepted, matching is case-insensitive,
# and a file may carry an extra suffix (RED-CIRCLE_20260807_cam3.mp4) and still
# resolve. Nothing is renamed or moved; the directory is read-only to Trinetra.
# ════════════════════════════════════════════════════════════════════════════

def _utc_to_local(dt: datetime) -> datetime:
    """DB timestamps are naive UTC — shift to server-local wall time.

    Rounded to the nearest second: rows written before the parse_filename fix
    carry sub-second noise, and truncating that renders 10:00:00 as 09:59:59.
    """
    local = dt + (datetime.now() - datetime.utcnow())
    return (local + timedelta(milliseconds=500)).replace(microsecond=0)


def index_videos() -> list[tuple[str, str]]:
    """List VIDEO_DIR once as [(lowercased stem, real filename)].

    Read fresh on each call so dropping a file in the directory shows up on the
    next dashboard refresh without a server restart.
    """
    d = Path(config.VIDEO_DIR)
    if not d.is_dir():
        return []
    out = []
    try:
        for f in sorted(d.iterdir()):
            if f.is_file() and f.suffix.lower() in config.VIDEO_EXTENSIONS:
                out.append((f.stem.lower(), f.name))
    except OSError as e:
        print(f"[video] cannot read VIDEO_DIR {d}: {e}")
    return out


def match_video(index: list[tuple[str, str]], badge: str, first_seen: str):
    """Find the recording for one visit. Returns a dict or None."""
    if not index:
        return None
    try:
        start_local = _utc_to_local(datetime.fromisoformat(first_seen))
    except (ValueError, TypeError):
        return None

    prefixes = [
        f"{badge}_{start_local.strftime('%Y%m%d')}".lower(),
        f"{badge}_{start_local.strftime('%Y-%m-%d')}".lower(),
    ]
    # Exact stem wins; otherwise accept a longer name with the same prefix.
    for want in prefixes:
        for stem, name in index:
            if stem == want:
                return _video_meta(name)
    for want in prefixes:
        for stem, name in index:
            if stem.startswith(want):
                return _video_meta(name)
    return None


def _video_meta(filename: str) -> dict:
    p = Path(config.VIDEO_DIR) / filename
    ext = p.suffix.lower()
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    return {
        "filename": filename,
        "size_bytes": size,
        # False for .mkv/.avi/.mov — the file is found, but no browser will
        # decode it, so the UI offers download instead of playback.
        "playable": ext in config.BROWSER_VIDEO_EXTENSIONS,
    }


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _safe_video_path(filename: str) -> Path:
    """Resolve a request filename inside VIDEO_DIR, refusing path escapes."""
    p = Path(config.VIDEO_DIR) / Path(filename).name
    if p.suffix.lower() not in config.VIDEO_EXTENSIONS:
        raise HTTPException(404, "not a video file")
    if not p.is_file():
        raise HTTPException(404, "video not found")
    return p


def _build_video_response(filename: str, request: Request, download: int):
    """Stream a recording with HTTP Range support.

    Starlette 0.38 (pinned by fastapi 0.115) has no Range support in
    FileResponse, and range support only landed upstream in 0.45 — outside that
    pin. Without a 206 response Safari/iOS refuse to play at all and seeking
    re-downloads from byte 0, so the partial-content path is implemented here.
    """
    p = _safe_video_path(filename)
    file_size = p.stat().st_size
    media_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"

    headers = {"accept-ranges": "bytes"}
    if download:
        headers["content-disposition"] = f'attachment; filename="{p.name}"'

    range_header = (request.headers.get("range") or "").strip()
    m = _RANGE_RE.fullmatch(range_header) if range_header else None
    if not m:
        # No (or unparseable) Range — serve the whole file.
        return FileResponse(p, media_type=media_type, headers=headers)

    start_raw, end_raw = m.group(1), m.group(2)
    if start_raw:
        start = int(start_raw)
        end = int(end_raw) if end_raw else file_size - 1
    elif end_raw:
        start = max(0, file_size - int(end_raw))   # suffix range: bytes=-N
        end = file_size - 1
    else:
        return FileResponse(p, media_type=media_type, headers=headers)

    if start >= file_size:
        return Response(status_code=416,
                        headers={"content-range": f"bytes */{file_size}"})
    end = min(end, file_size - 1)
    length = end - start + 1

    def stream():
        with open(p, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers["content-range"] = f"bytes {start}-{end}/{file_size}"
    headers["content-length"] = str(length)
    return StreamingResponse(stream(), status_code=206,
                             media_type=media_type, headers=headers)


def _lookup_visit_video(visit_id: int):
    """Resolve the recording for one visit (used for a single-row re-check)."""
    conn = db()
    try:
        row = conn.execute(
            "SELECT badge, first_seen FROM sessions WHERE id=?", (visit_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, f"visit {visit_id} not found")

    video = match_video(index_videos(), row["badge"], row["first_seen"])
    return {
        "visit_id": visit_id,
        "video_dir": config.VIDEO_DIR,
        "available": video is not None,
        "video": video,
    }


# ════════════════════════════════════════════════════════════════════════════
# FASTAPI APP
# ════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _main_loop
    _main_loop = asyncio.get_event_loop()

    config.print_summary()
    init_db()
    threading.Thread(target=scan_existing, daemon=True).start()
    observer = start_watcher()
    loiter_task   = asyncio.create_task(loitering_loop())
    cleanup_task  = asyncio.create_task(image_cleanup_loop())
    print("[trinetra] startup complete · http://%s:%d" % (config.HOST, config.PORT))
    try:
        yield
    finally:
        print("[trinetra] shutting down")
        observer.stop()
        observer.join(timeout=2)
        loiter_task.cancel()
        cleanup_task.cancel()


app = FastAPI(title="Trinetra Systems Monitor", lifespan=lifespan)


# ─── /api/stats — KPIs + chart data ───────────────────────────────────────
@app.get("/api/stats")
def api_stats():
    conn = db()
    try:
        # NOTE: timestamps are stored in UTC, so the date must be converted to
        # local before comparing — otherwise this counts a different day than
        # /api/detections does and the KPI contradicts the chart beside it.
        total_today = conn.execute(
            "SELECT COUNT(*) FROM detections "
            "WHERE date(timestamp,'localtime')=date('now','localtime')"
        ).fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM sessions WHERE closed=0").fetchone()[0]
        # Count sessions loitering by actual time elapsed, matching what the
        # frontend banner shows. Uses first_seen age so it works immediately
        # (no wait for the loitering loop) and includes whitelisted sessions
        # (so the count stays > 0 even after acking a badge).
        loitering = conn.execute(
            "SELECT COUNT(*) FROM sessions "
            "WHERE closed=0 "
            "AND (julianday('now') - julianday(first_seen)) * 1440 >= ?"
            , (config.LOITER_MINUTES,)
        ).fetchone()[0] if config.LOITER_ALERTS else 0

        by_badge = conn.execute("""
            SELECT badge, COUNT(*) AS c FROM detections
            WHERE date(timestamp,'localtime')=date('now','localtime')
            GROUP BY badge ORDER BY c DESC LIMIT 7
        """).fetchall()
        by_camera = conn.execute("""
            SELECT camera, COUNT(*) AS c FROM detections
            WHERE date(timestamp,'localtime')=date('now','localtime')
            GROUP BY camera ORDER BY c DESC
        """).fetchall()

        # Hourly buckets for today
        hourly = [0] * 24
        rows = conn.execute("""
            SELECT CAST(strftime('%H', timestamp, 'localtime') AS INT) AS h, COUNT(*) AS c
            FROM detections
            WHERE date(timestamp,'localtime')=date('now','localtime')
            GROUP BY h
        """).fetchall()
        for r in rows:
            hourly[r["h"]] = r["c"]

        # Daily totals (last 30 days)
        daily = [0] * 30
        rows = conn.execute("""
            SELECT date(timestamp,'localtime') AS d, COUNT(*) AS c
            FROM detections
            WHERE timestamp > datetime('now', '-30 days')
            GROUP BY d
        """).fetchall()
        today = datetime.now().date()
        for r in rows:
            day = datetime.strptime(r["d"], "%Y-%m-%d").date()
            idx = 29 - (today - day).days
            if 0 <= idx < 30:
                daily[idx] = r["c"]

        # Loitering incidents (last 30 days)
        loiter_daily = [0] * 30
        rows = conn.execute("""
            SELECT date(triggered_at,'localtime') AS d, COUNT(*) AS c
            FROM alerts
            WHERE triggered_at > datetime('now', '-30 days')
            GROUP BY d
        """).fetchall()
        for r in rows:
            day = datetime.strptime(r["d"], "%Y-%m-%d").date()
            idx = 29 - (today - day).days
            if 0 <= idx < 30:
                loiter_daily[idx] = r["c"]

        js = jetson_status()

        return {
            "total_today": total_today,
            "active_sessions": active,
            "loitering_count": loitering,
            # Drives whether the dashboard shows the banner, KPI and status
            # tags at all. loiter_minutes replaces the frontend's hardcoded 15.
            "loiter_alerts_enabled": config.LOITER_ALERTS,
            "movement_alerts_enabled": config.MOVEMENT_ALERTS,
            "loiter_minutes": config.LOITER_MINUTES,
            "jetsons_online": js["online"],
            "jetsons_total": js["total"],
            "by_badge": [[r["badge"], r["c"]] for r in by_badge],
            "by_camera": [[r["camera"], r["c"]] for r in by_camera],
            "hourly_today": hourly,
            "daily_30d": daily,
            "loitering_30d": loiter_daily,
        }
    finally:
        conn.close()


# ─── /api/badges — full list of badges from classes.txt ──────────────────
def load_classes() -> list[str]:
    """Read classes.txt and return a sorted list of badges."""
    path = config.CLASSES_FILE
    if not os.path.exists(path):
        return []
    seen = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                seen.add(line)
    return sorted(seen)


@app.get("/api/badges")
def api_badges():
    """Return the full badge list from classes.txt PLUS any badges seen in detections.
    The 'source' field shows where each badge came from."""
    classes = set(load_classes())
    # Also include any badges that showed up in detections but aren't in classes.txt
    conn = db()
    try:
        seen = {r[0] for r in conn.execute("SELECT DISTINCT badge FROM detections").fetchall()}
        whitelisted = {r[0] for r in conn.execute("SELECT badge FROM whitelist").fetchall()}
    finally:
        conn.close()

    all_badges = sorted(classes | seen)
    return {
        "classes_file": str(config.CLASSES_FILE),
        "badges": [
            {
                "badge": b,
                "in_classes_file": b in classes,
                "auto_discovered": b in seen and b not in classes,
                "whitelisted": b in whitelisted,
            } for b in all_badges
        ],
    }


@app.get("/api/badges/{badge}/history")
def api_badge_history(badge: str, limit: int = 50):
    """Return recent detections for a specific badge, ordered newest first.
    Used by the Currently in Premises modal to show log history per badge."""
    conn = db()
    try:
        rows = conn.execute("""
            SELECT id, filename, camera, badge, timestamp, type, session_id
            FROM detections WHERE badge=?
            ORDER BY timestamp DESC LIMIT ?
        """, (badge, limit)).fetchall()
        archive = Path(config.ARCHIVE_DIR)
        return [{
            "id": r["id"],
            "filename": r["filename"],
            "camera": r["camera"],
            "badge": r["badge"],
            "timestamp": r["timestamp"],
            "type": r["type"],
            "session_id": r["session_id"],
            "has_image": (archive / r["filename"]).exists(),
        } for r in rows]
    finally:
        conn.close()


@app.get("/api/badges/{badge}/sessions")
def api_badge_sessions(badge: str, limit: int = 20):
    """Return all sessions (visits) for a specific badge, newest first.
    Used by the Currently in Premises modal to show visit history."""
    conn = db()
    try:
        rows = conn.execute("""
            SELECT s.id, s.badge, s.first_seen, s.last_seen, s.last_camera,
                   s.total_sightings, s.closed, s.closed_at,
                   CAST((julianday(COALESCE(s.closed_at, datetime('now'))) -
                         julianday(s.first_seen)) * 86400 AS INTEGER) AS duration_seconds,
                   CASE WHEN a.id IS NOT NULL THEN 1 ELSE 0 END AS had_alert
            FROM sessions s
            LEFT JOIN alerts a ON a.session_id = s.id
            WHERE s.badge = ?
            ORDER BY s.first_seen DESC LIMIT ?
        """, (badge, limit)).fetchall()
        return [{
            "id": r["id"],
            "badge": r["badge"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "last_camera": r["last_camera"],
            "total_sightings": r["total_sightings"],
            "closed": bool(r["closed"]),
            "closed_at": r["closed_at"],
            "duration_seconds": r["duration_seconds"] or 0,
            "had_alert": bool(r["had_alert"]),
        } for r in rows]
    finally:
        conn.close()


@app.get("/api/sessions/{session_id}/detections")
def api_session_detections(session_id: int):
    """Return all detections belonging to a single session, oldest first.
    Used by the premises modal to show the full detection log for a visit."""
    conn = db()
    try:
        rows = conn.execute("""
            SELECT id, filename, camera, badge, timestamp, type
            FROM detections WHERE session_id=?
            ORDER BY timestamp ASC
        """, (session_id,)).fetchall()
        archive = Path(config.ARCHIVE_DIR)
        return [{
            "id": r["id"],
            "filename": r["filename"],
            "camera": r["camera"],
            "badge": r["badge"],
            "timestamp": r["timestamp"],
            "type": r["type"],
            "has_image": (archive / r["filename"]).exists(),
        } for r in rows]
    finally:
        conn.close()


# ─── /api/visits — list of all visits (sessions) with duration + badge ────
@app.get("/api/visits")
def api_visits(badge: Optional[str] = None, status: str = "all",
               date: Optional[str] = None, limit: int = 200):
    """List visits (sessions), newest first.

    Query params:
      badge:  filter by specific badge
      status: 'open' | 'closed' | 'all'
      date:   YYYY-MM-DD (filter by first_seen date in local time)
      limit:  max rows
    """
    where = []
    params = []
    if badge:
        where.append("s.badge = ?"); params.append(badge)
    if status == "open":
        where.append("s.closed = 0")
    elif status == "closed":
        where.append("s.closed = 1")
    if date:
        where.append("date(s.first_seen, 'localtime') = ?"); params.append(date)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = db()
    try:
        rows = conn.execute(f"""
            SELECT s.id, s.badge, s.first_seen, s.last_seen, s.last_camera,
                   s.total_sightings, s.closed, s.closed_at, s.cameras_json,
                   CASE WHEN s.badge IN (SELECT badge FROM whitelist) THEN 1 ELSE 0 END AS whitelisted
            FROM sessions s
            {where_sql}
            ORDER BY s.first_seen DESC
            LIMIT ?
        """, [*params, limit]).fetchall()

        # Listed once for the whole page, not once per row.
        video_index = index_videos()

        visits = []
        for r in rows:
            first = datetime.fromisoformat(r["first_seen"])
            last  = datetime.fromisoformat(r["last_seen"])
            duration = (last - first).total_seconds()
            cams = json.loads(r["cameras_json"] or "{}")
            visits.append({
                "video": match_video(video_index, r["badge"], r["first_seen"]),
                "id": r["id"],
                "badge": r["badge"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "duration_seconds": int(duration),
                "total_sightings": r["total_sightings"],
                "last_camera": r["last_camera"],
                "closed": bool(r["closed"]),
                "closed_at": r["closed_at"],
                "cameras": cams,                          # {"cam-a": 5, "cam-b": 2}
                "whitelisted": bool(r["whitelisted"]),
            })
        return visits
    finally:
        conn.close()


@app.get("/api/visits/{visit_id}/timeline")
def api_visit_timeline(visit_id: int):
    """Return all detections for one visit (session_id), oldest first
    so it reads naturally as a timeline (entry → sightings → exit)."""
    conn = db()
    try:
        # Validate the session exists and grab its metadata
        sess = conn.execute(
            "SELECT id, badge, first_seen, last_seen, total_sightings, closed, "
            "closed_at, last_camera FROM sessions WHERE id=?",
            (visit_id,)
        ).fetchone()
        if not sess:
            raise HTTPException(404, f"visit {visit_id} not found")

        rows = conn.execute("""
            SELECT id, filename, camera, badge, timestamp, type
            FROM detections WHERE session_id=?
            ORDER BY timestamp ASC
        """, (visit_id,)).fetchall()
        archive = Path(config.ARCHIVE_DIR)
        return {
            "visit": {
                "id": sess["id"],
                "badge": sess["badge"],
                "first_seen": sess["first_seen"],
                "last_seen": sess["last_seen"],
                "total_sightings": sess["total_sightings"],
                "closed": bool(sess["closed"]),
                "closed_at": sess["closed_at"],
                "last_camera": sess["last_camera"],
            },
            "detections": [{
                "id": r["id"],
                "filename": r["filename"],
                "camera": r["camera"],
                "badge": r["badge"],
                "timestamp": r["timestamp"],
                "type": r["type"],
                "has_image": (archive / r["filename"]).exists(),
            } for r in rows],
        }
    finally:
        conn.close()


# ─── /api/sessions/active — currently in premises ─────────────────────────
@app.get("/api/sessions/active")
def api_sessions_active():
    conn = db()
    try:
        rows = conn.execute("""
            SELECT s.id, s.badge, s.first_seen, s.last_seen, s.last_camera,
                   s.total_sightings, s.cameras_json,
                   CASE WHEN s.badge IN (SELECT badge FROM whitelist) THEN 1 ELSE 0 END AS whitelisted,
                   a.id AS alert_id, a.acknowledged, a.acknowledged_at, a.acknowledged_by,
                   (
                       SELECT d.filename FROM detections d
                       WHERE d.session_id = s.id
                       ORDER BY d.timestamp DESC LIMIT 1
                   ) AS last_image
            FROM sessions s
            LEFT JOIN alerts a ON a.session_id = s.id
            WHERE s.closed=0
            ORDER BY s.first_seen ASC
        """).fetchall()
        archive = Path(config.ARCHIVE_DIR)
        return [{
            "id": r["id"],
            "badge": r["badge"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "last_camera": r["last_camera"],
            "total_sightings": r["total_sightings"],
            "whitelisted": bool(r["whitelisted"]),
            "alert_id": r["alert_id"],
            "acked": bool(r["acknowledged"]) if r["acknowledged"] is not None else False,
            "acked_at": r["acknowledged_at"],
            "acked_by": r["acknowledged_by"],
            "last_image": r["last_image"],
            "has_image": bool(r["last_image"] and (archive / r["last_image"]).exists()),
        } for r in rows]
    finally:
        conn.close()


# ─── /api/detections — filtered detection log ─────────────────────────────
def _detections_query(date: Optional[str], month: Optional[str],
                      badge: Optional[str], camera: Optional[str],
                      search: Optional[str]):
    where, params = [], []
    if date:
        where.append("date(timestamp,'localtime')=?"); params.append(date)
    elif month:
        where.append("strftime('%Y-%m', timestamp, 'localtime')=?"); params.append(month)
    else:
        where.append("date(timestamp,'localtime')=date('now','localtime')")
    if badge:
        where.append("badge=?"); params.append(badge)
    if camera:
        where.append("camera=?"); params.append(camera)
    if search:
        where.append("(badge LIKE ? OR camera LIKE ? OR strftime('%H:%M:%S', timestamp,'localtime') LIKE ?)")
        s = f"%{search}%"; params += [s, s, s]
    return " AND ".join(where), params


@app.get("/api/detections")
def api_detections(date: Optional[str] = None, month: Optional[str] = None,
                   badge: Optional[str] = None, camera: Optional[str] = None,
                   search: Optional[str] = None,
                   limit: int = 200, offset: int = 0):
    where, params = _detections_query(date, month, badge, camera, search)
    sql = (
        "SELECT id, filename, camera, badge, timestamp, type, session_id "
        f"FROM detections WHERE {where} "
        "ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    )
    conn = db()
    try:
        rows = conn.execute(sql, [*params, limit, offset]).fetchall()
        archive = Path(config.ARCHIVE_DIR)
        return [{
            "id": r["id"],
            "filename": r["filename"],
            "camera": r["camera"],
            "badge": r["badge"],
            "timestamp": r["timestamp"],
            "type": r["type"],
            "session_id": r["session_id"],
            "has_image": (archive / r["filename"]).exists(),
        } for r in rows]
    finally:
        conn.close()


@app.get("/api/detections/export")
def api_detections_export(date: Optional[str] = None, month: Optional[str] = None,
                          badge: Optional[str] = None, camera: Optional[str] = None,
                          search: Optional[str] = None):
    where, params = _detections_query(date, month, badge, camera, search)
    sql = (
        "SELECT id, timestamp, badge, camera, type, filename "
        f"FROM detections WHERE {where} ORDER BY timestamp DESC"
    )
    conn = db()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "timestamp", "badge", "camera", "type", "filename"])
    for r in rows:
        writer.writerow([r["id"], r["timestamp"], r["badge"], r["camera"], r["type"], r["filename"]])
    buf.seek(0)
    fname = f"trinetra-detections-{datetime.now():%Y%m%d-%H%M%S}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )


# ─── /api/movements — camera-to-camera transition feed ────────────────────
@app.get("/api/movements")
def api_movements(badge: Optional[str] = None, event: Optional[str] = None,
                  date: Optional[str] = None, limit: int = 100):
    """Movement events, newest first.

    Query params:
      badge: filter by badge
      event: 'entry' | 'move' | 'exit'
      date:  YYYY-MM-DD (local date of the event)
      limit: max rows
    """
    where, params = [], []
    if badge:
        where.append("badge = ?"); params.append(badge)
    if event:
        where.append("event = ?"); params.append(event)
    if date:
        where.append("date(timestamp,'localtime') = ?"); params.append(date)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = db()
    try:
        rows = conn.execute(f"""
            SELECT id, badge, session_id, detection_id, from_camera, to_camera,
                   event, statement, timestamp, filename, notified
            FROM movements
            {where_sql}
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
        """, [*params, limit]).fetchall()
        archive = Path(config.ARCHIVE_DIR)
        return [{
            "id": r["id"],
            "badge": r["badge"],
            "session_id": r["session_id"],
            "detection_id": r["detection_id"],
            "from_camera": r["from_camera"],
            "to_camera": r["to_camera"],
            "from_label": camera_label(r["from_camera"]) if r["from_camera"] else None,
            "to_label": camera_label(r["to_camera"]),
            "event": r["event"],
            "statement": r["statement"],
            "timestamp": r["timestamp"],
            "filename": r["filename"],
            "notified": bool(r["notified"]),
            "has_image": bool(r["filename"] and (archive / r["filename"]).exists()),
        } for r in rows]
    finally:
        conn.close()


# ─── /api/jetsons/status ──────────────────────────────────────────────────
@app.get("/api/jetsons/status")
def api_jetsons_status():
    return jetson_status()


# ─── /api/whitelist ───────────────────────────────────────────────────────
@app.get("/api/whitelist")
def api_whitelist_get():
    conn = db()
    try:
        rows = conn.execute(
            "SELECT badge, reason, added_at FROM whitelist ORDER BY added_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/whitelist")
async def api_whitelist_add(request: Request):
    body = await request.json()
    badge = (body.get("badge") or "").strip().upper()
    reason = (body.get("reason") or "").strip()
    if not badge:
        raise HTTPException(400, "badge required")
    conn = db()
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO whitelist(badge, reason) VALUES (?,?)",
                (badge, reason)
            )
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/whitelist/{badge}")
def api_whitelist_remove(badge: str):
    conn = db()
    try:
        with conn:
            conn.execute("DELETE FROM whitelist WHERE badge=?", (badge,))
        return {"ok": True}
    finally:
        conn.close()


# ─── /api/alerts/{id}/ack ─────────────────────────────────────────────────
@app.post("/api/alerts/{alert_id}/ack")
async def api_alert_ack(alert_id: int, request: Request):
    try:
        body = await request.json()
        acked_by = (body.get("acked_by") or "operator").strip()
    except Exception:
        acked_by = "operator"
    now = datetime.utcnow()
    conn = db()
    try:
        with conn:
            # 1. Mark the alert as acknowledged
            conn.execute(
                "UPDATE alerts SET acknowledged=1, acknowledged_at=?, acknowledged_by=? WHERE id=?",
                (now.isoformat(), acked_by, alert_id)
            )
            # 2. Auto-whitelist the badge so it is recorded and no longer
            #    flagged as loitering. The badge stays visible in the premises
            #    table with WHITELIST status and is filtered out of the
            #    loitering banner automatically by the frontend.
            row = conn.execute(
                "SELECT badge FROM alerts WHERE id=?", (alert_id,)
            ).fetchone()
            if row:
                badge = row["badge"]
                reason = f"Auto-whitelisted: acked alert #{alert_id} by {acked_by}"
                conn.execute(
                    "INSERT OR REPLACE INTO whitelist(badge, reason) VALUES (?,?)",
                    (badge, reason)
                )
        return {"ok": True}
    finally:
        conn.close()


# ─── /images/{filename} — serve archived images ───────────────────────────
@app.get("/images/{filename}")
def get_image(filename: str):
    safe = Path(filename).name  # strip any path separators
    p = Path(config.ARCHIVE_DIR) / safe
    if p.exists():
        return FileResponse(p)
    # Fallback: try to find last-seen image for the badge in this camera
    parsed = parse_filename(safe)
    if parsed:
        camera, badge, _ = parsed
        prefix = f"{camera}_{badge}_"
        for cand in sorted(Path(config.ARCHIVE_DIR).glob(f"{prefix}*.jpg"), reverse=True):
            return FileResponse(cand)
    raise HTTPException(404, "no image available")


# ─── /video/{filename} — stream a visit recording (Range-aware) ───────────
@app.get("/video/{filename}")
def get_video(filename: str, request: Request, download: int = 0):
    return _build_video_response(filename, request, download)


# ─── /api/visits/{id}/video — resolve the recording for one visit ─────────
@app.get("/api/visits/{visit_id}/video")
def api_visit_video(visit_id: int):
    return _lookup_visit_video(visit_id)


# ════════════════════════════════════════════════════════════════════════════
# ADMIN — login, config read/write
# ════════════════════════════════════════════════════════════════════════════
import secrets as _secrets

_admin_sessions: dict = {}   # token → expires datetime


def _check_admin(request: Request) -> bool:
    token = request.cookies.get("admin_token", "")
    info = _admin_sessions.get(token)
    if not info:
        return False
    if datetime.utcnow() > info:
        _admin_sessions.pop(token, None)
        return False
    return True


@app.post("/api/admin/login")
async def admin_login(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")
    if email != config.ADMIN_EMAIL.lower() or password != config.ADMIN_PASSWORD:
        raise HTTPException(401, "Invalid credentials")
    token = _secrets.token_hex(32)
    _admin_sessions[token] = datetime.utcnow() + timedelta(hours=8)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("admin_token", token, httponly=True,
                    max_age=8 * 3600, samesite="lax", path="/")
    return resp


@app.post("/api/admin/logout")
async def admin_logout(request: Request):
    from fastapi.responses import JSONResponse
    token = request.cookies.get("admin_token", "")
    _admin_sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("admin_token", path="/")
    return resp


@app.get("/api/admin/config")
def admin_get_config(request: Request):
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    return {
        "TRINETRA_LOITER_MINUTES":            config.LOITER_MINUTES,
        "TRINETRA_SESSION_GAP_MINUTES":       config.SESSION_GAP_MINUTES,
        "TRINETRA_SESSION_AUTO_CLOSE_MINUTES": config.SESSION_AUTO_CLOSE_MINUTES,
        "TRINETRA_EXIT_CAMERAS":              ",".join(sorted(config.EXIT_CAMERAS)),
        "TRINETRA_MOVEMENT_ALERTS":           "1" if config.MOVEMENT_ALERTS else "0",
        "TRINETRA_LOITER_ALERTS":             "1" if config.LOITER_ALERTS else "0",
        "TRINETRA_IMAGE_RETENTION_DAYS":      config.IMAGE_RETENTION_DAYS,
        "TRINETRA_VIDEO_DIR":                 config.VIDEO_DIR,
        "TRINETRA_JETSONS":                   ",".join(config.EXPECTED_JETSONS),
        "TRINETRA_HB_STALE":                  config.HEARTBEAT_STALE_SECONDS,
        "TELEGRAM_TOKEN":                     config.TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID":                   ",".join(config.TELEGRAM_CHAT_IDS),
        "SMTP_HOST":                          config.SMTP_HOST,
        "SMTP_PORT":                          config.SMTP_PORT,
        "SMTP_USER":                          config.SMTP_USER,
        "SMTP_TO":                            config.SMTP_TO,
        "ADMIN_EMAIL":                        config.ADMIN_EMAIL,
    }


@app.post("/api/admin/config")
async def admin_update_config(request: Request):
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    updates: dict = await request.json()

    # Coerce integer fields
    int_fields = ["TRINETRA_LOITER_MINUTES", "TRINETRA_SESSION_GAP_MINUTES",
                  "TRINETRA_SESSION_AUTO_CLOSE_MINUTES", "TRINETRA_IMAGE_RETENTION_DAYS",
                  "TRINETRA_HB_STALE", "SMTP_PORT"]
    for f in int_fields:
        if f in updates:
            try:
                updates[f] = str(int(updates[f]))
            except (ValueError, TypeError):
                raise HTTPException(400, f"{f} must be an integer")

    _write_env(updates)
    _apply_config(updates)

    restart_needed = bool(set(updates) & {"TRINETRA_HOST", "TRINETRA_PORT",
                                           "TRINETRA_DATA", "TRINETRA_INCOMING",
                                           "TRINETRA_ARCHIVE"})
    return {"ok": True, "restart_required": restart_needed}


def _write_env(updates: dict):
    """Rewrite .env preserving comments; append any keys not already present."""
    env_path = Path(config.PROJECT_ROOT) / ".env"
    existing = env_path.read_text().splitlines() if env_path.exists() else []
    seen = set()
    out = []
    for line in existing:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")
    env_path.write_text("\n".join(out) + "\n")


def _apply_config(updates: dict):
    """Hot-reload config module attributes so changes take effect immediately."""
    mapping = {
        "TRINETRA_LOITER_MINUTES":             ("LOITER_MINUTES",              int),
        "TRINETRA_SESSION_GAP_MINUTES":        ("SESSION_GAP_MINUTES",         int),
        "TRINETRA_SESSION_AUTO_CLOSE_MINUTES": ("SESSION_AUTO_CLOSE_MINUTES",  int),
        "TRINETRA_IMAGE_RETENTION_DAYS":       ("IMAGE_RETENTION_DAYS",        int),
        "TRINETRA_HB_STALE":                   ("HEARTBEAT_STALE_SECONDS",     int),
        # Relative paths anchor to PROJECT_ROOT, same as on startup.
        "TRINETRA_VIDEO_DIR":                  ("VIDEO_DIR",
                                                lambda v: config.resolve_dir(v, config.VIDEO_DIR)),
        "TRINETRA_EXIT_CAMERAS":               ("EXIT_CAMERAS",
                                                lambda v: {c.strip() for c in v.split(",") if c.strip()}),
        "TRINETRA_MOVEMENT_ALERTS":            ("MOVEMENT_ALERTS",
                                                lambda v: str(v).strip().lower() in ("1", "true", "yes", "on")),
        "TRINETRA_LOITER_ALERTS":              ("LOITER_ALERTS",
                                                lambda v: str(v).strip().lower() in ("1", "true", "yes", "on")),
        "TRINETRA_JETSONS":                    ("EXPECTED_JETSONS",
                                                lambda v: [j.strip() for j in v.split(",") if j.strip()]),
        "TELEGRAM_TOKEN":   ("TELEGRAM_TOKEN",    str),
        "TELEGRAM_CHAT_ID": ("TELEGRAM_CHAT_IDS",
                             lambda v: [cid.strip() for cid in v.split(",") if cid.strip()]),
        "SMTP_HOST":        ("SMTP_HOST",         str),
        "SMTP_PORT":        ("SMTP_PORT",          int),
        "SMTP_USER":        ("SMTP_USER",         str),
        "SMTP_TO":          ("SMTP_TO",           str),
        "ADMIN_EMAIL":      ("ADMIN_EMAIL",       str),
        "ADMIN_PASSWORD":   ("ADMIN_PASSWORD",    str),
    }
    for env_key, (attr, fn) in mapping.items():
        if env_key in updates:
            setattr(config, attr, fn(updates[env_key]))
# Tables exposed by the admin database browser, each with the column it sorts
# newest-first by. Single source of truth for the three endpoints below.
ADMIN_TABLES = {
    "detections": "timestamp",
    "sessions":   "first_seen",
    "movements":  "timestamp",
    "alerts":     "triggered_at",
    "whitelist":  "added_at",
}


def _delete_archive_image(filename: Optional[str]):
    if not filename:
        return
    p = Path(config.ARCHIVE_DIR) / Path(filename).name
    try:
        if p.exists():
            p.unlink()
    except OSError as e:
        print(f"[admin] could not delete image {filename}: {e}")


@app.get("/api/admin/database/{table}")
def admin_get_database_table(request: Request, table: str, limit: int = 50, offset: int = 0):
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    if table not in ADMIN_TABLES:
        raise HTTPException(400, "Invalid table")

    order_col = ADMIN_TABLES[table]
    conn = db()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        return {
            "total": total,
            "records": [dict(r) for r in rows]
        }
    finally:
        conn.close()


@app.delete("/api/admin/database/{table}/{record_id}")
def admin_delete_database_record(request: Request, table: str, record_id: str):
    """Delete one row, clearing anything that references it first.

    foreign_keys=ON, so a plain DELETE fails with "FOREIGN KEY constraint
    failed" whenever another table points at the row — e.g. movements.detection_id
    pins every detection that produced a movement. Dependants are removed
    explicitly here, in order.
    """
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    if table not in ADMIN_TABLES:
        raise HTTPException(400, "Invalid table")

    conn = db()
    try:
        with conn:
            if table == "detections":
                # A movement is derived from its detection — remove it too.
                conn.execute("DELETE FROM movements WHERE detection_id=?", (record_id,))
                row = conn.execute(
                    "SELECT filename FROM detections WHERE id=?", (record_id,)
                ).fetchone()
                if row:
                    _delete_archive_image(row["filename"])
                cur = conn.execute("DELETE FROM detections WHERE id=?", (record_id,))

            elif table == "sessions":
                # Alerts and movements belong to the visit; detections are the
                # raw log, so they are detached rather than destroyed.
                conn.execute("DELETE FROM alerts    WHERE session_id=?", (record_id,))
                conn.execute("DELETE FROM movements WHERE session_id=?", (record_id,))
                conn.execute("UPDATE detections SET session_id=NULL WHERE session_id=?", (record_id,))
                cur = conn.execute("DELETE FROM sessions WHERE id=?", (record_id,))

            elif table == "whitelist":
                cur = conn.execute("DELETE FROM whitelist WHERE badge=?", (record_id,))

            else:   # movements, alerts — nothing references these
                cur = conn.execute(f"DELETE FROM {table} WHERE id=?", (record_id,))

        if cur.rowcount == 0:
            raise HTTPException(404, f"no {table} record with id {record_id}")
        return {"ok": True, "deleted": cur.rowcount}
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, f"cannot delete: {e}")
    finally:
        conn.close()

@app.delete("/api/admin/database/{table}")
def admin_clear_database_table(request: Request, table: str):
    """Empty a table, clearing referencing rows first (see the note above)."""
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    if table not in ADMIN_TABLES:
        raise HTTPException(400, "Invalid table")

    conn = db()
    try:
        with conn:
            if table == "detections":
                conn.execute("DELETE FROM movements")   # every row points at a detection
                for row in conn.execute("SELECT filename FROM detections").fetchall():
                    _delete_archive_image(row["filename"])
            elif table == "sessions":
                conn.execute("DELETE FROM alerts")
                conn.execute("DELETE FROM movements")
                conn.execute("UPDATE detections SET session_id=NULL")
            cur = conn.execute(f"DELETE FROM {table}")
        return {"ok": True, "deleted": cur.rowcount}
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, f"cannot clear {table}: {e}")
    finally:
        conn.close()


# ─── Badge purge — erase everything recorded for one badge ────────────────
# Tables are cleared in FK-safe order: movements and alerts point at sessions
# (and movements also at detections), so sessions must go last.
_BADGE_TABLES = ("movements", "alerts", "detections", "sessions", "whitelist")


def _badge_row_counts(conn, badge: str) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE badge=?", (badge,)).fetchone()[0]
            for t in _BADGE_TABLES}


@app.get("/api/admin/badges/{badge}/summary")
def admin_badge_summary(request: Request, badge: str):
    """What a purge would remove — shown to the operator before they confirm."""
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    conn = db()
    try:
        counts = _badge_row_counts(conn, badge)
        files = [r["filename"] for r in
                 conn.execute("SELECT filename FROM detections WHERE badge=?", (badge,)).fetchall()]
        first_last = conn.execute(
            "SELECT MIN(timestamp) AS first, MAX(timestamp) AS last "
            "FROM detections WHERE badge=?", (badge,)
        ).fetchone()
    finally:
        conn.close()

    archive = Path(config.ARCHIVE_DIR)
    return {
        "badge": badge,
        "counts": counts,
        "images_on_disk": sum(1 for f in files if (archive / f).exists()),
        "total_rows": sum(counts.values()),
        "first_seen": first_last["first"],
        "last_seen": first_last["last"],
    }


@app.delete("/api/admin/badges/{badge}")
def admin_purge_badge(request: Request, badge: str):
    """Erase a badge's entire history: rows in every table plus archived images.

    Irreversible. Scoped strictly to this badge — sessions, alerts and
    movements all carry their own badge column, so nothing belonging to
    another badge is reachable from here.
    """
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")

    deleted = {}
    with _db_lock:
        conn = db()
        try:
            with conn:
                files = [r["filename"] for r in
                         conn.execute("SELECT filename FROM detections WHERE badge=?",
                                      (badge,)).fetchall()]
                for t in _BADGE_TABLES:
                    deleted[t] = conn.execute(
                        f"DELETE FROM {t} WHERE badge=?", (badge,)
                    ).rowcount
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, f"cannot purge {badge}: {e}")
        finally:
            conn.close()

    images = 0
    for f in files:
        p = Path(config.ARCHIVE_DIR) / Path(f).name
        try:
            if p.exists():
                p.unlink()
                images += 1
        except OSError as e:
            print(f"[admin] could not delete image {f}: {e}")

    total = sum(deleted.values())
    print(f"[admin] PURGED badge {badge} — {total} row(s), {images} image(s): {deleted}")
    return {"ok": True, "badge": badge, "deleted": deleted,
            "deleted_images": images, "total_rows": total}


@app.get("/api/admin/test_images")
def admin_get_test_images(request: Request):
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    test_images_dir = Path(__file__).parent / "test_images"
    if not test_images_dir.exists():
        return []
    images = [f.name for f in test_images_dir.glob("*.jpg")]
    return sorted(images)

@app.post("/api/admin/simulate")
async def admin_simulate(request: Request):
    """Drop a single detection file into incoming/ with a specific timestamp."""
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")

    body = await request.json()
    badge       = (body.get("badge") or "TEST-BADGE").strip().upper()
    camera      = (body.get("camera") or "cam-entrance").strip().lower()
    
    date_str     = body.get("date")
    time_str     = body.get("time")
    image_base64 = body.get("image_base64")
    image_file   = body.get("image")

    if not re.match(r'^[A-Za-z0-9-]+$', badge):
        raise HTTPException(400, "Badge name may only contain letters, numbers and hyphens")
    if not re.match(r'^[A-Za-z0-9-]+$', camera):
        raise HTTPException(400, "Camera name may only contain letters, numbers and hyphens")

    incoming = Path(config.INCOMING_DIR)
    incoming.mkdir(parents=True, exist_ok=True)

    import base64
    test_images_dir = Path(__file__).parent / "test_images"
    image_bytes = MINIMAL_JPEG
    test_image_used = False
    
    if image_base64:
        try:
            image_bytes = base64.b64decode(image_base64)
            test_image_used = True
        except Exception:
            pass
    elif image_file:
        safe_img = Path(image_file).name
        img_path = test_images_dir / safe_img
        if img_path.exists():
            image_bytes = img_path.read_bytes()
            test_image_used = True
    else:
        # Fallback to random if none provided
        test_images = sorted(test_images_dir.glob("*.jpg")) if test_images_dir.exists() else []
        if test_images:
            image_bytes = random.choice(test_images).read_bytes()
            test_image_used = True

    now = datetime.now()
    parsed_date = date_str if (date_str and date_str.strip()) else now.strftime("%Y-%m-%d")
    parsed_time = time_str if (time_str and time_str.strip()) else now.strftime("%H:%M")

    try:
        ts = datetime.strptime(f"{parsed_date} {parsed_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        ts = now
        
    filename = f"{camera}_{badge}_{ts.strftime('%Y%m%d-%H%M%S')}.jpg"
    (incoming / filename).write_bytes(image_bytes)

    return {"ok": True, "filename": filename, "test_image_used": test_image_used}


@app.post("/api/admin/reset")
def admin_reset_all(request: Request):
    """Wipe ALL data: every DB table + archive images + dead-letter + heartbeats. Irreversible."""
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")

    def _clear_dir(directory: str, pattern: str = "*") -> int:
        """Delete all files matching pattern in a directory. Returns count deleted."""
        count = 0
        p = Path(directory)
        if not p.exists():
            return 0
        for f in p.glob(pattern):
            if f.is_file():
                try:
                    f.unlink()
                    count += 1
                except Exception:
                    pass
        return count

    with _db_lock:
        conn = db()
        try:
            with conn:
                # 1. Clear archive images
                deleted_images    = _clear_dir(config.ARCHIVE_DIR, "*.jpg")
                # 2. Clear dead-letter files
                deleted_dead      = _clear_dir(config.DEAD_LETTER_DIR)
                # 3. Clear heartbeat files
                deleted_hb        = _clear_dir(config.HEARTBEATS_DIR)
                # 4. Clear all DB tables (FK-safe order)
                conn.execute("DELETE FROM alerts")
                conn.execute("DELETE FROM detections")
                conn.execute("DELETE FROM sessions")
                conn.execute("DELETE FROM whitelist")
                conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('alerts','detections','sessions')")
        finally:
            conn.close()

    print(
        f"[admin] FULL RESET — all tables cleared | "
        f"images={deleted_images} | dead-letter={deleted_dead} | heartbeats={deleted_hb}"
    )
    return {
        "ok": True,
        "deleted_images": deleted_images,
        "deleted_dead_letter": deleted_dead,
        "deleted_heartbeats": deleted_hb,
    }


# ════════════════════════════════════════════════════════════════════════════
# PUSH NOTIFICATIONS — user-facing endpoints
#
# SECURITY: the caller's userId is ALWAYS resolved from the signed-in session
# cookie (notif.user_for_session). A userId in a request body is ignored, so a
# device can never be registered against somebody else's account.
# ════════════════════════════════════════════════════════════════════════════

USER_COOKIE = "trinetra_user"


def _current_user(request: Request) -> Optional[str]:
    """Resolve the caller from either transport.

    Web uses an httpOnly cookie (safe from XSS). Native mobile uses a Bearer
    token, because React Native's cookie handling is inconsistent across
    platforms and doesn't survive app restarts reliably. Both resolve through
    the same user_sessions table — a userId in a request body is still never
    trusted either way.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        uid = notif.user_for_session(auth[7:].strip())
        if uid:
            return uid
    return notif.user_for_session(request.cookies.get(USER_COOKIE, ""))


def _require_user(request: Request) -> str:
    uid = _current_user(request)
    if not uid:
        raise HTTPException(401, "Not signed in")
    return uid


@app.get("/api/notifications/config")
def api_push_config():
    """Public Firebase web config for the browser SDK. Contains no secrets —
    the VAPID key is public by design and the service account never appears."""
    st = config.firebase_status()
    return {
        "firebase": config.FIREBASE_WEB_CONFIG,
        "vapidKey": config.FIREBASE_VAPID_KEY,
        "ready": st["vapid_key"],
        "senderReady": st["credentials_present"],
    }


@app.post("/api/notifications/login")
async def api_push_login(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    user_id = (body.get("userId") or "").strip()
    passcode = body.get("passcode") or ""
    token = notif.login(user_id, passcode)
    if not token:
        raise HTTPException(401, "Invalid userId or passcode")

    # Mobile clients ask for the raw token and send it as a Bearer header.
    # Browsers never receive it — keeping the cookie httpOnly means XSS on the
    # dashboard cannot read a session.
    if (body.get("client") or "").lower() == "mobile":
        return JSONResponse({"ok": True, "userId": user_id, "token": token,
                             "expiresInDays": config.USER_SESSION_DAYS})

    resp = JSONResponse({"ok": True, "userId": user_id})
    resp.set_cookie(USER_COOKIE, token, httponly=True, samesite="lax",
                    max_age=config.USER_SESSION_DAYS * 86400, path="/")
    return resp


@app.post("/api/notifications/firebase-login")
async def api_push_firebase_login(request: Request):
    """Sign in with a Firebase ID token — the SAME login the website and mobile
    app already use. No second passcode exists on this path.

    The token's signature is verified against the service account, so a client
    cannot forge an identity; the userId comes from the verified claims.
    """
    from fastapi.responses import JSONResponse
    body = await request.json()
    id_token = (body.get("idToken") or "").strip()
    if not id_token:
        raise HTTPException(400, "idToken required")

    if not config.firebase_status()["credentials_present"]:
        raise HTTPException(503, "Firebase service account not configured on the server — "
                                 "ID tokens cannot be verified yet")

    result = notif.login_with_firebase(id_token)
    if not result:
        raise HTTPException(401, "Invalid Firebase token, or no Trinetra user for this account")
    token, user_id = result

    if (body.get("client") or "").lower() == "mobile":
        return JSONResponse({"ok": True, "userId": user_id, "token": token,
                             "expiresInDays": config.USER_SESSION_DAYS})
    resp = JSONResponse({"ok": True, "userId": user_id})
    resp.set_cookie(USER_COOKIE, token, httponly=True, samesite="lax",
                    max_age=config.USER_SESSION_DAYS * 86400, path="/")
    return resp


@app.get("/api/notifications/me")
def api_push_me(request: Request):
    uid = _current_user(request)
    if not uid:
        return {"signedIn": False}
    user = notif.get_user(uid)
    return {"signedIn": True, "userId": uid,
            "notificationEnabled": bool(user and user["notification_enabled"]),
            "devices": (user or {}).get("devices", [])}


@app.post("/api/notifications/register-device")
async def api_register_device(request: Request):
    """Bind this browser's FCM token to the signed-in user."""
    user_id = _require_user(request)
    body = await request.json()
    token = (body.get("fcmToken") or "").strip()
    if not token:
        raise HTTPException(400, "fcmToken required")
    device = notif.register_device(
        user_id=user_id,                                  # from the session, never the body
        fcm_token=token,
        platform=(body.get("platform") or "WEB"),
        browser=(body.get("browser") or "")[:80],
        device_id=(body.get("deviceId") or "")[:120],
    )
    print(f"[push] device registered for {user_id} ({device['platform']}/{device['browser']})")
    return {"ok": True, "userId": user_id, "device": device}


@app.delete("/api/notifications/unregister-device")
async def api_unregister_device(request: Request):
    """Detach this browser's token. Used on logout so the next person to sign
    in on this browser cannot receive the previous user's notifications."""
    user_id = _require_user(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    token = (body.get("fcmToken") or "").strip()
    removed = notif.deactivate_token(token, user_id) if token else 0
    return {"ok": True, "deactivated": removed}


@app.post("/api/notifications/logout")
async def api_push_logout(request: Request):
    from fastapi.responses import JSONResponse
    user_id = _current_user(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    token = (body.get("fcmToken") or "").strip()
    if user_id and token:
        notif.deactivate_token(token, user_id)
    # End the session on whichever transport carried it
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        notif.logout(auth[7:].strip())
    notif.logout(request.cookies.get(USER_COOKIE, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(USER_COOKIE, path="/")
    return resp


# ════════════════════════════════════════════════════════════════════════════
# PUSH NOTIFICATIONS — admin endpoints (admin cookie required)
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/admin/notification-users")
def api_admin_notif_users(request: Request, search: Optional[str] = None):
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    users = notif.list_users()
    if search:
        q = search.strip().lower()
        users = [u for u in users if q in u["user_id"].lower()
                 or q in (u.get("name") or "").lower()
                 or q in (u.get("email") or "").lower()]
    return {"users": users, "firebase": config.firebase_status()}


@app.get("/api/admin/notification-users/{user_id}")
def api_admin_notif_user(request: Request, user_id: str):
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    user = notif.get_user(user_id)
    if not user:
        raise HTTPException(404, f"no user {user_id}")
    return user


@app.post("/api/admin/notification-users")
async def api_admin_notif_create(request: Request):
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    body = await request.json()
    user_id = (body.get("userId") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{3,40}", user_id):
        raise HTTPException(400, "userId must be 3-40 chars: letters, digits, _ or -")
    if notif.get_user(user_id):
        raise HTTPException(409, f"{user_id} already exists")
    return notif.upsert_user(
        user_id,
        name=(body.get("name") or "").strip(),
        email=(body.get("email") or "").strip(),
        passcode=body.get("passcode") or None,
        notification_enabled=body.get("notificationEnabled", True),
    )


@app.put("/api/admin/notification-users/{user_id}")
async def api_admin_notif_update(request: Request, user_id: str):
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    if not notif.get_user(user_id):
        raise HTTPException(404, f"no user {user_id}")
    body = await request.json()
    enabled = body.get("notificationEnabled")

    # Optional rename — carries devices, sessions and history to the new id.
    new_id = (body.get("newUserId") or "").strip()
    if new_id and new_id != user_id:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{3,40}", new_id):
            raise HTTPException(400, "userId must be 3-40 chars: letters, digits, _ or -")
        try:
            notif.rename_user(user_id, new_id)
        except ValueError as e:
            raise HTTPException(409, str(e))
        user_id = new_id

    return notif.upsert_user(
        user_id,
        name=(body.get("name") or "").strip(),
        email=(body.get("email") or "").strip(),
        passcode=body.get("passcode") or None,
        notification_enabled=None if enabled is None else bool(enabled),
    )


@app.delete("/api/admin/notification-users/{user_id}")
def api_admin_notif_delete(request: Request, user_id: str):
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    return notif.delete_user(user_id)


@app.delete("/api/admin/notification-users/{user_id}/devices/{device_row_id}")
def api_admin_notif_del_device(request: Request, user_id: str, device_row_id: int):
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    removed = notif.delete_device(device_row_id)
    if not removed:
        raise HTTPException(404, "no such device")
    return {"ok": True, "deleted": removed}


@app.post("/api/admin/notification-users/{user_id}/test")
async def api_admin_notif_test(request: Request, user_id: str):
    """Send a test notification to one user — the quickest end-to-end check."""
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    result = await asyncio.get_event_loop().run_in_executor(
        None, NotificationService.send_to_user, user_id,
        {"title": "Trinetra test notification",
         "body": f"Push is working for {user_id}.",
         "data": {"eventType": "TEST"}, "link": "/index.html"}
    )
    return result


# ─── /ws — WebSocket for live updates ─────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await wsm.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        pass
    finally:
        await wsm.disconnect(ws)


# ─── Static frontend — mounted LAST so /api and /ws take precedence ──────
app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="frontend")


# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=config.HOST, port=config.PORT, log_level="info")
