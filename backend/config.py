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

# Cast all to strings for backward compat with code that uses os.path
DATA_DIR        = str(DATA_DIR)
DB_PATH         = str(DB_PATH)
INCOMING_DIR    = str(INCOMING_DIR)
ARCHIVE_DIR     = str(ARCHIVE_DIR)
DEAD_LETTER_DIR = str(DEAD_LETTER_DIR)
HEARTBEATS_DIR  = str(HEARTBEATS_DIR)
FRONTEND_DIR    = str(FRONTEND_DIR)
CLASSES_FILE    = str(CLASSES_FILE)

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

# ─── IMAGE CLEANUP ────────────────────────────────────────────────────────────
# Delete archived images older than this many days. 0 = keep forever (disabled).
IMAGE_RETENTION_DAYS = int(os.environ.get("TRINETRA_IMAGE_RETENTION_DAYS", "0"))

# ─── NOTIFICATIONS ───────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_TO   = os.environ.get("SMTP_TO",   "")

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
    print(f"[config] JETSONS       = {EXPECTED_JETSONS}")
    print(f"[config] LOITER_MIN    = {LOITER_MINUTES} min")
    print(f"[config] GAP_MIN       = {SESSION_GAP_MINUTES} min")
    print(f"[config] AUTO_CLOSE    = {SESSION_AUTO_CLOSE_MINUTES} min")
    print(f"[config] EXIT_CAMERAS  = {sorted(EXIT_CAMERAS)}")
    print(f"[config] IMAGE_CLEANUP = {'disabled' if IMAGE_RETENTION_DAYS == 0 else f'{IMAGE_RETENTION_DAYS} days'}")
