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
import hmac
import uuid
import io
import json
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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import access
import alert_rules
import captures
import config
import dongle
import ds_control
import speaker
import housekeeping
import security

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


ACCESS = access.Access(lambda: db(), _db_lock)
_DB_KEEPER: Optional[sqlite3.Connection] = None   # one connection stays open (see lifespan)


def init_db():
    """Apply schema.sql once. Idempotent. Also migrates older DBs."""
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    schema = Path(__file__).parent / "schema.sql"
    conn = db()
    try:
        conn.executescript(schema.read_text())
        # MIGRATION: add session_id to detections if older DB doesn't have it
        cols = [r[1] for r in conn.execute("PRAGMA table_info(detections)").fetchall()]
        if "session_id" not in cols:
            print("[migrate] adding session_id column to detections")
            conn.execute("ALTER TABLE detections ADD COLUMN session_id INTEGER REFERENCES sessions(id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_det_session ON detections(session_id)")
        # MIGRATION: classify alerts by perimeter zone (breach vs early warning)
        acols = [r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()]
        if "kind" not in acols:
            print("[migrate] adding kind/camera/zone columns to alerts")
            conn.execute("ALTER TABLE alerts ADD COLUMN kind TEXT DEFAULT 'dwell'")
            conn.execute("ALTER TABLE alerts ADD COLUMN camera TEXT")
            conn.execute("ALTER TABLE alerts ADD COLUMN zone TEXT")
            conn.execute("ALTER TABLE alerts ADD COLUMN image TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_kind ON alerts(kind, triggered_at DESC)")
        # Sign-in accounts, sessions and the Activity Logs (access.py)
        ACCESS.init_schema(conn)
        # Alert rules (the Alerts page) + frame observations; the alerts table also holds rule alerts
        alert_rules.migrate(conn)
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
        dt_local = datetime.strptime(m["ts"], "%Y%m%d-%H%M%S") + timedelta(milliseconds=int(m["ms"] or 0))
        local_offset = datetime.now() - datetime.utcnow()
        dt = dt_local - local_offset
    except ValueError:
        return None
    return m["camera"], m["badge"], dt


# The alerts come from the alert rules (alert_rules.py, the Alerts page) since 25 Nov 2026; the class-based alerts below
# (instant breach classes, per-class dwell from the settings file) are kept switched off. The sessions they kept still
# feed the Visits page.
LEGACY_CLASS_ALERTS = False


def _alert_if_dwell_met(conn, sess, pending_alerts):
    """Raise the early warning for a presence that is being closed, if it met
    its dwell rule and has not alerted yet. Exactly-once via alert_sent."""
    if not LEGACY_CLASS_ALERTS or pending_alerts is None or sess["alert_sent"]:
        return
    dwell_min = config.dwell_minutes_for(sess["badge"])
    if dwell_min is None:
        return
    if conn.execute("SELECT 1 FROM whitelist WHERE badge=?", (sess["badge"],)).fetchone():
        return
    first = datetime.fromisoformat(sess["first_seen"])
    last = datetime.fromisoformat(sess["last_seen"])
    span = int((last - first).total_seconds())
    if span < dwell_min * 60:
        return

    img = conn.execute(
        "SELECT filename FROM detections WHERE session_id=? ORDER BY timestamp DESC LIMIT 1",
        (sess["id"],)
    ).fetchone()
    last_image = img["filename"] if img else None
    zone = config.zone_for(sess["badge"])
    cur = conn.execute(
        "INSERT INTO alerts(session_id, badge, triggered_at, duration_seconds, kind, camera, zone, image) "
        "VALUES (?,?,?,?, 'dwell', ?, ?, ?)",
        (sess["id"], sess["badge"], datetime.utcnow().isoformat(), span,
         sess["last_camera"], zone, last_image)
    )
    conn.execute("UPDATE sessions SET alert_sent=1 WHERE id=?", (sess["id"],))
    pending_alerts.append({
        "alert_id": cur.lastrowid,
        "session_id": sess["id"],
        "badge": sess["badge"],
        "first_seen": sess["first_seen"],
        "last_seen": sess["last_seen"],
        "last_camera": sess["last_camera"],
        "duration_seconds": span,
        "dwell_minutes": dwell_min,
        "zone": zone,
        "last_image": last_image,
    })


def _update_perimeter_session(conn, badge: str, camera: str, ts: datetime,
                              pending_alerts=None):
    """Continuous-presence sessions for breach / outside-perimeter classes.

    A detection joins the open session only if it falls within
    PERIMETER_GAP_MINUTES of that session's observed span. Otherwise the object
    had left: the old session is closed and a fresh one starts, so the dwell
    timer restarts and the class can raise a new early warning.

    Order-safe: files may be ingested out of timestamp order, so the span is
    widened in both directions rather than assuming every frame is the newest.

    Counted classes ("Outside-person-2-object-1") share one presence across
    tally changes — a second person arriving must not reset the dwell timer.
    """
    gap = timedelta(minutes=config.PERIMETER_GAP_MINUTES)

    cols = "id, badge, first_seen, last_seen, last_camera, cameras_json, alert_sent"
    if config.is_counted_outside(badge):
        row = conn.execute(
            f"SELECT {cols} FROM sessions "
            f"WHERE closed=0 AND {_NORM_SQL} LIKE ? ORDER BY id DESC LIMIT 1",
            (config.OUTSIDE_COUNT_LIKE,)
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT {cols} FROM sessions "
            f"WHERE closed=0 AND {_NORM_SQL}=? ORDER BY id DESC LIMIT 1",
            (config.normalize_badge(badge),)
        ).fetchone()

    if row:
        first = datetime.fromisoformat(row["first_seen"])
        last = datetime.fromisoformat(row["last_seen"])
        if (first - gap) <= ts <= (last + gap):
            cams = json.loads(row["cameras_json"] or "{}")
            cams[camera] = cams.get(camera, 0) + 1
            new_first = min(first, ts)
            new_last = max(last, ts)
            if ts >= last:
                # Newest frame: it carries the current camera and tally.
                conn.execute(
                    "UPDATE sessions SET badge=?, first_seen=?, last_seen=?, last_camera=?, "
                    "total_sightings=total_sightings+1, cameras_json=? WHERE id=?",
                    (badge, new_first.isoformat(), new_last.isoformat(), camera,
                     json.dumps(cams), row["id"])
                )
            else:
                # Late-arriving older frame: only widens the span backwards.
                conn.execute(
                    "UPDATE sessions SET first_seen=?, last_seen=?, "
                    "total_sightings=total_sightings+1, cameras_json=? WHERE id=?",
                    (new_first.isoformat(), new_last.isoformat(), json.dumps(cams), row["id"])
                )
            return row["id"], "sighting", bool(row["alert_sent"])

        if ts < first - gap:
            # A late frame from BEFORE the current presence. It must not end
            # the live session — file it into the past presence it belongs to.
            return _file_historical_frame(conn, badge, camera, ts, gap)

        # Newer and not seen for longer than the gap — that presence is over.
        # The dwell checker only looks at OPEN sessions every 30s, so a
        # presence that met its dwell rule but is closed here, between ticks,
        # would otherwise never raise its early warning. Judge it now.
        _alert_if_dwell_met(conn, row, pending_alerts)
        conn.execute("UPDATE sessions SET closed=1, closed_at=? WHERE id=?",
                     (last.isoformat(), row["id"]))

    cur = conn.execute(
        "INSERT INTO sessions(badge, first_seen, last_seen, last_camera, cameras_json) "
        "VALUES (?,?,?,?,?)",
        (badge, ts.isoformat(), ts.isoformat(), camera, json.dumps({camera: 1}))
    )
    return cur.lastrowid, "entry", False


def _file_historical_frame(conn, badge: str, camera: str, ts: datetime, gap: timedelta):
    """Attach a late-arriving old frame to the closed presence it falls in,
    or record it as its own closed presence. Never touches the open session."""
    family_sql = (f"{_NORM_SQL} LIKE ?", (config.OUTSIDE_COUNT_LIKE,)) \
        if config.is_counted_outside(badge) \
        else (f"{_NORM_SQL}=?", (config.normalize_badge(badge),))

    lo, hi = (ts - gap).isoformat(), (ts + gap).isoformat()
    past = conn.execute(
        "SELECT id, first_seen, last_seen, cameras_json FROM sessions "
        f"WHERE closed=1 AND {family_sql[0]} AND last_seen >= ? AND first_seen <= ? "
        "ORDER BY id DESC LIMIT 1",
        (*family_sql[1], lo, hi)
    ).fetchone()

    if past:
        cams = json.loads(past["cameras_json"] or "{}")
        cams[camera] = cams.get(camera, 0) + 1
        first = min(datetime.fromisoformat(past["first_seen"]), ts)
        last = max(datetime.fromisoformat(past["last_seen"]), ts)
        conn.execute(
            "UPDATE sessions SET first_seen=?, last_seen=?, closed_at=?, "
            "total_sightings=total_sightings+1, cameras_json=? WHERE id=?",
            (first.isoformat(), last.isoformat(), last.isoformat(), json.dumps(cams), past["id"])
        )
        return past["id"], "sighting", False

    cur = conn.execute(
        "INSERT INTO sessions(badge, first_seen, last_seen, last_camera, cameras_json, closed, closed_at) "
        "VALUES (?,?,?,?,?,1,?)",
        (badge, ts.isoformat(), ts.isoformat(), camera, json.dumps({camera: 1}), ts.isoformat())
    )
    return cur.lastrowid, "entry", False


def _update_outside_presence(conn, badge: str, camera: str, ts: datetime):
    """Presence for an outside-perimeter class on ONE camera.

    A detection means the object is there from that moment. The presence then
    stays open until an operator acknowledges its early warning — it does not
    end just because no further frame arrived. Dwell is therefore
    now - first sighting, so an entry at 11:00 has dwelt 60 min by 12:00.

    Keyed by class AND camera: an object at cam-perimeter-4 and another at
    cam-perimeter-7 are separate presences with separate alerts. Counted
    classes (Outside-person-N-object-M) share one presence per camera as the
    tally changes.
    """
    if config.is_counted_outside(badge):
        fam_sql, fam_params = f"{_NORM_SQL} LIKE ?", (config.OUTSIDE_COUNT_LIKE,)
    else:
        fam_sql, fam_params = f"{_NORM_SQL}=?", (config.normalize_badge(badge),)

    row = conn.execute(
        "SELECT id, first_seen, last_seen, cameras_json, alert_sent FROM sessions "
        f"WHERE closed=0 AND {fam_sql} AND last_camera=? ORDER BY id DESC LIMIT 1",
        (*fam_params, camera)
    ).fetchone()

    if row:
        first = datetime.fromisoformat(row["first_seen"])
        last = datetime.fromisoformat(row["last_seen"])
        cams = json.loads(row["cameras_json"] or "{}")
        cams[camera] = cams.get(camera, 0) + 1
        # Order-safe: an older frame widens the presence back to when it began.
        conn.execute(
            "UPDATE sessions SET badge=CASE WHEN ? >= last_seen THEN ? ELSE badge END, "
            "first_seen=?, last_seen=?, total_sightings=total_sightings+1, cameras_json=? "
            "WHERE id=?",
            (ts.isoformat(), badge, min(first, ts).isoformat(), max(last, ts).isoformat(),
             json.dumps(cams), row["id"])
        )
        return row["id"], "sighting", bool(row["alert_sent"])

    cur = conn.execute(
        "INSERT INTO sessions(badge, first_seen, last_seen, last_camera, cameras_json) "
        "VALUES (?,?,?,?,?)",
        (badge, ts.isoformat(), ts.isoformat(), camera, json.dumps({camera: 1}))
    )
    return cur.lastrowid, "entry", False


def update_session(conn, badge: str, camera: str, ts: datetime, is_exit: bool,
                   pending_alerts=None):
    """
    Find open session for badge, extend it OR close it (on exit) OR open new one.
    Returns (session_id, detection_type, is_loitering)
      where type is 'entry' | 'sighting' | 'exit'
      and is_loitering=True if the session already had alert_sent=1.
    """
    # Perimeter classes use continuous presence; everything else keeps the
    # original behaviour below, untouched.
    if config.zone_for(badge) == config.ZONE_OUTSIDE:
        return _update_outside_presence(conn, badge, camera, ts)
    if config.is_perimeter_class(badge):          # breach classes
        return _update_perimeter_session(conn, badge, camera, ts, pending_alerts)

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
        return row["id"], "exit", False

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
            return row["id"], "sighting", already_loitering
        # Gap too long → close old session, open new one below
        conn.execute("UPDATE sessions SET closed=1, closed_at=? WHERE id=?",
                     (ts.isoformat(), row["id"]))

    if is_exit:
        # Exit on a badge with no open session — just log it, no session
        return None, "exit", False

    cur = conn.execute(
        "INSERT INTO sessions(badge, first_seen, last_seen, last_camera, cameras_json) "
        "VALUES (?,?,?,?,?)",
        (badge, ts.isoformat(), ts.isoformat(), camera, json.dumps({camera: 1}))
    )
    return cur.lastrowid, "entry", False


