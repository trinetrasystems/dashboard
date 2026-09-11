"""Trinetra Monitor - configuration.

Settings come from (in order of priority):
  1. Real OS environment variables  (highest priority)
  2. A .env file at the project root
  3. The defaults below              (lowest priority)

Paths are normalized so forward / and backslash both work on Windows and Linux.
Relative paths are resolved relative to the project root (parent of backend/).
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _HAS_DOTENV = True
except ImportError:
    _HAS_DOTENV = False

# ─── Resolve project root (one level above backend/) ─────────────────────────
BACKEND_DIR  = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent

# ─── Load .env from project root if present ──────────────────────────────────
ENV_FILE = PROJECT_ROOT / ".env"
if _HAS_DOTENV and ENV_FILE.exists():
    load_dotenv(ENV_FILE)
    print(f"[config] loaded .env from {ENV_FILE}")
elif not _HAS_DOTENV:
    print("[config] python-dotenv not installed — using OS env vars only")
elif not ENV_FILE.exists():
    print(f"[config] no .env at {ENV_FILE} — using defaults (this is fine for first run)")


# ─── Helper: resolve a path that might be relative or absolute ────────────────
def _resolve_path(value: str, default: Path) -> Path:
    """Convert env-var value to a Path. Relative paths anchor to PROJECT_ROOT."""
    if not value:
        return default
    p = Path(value.replace("\\", "/"))   # normalize Windows backslashes
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


# ─── PATHS ───────────────────────────────────────────────────────────────────
DATA_DIR        = _resolve_path(os.environ.get("TRINETRA_DATA",     ""), PROJECT_ROOT / "data")
DB_PATH         = _resolve_path(os.environ.get("TRINETRA_DB",       ""), DATA_DIR / "trinetra.db")
INCOMING_DIR    = _resolve_path(os.environ.get("TRINETRA_INCOMING", ""), DATA_DIR / "incoming")
ARCHIVE_DIR     = _resolve_path(os.environ.get("TRINETRA_ARCHIVE",  ""), DATA_DIR / "archive")
DEAD_LETTER_DIR = _resolve_path(os.environ.get("TRINETRA_DEAD",     ""), DATA_DIR / "dead-letter")
HEARTBEATS_DIR  = _resolve_path(os.environ.get("TRINETRA_HEARTBEATS",""),DATA_DIR / "heartbeats")
FRONTEND_DIR    = _resolve_path(os.environ.get("TRINETRA_FRONTEND", ""), PROJECT_ROOT / "frontend")
CLASSES_FILE    = _resolve_path(os.environ.get("TRINETRA_CLASSES",  ""), BACKEND_DIR / "classes.txt")
# Where visit recordings live. Point this anywhere on local storage — an
# external drive, an NVR mount, etc. Files are matched to visits by name.
VIDEO_DIR       = _resolve_path(os.environ.get("TRINETRA_VIDEO_DIR", ""), DATA_DIR / "videos")

# Cast all to strings for backward compat with code that uses os.path
DATA_DIR        = str(DATA_DIR)
DB_PATH         = str(DB_PATH)
INCOMING_DIR    = str(INCOMING_DIR)
ARCHIVE_DIR     = str(ARCHIVE_DIR)
DEAD_LETTER_DIR = str(DEAD_LETTER_DIR)
HEARTBEATS_DIR  = str(HEARTBEATS_DIR)
FRONTEND_DIR    = str(FRONTEND_DIR)
CLASSES_FILE    = str(CLASSES_FILE)
VIDEO_DIR       = str(VIDEO_DIR)


def resolve_dir(value: str, fallback: str) -> str:
    """Public wrapper around _resolve_path — used when the admin panel edits a
    path at runtime, so a relative value still anchors to PROJECT_ROOT."""
    return str(_resolve_path(value, Path(fallback)))


# Extensions treated as playable recordings. Only .mp4/.webm play natively in
# browsers; the rest are still listed so a wrong-container file is visible
# (and reported) rather than silently missing.
VIDEO_EXTENSIONS      = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"}
BROWSER_VIDEO_EXTENSIONS = {".mp4", ".webm", ".m4v"}

# ─── SERVER ──────────────────────────────────────────────────────────────────
HOST = os.environ.get("TRINETRA_HOST", "0.0.0.0")
PORT = int(os.environ.get("TRINETRA_PORT", "8000"))

# ─── JETSON REGISTRY ─────────────────────────────────────────────────────────
EXPECTED_JETSONS = [
    j.strip() for j in os.environ.get(
        "TRINETRA_JETSONS",
        "central,edge-1,edge-2,edge-3"
    ).split(",") if j.strip()
]
HEARTBEAT_STALE_SECONDS = int(os.environ.get("TRINETRA_HB_STALE", "90"))

# ─── DETECTION / LOITERING SETTINGS ──────────────────────────────────────────
LOITER_MINUTES        = int(os.environ.get("TRINETRA_LOITER_MINUTES",      "15"))
SESSION_GAP_MINUTES   = int(os.environ.get("TRINETRA_SESSION_GAP_MINUTES",  "5"))
SESSION_AUTO_CLOSE_MINUTES = int(os.environ.get("TRINETRA_SESSION_AUTO_CLOSE_MINUTES", "10"))
EXIT_CAMERAS = {
    c.strip() for c in os.environ.get("TRINETRA_EXIT_CAMERAS", "cam-gate").split(",")
    if c.strip()
}

# ─── MOVEMENT ALERTS ─────────────────────────────────────────────────────────
# Event-driven, NOT time-based: one alert each time a badge is seen at a
# different camera than the one it was last seen at. Independent of loitering.
def _as_bool(value: str, default: bool) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


MOVEMENT_ALERTS = _as_bool(os.environ.get("TRINETRA_MOVEMENT_ALERTS", ""), True)

# ─── LOITERING ALERTS (master switch) ────────────────────────────────────────
# Turn the whole time-based loitering feature off — no alerts are raised, no
# Telegram/email is sent, and the dashboard hides the banner, the KPI card and
# the LOITERING/APPROACHING status tags. Useful when tracking vehicles, where
# "parked for a while" is normal and only movement between bays matters.
# Session housekeeping (auto-closing stale sessions) keeps running regardless.
LOITER_ALERTS = _as_bool(os.environ.get("TRINETRA_LOITER_ALERTS", ""), True)

# ─── IMAGE CLEANUP ────────────────────────────────────────────────────────────
# Delete archived images older than this many days. 0 = keep forever (disabled).
IMAGE_RETENTION_DAYS = int(os.environ.get("TRINETRA_IMAGE_RETENTION_DAYS", "0"))

# ─── ADMIN CREDENTIALS ────────────────────────────────────────────────────────
ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL",    "trinetra@admin.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin@123")

# ─── NOTIFICATIONS ───────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "").strip()
# Supports multiple recipients: comma-separated list of chat IDs
TELEGRAM_CHAT_IDS = [
    cid.strip() for cid in os.environ.get("TELEGRAM_CHAT_ID", "").split(",")
    if cid.strip()
]

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_TO   = os.environ.get("SMTP_TO",   "")

# ─── FIREBASE CLOUD MESSAGING ────────────────────────────────────────────────
# The web config below is PUBLIC by design — it ships inside every Firebase web
# app and identifies the project, it does not authorise anything. It is served
# to the browser by /api/notifications/config.
#
# The two secrets are elsewhere and never reach the frontend:
#   FIREBASE_VAPID_KEY        — Console ▸ Project Settings ▸ Cloud Messaging ▸
#                               Web Push certificates ("Key pair")
#   FIREBASE_CREDENTIALS_FILE — Console ▸ Project Settings ▸ Service Accounts ▸
#                               "Generate new private key" (a .json file)
FIREBASE_WEB_CONFIG = {
    "apiKey":            os.environ.get("FIREBASE_API_KEY",             "AIzaSyCIxcZYt7JwY6DUJ50DGxOH7S5_0xT2SVI"),
    "authDomain":        os.environ.get("FIREBASE_AUTH_DOMAIN",         "trinetra-demo.firebaseapp.com"),
    "projectId":         os.environ.get("FIREBASE_PROJECT_ID",          "trinetra-demo"),
    "storageBucket":     os.environ.get("FIREBASE_STORAGE_BUCKET",      "trinetra-demo.firebasestorage.app"),
    "messagingSenderId": os.environ.get("FIREBASE_MESSAGING_SENDER_ID", "772686068265"),
    "appId":             os.environ.get("FIREBASE_APP_ID",              "1:772686068265:web:fe5790b53f3e8a70a2ba38"),
}

FIREBASE_VAPID_KEY = os.environ.get("FIREBASE_VAPID_KEY", "").strip()

# Resolved against PROJECT_ROOT, not the current working directory — otherwise
# "./secrets/…" silently means different files depending on where the server is
# launched from (systemd sets its own WorkingDirectory).
_fb_cred_raw = os.environ.get("FIREBASE_CREDENTIALS_FILE", "").strip()
FIREBASE_CREDENTIALS_FILE = (
    str(_resolve_path(_fb_cred_raw, PROJECT_ROOT / "secrets" / "firebase-service-account.json"))
    if _fb_cred_raw else ""
)

# Public HTTPS origin the dashboard is reachable at, e.g.
# https://monitor.trinetrasystems.com — used to build absolute notification
# click-through links. FCM REJECTS a webpush link that isn't absolute HTTPS,
# so when this is unset (or plain http, as on localhost) the link is omitted
# and the service worker falls back to routing from the data payload.
PUBLIC_URL = os.environ.get("TRINETRA_PUBLIC_URL", "").strip().rstrip("/")

# Android notification channel the mobile app creates. Without this on the
# outgoing message, Android drops background notifications into the default
# channel, where they can be silent. Must match the channel id the app
# registers with notifee.
ANDROID_CHANNEL_ID = os.environ.get("TRINETRA_ANDROID_CHANNEL", "trinetra-events").strip()


def public_link(path: str) -> str:
    """Absolute HTTPS link for a notification click, or "" if not possible."""
    if not PUBLIC_URL.lower().startswith("https://"):
        return ""
    return PUBLIC_URL + (path if path.startswith("/") else "/" + path)


# How long a device-claim session lasts before the user must sign in again.
USER_SESSION_DAYS = int(os.environ.get("TRINETRA_USER_SESSION_DAYS", "30"))

# When a Firebase account signs in with no matching Trinetra user, create one
# automatically. Off = only users the admin pre-created (matched by email) can
# register a device — tighter, but every new employee needs a manual step.
FCM_AUTO_PROVISION = _as_bool(os.environ.get("TRINETRA_FCM_AUTO_PROVISION", ""), True)


def firebase_status() -> dict:
    """What is / isn't configured — surfaced in the admin panel and on startup."""
    cred = Path(FIREBASE_CREDENTIALS_FILE) if FIREBASE_CREDENTIALS_FILE else None
    return {
        "web_config": bool(FIREBASE_WEB_CONFIG.get("apiKey")),
        "vapid_key": bool(FIREBASE_VAPID_KEY),
        "credentials_file": str(cred) if cred else "",
        "credentials_present": bool(cred and cred.is_file()),
    }


