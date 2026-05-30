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
import os
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
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import config

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
        conn.executescript(schema.read_text())
        # MIGRATION: add session_id to detections if older DB doesn't have it
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
        local_offset = datetime.now() - datetime.utcnow()
        dt = dt_local - local_offset
    except ValueError:
        return None
    return m["camera"], m["badge"], dt


def update_session(conn, badge: str, camera: str, ts: datetime, is_exit: bool):
    """
    Find open session for badge, extend it OR close it (on exit) OR open new one.
    Returns (session_id, detection_type)  where type is 'entry' | 'sighting' | 'exit'.
    """
    gap = timedelta(minutes=config.SESSION_GAP_MINUTES)

    row = conn.execute(
        "SELECT id, last_seen, cameras_json FROM sessions "
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
        return row["id"], "exit"

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
            return row["id"], "sighting"
        # Gap too long → close old session, open new one below
        conn.execute("UPDATE sessions SET closed=1, closed_at=? WHERE id=?",
                     (ts.isoformat(), row["id"]))

    if is_exit:
        # Exit on a badge with no open session — just log it, no session
        return None, "exit"

    cur = conn.execute(
        "INSERT INTO sessions(badge, first_seen, last_seen, last_camera, cameras_json) "
        "VALUES (?,?,?,?,?)",
        (badge, ts.isoformat(), ts.isoformat(), camera, json.dumps({camera: 1}))
    )
    return cur.lastrowid, "entry"


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
    session_id, det_type = None, None

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
                session_id, det_type = update_session(conn, badge, camera, ts, is_exit)
                # Stamp the detection with its session (visit) ID and final type
                conn.execute(
                    "UPDATE detections SET type=?, session_id=? WHERE id=?",
                    (det_type, session_id, detection_id)
                )
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
            rows = conn.execute("""
                SELECT id, badge, first_seen, last_seen, last_camera FROM sessions
                WHERE closed=0 AND alert_sent=0
                  AND badge NOT IN (SELECT badge FROM whitelist)
            """).fetchall()

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
                    print(f"[alert] telegram failed for chat_id={chat_id}: {e}")
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
        total_today = conn.execute(
            "SELECT COUNT(*) FROM detections WHERE date(timestamp)=date('now','localtime')"
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
        ).fetchone()[0]

        by_badge = conn.execute("""
            SELECT badge, COUNT(*) AS c FROM detections
            WHERE date(timestamp)=date('now','localtime')
            GROUP BY badge ORDER BY c DESC LIMIT 7
        """).fetchall()
        by_camera = conn.execute("""
            SELECT camera, COUNT(*) AS c FROM detections
            WHERE date(timestamp)=date('now','localtime')
            GROUP BY camera ORDER BY c DESC
        """).fetchall()

        # Hourly buckets for today
        hourly = [0] * 24
        rows = conn.execute("""
            SELECT CAST(strftime('%H', timestamp, 'localtime') AS INT) AS h, COUNT(*) AS c
            FROM detections
            WHERE date(timestamp)=date('now','localtime')
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
                   CASE WHEN s.badge IN (SELECT badge FROM whitelist) THEN 1 ELSE 0 END AS whitelisted,
                   a.id AS alert_id, a.acknowledged, a.acknowledged_at, a.acknowledged_by
            FROM sessions s
            LEFT JOIN alerts a ON a.session_id = s.id
            WHERE s.closed=0
            ORDER BY s.first_seen ASC
        """).fetchall()
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
        "TRINETRA_IMAGE_RETENTION_DAYS":      config.IMAGE_RETENTION_DAYS,
        "TRINETRA_JETSONS":                   ",".join(config.EXPECTED_JETSONS),
        "TRINETRA_HB_STALE":                  config.HEARTBEAT_STALE_SECONDS,
        "TELEGRAM_TOKEN":                     config.TELEGRAM_TOKEN,
        "TELEGRAM_CHAT_ID":                   config.TELEGRAM_CHAT_ID,
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
        "TRINETRA_EXIT_CAMERAS":               ("EXIT_CAMERAS",
                                                lambda v: {c.strip() for c in v.split(",") if c.strip()}),
        "TRINETRA_JETSONS":                    ("EXPECTED_JETSONS",
                                                lambda v: [j.strip() for j in v.split(",") if j.strip()]),
        "TELEGRAM_TOKEN":   ("TELEGRAM_TOKEN",   str),
        "TELEGRAM_CHAT_ID": ("TELEGRAM_CHAT_ID", str),
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
@app.get("/api/admin/database/{table}")
def admin_get_database_table(request: Request, table: str, limit: int = 50, offset: int = 0):
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
    if table not in ["detections", "sessions", "alerts", "whitelist"]:
        raise HTTPException(400, "Invalid table")
    
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
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
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
    if not _check_admin(request):
        raise HTTPException(401, "Not authenticated")
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