def _breach_cooldown_passed(conn, badge: str, camera: str, ts: datetime) -> bool:
    """True if enough time has passed since the last breach alert for this
    badge+camera. With TRINETRA_BREACH_COOLDOWN=0 (the default) every single
    detection in the Perimeter Alert Region fires its own alert."""
    if config.BREACH_COOLDOWN_SECONDS <= 0:
        return True
    row = conn.execute(
        "SELECT triggered_at FROM alerts WHERE badge=? AND camera=? AND kind='breach' "
        "ORDER BY id DESC LIMIT 1",
        (badge, camera)
    ).fetchone()
    if not row or not row["triggered_at"]:
        return True
    try:
        last = datetime.fromisoformat(row["triggered_at"])
    except ValueError:
        return True
    return (datetime.utcnow() - last).total_seconds() >= config.BREACH_COOLDOWN_SECONDS


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
    breach = None
    closed_presence_alerts = []   # early warnings for presences closed during ingest

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
                # A Perimeter Alert Region class is never an "exit" — it must
                # always open/extend a session so the breach alert has context.
                is_breach = config.is_breach_badge(badge)
                is_exit = (camera in config.EXIT_CAMERAS) and not is_breach
                session_id, det_type, is_loitering = update_session(
                    conn, badge, camera, ts, is_exit, closed_presence_alerts)
                # Stamp the detection with its session (visit) ID and final type
                conn.execute(
                    "UPDATE detections SET type=?, session_id=? WHERE id=?",
                    (det_type, session_id, detection_id)
                )
                # If this badge is already loitering, fetch session info for re-alert
                # but ONLY if the badge is not whitelisted (may have been added after first alert)
                is_whitelisted = conn.execute(
                    "SELECT 1 FROM whitelist WHERE badge=?", (badge,)
                ).fetchone() is not None

                if LEGACY_CLASS_ALERTS and is_loitering and session_id and det_type == "sighting" and not is_whitelisted:
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

                # ── INSTANT BREACH: Perimeter Alert Region needs no dwell ──
                breach = None
                if LEGACY_CLASS_ALERTS and is_breach and session_id and not is_whitelisted:
                    if _breach_cooldown_passed(conn, badge, camera, ts):
                        bcur = conn.execute(
                            "INSERT INTO alerts(session_id, badge, triggered_at, "
                            "duration_seconds, kind, camera, zone, image) "
                            "VALUES (?,?,?,?, 'breach', ?, ?, ?)",
                            (session_id, badge, datetime.utcnow().isoformat(), 0,
                             camera, config.ZONE_BREACH, path.name)
                        )
                        breach = {
                            "alert_id": bcur.lastrowid,
                            "session_id": session_id,
                            "badge": badge,
                            "camera": camera,
                            "zone": config.ZONE_BREACH,
                            "zone_label": config.ZONE_LABELS[config.ZONE_BREACH],
                            "timestamp": ts.isoformat(),
                            "last_image": path.name,
                        }
                    else:
                        print(f"[breach] cooldown active — {badge} @ {camera} not re-alerted")
                elif LEGACY_CLASS_ALERTS and is_breach and is_whitelisted:
                    print(f"[breach] suppressed: {badge} is whitelisted")
        finally:
            conn.close()

    # Move image to archive (served at /images/<filename>)
    Path(config.ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(path), Path(config.ARCHIVE_DIR) / path.name)
    except FileNotFoundError:
        pass

    _run_alert_rules(path, camera, badge, ts)

    broadcast_sync({
        "type": "detection",
        "filename": path.name,
        "camera": camera,
        "badge": badge,
        "timestamp": ts.isoformat(),
        "session_id": session_id,
        "detection_type": det_type,
    })

    # Early warnings for presences that met their dwell rule and closed here.
    # Reuses the existing, unchanged fire_alert() delivery path.
    if closed_presence_alerts and _main_loop:
        for a in closed_presence_alerts:
            print(f"[dwell] early warning on close: {a['badge']} present {a['duration_seconds'] // 60} min")
            asyncio.run_coroutine_threadsafe(fire_alert(a), _main_loop)

    # Fire BREACH alert AFTER archiving so the image file exists for Telegram
    if breach and _main_loop:
        print(f"[breach] INSTANT alert: {badge} in Perimeter Alert Region @ {camera}")
        asyncio.run_coroutine_threadsafe(fire_breach_alert(breach), _main_loop)

    # Fire re-alert AFTER archiving so the image file exists when Telegram reads it
    if loiter_resight and _main_loop:
        print(f"[loiter] re-alert: {badge} still loitering, new sighting at {camera}")
        asyncio.run_coroutine_threadsafe(
            fire_loiter_resight(loiter_resight), _main_loop
        )


def _run_alert_rules(path: Path, camera: str, badge: str, ts_utc: datetime):
    """the frame description the pipeline wrote next to the image (<image>.json) → the alert rules; an image without one
    (an edge device, a test image) counts by its file-name class"""
    side, meta = path.with_suffix(".json"), None
    try:
        if side.is_file() and side.stat().st_size <= 256_000:
            meta = json.loads(side.read_text(errors="replace"))
            if not isinstance(meta, dict):
                meta = None
    except (OSError, ValueError, RecursionError) as e:
        print(f"[rules] frame description not read ({side.name}): {e!r}")
    try:
        side.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        RULES.ingest(camera, ts_utc + (datetime.now() - datetime.utcnow()), path.name, meta, badge)
    except Exception as e:
        print(f"[rules] {path.name}: {e!r}")


def _rule_alert_notify(alert: dict):
    """a new rule alert (any thread) → the dashboards (websocket) + Telegram / email when set up (optional: offline sites
    see the alert on the dashboard)"""
    print(f"[alert] {alert['severity'].upper()} · {alert['rule_name']} · {alert['camera']} · {alert['message']}")
    speaker.on_alert(alert)                          # the Bluetooth speaker on the Jetson, when switched on
    if _main_loop:
        asyncio.run_coroutine_threadsafe(fire_rule_alert(alert), _main_loop)


async def fire_rule_alert(alert: dict):
    await wsm.broadcast({"type": "alert", **{k: alert[k] for k in ("alert_id", "rule_name", "severity", "kind", "camera",
                                                                   "zone", "message", "image", "sound")}})
    text = (f"{'🚨' if alert['severity'] == 'critical' else '⚠️'} {alert['rule_name'].upper()}\n"
            f"📷 Camera : {alert['camera']}\n🧱 Region : {alert['zone']}\n📝 {alert['message']}\n"
            f"🕐 {datetime.now().strftime('%d %b %Y  %H:%M:%S')}")
    if config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_IDS:
        photo = Path(config.ARCHIVE_DIR) / alert["image"] if alert.get("image") else None
        for chat_id in config.TELEGRAM_CHAT_IDS:
            try:
                if photo and photo.is_file():
                    await _send_telegram_photo(chat_id, text, photo)
                else:
                    await _send_telegram(chat_id, text)
            except Exception as e:
                print(f"[alert] telegram to {chat_id}: {type(e).__name__}")
        _mark_alert(alert["alert_id"], "telegram_sent")
    if config.SMTP_USER and config.SMTP_TO and config.SMTP_PASS:
        try:
            await asyncio.get_event_loop().run_in_executor(None, _send_email, f"Trinetra alert: {alert['rule_name']}", text)
            _mark_alert(alert["alert_id"], "email_sent")
        except Exception as e:
            print(f"[alert] email: {type(e).__name__}")


RULES = alert_rules.Engine(lambda: db(), _db_lock, _rule_alert_notify, lambda: config.PRESENCE_GAP_SECONDS)


async def offline_rules_loop():
    """every 30 s: cameras without video (or the pipeline down while it should run) → the offline rules"""
    while True:
        await asyncio.sleep(30)
        try:
            if not ds_control.available():
                continue
            cfg = ds_control.load_config()
            cams = [s["name"] for s in cfg["streams"] if s["enabled"]]
            st = ds_control.running_state()
            streams = {s["name"]: s.get("last_frame_s") for s in st.get("streams", [])}
            await asyncio.to_thread(RULES.check_offline, datetime.now(), cams, bool(ds_control.pid_alive()),
                                    ds_control.intent(), streams)
        except Exception as e:
            print(f"[rules] offline check: {e!r}")


async def speaker_loop():
    """every 5 s: beep again on the Bluetooth speaker while an alert it is for stays unacknowledged (when set)"""
    while True:
        await asyncio.sleep(5)
        try:
            if not speaker.load()["repeat_s"]:
                continue
            conn = db()
            try:
                rows = conn.execute("SELECT COALESCE(severity, CASE WHEN kind='breach' THEN 'critical' ELSE 'warning' END), "
                                    "COALESCE(sound, 1) FROM alerts WHERE acknowledged=0 AND rule_id IS NOT NULL").fetchall()
            finally:
                conn.close()
            speaker.repeat_tick([(r[0], bool(r[1])) for r in rows])
        except Exception as e:
            print(f"[speaker] repeat: {e!r}")


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


def _by_frame_time(p: Path):
    """images in the order they were taken (the time in the file name; else the file time): a backlog after a restart
    is replayed in order, so the alert rules see presences as they happened"""
    m = config.FILENAME_RE.match(p.name)
    try:
        return ((m["ts"] + "-" + (m["ms"] or "000")) if m else datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y%m%d-%H%M%S-000"), p.name)
    except OSError:
        return ("", p.name)


def scan_existing():
    """At startup, process any files left in incoming/ from a previous run."""
    incoming = Path(config.INCOMING_DIR)
    incoming.mkdir(parents=True, exist_ok=True)
    files = sorted(incoming.glob("*.jpg"), key=_by_frame_time)   # in time order: presences / alerts as they happened
    if files:
        print(f"[ingest] catching up on {len(files)} leftover file(s)")
    for f in files:
        handle_new_file(f)


def incoming_sweeper():
    """Safety net: a file that arrives without a close / move event — moved in from another folder on the same disk
    (`mv /tmp/x.jpg incoming/`) shows up only as 'created' — is picked up within ~10 s. 3 s old at least, so a file
    still being written is never read half-way; a file already ingested is ignored by the database (same name)."""
    while True:
        time.sleep(10)
        try:
            now = time.time()
            for f in sorted(Path(config.INCOMING_DIR).glob("*.jpg"), key=_by_frame_time):
                try:
                    if now - f.stat().st_mtime >= 3:
                        handle_new_file(f)
                except FileNotFoundError:
                    pass
        except Exception as e:
            print(f"[ingest] sweep: {e!r}")


def start_watcher() -> Observer:
    Path(config.INCOMING_DIR).mkdir(parents=True, exist_ok=True)
    threading.Thread(target=incoming_sweeper, daemon=True, name="incoming-sweeper").start()
    obs = Observer()
    obs.schedule(IncomingHandler(), config.INCOMING_DIR, recursive=False)
    obs.start()
    print(f"[ingest] watching {config.INCOMING_DIR}")
    return obs


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
# PERIMETER BREACH — instant alert, no dwell wait
#
# Reuses the same Telegram/email delivery helpers as the loitering alerts
# (_send_telegram_photo / _send_telegram / _mark_alert) — that path is known
# good and is deliberately left untouched.
# ════════════════════════════════════════════════════════════════════════════

async def fire_breach_alert(breach: dict):
    """Any detection inside the Perimeter Alert Region alerts immediately."""
    badge  = breach["badge"]
    camera = breach.get("camera") or "unknown"

    local_offset = datetime.now() - datetime.utcnow()
    seen_local = datetime.fromisoformat(breach["timestamp"]) + local_offset

    text = (
        f"🚨 PERIMETER BREACH\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧱 Zone        : {breach.get('zone_label', 'Perimeter Alert Region')}\n"
        f"🎯 Detection   : {badge}\n"
        f"📷 Camera      : {camera}\n"
        f"🕐 Time        : {seen_local.strftime('%d %b %Y  %H:%M:%S')}\n"
        f"⚡ Rule        : instant — no dwell wait"
    )

    if config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_IDS:
        try:
            photo_path = None
            if breach.get("last_image"):
                photo_path = Path(config.ARCHIVE_DIR) / breach["last_image"]
            has_photo = photo_path and photo_path.exists() and photo_path.stat().st_size > 0

            for chat_id in config.TELEGRAM_CHAT_IDS:
                try:
                    if has_photo:
                        await _send_telegram_photo(chat_id, text, photo_path)
                    else:
                        await _send_telegram(chat_id, text)
                    print(f"[breach] sent to chat_id={chat_id} for badge={badge}")
                except Exception as e:
                    print(f"[breach] photo send failed for chat_id={chat_id}: {e} — falling back to text")
                    try:
                        await _send_telegram(chat_id, text)
                        print(f"[breach] text fallback sent to chat_id={chat_id}")
                    except Exception as e2:
                        print(f"[breach] text fallback also failed for chat_id={chat_id}: {e2}")
            _mark_alert(breach["alert_id"], "telegram_sent")
        except Exception as e:
            print(f"[breach] telegram setup failed: {e}")

    if config.SMTP_USER and config.SMTP_TO and config.SMTP_PASS:
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, _send_email, "Trinetra Perimeter Breach", text
            )
            _mark_alert(breach["alert_id"], "email_sent")
        except Exception as e:
            print(f"[breach] email failed: {e}")

    await wsm.broadcast({"type": "perimeter_breach", **breach})


# ════════════════════════════════════════════════════════════════════════════
# DWELL CHECKER (runs every 30s)
# ════════════════════════════════════════════════════════════════════════════