# ─── FILENAME PATTERN ────────────────────────────────────────────────────────
# Edge Jetsons MUST save detections as: <camera>_<badge>_<YYYYMMDD-HHMMSS>.jpg
# Camera and badge names CANNOT contain underscores (use hyphens or camelCase).
import re
FILENAME_RE = re.compile(
    r"^(?P<camera>[A-Za-z0-9-]+)_(?P<badge>[A-Za-z0-9-]+)_(?P<ts>\d{8}-\d{6})\.jpg$"
)


# ─── PRINT CONFIG SUMMARY ON IMPORT ──────────────────────────────────────────
def print_summary():
    """Called from app.py on startup so user sees resolved paths in logs."""
    print(f"[config] PROJECT_ROOT  = {PROJECT_ROOT}")
    print(f"[config] DATA_DIR      = {DATA_DIR}")
    print(f"[config] DB_PATH       = {DB_PATH}")
    print(f"[config] INCOMING_DIR  = {INCOMING_DIR}")
    print(f"[config] ARCHIVE_DIR   = {ARCHIVE_DIR}")
    print(f"[config] FRONTEND_DIR  = {FRONTEND_DIR}")
    print(f"[config] CLASSES_FILE  = {CLASSES_FILE}")
    print(f"[config] VIDEO_DIR     = {VIDEO_DIR}"
          f"{'' if Path(VIDEO_DIR).is_dir() else '  (does not exist yet)'}")
    print(f"[config] JETSONS       = {EXPECTED_JETSONS}")
    print(f"[config] LOITER_MIN    = {LOITER_MINUTES} min")
    print(f"[config] GAP_MIN       = {SESSION_GAP_MINUTES} min")
    print(f"[config] AUTO_CLOSE    = {SESSION_AUTO_CLOSE_MINUTES} min")
    print(f"[config] EXIT_CAMERAS  = {sorted(EXIT_CAMERAS)}")
    print(f"[config] MOVE_ALERTS   = {'on' if MOVEMENT_ALERTS else 'off'}")
    fb = firebase_status()
    if fb["credentials_present"] and fb["vapid_key"]:
        print(f"[config] FCM PUSH     = ready (project {FIREBASE_WEB_CONFIG['projectId']})")
    else:
        missing = []
        if not fb["vapid_key"]:
            missing.append("FIREBASE_VAPID_KEY")
        if not fb["credentials_present"]:
            missing.append("FIREBASE_CREDENTIALS_FILE")
        print(f"[config] FCM PUSH     = log-only mode · missing {', '.join(missing)}")
    print(f"[config] LOITER_ALERTS = {'on' if LOITER_ALERTS else 'off'}"
          f"{'' if LOITER_ALERTS else '  (banner + KPI hidden, no alerts raised)'}")
    print(f"[config] IMAGE_CLEANUP = {'disabled' if IMAGE_RETENTION_DAYS == 0 else f'{IMAGE_RETENTION_DAYS} days'}")
