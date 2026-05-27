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
import io
import json
import os
import shutil
import smtplib
import sqlite3
import threading
import time
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

            # Sessions over threshold, not yet alerted, not whitelisted
            rows = conn.execute("""
                SELECT id, badge, first_seen FROM sessions
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
                    alerts_to_send.append({
                        "alert_id": cur.lastrowid,
                        "session_id": r["id"],
                        "badge": r["badge"],
                        "first_seen": r["first_seen"],
                        "duration_seconds": duration,
                    })

            # Close stale sessions — no detection in SESSION_AUTO_CLOSE_MINUTES.
            # When this fires, the badge is considered "left without exiting properly".
            # If the same badge is later detected again (e.g. handed off to another
            # person), a fresh session opens — so the two visits are tracked separately.
            stale_cutoff = (now - timedelta(minutes=config.SESSION_AUTO_CLOSE_MINUTES)).isoformat()
            with conn:
                conn.execute(
                    "UPDATE sessions SET closed=1, closed_at=? "
                    "WHERE closed=0 AND last_seen < ?",
                    (now.isoformat(), stale_cutoff)
                )
        finally:
            conn.close()

    for a in alerts_to_send:
        await fire_alert(a)


async def fire_alert(alert: dict):
    """Send notifications and broadcast over WebSocket."""
    badge = alert["badge"]
    first = datetime.fromisoformat(alert["first_seen"])
    duration_min = alert["duration_seconds"] // 60
    text = (
        f"⚠️ LOITERING ALERT\n"
        f"Badge: {badge}\n"
        f"Duration: {duration_min} minutes\n"
        f"First seen: {first.strftime('%H:%M:%S')}"
    )

    if config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID:
        try:
            await _send_telegram(text)
            _mark_alert(alert["alert_id"], "telegram_sent")
        except Exception as e:
            print(f"[alert] telegram failed: {e}")

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


async def _send_telegram(text: str):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": config.TELEGRAM_CHAT_ID, "text": text
    }).encode()

    def _send():
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=5).read()

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
    task = asyncio.create_task(loitering_loop())
    print("[trinetra] startup complete · http://%s:%d" % (config.HOST, config.PORT))
    try:
        yield
    finally:
        print("[trinetra] shutting down")
        observer.stop()
        observer.join(timeout=2)
        task.cancel()


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
        loitering = conn.execute(
            "SELECT COUNT(*) FROM sessions s "
            "WHERE s.closed=0 AND s.alert_sent=1 "
            "AND s.badge NOT IN (SELECT badge FROM whitelist) "
            "AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.session_id=s.id AND a.acknowledged=1)"
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
    conn = db()
    try:
        with conn:
            conn.execute(
                "UPDATE alerts SET acknowledged=1, acknowledged_at=?, acknowledged_by=? WHERE id=?",
                (datetime.utcnow().isoformat(), acked_by, alert_id)
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