def _presence_seconds(badge: str, first_seen: str, last_seen: str, now: datetime,
                      closed_at: Optional[str] = None) -> float:
    """How long this object has been CONTINUOUSLY present, in seconds.

    Perimeter classes: while still being seen (idle within the gap) the timer
    runs to now; once it has gone quiet, presence is the observed span only.
    That stops a single old frame from reading as hours of dwell.

    Legacy classes keep the original first_seen-to-now measure.
    """
    first = datetime.fromisoformat(first_seen)
    if config.zone_for(badge) == config.ZONE_OUTSIDE:
        # Present from first sighting until the alert is acknowledged.
        end = datetime.fromisoformat(closed_at) if closed_at else now
        return max(0.0, (end - first).total_seconds())
    if not config.is_perimeter_class(badge):
        return (now - first).total_seconds()
    last = datetime.fromisoformat(last_seen)
    still_present = (now - last) <= timedelta(minutes=config.PERIMETER_GAP_MINUTES)
    end = now if still_present else last
    return max(0.0, (end - first).total_seconds())


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
            now = datetime.utcnow()

            # ── STEP 1: Loitering alert check (runs BEFORE stale-close) ──────
            # A session qualifies if first_seen is old enough, regardless of
            # how old last_seen is. This matters when a back-dated image file
            # is dropped: last_seen == ts (in the past), but first_seen also
            # shows the badge has been here long enough to loiter.
            rows = conn.execute("""
                SELECT id, badge, first_seen, last_seen, last_camera FROM sessions
                WHERE closed=0 AND alert_sent=0
                  AND badge NOT IN (SELECT badge FROM whitelist)
            """).fetchall()

            for r in (rows if LEGACY_CLASS_ALERTS else []):
                # Per-badge dwell threshold:
                #   outside-perimeter classes use their own rule (default 10 min)
                #   breach classes already alerted instantly  → None, skip
                #   inside-factory classes are tracked, no alarm → None, skip
                #   anything else falls back to TRINETRA_LOITER_MINUTES
                dwell_min = config.dwell_minutes_for(r["badge"])
                if dwell_min is None:
                    continue
                first = datetime.fromisoformat(r["first_seen"])
                presence = _presence_seconds(r["badge"], r["first_seen"], r["last_seen"], now)
                if presence >= dwell_min * 60:
                    duration = int(presence)
                    zone = config.zone_for(r["badge"])
                    with conn:
                        last_image_row = conn.execute(
                            "SELECT filename FROM detections WHERE session_id=? ORDER BY timestamp DESC LIMIT 1",
                            (r["id"],)
                        ).fetchone()
                        last_image = last_image_row["filename"] if last_image_row else None
                        cur = conn.execute(
                            "INSERT INTO alerts(session_id, badge, triggered_at, "
                            "duration_seconds, kind, camera, zone, image) "
                            "VALUES (?,?,?,?, 'dwell', ?, ?, ?)",
                            (r["id"], r["badge"], now.isoformat(), duration,
                             r["last_camera"], zone, last_image)
                        )
                        conn.execute("UPDATE sessions SET alert_sent=1 WHERE id=?", (r["id"],))
                    alerts_to_send.append({
                        "alert_id": cur.lastrowid,
                        "session_id": r["id"],
                        "badge": r["badge"],
                        "first_seen": r["first_seen"],
                        "last_seen": r["last_seen"],
                        "last_camera": r["last_camera"],
                        "duration_seconds": duration,
                        "dwell_minutes": dwell_min,
                        "zone": zone,
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

            # Perimeter classes have no exit camera, so the "keep an alerted
            # session open until it exits" rule above would hold them open
            # forever and swallow every later detection. Instead, once a
            # perimeter object has not been seen for PERIMETER_GAP_MINUTES,
            # its presence is over — alerted or not.
            perim_cutoff = now - timedelta(minutes=config.PERIMETER_GAP_MINUTES)
            to_close = [
                s["id"] for s in conn.execute(
                    "SELECT id, badge, last_seen FROM sessions WHERE closed=0"
                ).fetchall()
                if config.zone_for(s["badge"]) == config.ZONE_BREACH
                and datetime.fromisoformat(s["last_seen"]) < perim_cutoff
            ]
            if to_close:
                with conn:
                    conn.executemany(
                        "UPDATE sessions SET closed=1, closed_at=last_seen WHERE id=?",
                        [(sid,) for sid in to_close]
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
# HOUSEKEEPING — image retention, disk guard, Activity Logs pruning, nightly DB backup (housekeeping.py)
# ════════════════════════════════════════════════════════════════════════════

async def housekeeping_loop():
    """every 10 min: the disk guard; hourly: the days of images kept (night only) and the Activity Logs"""
    await asyncio.sleep(60)                          # let the server finish starting
    n = 0
    while True:
        try:
            await asyncio.to_thread(housekeeping.disk_guard)
            if n % 6 == 0:
                await asyncio.to_thread(housekeeping.retention)
                await asyncio.to_thread(ACCESS.prune)
                await asyncio.to_thread(_prune_observations)
        except Exception as e:
            print(f"[housekeeping] {e!r}")
        n += 1
        await asyncio.sleep(600)


def _delete_batches(sql_ids: str, args: tuple, batch: int = 5000) -> int:
    """delete in small batches (the dashboard and the ingest keep the database between batches)"""
    total = 0
    while True:
        conn = db()
        try:
            with _db_lock, conn:
                n = conn.execute(sql_ids.format(batch=batch), args).rowcount
        finally:
            conn.close()
        total += n
        if n < batch:
            return total
        time.sleep(0.2)


def _prune_observations():
    """frame observations: the alert rules look back at most a day (a dwell rule of 24 h) — 3 days kept; Detection Log
    rows + finished visits older than TRINETRA_DETECTION_RETENTION_DAYS; a frame description left in incoming/ without
    its image (the pipeline stopped between the two) goes after an hour"""
    n = _delete_batches("DELETE FROM observations WHERE id IN (SELECT id FROM observations WHERE ts<? LIMIT {batch})",
                        ((datetime.now() - timedelta(days=3)).isoformat(timespec="seconds"),))
    d = v = 0
    if config.DETECTION_RETENTION_DAYS > 0:
        cut = (datetime.utcnow() - timedelta(days=config.DETECTION_RETENTION_DAYS)).isoformat()
        d = _delete_batches("DELETE FROM detections WHERE id IN (SELECT id FROM detections WHERE timestamp<? LIMIT {batch})", (cut,))
        v = _delete_batches("DELETE FROM sessions WHERE id IN (SELECT id FROM sessions WHERE closed=1 AND last_seen<? "
                            "AND id NOT IN (SELECT session_id FROM alerts WHERE session_id IS NOT NULL) "
                            "AND NOT EXISTS (SELECT 1 FROM detections d WHERE d.session_id=sessions.id) LIMIT {batch})", (cut,))
                                                       # (a visit still named by a detection or an alert stays)
        if d:
            _stats_reset(); _SUMMARY.clear()
    stale = [f for f in Path(config.INCOMING_DIR).glob("*.json") if time.time() - f.stat().st_mtime > 3600]
    for f in stale:
        f.unlink(missing_ok=True)
    if n or d or v or stale:
        print(f"[housekeeping] removed: {n} frame observation(s) > 3 days, {d} detection(s) + {v} visit(s) > "
              f"{config.DETECTION_RETENTION_DAYS} days, {len(stale)} stray frame description(s)")


async def backup_loop():
    last = None
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        if not housekeeping.backup_due(now, last):
            continue
        last = now.date()
        try:
            await asyncio.to_thread(housekeeping.db_backup, db, now)
        except Exception as e:
            print(f"[backup] {e!r}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _main_loop, _DB_KEEPER
    _main_loop = asyncio.get_event_loop()

    config.print_summary()
    init_db()
    # One connection stays open: otherwise the last short-lived connection to close checkpoints and deletes the
    # -wal / -shm files every time — many small disk writes per poll while the dashboard is idle.
    _DB_KEEPER = db()
    threading.Thread(target=scan_existing, daemon=True).start()
    observer = start_watcher()
    loiter_task = asyncio.create_task(loitering_loop())
    house_task  = asyncio.create_task(housekeeping_loop())
    backup_task = asyncio.create_task(backup_loop())
    offline_task = asyncio.create_task(offline_rules_loop())
    speaker_task = asyncio.create_task(speaker_loop())
    if ds_control.available():
        ds_control.start()                           # DeepStream page: supervisor (keeps the pipeline running when wanted)
        print("[ds] DeepStream control on (pipeline restarted automatically when it was running)")
    else:
        print("[ds] DeepStream not installed here — the DeepStream page shows the settings only")
    captures.start()                                 # Recordings page: carries on recordings / frames / packing cut off
    if not config.SUPPORT_PASSWORD:
        print("[access] WARNING: SUPPORT_PASSWORD is empty — nobody can sign in until it is set in the settings file")
    print("[trinetra] startup complete · http://%s:%d" % (config.HOST, config.PORT))
    try:
        yield
    finally:
        print("[trinetra] shutting down")
        observer.stop()
        observer.join(timeout=2)
        for t in (loiter_task, house_task, backup_task, offline_task, speaker_task):
            t.cancel()
        if _DB_KEEPER is not None:
            _DB_KEEPER.close()


# API docs (/docs, /redoc, /openapi.json) are off: they publish every route
# and parameter to anyone who can reach the server.
app = FastAPI(title="Trinetra Systems Monitor", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


# ════════════════════════════════════════════════════════════════════════════
# SECURITY — one central check on every request (deny by default). Who may open what: access.py
# ════════════════════════════════════════════════════════════════════════════
SESSION_COOKIE = access.SESSION_COOKIE
# Extra browser origins allowed to send changes (e.g. a reverse proxy that
# rewrites the Host header). Comma-separated, e.g. https://site.example.ts.net
ALLOWED_ORIGINS = {o.strip().rstrip("/").lower()
                   for o in os.environ.get("TRINETRA_ALLOWED_ORIGINS", "").split(",") if o.strip()}


def _is_https(request) -> bool:
    return request.url.scheme in ("https", "wss") or request.headers.get("x-forwarded-proto") == "https"


def _client_ip(request) -> str:
    """the caller's address; behind the local Tailscale Funnel / serve proxy (127.0.0.1) the forwarded one"""
    host = request.client.host if request.client else "?"
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd and host in ("127.0.0.1", "::1"):
        return fwd.split(",")[0].strip()[:64]
    return host


def _origin_ok(origin: str, headers) -> bool:
    o = origin.strip().rstrip("/").lower()
    if o in ALLOWED_ORIGINS:
        return True
    netloc = urllib.parse.urlsplit(o).netloc
    hosts = {(headers.get("host") or "").lower(), (headers.get("x-forwarded-host") or "").lower()}
    return bool(netloc) and netloc in hosts


def audit(request: Request, category: str, action: str, target: str = "", ok: bool = True):
    """one Activity Logs row for the person making this request (never raises)"""
    i = getattr(request.state, "ident", None) or {"username": "", "role": "anonymous"}
    ACCESS.audit(i.get("username") or "", i["role"], category, action, target, _client_ip(request), ok)


def _audit_target(path: str, key: str) -> str:
    """the name of what a change is about, read before the change (a deleted row is gone after)"""
    if key.isdigit() and path.startswith(("/api/users/", "/api/admins/")):
        conn = db()
        try:
            r = conn.execute("SELECT username FROM users WHERE id=?", (int(key),)).fetchone()
            return r["username"] if r else f"#{key}"
        finally:
            conn.close()
    if key.isdigit() and path.startswith("/api/alerts/"):
        return f"alert #{key}"
    return urllib.parse.unquote(key)


def _no_cache(path: str, response):
    if path.startswith("/images/"):
        response.headers["Cache-Control"] = "public, max-age=604800"      # an archived image never changes
    elif path.endswith((".html", ".js", ".css")) or path == "/":
        # every device checks for a new version on each load; unchanged = a short 304 (ETag)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"


MAX_BODY = 1_000_000                                  # bytes: the largest real request (regions of a camera) is far smaller


@app.middleware("http")
async def security_guard(request: Request, call_next):
    path, method = request.url.path, request.method
    response = None
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and not _origin_ok(origin, request.headers):
            response = JSONResponse({"detail": "Cross-site request refused"}, 403)
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if response is None and ctype and ctype != "application/json":
            response = JSONResponse({"detail": "Requests must be JSON"}, 415)
        # a body is read into memory: at most MAX_BODY, and its size must be declared (no endless chunked upload)
        clen = request.headers.get("content-length")
        if response is None and "chunked" in request.headers.get("transfer-encoding", "").lower():
            response = JSONResponse({"detail": "Length required"}, 411)
        elif response is None and clen:
            try:
                if int(clen) > MAX_BODY:
                    response = JSONResponse({"detail": "Request too large"}, 413)
            except ValueError:
                response = JSONResponse({"detail": "Invalid request"}, 400)
    ident = await asyncio.to_thread(ACCESS.identity, request.cookies.get(SESSION_COOKIE))
    request.state.ident = ident
    need = ACCESS.route_permission(method, path)
    is_page = method == "GET" and (path == "/" or path.endswith(".html"))
    if response is None and not ACCESS.allowed(ident, need):
        if is_page:
            if ident["role"] == "anonymous":
                nxt = "" if path in ("/", "/index.html", "/login.html") else "?next=" + urllib.parse.quote(path.lstrip("/"))
                response = RedirectResponse(f"/login.html{nxt}", 303)
            else:                                     # signed in, not this page: the first page they may open
                response = RedirectResponse(ACCESS.first_page(ident) or "/login.html", 303)
        elif ident["role"] == "anonymous":
            response = JSONResponse({"detail": "Please sign in"}, 401)
        else:
            response = JSONResponse({"detail": "You don't have permission for this"}, 403)
    rule = None
    if response is None:
        rule = access.audit_rule(method, path) if path.startswith("/api/") and ident["role"] != "anonymous" else None
        before = await asyncio.to_thread(_audit_target, path, rule[2]) if rule else ""
        response = await call_next(request)
        if ident["role"] != "anonymous" and response.status_code < 400:
            if is_page and path in access.PAGE_LABELS and ACCESS.page_opened(ident, path):
                await asyncio.to_thread(audit, request, "pages", "Opened page", access.PAGE_LABELS[path])
            elif method == "GET" and path == "/api/detections/export":
                await asyncio.to_thread(audit, request, "reports", "Downloaded", "detections CSV")
            elif rule:
                cat, act, _ = rule
                await asyncio.to_thread(audit, request, cat, act, getattr(request.state, "audit_target", "") or before)
    _no_cache(path, response)
    for k, v in security.security_headers(_is_https(request)).items():
        response.headers.setdefault(k, v)
    return response


class TextGZip(GZipMiddleware):
    """gzip for pages / JSON / JS / CSS only: JPEGs are compressed already (gzip on them only costs CPU)"""
    async def __call__(self, scope, receive, send):
        p = scope.get("path", "") if scope["type"] == "http" else ""
        if not p or p.startswith(("/images/", "/assets/")) or p.endswith((".jpg", ".jpeg", ".png", ".ico", ".mp4")):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


app.add_middleware(TextGZip, minimum_size=1000, compresslevel=6)   # outermost: the guard's answers are compressed too


@app.exception_handler(RequestValidationError)
async def _invalid_request(request: Request, exc: RequestValidationError):
    where = [(".".join(str(x) for x in e.get("loc", ())), e.get("type")) for e in exc.errors()[:5]]
    print(f"[request] invalid request {request.method} {request.url.path}: {where}")
    return JSONResponse({"detail": "Invalid request"}, 422)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else "Request refused"
    return JSONResponse({"detail": detail}, exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(Exception)
async def _server_error(request: Request, exc: Exception):
    print(f"[error] {request.method} {request.url.path}: {type(exc).__name__}: {exc}")
    return JSONResponse({"detail": "Internal error"}, 500)


async def read_json(request: Request) -> dict:
    """Parse a JSON-object body. Anything else gets a short generic answer."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(415, "Requests must be JSON")
    try:
        return security.parse_json_object(await request.body())
    except security.BadRequest:
        raise HTTPException(422, "Invalid request")


def _str(body: dict, key: str, default: str = "", maxlen: int = 256) -> str:
    """A string field of a request body, validated for type, length and control characters."""
    v = body.get(key, default)
    if v is None:
        v = default
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str) or len(v) > maxlen or security.has_control_chars(v):
        raise HTTPException(422, "Invalid request")
    return v


# ─── /api/stats — KPIs + chart data ───────────────────────────────────────
def _utc_bounds(first_day, days: int = 1) -> tuple:
    """local calendar days [first_day, first_day + days) → (start, end) as the stored UTC ISO text: a range the
    timestamp index can use (date(timestamp,'localtime')=… cannot, and compared the UTC date with the local one)"""
    from datetime import time as dtime, timezone
    a = datetime.combine(first_day, dtime.min).astimezone(timezone.utc).replace(tzinfo=None)
    b = datetime.combine(first_day + timedelta(days=days), dtime.min).astimezone(timezone.utc).replace(tzinfo=None)
    return a.isoformat(), b.isoformat()


_SERIES = {"key": None, "at": 0.0, "data": None}       # the 30-day series of the finished days (they do not change)


def _past_series(conn, today) -> dict:
    """detections / alerts per local day for the 29 days before today — computed at most every 15 min"""
    if _SERIES["key"] == today and time.time() - _SERIES["at"] < 900:
        return _SERIES["data"]
    lo, hi = _utc_bounds(today - timedelta(days=29), 29)
    det, alr = {}, {}
    for r in conn.execute("SELECT date(timestamp,'localtime') d, COUNT(*) c FROM detections WHERE timestamp>=? AND timestamp<? GROUP BY d", (lo, hi)):
        det[r["d"]] = r["c"]
    for r in conn.execute("SELECT date(triggered_at,'localtime') d, SUM(kind='breach') b, SUM(kind!='breach') w, COUNT(*) c FROM alerts "
                          "WHERE triggered_at>=? AND triggered_at<? GROUP BY d", (lo, hi)):
        alr[r["d"]] = (r["b"] or 0, r["w"] or 0, r["c"])
    _SERIES.update(key=today, at=time.time(), data={"det": det, "alr": alr})
    return _SERIES["data"]


def _stats_reset():
    _SERIES["key"] = None


@app.get("/api/stats")
def api_stats():
    """the top bar of every page + the Live page + the Logs charts: polled every few seconds, so today is read in ONE
    pass over today's rows (index range) and the finished days come from a cache"""
    today = datetime.now().date()
    lo, hi = _utc_bounds(today)
    conn = db()
    try:
        by_badge, by_camera, hourly, det_by_zone, total_today = {}, {}, [0] * 24, {}, 0
        for r in conn.execute("SELECT badge, camera, CAST(strftime('%H', timestamp, 'localtime') AS INT) h, COUNT(*) c FROM detections "
                              "WHERE timestamp>=? AND timestamp<? GROUP BY badge, camera, h", (lo, hi)):
            c = r["c"]; total_today += c
            by_badge[r["badge"]] = by_badge.get(r["badge"], 0) + c
            by_camera[r["camera"]] = by_camera.get(r["camera"], 0) + c
            if r["h"] is not None and 0 <= r["h"] < 24:
                hourly[r["h"]] += c
        for b, c in by_badge.items():
            z = config.zone_for(b)
            det_by_zone[z] = det_by_zone.get(z, 0) + c
        for z in (config.ZONE_BREACH, config.ZONE_OUTSIDE, config.ZONE_INSIDE, config.ZONE_UNKNOWN):
            det_by_zone.setdefault(z, 0)
        a = conn.execute("SELECT SUM(kind='breach'), SUM(kind!='breach') FROM alerts WHERE triggered_at>=? AND triggered_at<?", (lo, hi)).fetchone()
        breaches_today, warnings_today = a[0] or 0, a[1] or 0

        zone_counts = {config.ZONE_BREACH: 0, config.ZONE_OUTSIDE: 0, config.ZONE_INSIDE: 0, config.ZONE_UNKNOWN: 0}
        active = 0
        for r in conn.execute("SELECT badge, COUNT(*) c FROM sessions WHERE closed=0 GROUP BY badge"):
            z = config.zone_for(r["badge"]); active += r["c"]
            zone_counts[z] = zone_counts.get(z, 0) + r["c"]
        watching = zone_counts.get(config.ZONE_OUTSIDE, 0)

        past = _past_series(conn, today)
        days = [(today - timedelta(days=29 - i)).isoformat() for i in range(30)]
        daily = [past["det"].get(d, 0) for d in days[:-1]] + [total_today]
        breach_30d = [past["alr"].get(d, (0, 0, 0))[0] for d in days[:-1]] + [breaches_today]
        warn_30d = [past["alr"].get(d, (0, 0, 0))[1] for d in days[:-1]] + [warnings_today]
        alerts_30d = [past["alr"].get(d, (0, 0, 0))[2] for d in days[:-1]] + [breaches_today + warnings_today]
        return {
            "total_today": total_today,
            "active_sessions": active,
            "breaches_today": breaches_today,
            "warnings_today": warnings_today,
            "alerts_today": breaches_today + warnings_today,
            "watching_outside": watching,
            "zone_sessions": zone_counts,
            "zone_detections_today": det_by_zone,
            "breach_30d": breach_30d,
            "warning_30d": warn_30d,
            "by_badge": sorted(([b, c] for b, c in by_badge.items()), key=lambda x: -x[1])[:7],
            "by_camera": sorted(([b, c] for b, c in by_camera.items()), key=lambda x: -x[1]),
            "hourly_today": hourly,
            "daily_30d": daily,
            "loitering_30d": alerts_30d,
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
    try:                                              # + the labels of the DeepStream model in use (whitelist by label)
        if ds_control.available():
            m = ds_control.model_by_name(ds_control.load_config().get("model") or "")
            classes |= set((m or {}).get("labels") or [])
    except Exception as e:
        print(f"[badges] model labels not read: {e!r}")
    # Also include any badges that showed up in detections but aren't in classes.txt
    conn = db()
    try:
        seen = {r[0] for r in conn.execute("SELECT DISTINCT badge FROM detections").fetchall()}
        whitelisted = {r[0].lower() for r in conn.execute("SELECT badge FROM whitelist").fetchall()}
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
                "whitelisted": b.lower() in whitelisted,
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
        """, (badge, max(1, min(limit, 1000)))).fetchall()
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
        """, (badge, max(1, min(limit, 1000)))).fetchall()
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
                   CASE WHEN LOWER(s.badge) IN (SELECT LOWER(badge) FROM whitelist) THEN 1 ELSE 0 END AS whitelisted
            FROM sessions s
            {where_sql}
            ORDER BY s.first_seen DESC
            LIMIT ?
        """, [*params, limit]).fetchall()

        visits = []
        for r in rows:
            first = datetime.fromisoformat(r["first_seen"])
            last  = datetime.fromisoformat(r["last_seen"])
            duration = (last - first).total_seconds()
            cams = json.loads(r["cameras_json"] or "{}")
            visits.append({
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
                   CASE WHEN LOWER(s.badge) IN (SELECT LOWER(badge) FROM whitelist) THEN 1 ELSE 0 END AS whitelisted,
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
# SQL expression that mirrors config.normalize_badge() so zone filtering in
# the database matches the Python zone mapping exactly.
_NORM_SQL = "REPLACE(LOWER(REPLACE(badge,'_','-')),'boundary','boundry')"


def _zone_predicate(zone: str):
    """Return (sql_fragment, params) restricting rows to one perimeter zone."""
    named = (sorted(config.BREACH_BADGES) + sorted(config.DWELL_RULES)
             + sorted(config.INSIDE_BADGES))
    if zone == config.ZONE_BREACH:
        members = sorted(config.BREACH_BADGES)
    elif zone == config.ZONE_OUTSIDE:
        # Named outside classes OR a counted one like "outside-person-3-object-2"
        members = sorted(config.DWELL_RULES)
        holes = ",".join("?" * len(members)) if members else "NULL"
        return (f"({_NORM_SQL} IN ({holes}) OR {_NORM_SQL} LIKE ?)",
                members + [config.OUTSIDE_COUNT_LIKE])
    elif zone == config.ZONE_INSIDE:
        members = sorted(config.INSIDE_BADGES)
    elif zone == config.ZONE_UNKNOWN:
        if not named:
            return f"{_NORM_SQL} NOT LIKE ?", [config.OUTSIDE_COUNT_LIKE]
        holes = ",".join("?" * len(named))
        return (f"({_NORM_SQL} NOT IN ({holes}) AND {_NORM_SQL} NOT LIKE ?)",
                named + [config.OUTSIDE_COUNT_LIKE])
    else:
        return None, []

    if not members:
        return "1=0", []          # zone configured with no classes -> no rows
    holes = ",".join("?" * len(members))
    return f"{_NORM_SQL} IN ({holes})", members


def _detections_query(date: Optional[str], month: Optional[str],
                      badge: Optional[str], camera: Optional[str],
                      search: Optional[str], zone: Optional[str] = None,
                      all_time: bool = False):
    """Build the WHERE clause. Defaults to today unless a date/month is given
    or all_time is set (the Logs page browses the full history)."""
    where, params = [], []
    try:                                               # local days → UTC ranges (the timestamp index is used)
        if date:
            lo, hi = _utc_bounds(datetime.strptime(date, "%Y-%m-%d").date())
        elif month:
            m0 = datetime.strptime(month + "-01", "%Y-%m-%d").date()
            m1 = (m0 + timedelta(days=32)).replace(day=1)
            lo, hi = _utc_bounds(m0, (m1 - m0).days)
        elif not all_time:
            lo, hi = _utc_bounds(datetime.now().date())
    except ValueError:
        raise HTTPException(400, "Dates are YYYY-MM-DD, months YYYY-MM")
    if date or month or not all_time:
        where.append("timestamp>=? AND timestamp<?"); params += [lo, hi]
    if badge:
        where.append("badge=?"); params.append(badge)
    if camera:
        where.append("camera=?"); params.append(camera)
    if zone:
        zsql, zparams = _zone_predicate(zone)
        if zsql:
            where.append(zsql); params += zparams
    if search:
        where.append("(badge LIKE ? OR camera LIKE ? OR strftime('%H:%M:%S', timestamp,'localtime') LIKE ?)")
        s = f"%{search}%"; params += [s, s, s]
    # all_time with no other filter leaves no clauses at all — keep the SQL valid
    return (" AND ".join(where) if where else "1=1"), params


@app.get("/api/detections")
def api_detections(date: Optional[str] = None, month: Optional[str] = None,
                   badge: Optional[str] = None, camera: Optional[str] = None,
                   search: Optional[str] = None, zone: Optional[str] = None,
                   all_time: bool = False,
                   limit: int = 200, offset: int = 0):
    where, params = _detections_query(date, month, badge, camera, search, zone, all_time)
    # Qualify columns: the WHERE clause refers to detections' own badge/camera/
    # timestamp, so the session join must not make those names ambiguous.
    where_q = where
    sql = (
        "SELECT d.id, d.filename, d.camera, d.badge, d.timestamp, d.type, d.session_id, "
        "       s.first_seen AS s_first, s.last_seen AS s_last, s.closed AS s_closed, "
        "       s.closed_at AS s_closed_at "
        "FROM (SELECT * FROM detections WHERE " + where_q + ") d "
        "LEFT JOIN sessions s ON s.id = d.session_id "
        "ORDER BY d.timestamp DESC LIMIT ? OFFSET ?"
    )
    conn = db()
    try:
        rows = conn.execute(sql, [*params, max(1, min(limit, 5000)), max(0, offset)]).fetchall()
        archive = Path(config.ARCHIVE_DIR)
        return [{
            "id": r["id"],
            "filename": r["filename"],
            "camera": r["camera"],
            "badge": r["badge"],
            "timestamp": r["timestamp"],
            "type": r["type"],
            "session_id": r["session_id"],
            "zone": config.zone_for(r["badge"]),
            "zone_label": config.zone_label(r["badge"]),
            "counts": config.parse_counts(r["badge"]),
            # Continuous-presence span of the session behind this detection,
            # so the UI can show real dwell instead of time-since-first-ever.
            "session_first_seen": r["s_first"],
            "session_last_seen": r["s_last"],
            "session_closed": bool(r["s_closed"]) if r["s_closed"] is not None else None,
            "session_closed_at": r["s_closed_at"],
            "dwell_seconds": (
                int(_presence_seconds(r["badge"], r["s_first"], r["s_last"], datetime.utcnow(),
                                      r["s_closed_at"] if r["s_closed"] else None))
                if r["s_first"] and r["s_last"] else None
            ),
            "has_image": (archive / r["filename"]).exists(),
        } for r in rows]
    finally:
        conn.close()


# ─── /api/detections/summary — totals for the CURRENT filter set ─────────
# The Logs table is paged (20 rows), so its charts must not be derived from
# the visible page. This returns aggregates over every matching row.
_SUMMARY = {}                                         # (filter) → (time, answer): a short cache


@app.get("/api/detections/summary")
def api_detections_summary(date: Optional[str] = None, month: Optional[str] = None,
                           badge: Optional[str] = None, camera: Optional[str] = None,
                           search: Optional[str] = None, zone: Optional[str] = None,
                           all_time: bool = False):
    where, params = _detections_query(date, month, badge, camera, search, zone, all_time)
    key = (where, tuple(params))
    hit = _SUMMARY.get(key)
    if hit and time.time() - hit[0] < (60 if all_time or month else 8):   # the Logs page asks every 10 s
        return hit[1]
    conn = db()
    try:
        bb, bc, total = {}, {}, 0                      # ONE pass: badge × camera counts
        for r in conn.execute(f"SELECT badge, camera, COUNT(*) c FROM detections WHERE {where} GROUP BY badge, camera", params):
            total += r["c"]
            bb[r["badge"]] = bb.get(r["badge"], 0) + r["c"]
            bc[r["camera"]] = bc.get(r["camera"], 0) + r["c"]
    finally:
        conn.close()
    zone_counts = {}
    for b, c in bb.items():
        z = config.zone_for(b)
        zone_counts[z] = zone_counts.get(z, 0) + c
    top_b = sorted(bb.items(), key=lambda x: -x[1])[:12]
    out = {
        "total": total,
        "by_badge": [[b, c] for b, c in top_b],
        "by_camera": [[k, c] for k, c in sorted(bc.items(), key=lambda x: -x[1])[:12]],
        "by_zone": zone_counts,
        "badge_zones": {b: config.zone_for(b) for b, _ in top_b},
    }
    if len(_SUMMARY) > 50:
        _SUMMARY.clear()
    _SUMMARY[key] = (time.time(), out)
    return out


@app.get("/api/detections/export")
def api_detections_export(request: Request, date: Optional[str] = None, month: Optional[str] = None,
                          badge: Optional[str] = None, camera: Optional[str] = None,
                          search: Optional[str] = None, zone: Optional[str] = None,
                          all_time: bool = False):
    where, params = _detections_query(date, month, badge, camera, search, zone, all_time)
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
    writer.writerow(["id", "timestamp", "detection", "zone", "camera", "type", "filename"])
    for r in rows:
        writer.writerow([r["id"], r["timestamp"], r["badge"],
                         config.zone_label(r["badge"]),
                         r["camera"], r["type"], r["filename"]])
    buf.seek(0)
    fname = f"trinetra-detections-{datetime.now():%Y%m%d-%H%M%S}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )


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


_BADGE_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


@app.post("/api/whitelist")
async def api_whitelist_add(request: Request):
    body = await read_json(request)
    badge = _str(body, "badge", maxlen=64).strip()           # as the class / label is spelt; matched without case
    reason = _str(body, "reason", maxlen=200).strip()
    if not badge:
        raise HTTPException(400, "badge required")
    if not _BADGE_RE.match(badge):
        raise HTTPException(400, "Badge may only contain letters, numbers and hyphens")
    conn = db()
    try:
        with conn:
            conn.execute("DELETE FROM whitelist WHERE badge=? COLLATE NOCASE", (badge,))
            conn.execute("INSERT INTO whitelist(badge, reason) VALUES (?,?)", (badge, reason))
    finally:
        conn.close()
    audit(request, "whitelist", "Added to whitelist", f"{badge}: {reason}"[:200])
    return {"ok": True}


@app.delete("/api/whitelist/{badge}")
def api_whitelist_remove(badge: str, request: Request):
    conn = db()
    try:
        with conn:
            cur = conn.execute("DELETE FROM whitelist WHERE badge=? COLLATE NOCASE", (badge[:64],))
    finally:
        conn.close()
    if not cur.rowcount:
        raise HTTPException(404, "Not on the whitelist")
    return {"ok": True}


def _rule_dwell_minutes() -> dict:
    """the shortest enabled dwell rule that covers each image class (the Live page times presences against it)"""
    out = {}
    conn = db()
    try:
        rules = [alert_rules.row_out(r) for r in conn.execute("SELECT * FROM alert_rules WHERE enabled=1 AND type='dwell'")]
    finally:
        conn.close()
    for r in rules:
        kinds = ("person", "object") if r["target"] == "any" else (r["target"],)
        sides = {"*outside": ("outside",), "*alert": (), "*any": ("outside", "zone")}.get(r["region"], ("zone",))
        for side in sides:
            for k in kinds:
                c = f"{side}-{k}"
                out[c] = min(out.get(c, r["minutes"]), r["minutes"])
    return dict(sorted(out.items()))


def _instant_rule_names() -> list:
    conn = db()
    try:
        return [r["name"] for r in conn.execute("SELECT name FROM alert_rules WHERE enabled=1 AND type='presence' "
                                                "AND region IN ('*alert','*any') ORDER BY id")]
    finally:
        conn.close()


# ─── /api/perimeter/rules — zone config, so the UI never hardcodes it ─────
@app.get("/api/perimeter/rules")
def api_perimeter_rules():
    return {
        "breach_badges": sorted(config.BREACH_BADGES),
        "outside_badges": sorted(config.DWELL_RULES),
        "inside_badges": sorted(config.INSIDE_BADGES),
        "dwell_rules": _rule_dwell_minutes(),            # class → minutes, from the client's enabled dwell rules
        "instant_rules": _instant_rule_names(),          # enabled presence rules on the alert regions
        "default_dwell_minutes": None,
        "breach_cooldown_seconds": config.BREACH_COOLDOWN_SECONDS,
        "perimeter_gap_minutes": config.PERIMETER_GAP_MINUTES,
        "zone_labels": config.ZONE_LABELS,
    }


# ─── /api/alerts/recent — perimeter alert log (breach + early warning) ────
@app.get("/api/alerts/recent")
def api_alerts_recent(limit: int = 50, kind: Optional[str] = None,
                      today: bool = False, status: Optional[str] = None):
    """status=open → every UNACKNOWLEDGED alert, whatever day it fired on.
    The alert bar uses this: an alert must never drop off it without an Ack,
    including across midnight."""
    conn = db()
    try:
        sql = ("SELECT a.id, a.badge, a.triggered_at, a.duration_seconds, "
               "a.kind, a.camera, a.zone, a.image, a.acknowledged, a.rule_id, a.rule_name, a.severity, a.message, "
               "a.object_count, a.sound, a.presence_start, "
               "a.acknowledged_at, a.acknowledged_by, a.session_id, "
               "s.last_camera AS session_camera, s.first_seen AS s_first, "
               "s.closed AS s_closed, s.closed_at AS s_closed_at "
               "FROM alerts a LEFT JOIN sessions s ON s.id = a.session_id ")
        params = []
        clauses = []
        if kind in ("breach", "dwell", "presence", "count", "offline"):
            clauses.append("COALESCE(a.kind,'dwell')=?")
            params.append(kind)
        if today:
            clauses.append("a.triggered_at>=? AND a.triggered_at<?")
            params += list(_utc_bounds(datetime.now().date()))
        if status == "open":
            clauses.append("a.acknowledged=0")
        elif status == "acked":
            clauses.append("a.acknowledged=1")
        if clauses:
            sql += "WHERE " + " AND ".join(clauses) + " "
        sql += "ORDER BY a.triggered_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))

        rows = conn.execute(sql, params).fetchall()
        archive = Path(config.ARCHIVE_DIR)
        out = []
        for r in rows:
            image = r["image"]
            if not image and r["session_id"]:
                img_row = conn.execute(
                    "SELECT filename FROM detections WHERE session_id=? "
                    "ORDER BY timestamp DESC LIMIT 1", (r["session_id"],)
                ).fetchone()
                image = img_row["filename"] if img_row else None
            kind_val = r["kind"] or "dwell"
            zone_val = r["zone"] or config.zone_for(r["badge"])
            sev = r["severity"] or ("critical" if kind_val == "breach" else "warning")
            out.append({
                "rule_id": r["rule_id"], "rule_name": r["rule_name"] or ("Perimeter breach" if kind_val == "breach" else "Early warning"),
                "severity": sev, "message": r["message"] or "", "object_count": r["object_count"],
                "sound": bool(r["sound"] if r["sound"] is not None else 1),
                "presence_start": r["presence_start"],
                "id": r["id"],
                "badge": r["badge"],
                "triggered_at": r["triggered_at"],
                "duration_seconds": r["duration_seconds"] or 0,
                "kind": kind_val,
                "zone": zone_val,
                "zone_label": config.ZONE_LABELS.get(zone_val, zone_val) if not r["rule_id"] else zone_val,
                "counts": config.parse_counts(r["badge"]),
                # When this presence began, for a live "present for" duration
                "session_first_seen": r["s_first"],
                "session_closed": bool(r["s_closed"]) if r["s_closed"] is not None else None,
                "session_closed_at": r["s_closed_at"],
                "camera": r["camera"] or r["session_camera"] or "—",
                "session_id": r["session_id"],
                "acked": bool(r["acknowledged"]),
                "acked_at": r["acknowledged_at"],
                "acked_by": r["acknowledged_by"],
                "image": image,
                "has_image": bool(image and (archive / image).exists()),
            })
        return out
    finally:
        conn.close()


# ─── /api/alerts/{id}/ack ─────────────────────────────────────────────────
@app.post("/api/alerts/{alert_id}/ack")
async def api_alert_ack(alert_id: int, request: Request):
    # Who acknowledged is the signed-in account — never a name the client sends.
    acked_by = request.state.ident["username"]
    now = datetime.utcnow()
    conn = db()
    try:
        if not conn.execute("SELECT 1 FROM alerts WHERE id=?", (alert_id,)).fetchone():
            raise HTTPException(404, "Alert not found")
        with conn:
            # 1. Mark the alert as acknowledged
            conn.execute(
                "UPDATE alerts SET acknowledged=1, acknowledged_at=?, acknowledged_by=? WHERE id=?",
                (now.isoformat(), acked_by, alert_id)
            )
            # 2. Acknowledging DISMISSES this one alert. It deliberately does
            #    NOT whitelist the class: perimeter classes are detection
            #    classes, not people, so muting "Boundry-object" after one ack
            #    would silence every future breach on that camera.
            #
            # 3. An early warning's presence stays open until acknowledged, so
            #    the ack is what ends it. The next detection on that camera
            #    starts a fresh presence with its own 10-minute clock.
            arow = conn.execute(
                "SELECT session_id, kind FROM alerts WHERE id=?", (alert_id,)
            ).fetchone()
            if arow and arow["session_id"] and (arow["kind"] or "dwell") != "breach":
                conn.execute(
                    "UPDATE sessions SET closed=1, closed_at=? WHERE id=? AND closed=0",
                    (now.isoformat(), arow["session_id"])
                )
    finally:
        conn.close()
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════════════
# ALERTS PAGE — alert rules (what to alert about), the alert history, the open alerts (the flashing strip on every
# page), regions for the client (drawn on the camera picture; no stream addresses)
# ════════════════════════════════════════════════════════════════════════════
def _cameras_and_regions() -> dict:
    """{camera: [{name, kind}]} of the configured cameras (their regions from the zones file)"""
    cfg = ds_control.load_config()
    zones = ds_control.load_zones()
    from pipeline import zones as Z
    return {s["name"]: [{"name": r["name"], "kind": r["kind"]} for r in Z.regions(zones.get(s["name"]) or {})
                        if r["kind"] not in ("ignore",) + Z.DETECTION_KINDS] for s in cfg["streams"]}


@app.get("/api/alert-rules")
def api_alert_rules():
    conn = db()
    try:
        rules = [alert_rules.row_out(r) for r in conn.execute("SELECT * FROM alert_rules ORDER BY id")]
        last = {r[0]: r[1] for r in conn.execute("SELECT rule_id, MAX(triggered_at) FROM alerts WHERE rule_id IS NOT NULL GROUP BY rule_id")}
    finally:
        conn.close()
    for r in rules:
        r["last_alert"] = last.get(r["id"])
    return {"rules": rules, "cameras": _cameras_and_regions(), "special_regions": alert_rules.SPECIAL_REGIONS,
            "types": list(alert_rules.RULE_TYPES), "presence_gap_seconds": config.PRESENCE_GAP_SECONDS}


async def _rule_body(request: Request) -> dict:
    try:
        return alert_rules.check(await read_json(request), set(_cameras_and_regions()))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/alert-rules")
async def api_alert_rule_create(request: Request):
    c = await _rule_body(request)
    conn = db()
    try:
        if conn.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0] >= 200:
            raise HTTPException(400, "At most 200 alert rules")
        with _db_lock, conn:
            cols = {**c, "created_by": request.state.ident.get("username")}
            cur = conn.execute(f"INSERT INTO alert_rules({', '.join(alert_rules._q(k) for k in cols)}) VALUES({', '.join('?' * len(cols))})",
                               [json.dumps(v) if isinstance(v, list) else v for v in cols.values()])
        request.state.audit_target = c["name"]
        return alert_rules.row_out(conn.execute("SELECT * FROM alert_rules WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


@app.put("/api/alert-rules/{rule_id}")
async def api_alert_rule_update(rule_id: int, request: Request):
    c = await _rule_body(request)
    conn = db()
    try:
        if not conn.execute("SELECT 1 FROM alert_rules WHERE id=?", (rule_id,)).fetchone():
            raise HTTPException(404, "Alert rule not found")
        with _db_lock, conn:
            conn.execute(f"UPDATE alert_rules SET {', '.join(alert_rules._q(k) + '=?' for k in c)}, updated_at=? WHERE id=?",
                         [json.dumps(v) if isinstance(v, list) else v for v in c.values()] + [datetime.now().isoformat(timespec="seconds"), rule_id])
        return alert_rules.row_out(conn.execute("SELECT * FROM alert_rules WHERE id=?", (rule_id,)).fetchone())
    finally:
        conn.close()


@app.delete("/api/alert-rules/{rule_id}")
def api_alert_rule_delete(rule_id: int):
    conn = db()
    try:
        with _db_lock, conn:
            if not conn.execute("DELETE FROM alert_rules WHERE id=?", (rule_id,)).rowcount:
                raise HTTPException(404, "Alert rule not found")
    finally:
        conn.close()
    return {"ok": True}


def _alert_out(r, archive: Path) -> dict:
    return {"id": r["id"], "rule_id": r["rule_id"], "rule_name": r["rule_name"] or ("Perimeter breach" if r["kind"] == "breach" else "Dwell"),
            "severity": r["severity"] or ("critical" if r["kind"] == "breach" else "warning"), "kind": r["kind"] or "dwell",
            "camera": r["camera"] or "—", "zone": r["zone"] or "", "message": r["message"] or r["badge"], "badge": r["badge"],
            "triggered_at": r["triggered_at"], "presence_start": r["presence_start"], "duration_seconds": r["duration_seconds"] or 0,
            "count": r["object_count"],
            "image": r["image"], "has_image": bool(r["image"] and (archive / r["image"]).exists()), "sound": bool(r["sound"] if r["sound"] is not None else 1),
            "acked": bool(r["acknowledged"]), "acked_at": r["acknowledged_at"], "acked_by": r["acknowledged_by"],
            "false_alarm": r["verdict"] == "false_alarm", "verdict_by": r["verdict_by"], "verdict_at": r["verdict_at"],
            "verdict_note": r["verdict_note"] or "", "learn": alert_rules.FEEDBACK_LEARN.get(r["kind"] or "dwell", "none"),
            "can_false_alarm": (r["kind"] or "dwell") not in alert_rules.NO_FALSE_ALARM_KINDS}


_STATUS_SQL = ("acknowledged=0", "acknowledged=1", "verdict='false_alarm'")   # the status filter's clauses (no arguments)


@app.get("/api/alerts")
def api_alerts(date_from: Optional[str] = None, date_to: Optional[str] = None, severity: str = "", camera: str = "",
               status: str = "", rule_id: Optional[int] = None, q: str = "", page: int = 1, size: int = 10):
    """the alert history, newest first, a page at a time (dates: local days, YYYY-MM-DD)"""
    where, args = [], []
    try:                                               # local days → UTC range on the stored text (the index is used)
        if date_from:
            where.append("triggered_at>=?"); args.append(_utc_bounds(datetime.strptime(date_from, "%Y-%m-%d").date())[0])
        if date_to:
            where.append("triggered_at<?"); args.append(_utc_bounds(datetime.strptime(date_to, "%Y-%m-%d").date())[1])
    except (ValueError, OverflowError):
        raise HTTPException(400, "Dates are YYYY-MM-DD")
    if severity in alert_rules.SEVERITIES:
        where.append("COALESCE(severity, CASE WHEN kind='breach' THEN 'critical' ELSE 'warning' END)=?"); args.append(severity)
    if camera:
        where.append("camera=?"); args.append(camera[:40])
    if status == "open":
        where.append("acknowledged=0")
    elif status == "acked":
        where.append("acknowledged=1")
    elif status == "false_alarm":
        where.append("verdict='false_alarm'")
    if rule_id:
        where.append("rule_id=?"); args.append(rule_id)
    if q.strip():
        like = "%" + q.strip()[:80].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(rule_name LIKE ? ESCAPE '\\' OR message LIKE ? ESCAPE '\\' OR camera LIKE ? ESCAPE '\\' OR zone LIKE ? ESCAPE '\\' OR badge LIKE ? ESCAPE '\\')")
        args += [like] * 5
    w = (" WHERE " + " AND ".join(where)) if where else ""
    size, page = max(1, min(size, 100)), max(1, page)
    conn = db()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM alerts{w}", args).fetchone()[0]
        fw = " WHERE " + " AND ".join([c for c in where if c not in _STATUS_SQL] + ["verdict='false_alarm'"])
        fargs = args
        false_alarms = conn.execute(f"SELECT COUNT(*) FROM alerts{fw}", fargs).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM alerts{w} ORDER BY triggered_at DESC, id DESC LIMIT ? OFFSET ?",
                            args + [size, (page - 1) * size]).fetchall()
        cams = [r[0] for r in conn.execute("SELECT DISTINCT camera FROM alerts WHERE camera IS NOT NULL ORDER BY camera")]
    finally:
        conn.close()
    archive = Path(config.ARCHIVE_DIR)
    return {"items": [_alert_out(r, archive) for r in rows], "total": total, "page": page, "size": size, "cameras": cams,
            "false_alarms": false_alarms}


@app.get("/api/alerts/live")
def api_alerts_live():
    """the open (unacknowledged) alerts for the flashing strip on every page: how many, the newest few"""
    conn = db()
    try:
        n = conn.execute("SELECT COUNT(*), SUM(COALESCE(severity, CASE WHEN kind='breach' THEN 'critical' ELSE 'warning' END)='critical') "
                         "FROM alerts WHERE acknowledged=0").fetchone()
        rows = conn.execute("SELECT * FROM alerts WHERE acknowledged=0 ORDER BY id DESC LIMIT 5").fetchall()
    finally:
        conn.close()
    archive = Path(config.ARCHIVE_DIR)
    return {"open": n[0] or 0, "critical": n[1] or 0, "latest": [_alert_out(r, archive) for r in rows]}


@app.post("/api/alerts/ack-all")
async def api_alerts_ack_all(request: Request):
    conn = db()
    try:
        with _db_lock, conn:
            n = conn.execute("UPDATE alerts SET acknowledged=1, acknowledged_at=?, acknowledged_by=? WHERE acknowledged=0",
                             (datetime.utcnow().isoformat(), request.state.ident["username"])).rowcount
    finally:
        conn.close()
    request.state.audit_target = f"{n} alert(s)"
    return {"ok": True, "acknowledged": n}


# ─── "False alarm" (client admins + the Trinetra team): the alert is marked, acknowledged if still open, and handed to
# the detection workers through data/feedback/feedback.jsonl (what each detection learns: alert_rules.FEEDBACK_LEARN).
FEEDBACK_FILE = Path(config.DATA_DIR) / "feedback" / "feedback.jsonl"


def _feedback_line(row, verdict: str, ident: dict, note: str):
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    kind = row["kind"] or "dwell"
    line = {"ts": datetime.now().isoformat(timespec="seconds"), "alert_id": row["id"], "verdict": verdict,
            "by": ident.get("username"), "role": ident.get("role"), "kind": kind, "rule": row["rule_name"],
            "camera": row["camera"], "zone": row["zone"], "triggered_at": row["triggered_at"], "image": row["image"],
            "note": note, "learn": alert_rules.FEEDBACK_LEARN.get(kind, "none")}
    with open(FEEDBACK_FILE, "a") as f:
        f.write(json.dumps(line) + "\n")


@app.post("/api/alerts/{alert_id}/false-alarm")
async def api_alert_false_alarm(alert_id: int, request: Request):
    body = await read_json(request) if int(request.headers.get("content-length") or 0) else {}
    note = str((body or {}).get("note") or "").strip()[:200]
    ident = request.state.ident
    now = datetime.utcnow().isoformat()
    conn = db()
    try:
        row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Alert not found")
        if (row["kind"] or "dwell") in alert_rules.NO_FALSE_ALARM_KINDS:
            raise HTTPException(400, "A camera-offline alert cannot be a false alarm")
        if row["verdict"] == "false_alarm":
            raise HTTPException(409, "Already marked as a false alarm")
        with _db_lock, conn:
            conn.execute("UPDATE alerts SET verdict='false_alarm', verdict_by=?, verdict_at=?, verdict_note=? WHERE id=?",
                         (ident["username"], now, note, alert_id))
            if not row["acknowledged"]:              # a false alarm needs no one to act on it: off the open alerts
                conn.execute("UPDATE alerts SET acknowledged=1, acknowledged_at=?, acknowledged_by=? WHERE id=?",
                             (now, ident["username"], alert_id))
                if row["session_id"] and (row["kind"] or "dwell") != "breach":
                    conn.execute("UPDATE sessions SET closed=1, closed_at=? WHERE id=? AND closed=0", (now, row["session_id"]))
        _feedback_line(row, "false_alarm", ident, note)
    finally:
        conn.close()
    request.state.audit_target = f"#{alert_id} {row['rule_name'] or row['kind'] or ''} · {row['camera'] or ''}".strip()
    return {"ok": True, "learn": alert_rules.FEEDBACK_LEARN.get(row["kind"] or "dwell", "none")}


@app.delete("/api/alerts/{alert_id}/false-alarm")
def api_alert_false_alarm_undo(alert_id: int, request: Request):
    conn = db()
    try:
        row = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Alert not found")
        if row["verdict"] != "false_alarm":
            raise HTTPException(400, "This alert is not marked as a false alarm")
        with _db_lock, conn:                          # stays acknowledged: the undo only takes the lesson back
            conn.execute("UPDATE alerts SET verdict=NULL, verdict_by=NULL, verdict_at=NULL, verdict_note=NULL WHERE id=?", (alert_id,))
        _feedback_line(row, "undo", request.state.ident, "")
    finally:
        conn.close()
    request.state.audit_target = f"#{alert_id} {row['rule_name'] or row['kind'] or ''} · {row['camera'] or ''}".strip()
    return {"ok": True}


# ─── regions for the client (Alerts page → Regions): the cameras' pictures + zones, never the stream addresses ───
@app.get("/api/regions")
def api_regions():
    return {"cameras": [{"name": s["name"], "enabled": s["enabled"]} for s in ds_control.load_config()["streams"]]}


@app.get("/api/regions/{cam}/zone")
def api_region_zone(cam: str):
    return ds_control.api_zone(cam)


@app.put("/api/regions/{cam}/zone")
async def api_region_zone_save(cam: str, request: Request):
    request.state.audit_target = cam
    return await ds_control.api_zone_save(cam, request, team=False)   # the team's near / band / landing regions stay


@app.get("/api/regions/{cam}/frame")
async def api_region_frame(cam: str, refresh: int = 0):
    return await ds_control.api_frame(cam, refresh)


# ─── the Bluetooth speaker (Alerts page → Speaker; the host agent does the Bluetooth, see speaker.py) ───
def _speaker_out() -> dict:
    return {"config": speaker.load(), "status": speaker.status(), "repeats": list(speaker.REPEATS)}


@app.get("/api/speaker")
def api_speaker():
    return _speaker_out()


@app.put("/api/speaker")
async def api_speaker_save(request: Request):
    try:
        cfg = speaker.check(await read_json(request), speaker.load())
    except ValueError as e:
        raise HTTPException(400, str(e))
    if cfg["enabled"] and not cfg["device"]:
        raise HTTPException(400, "Connect a speaker first (Find speakers)")
    speaker.save(cfg)
    request.state.audit_target = f"{'on' if cfg['enabled'] else 'off'} · {cfg['severities']} · repeat {cfg['repeat_s']} s"
    return _speaker_out()


@app.post("/api/speaker/test")
async def api_speaker_test(request: Request):
    body = await read_json(request)
    cfg = speaker.load()
    if not cfg["device"]:
        raise HTTPException(400, "Connect a speaker first (Find speakers)")
    if not speaker.request("test", severity="critical" if body.get("severity") == "critical" else "warning"):
        raise HTTPException(503, "The speaker service on the Jetson is not answering")
    return _speaker_out()


@app.post("/api/speaker/scan")
async def api_speaker_scan(request: Request):
    await read_json(request)
    if not speaker.request("scan"):
        raise HTTPException(503, "The speaker service on the Jetson is not answering")
    return _speaker_out()


@app.post("/api/speaker/connect")
async def api_speaker_connect(request: Request):
    body = await read_json(request)
    try:
        mac = speaker.clean_mac(body.get("mac"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    name = re.sub(r"[^\w .()&+'-]", "", str(body.get("name") or ""))[:60]
    cfg = speaker.load()
    old = cfg["device"]
    cfg.update(device=mac, device_name=name)
    speaker.save(cfg)
    if old and old != mac:
        speaker.request("forget", mac=old)
    if not speaker.request("connect", mac=mac):
        raise HTTPException(503, "The speaker service on the Jetson is not answering")
    request.state.audit_target = f"{name or 'speaker'} ({mac})"
    return _speaker_out()


@app.post("/api/speaker/forget")
async def api_speaker_forget(request: Request):
    await read_json(request)
    cfg = speaker.load()
    if cfg["device"]:
        speaker.request("forget", mac=cfg["device"])
        request.state.audit_target = f"{cfg['device_name'] or 'speaker'} ({cfg['device']})"
    cfg.update(device="", device_name="", enabled=False)
    speaker.save(cfg)
    return _speaker_out()


# ─── /images/{filename} — serve archived images ───────────────────────────
@app.get("/images/{filename}")
def get_image(filename: str):
    safe = Path(filename).name  # strip any path separators
    p = Path(config.ARCHIVE_DIR) / safe
    if p.is_file():
        return FileResponse(p)
    # never another image in its place: an alert's snapshot is evidence (deleted by retention → "no image")
    raise HTTPException(404, "no image available")

# ════════════════════════════════════════════════════════════════════════════
# SIGN-IN — the Trinetra team, admin accounts (made by the team), users (made by admins)
# ════════════════════════════════════════════════════════════════════════════

def _set_session_cookie(resp, request: Request, token: str, max_age: int):
    resp.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="lax",
                    secure=_is_https(request), path="/")


@app.post("/api/login")
async def api_login(request: Request):
    body = await read_json(request)
    username = _str(body, "username", maxlen=64).strip()
    password = body.get("password")
    if not isinstance(password, str) or len(password) > security.PASSWORD_MAX:
        password = ""
    ip = _client_ip(request)
    if not username or _limiter_locked(username, ip):
        if username:
            ACCESS.audit(username, "anonymous", "sign-in", "Sign-in refused (locked: too many wrong passwords)", "", ip, False)
        raise HTTPException(429 if username else 401,
                            "Too many failed sign-ins — try again in 15 minutes" if username else "Wrong user ID or password")
    role, user_id, keep_pw, name = None, None, False, username
    team = ACCESS.team_member(username)
    if team and config.SUPPORT_PASSWORD and hmac.compare_digest(password.encode(), config.SUPPORT_PASSWORD.encode()):
        role, name = "support", team
    elif not team:
        conn = db()
        try:
            u = conn.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
        finally:
            conn.close()
        ok = await asyncio.to_thread(security.verify_password, password, u["password_hash"] if u else None)
        if u and ok:
            role, user_id, name = u["role"], u["id"], u["username"]
            # admin accounts: the Trinetra team sees the password in use (changed by the admin since)
            keep_pw = u["role"] == "admin" and ACCESS.unseal(u["password_enc"]) != password
    if not role:
        locked_now = ACCESS.limiter.fail(username.lower(), ip)
        ACCESS.audit(username, "anonymous", "sign-in", "Sign-in refused (wrong user ID or password)", "", ip, False)
        if locked_now:
            ACCESS.audit(username, "anonymous", "sign-in", "Sign-in locked for 15 minutes (too many wrong passwords)", "", ip, False)
        await asyncio.sleep(0.6)                      # slow down password guessing
        raise HTTPException(401, "Wrong user ID or password")
    ACCESS.limiter.success(username.lower())
    ACCESS.delete_session(request.cookies.get(SESSION_COOKIE))   # no session fixation
    token, max_age = await asyncio.to_thread(ACCESS.create_session, role, user_id, name, ip)
    if keep_pw:
        conn = db()
        try:
            with _db_lock, conn:
                conn.execute("UPDATE users SET password_enc=? WHERE id=?", (ACCESS.seal(password), user_id))
        finally:
            conn.close()
    ACCESS.audit(name, role, "sign-in", "Signed in", "", ip)
    resp = JSONResponse({"ok": True, "role": role, "session_hours": max_age // 3600})
    _set_session_cookie(resp, request, token, max_age)
    return resp


def _limiter_locked(username: str, ip: str) -> bool:
    return ACCESS.limiter.locked(username.lower(), ip)


@app.post("/api/logout")
async def api_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token and request.state.ident["role"] != "anonymous":
        audit(request, "sign-in", "Signed out")
    ACCESS.delete_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/me")
def api_me(request: Request):
    ident = request.state.ident
    return {**{k: ident.get(k) for k in ("role", "name", "username", "permissions")},
            "signed_in": ident["role"] != "anonymous",
            "expired": bool(ident.get("expired")),
            "own_password": bool(ident.get("user_id")),   # the team's password is in the settings file
            "support_enabled": bool(config.SUPPORT_PASSWORD),
            "first_page": ACCESS.first_page(ident) if ident["role"] != "anonymous" else None,
            "idle_minutes": config.IDLE_MINUTES,
            "all_permissions": [{"key": k, "label": l, "help": h} for k, l, h in access.PERMISSIONS]}


@app.post("/api/me/password")
async def api_change_own_password(request: Request):
    """An admin account or user changes their own password (the current one is needed). The Trinetra team's
    password is in the settings file."""
    ident = request.state.ident
    if not ident.get("user_id"):
        raise HTTPException(400, "This sign-in is set in the server's settings file, not here")
    body = await read_json(request)
    current, new = body.get("current"), body.get("new")
    if not isinstance(current, str) or not isinstance(new, str):
        raise HTTPException(422, "Invalid request")
    conn = db()
    try:
        u = conn.execute("SELECT * FROM users WHERE id=?", (ident["user_id"],)).fetchone()
        if not u or not await asyncio.to_thread(security.verify_password, current, u["password_hash"]):
            audit(request, "account", "Password change refused (wrong current password)", "", False)
            await asyncio.sleep(0.6)
            raise HTTPException(400, "The current password is wrong")
        if new == current:
            raise HTTPException(400, "The new password is the same as the current one")
        problem = security.password_problem(new, u["username"])
        if problem:
            raise HTTPException(400, problem)
        with _db_lock, conn:
            conn.execute("UPDATE users SET password_hash=?, password_enc=? WHERE id=?",
                         (security.hash_password(new), ACCESS.seal(new) if u["role"] == "admin" else None, u["id"]))
            ACCESS.delete_user_sessions(conn, u["id"], keep_token=request.cookies.get(SESSION_COOKIE))   # other devices out
    finally:
        conn.close()
    audit(request, "account", "Changed own password")
    return {"ok": True}


# ─── Users (admins) ──────────────────────────────────────────────────────────
def _user_out(r) -> dict:
    return {"id": r["id"], "username": r["username"], "name": r["name"], "active": bool(r["active"]),
            "permissions": [p for p in json.loads(r["permissions"] or "[]") if p in access.PERMISSION_KEYS],
            "created_at": r["created_at"], "created_by": r["created_by"], "last_login": r["last_login"]}


def _check_username(username: str):
    if not access.USERNAME_RE.match(username):
        raise HTTPException(400, "User ID must be 3–32 letters, numbers, dots, dashes or underscores")
    if username.lower() in access.RESERVED_IDS or ACCESS.team_member(username):
        raise HTTPException(400, "That user ID is reserved")


def _user_fields(body: dict, creating: bool) -> dict:
    out = {"name": _str(body, "name", maxlen=60).strip()}
    if creating:
        out["username"] = _str(body, "username", maxlen=32).strip()
        _check_username(out["username"])
    pw = body.get("password")
    if pw not in (None, ""):
        if not isinstance(pw, str):
            raise HTTPException(422, "Invalid request")
        problem = security.password_problem(pw, out.get("username") or body.get("username") or "")
        if problem:
            raise HTTPException(400, problem)
        out["password"] = pw
    perms = body.get("permissions", [])
    if not isinstance(perms, list) or any(p not in access.PERMISSION_KEYS for p in perms):
        raise HTTPException(400, "Unknown permission")
    out["permissions"] = list(dict.fromkeys(perms))
    act = body.get("active", True)
    if not isinstance(act, bool):
        raise HTTPException(422, "Invalid request")
    out["active"] = act
    return out


@app.get("/api/users")
def api_users():
    conn = db()
    try:
        return [_user_out(r) for r in conn.execute("SELECT * FROM users WHERE role='user' ORDER BY username COLLATE NOCASE")]
    finally:
        conn.close()


@app.post("/api/users")
async def api_create_user(request: Request):
    f = _user_fields(await read_json(request), creating=True)
    if "password" not in f:
        raise HTTPException(400, "Password is required")
    conn = db()
    try:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (f["username"],)).fetchone():
            raise HTTPException(400, f"User ID {f['username']} already exists")
        with _db_lock, conn:
            cur = conn.execute("INSERT INTO users(username,name,password_hash,permissions,active,role,created_by) "
                               "VALUES(?,?,?,?,?,'user',?)",
                               (f["username"], f["name"], security.hash_password(f["password"]), json.dumps(f["permissions"]),
                                int(f["active"]), request.state.ident.get("username") or None))
        request.state.audit_target = f["username"]
        return _user_out(conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


@app.put("/api/users/{user_id}")
async def api_update_user(user_id: int, request: Request):
    body = await read_json(request)
    conn = db()
    try:
        u = conn.execute("SELECT * FROM users WHERE id=? AND role='user'", (user_id,)).fetchone()
        if not u:
            raise HTTPException(404, "User not found")
        f = _user_fields({**body, "username": u["username"]}, creating=False)
        with _db_lock, conn:
            conn.execute("UPDATE users SET name=?, permissions=?, active=? WHERE id=?",
                         (f["name"], json.dumps(f["permissions"]), int(f["active"]), user_id))
            if "password" in f:
                conn.execute("UPDATE users SET password_hash=? WHERE id=?", (security.hash_password(f["password"]), user_id))
            if "password" in f or not f["active"]:
                ACCESS.delete_user_sessions(conn, user_id)          # signed out everywhere
        return _user_out(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
    finally:
        conn.close()


@app.delete("/api/users/{user_id}")
def api_delete_user(user_id: int):
    conn = db()
    try:
        with _db_lock, conn:
            if not conn.execute("SELECT 1 FROM users WHERE id=? AND role='user'", (user_id,)).fetchone():
                raise HTTPException(404, "User not found")
            ACCESS.delete_user_sessions(conn, user_id)
            conn.execute("DELETE FROM users WHERE id=? AND role='user'", (user_id,))
    finally:
        conn.close()
    return {"ok": True}


# ─── Admin accounts (Trinetra team only) ─────────────────────────────────────
def _admin_out(r) -> dict:
    return {"id": r["id"], "username": r["username"], "name": r["name"], "active": bool(r["active"]), "role": "admin",
            "created_at": r["created_at"], "created_by": r["created_by"], "last_login": r["last_login"]}


def _admin_row(conn, admin_id: int):
    r = conn.execute("SELECT * FROM users WHERE id=? AND role='admin'", (admin_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Admin account not found")
    return r


def _login_url() -> str:
    return f"{config.PUBLIC_URL.rstrip('/')}/login.html" if config.PUBLIC_URL else ""


def _admin_password(pw) -> str:
    if pw in (None, ""):
        return ACCESS.generate_password()
    if not isinstance(pw, str):
        raise HTTPException(422, "Invalid request")
    problem = security.password_problem(pw)
    if problem:
        raise HTTPException(400, problem)
    return pw


@app.get("/api/admins")
def api_admins():
    conn = db()
    try:
        return [_admin_out(r) for r in conn.execute("SELECT * FROM users WHERE role='admin' ORDER BY username COLLATE NOCASE")]
    finally:
        conn.close()


@app.post("/api/admins")
async def api_create_admin(request: Request):
    body = await read_json(request)
    username, name = _str(body, "username", maxlen=32).strip(), _str(body, "name", maxlen=60).strip()
    _check_username(username)
    password = _admin_password(body.get("password"))
    if security.password_problem(password, username):
        raise HTTPException(400, security.password_problem(password, username))
    conn = db()
    try:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise HTTPException(400, f"User ID {username} already exists")
        with _db_lock, conn:
            cur = conn.execute("INSERT INTO users(username,name,password_hash,permissions,active,role,created_by,password_enc) "
                               "VALUES(?,?,?,'[]',1,'admin',?,?)",
                               (username, name, security.hash_password(password), request.state.ident.get("username"),
                                ACCESS.seal(password)))
        print(f"[access] admin account {username} created by {request.state.ident.get('username')}")
        request.state.audit_target = username
        return {**_admin_out(_admin_row(conn, cur.lastrowid)), "password": password, "login_url": _login_url()}
    finally:
        conn.close()


@app.put("/api/admins/{admin_id}")
async def api_update_admin(admin_id: int, request: Request):
    body = await read_json(request)
    conn = db()
    try:
        _admin_row(conn, admin_id)
        with _db_lock, conn:
            if "name" in body:
                conn.execute("UPDATE users SET name=? WHERE id=?", (_str(body, "name", maxlen=60).strip(), admin_id))
            if "active" in body:
                if not isinstance(body["active"], bool):
                    raise HTTPException(422, "Invalid request")
                conn.execute("UPDATE users SET active=? WHERE id=?", (int(body["active"]), admin_id))
                if not body["active"]:
                    ACCESS.delete_user_sessions(conn, admin_id)      # signed out at once
        return _admin_out(_admin_row(conn, admin_id))
    finally:
        conn.close()


@app.post("/api/admins/{admin_id}/reset-password")
async def api_reset_admin_password(admin_id: int, request: Request):
    body = await read_json(request)
    conn = db()
    try:
        r = _admin_row(conn, admin_id)
        password = _admin_password(body.get("password"))
        if security.password_problem(password, r["username"]):
            raise HTTPException(400, security.password_problem(password, r["username"]))
        with _db_lock, conn:
            conn.execute("UPDATE users SET password_hash=?, password_enc=? WHERE id=?",
                         (security.hash_password(password), ACCESS.seal(password), admin_id))
            ACCESS.delete_user_sessions(conn, admin_id)              # signed out everywhere
        return {**_admin_out(_admin_row(conn, admin_id)), "password": password, "login_url": _login_url()}
    finally:
        conn.close()


@app.get("/api/admins/{admin_id}/credentials")
def api_admin_credentials(admin_id: int, request: Request):
    """the admin's sign-in details again (Trinetra team only); password None: the key file changed — set a new one"""
    conn = db()
    try:
        r = _admin_row(conn, admin_id)
    finally:
        conn.close()
    audit(request, "account", "Viewed admin credentials", r["username"])
    return {**_admin_out(r), "password": ACCESS.unseal(r["password_enc"]), "login_url": _login_url()}


@app.delete("/api/admins/{admin_id}")
def api_delete_admin(admin_id: int):
    """only a disabled admin can be deleted (an accidental click cannot lock a client out)"""
    conn = db()
    try:
        r = _admin_row(conn, admin_id)
        if r["active"]:
            raise HTTPException(400, "Disable this admin account before deleting it")
        with _db_lock, conn:
            ACCESS.delete_user_sessions(conn, admin_id)
            conn.execute("DELETE FROM users WHERE id=? AND role='admin'", (admin_id,))
    finally:
        conn.close()
    return {"ok": True}


# ─── Activity Logs (Trinetra team only) ──────────────────────────────────────
@app.get("/api/audit")
def api_audit(date_from: Optional[str] = None, date_to: Optional[str] = None, category: str = "", role: str = "",
              q: str = "", before: Optional[int] = None, limit: int = 100):
    """newest first, a page at a time (before = the id the last page ended at); counts cover every page"""
    limit = max(1, min(limit, 500))
    where, args = [], []
    try:
        if date_from:
            where.append("ts>=?"); args.append(datetime.strptime(date_from, "%Y-%m-%d").date().isoformat())
        if date_to:
            where.append("ts<?"); args.append((datetime.strptime(date_to, "%Y-%m-%d").date() + timedelta(days=1)).isoformat())
    except ValueError:
        raise HTTPException(400, "Dates are YYYY-MM-DD")
    if category:
        where.append("category=?"); args.append(category[:32])
    if role:
        where.append("actor_role=?"); args.append(role[:16])
    if q.strip():
        like = "%" + q.strip()[:100].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(actor LIKE ? ESCAPE '\\' OR action LIKE ? ESCAPE '\\' OR target LIKE ? ESCAPE '\\' OR ip LIKE ? ESCAPE '\\')")
        args += [like] * 4
    base = (" WHERE " + " AND ".join(where)) if where else ""
    page = base + (" AND " if where else " WHERE ") + "id<?" if before else base
    conn = db()
    try:
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM activity_log{page} ORDER BY id DESC LIMIT ?",
                                              args + ([before] if before else []) + [limit + 1])]
        counts = {r[0]: r[1] for r in conn.execute(f"SELECT actor_role, COUNT(*) FROM activity_log{base} GROUP BY actor_role", args)}
    finally:
        conn.close()
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": rows, "next": rows[-1]["id"] if more else None, "counts": counts}


# ─── Settings (Trinetra team only) ───────────────────────────────────────────
@app.get("/api/admin/storage")
def admin_storage():
    return housekeeping.storage_stats()


@app.get("/api/admin/config")
def admin_get_config(request: Request):
    return {
        "TRINETRA_SESSION_GAP_MINUTES":       config.SESSION_GAP_MINUTES,
        "TRINETRA_SESSION_AUTO_CLOSE_MINUTES": config.SESSION_AUTO_CLOSE_MINUTES,
        "TRINETRA_PRESENCE_GAP_SECONDS":      int(config.PRESENCE_GAP_SECONDS),
        "TRINETRA_IMAGE_RETENTION_DAYS":      config.IMAGE_RETENTION_DAYS,
        "TRINETRA_DETECTION_RETENTION_DAYS":  config.DETECTION_RETENTION_DAYS,
        # Secrets are never sent back — only whether one is set.
        "TELEGRAM_TOKEN":                     "",
        "TELEGRAM_TOKEN_SET":                 bool(config.TELEGRAM_TOKEN),
        "TELEGRAM_CHAT_ID":                   ",".join(config.TELEGRAM_CHAT_IDS),
        "SMTP_HOST":                          config.SMTP_HOST,
        "SMTP_PORT":                          config.SMTP_PORT,
        "SMTP_USER":                          config.SMTP_USER,
        "SMTP_TO":                            config.SMTP_TO,
    }


# Every setting the admin panel may change, with its validation rule.
# Anything not listed here is refused — the API can never write arbitrary keys
# (paths, host, port, account credentials) into .env.
_CONFIG_RULES = {
    "TRINETRA_SESSION_GAP_MINUTES":        ("int", 1, 10080),
    "TRINETRA_SESSION_AUTO_CLOSE_MINUTES": ("int", 1, 10080),
    "TRINETRA_IMAGE_RETENTION_DAYS":       ("int", 0, 3650),
    "TRINETRA_DETECTION_RETENTION_DAYS":   ("int", 0, 3650),
    "TRINETRA_PRESENCE_GAP_SECONDS":       ("int", 10, 3600),
    "SMTP_PORT":                           ("int", 1, 65535),
    "TELEGRAM_TOKEN":        ("re", re.compile(r"^[0-9]{1,20}:[A-Za-z0-9_-]{20,100}$")),
    "TELEGRAM_CHAT_ID":      ("re", re.compile(r"^[0-9, -]{0,500}$")),
    "SMTP_HOST":             ("re", re.compile(r"^[A-Za-z0-9.-]{0,253}$")),
    "SMTP_USER":             ("re", re.compile(r"^[^\s,;<>\"'\\]{0,254}$")),
    "SMTP_TO":               ("re", re.compile(r"^[^\s;<>\"'\\]{0,1000}$")),
}


def _validate_config(updates: dict) -> dict:
    clean = {}
    for key, val in updates.items():
        rule = _CONFIG_RULES.get(key)
        if rule is None:
            raise HTTPException(400, f"Unknown or read-only setting: {str(key)[:40]}")
        if isinstance(val, bool) or not isinstance(val, (str, int)):
            raise HTTPException(400, f"{key} has an invalid value")
        sval = str(val).strip()
        if security.has_control_chars(sval):
            raise HTTPException(400, f"{key} has an invalid value")
        if key == "TELEGRAM_TOKEN" and sval == "":
            continue                                   # blank = keep the current token
        if rule[0] == "int":
            try:
                n = int(sval)
            except ValueError:
                raise HTTPException(400, f"{key} must be an integer")
            if not rule[1] <= n <= rule[2]:
                raise HTTPException(400, f"{key} must be between {rule[1]} and {rule[2]}")
            sval = str(n)
        elif not rule[1].match(sval):
            raise HTTPException(400, f"{key} has an invalid value")
        clean[key] = sval
    return clean


@app.post("/api/admin/config")
async def admin_update_config(request: Request):
    updates = _validate_config(await read_json(request))
    _write_env(updates)
    _apply_config(updates)
    audit(request, "settings", "Changed settings", ", ".join(sorted(updates)))
    return {"ok": True, "restart_required": False}


def _write_env(updates: dict):
    """Rewrite .env preserving comments; append any keys not already present.
    Values are validated by _validate_config (no line breaks can reach here)."""
    env_path = Path(config.ENV_FILE)
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
    os.chmod(env_path, 0o600)                  # holds the bot token / SMTP password


def _apply_config(updates: dict):
    """Hot-reload config module attributes so changes take effect immediately."""
    mapping = {
        "TRINETRA_SESSION_GAP_MINUTES":        ("SESSION_GAP_MINUTES",         int),
        "TRINETRA_SESSION_AUTO_CLOSE_MINUTES": ("SESSION_AUTO_CLOSE_MINUTES",  int),
        "TRINETRA_IMAGE_RETENTION_DAYS":       ("IMAGE_RETENTION_DAYS",        int),
        "TRINETRA_DETECTION_RETENTION_DAYS":   ("DETECTION_RETENTION_DAYS",    int),
        "TRINETRA_PRESENCE_GAP_SECONDS":       ("PRESENCE_GAP_SECONDS",        float),
        "TELEGRAM_TOKEN":   ("TELEGRAM_TOKEN",    str),
        "TELEGRAM_CHAT_ID": ("TELEGRAM_CHAT_IDS",
                             lambda v: [cid.strip() for cid in v.split(",") if cid.strip()]),
        "SMTP_HOST":        ("SMTP_HOST",         str),
        "SMTP_PORT":        ("SMTP_PORT",          int),
        "SMTP_USER":        ("SMTP_USER",         str),
        "SMTP_TO":          ("SMTP_TO",           str),
    }
    for env_key, (attr, fn) in mapping.items():
        if env_key in updates:
            setattr(config, attr, fn(updates[env_key]))


@app.get("/api/admin/database/{table}")
def admin_get_database_table(request: Request, table: str, limit: int = 50, offset: int = 0):
    if table not in ["detections", "sessions", "alerts", "whitelist"]:
        raise HTTPException(400, "Invalid table")
    limit, offset = max(1, min(limit, 500)), max(0, offset)

    conn = db()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if table == "detections":
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY timestamp DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        elif table == "sessions":
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY first_seen DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        elif table == "alerts":
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY triggered_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        elif table == "whitelist":
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY added_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            
        return {
            "total": total,
            "records": [dict(r) for r in rows]
        }
    finally:
        conn.close()


@app.delete("/api/admin/database/{table}/{record_id}")
def admin_delete_database_record(request: Request, table: str, record_id: str):
    _stats_reset(); _SUMMARY.clear(); RULES.reset_cache()   # the cached counts / presences start again
    if table not in ["detections", "sessions", "alerts", "whitelist"]:
        raise HTTPException(400, "Invalid table")
        
    conn = db()
    try:
        with conn:
            if table == "detections":
                row = conn.execute("SELECT filename FROM detections WHERE id=?", (record_id,)).fetchone()
                if row:
                    filename = row["filename"]
                    image_path = Path(config.ARCHIVE_DIR) / filename
                    if image_path.exists():
                        try:
                            image_path.unlink()
                        except Exception as e:
                            print(f"Error deleting image file {filename}: {e}")
                conn.execute("DELETE FROM detections WHERE id=?", (record_id,))
            elif table == "whitelist":
                conn.execute("DELETE FROM whitelist WHERE badge=?", (record_id,))
            else:
                conn.execute(f"DELETE FROM {table} WHERE id=?", (record_id,))
        return {"ok": True}
    finally:
        conn.close()

@app.delete("/api/admin/database/{table}")
def admin_clear_database_table(request: Request, table: str):
    _stats_reset(); _SUMMARY.clear(); RULES.reset_cache()   # the cached counts / presences start again
    if table not in ["detections", "sessions", "alerts", "whitelist"]:
        raise HTTPException(400, "Invalid table")
        
    conn = db()
    try:
        with conn:
            if table == "detections":
                rows = conn.execute("SELECT filename FROM detections").fetchall()
                for row in rows:
                    filename = row["filename"]
                    image_path = Path(config.ARCHIVE_DIR) / filename
                    if image_path.exists():
                        try:
                            image_path.unlink()
                        except Exception:
                            pass
            conn.execute(f"DELETE FROM {table}")
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/admin/reset")
def admin_reset_all(request: Request):
    _stats_reset(); _SUMMARY.clear(); RULES.reset_cache()   # the cached counts / presences start again
    """Wipe ALL data: every DB table + archive images + dead-letter + heartbeats. Irreversible."""

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
                conn.execute("DELETE FROM observations")          # the alert rules' frame history
                conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('alerts','detections','sessions','observations')")
        finally:
            conn.close()

    housekeeping.refresh_storage()
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


# ─── /ws — WebSocket for live updates ─────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # Live feed needs a signed-in session from this site (no cross-site WebSocket hijacking).
    origin = ws.headers.get("origin")
    ident = await asyncio.to_thread(ACCESS.identity, ws.cookies.get(SESSION_COOKIE))
    if not ACCESS.allowed(ident, ("live",)) or (origin and not _origin_ok(origin, ws.headers)):
        await ws.close(code=1008)
        return
    await wsm.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        pass
    finally:
        await wsm.disconnect(ws)


app.include_router(ds_control.router)                # /api/ds/* — the DeepStream page (Trinetra team only)
app.include_router(captures.router)                  # /api/captures* — the Recordings page (Trinetra team only)
app.include_router(dongle.router)                    # /api/dongle* — the 4G dongle on the Recordings page (Trinetra team only)


# ─── Static frontend — mounted LAST so /api and /ws take precedence ──────
app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="frontend")


# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    # server_header=False: do not name the web-server software in responses.
    # proxy_headers: a local HTTPS proxy (e.g. Tailscale serve) marks requests as
    # https, so the session cookie gets Secure and HSTS is sent.
    uvicorn.run("app:app", host=config.HOST, port=config.PORT, log_level="info", access_log=config.ACCESS_LOG,
                server_header=False, proxy_headers=True, forwarded_allow_ips="127.0.0.1")
