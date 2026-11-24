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

# ─── Load the settings file: TRINETRA_ENV_FILE (the Jetson container: data/dashboard.env, outside the
#     image so it survives upgrades), else .env at the project root ─────────────
ENV_FILE = Path(os.environ.get("TRINETRA_ENV_FILE", "") or (PROJECT_ROOT / ".env"))
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
        ""
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

# ─── PERIMETER ZONE RULES ─────────────────────────────────────────────────────
# The perimeter camera sees three zones (see the Pharma Perimeter deck, slide 4).
# Every YOLO class the edge sends is mapped to exactly one of them:
#
#   ZONE_BREACH  · "Perimeter Alert Region" — wall top + concertina wire.
#                  Any detection here alerts INSTANTLY. No dwell wait.
#   ZONE_OUTSIDE · "Outside perimeter" — the strip beyond the wall.
#                  Alerts as an EARLY WARNING once the object has been seen
#                  continuously for its dwell threshold (default 10 min).
#   ZONE_INSIDE  · "Inside factory" — tracked for context, never alarms.
#
# Anything not listed below keeps the original behaviour: it is treated as a
# normal presence session and uses the global TRINETRA_LOITER_MINUTES threshold.
#
# Badge matching is case-insensitive, and "boundary"/"boundry" are folded
# together so either spelling of the class name works.

ZONE_BREACH  = "breach"
ZONE_OUTSIDE = "outside"
ZONE_INSIDE  = "inside"
ZONE_UNKNOWN = "unknown"

ZONE_LABELS = {
    ZONE_BREACH:  "Perimeter Alert Region",
    ZONE_OUTSIDE: "Outside perimeter",
    ZONE_INSIDE:  "Inside factory",
    ZONE_UNKNOWN: "Unzoned",
}


def normalize_badge(badge: str) -> str:
    """Canonical key for badge lookups: case/separator/spelling insensitive."""
    b = (badge or "").strip().lower().replace("_", "-")
    return b.replace("boundary", "boundry")


# Counted outside-perimeter classes, e.g. "Outside-person-3-object-2" means
# three people and two objects are currently outside the wall. The edge sends
# the tally in the class name, so the count changes frame to frame.
import re as _re
OUTSIDE_COUNT_RE = _re.compile(r"^outside-person-(\d+)-object-(\d+)$")
# SQL LIKE equivalent, for zone filtering inside the database
OUTSIDE_COUNT_LIKE = "outside-person-%-object-%"


def parse_counts(badge: str):
    """Return {'persons': n, 'objects': m} for a counted class, else None."""
    m = OUTSIDE_COUNT_RE.match(normalize_badge(badge))
    if not m:
        return None
    return {"persons": int(m.group(1)), "objects": int(m.group(2))}


def is_counted_outside(badge: str) -> bool:
    return OUTSIDE_COUNT_RE.match(normalize_badge(badge)) is not None


def _parse_badge_set(raw: str) -> set:
    return {normalize_badge(x) for x in raw.split(",") if x.strip()}


def _parse_dwell_rules(raw: str) -> dict:
    """Parse "badge:minutes,badge:minutes" into {normalized_badge: minutes}."""
    rules = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, mins = item.partition(":")
        name = normalize_badge(name)
        if not name:
            continue
        try:
            rules[name] = int(mins) if mins.strip() else 10
        except ValueError:
            rules[name] = 10
    return rules


# Instant-alert classes (Perimeter Alert Region)
BREACH_BADGES = _parse_badge_set(
    os.environ.get("TRINETRA_BREACH_BADGES", "Boundry-object")
)

# Dwell-rule classes (Outside perimeter) — "badge:minutes" pairs
DWELL_RULES = _parse_dwell_rules(
    os.environ.get("TRINETRA_DWELL_BADGES", "outside-person:10,outside-object:10")
)

# Tracked-but-silent classes (Inside factory). Empty by default so existing
# deployments keep alerting on their own badge classes.
INSIDE_BADGES = _parse_badge_set(os.environ.get("TRINETRA_INSIDE_BADGES", ""))

# Continuity window for perimeter classes, in minutes. Dwell means CONTINUOUS
# presence: if a perimeter object is not seen for this long, it has left, and
# its next sighting starts a fresh dwell timer (and can alert again).
# Kept separate from TRINETRA_SESSION_GAP_MINUTES so legacy badge classes keep
# their own behaviour. Must be longer than the edge's reporting interval.
PERIMETER_GAP_MINUTES = int(os.environ.get("TRINETRA_PERIMETER_GAP_MINUTES", "5"))

# Minimum seconds between two breach alerts for the same badge+camera.
# 0 = alert on every single detection (literal "instant" behaviour).
# Raise it if the edge writes many frames per second and Telegram floods.
BREACH_COOLDOWN_SECONDS = int(os.environ.get("TRINETRA_BREACH_COOLDOWN", "0"))


def zone_for(badge: str) -> str:
    """Which perimeter zone does this badge class belong to?"""
    key = normalize_badge(badge)
    if key in BREACH_BADGES:
        return ZONE_BREACH
    if key in DWELL_RULES or is_counted_outside(badge):
        return ZONE_OUTSIDE
    if key in INSIDE_BADGES:
        return ZONE_INSIDE
    return ZONE_UNKNOWN


def zone_label(badge: str) -> str:
    return ZONE_LABELS.get(zone_for(badge), ZONE_LABELS[ZONE_UNKNOWN])


def is_breach_badge(badge: str) -> bool:
    """True for classes that must alert the moment they are seen."""
    return normalize_badge(badge) in BREACH_BADGES


def is_perimeter_class(badge: str) -> bool:
    """Breach or outside-perimeter class — governed by continuous presence."""
    return zone_for(badge) in (ZONE_BREACH, ZONE_OUTSIDE)


def session_gap_minutes_for(badge: str) -> int:
    """How long a badge may go unseen before its presence is considered over."""
    return PERIMETER_GAP_MINUTES if is_perimeter_class(badge) else SESSION_GAP_MINUTES


def dwell_minutes_for(badge: str):
    """Dwell threshold in minutes, or None if this badge never dwell-alerts.

    Outside-perimeter classes use their own rule; inside-factory classes are
    silent; everything else falls back to the global loitering threshold.
    """
    key = normalize_badge(badge)
    if key in BREACH_BADGES:
        return None                      # handled instantly, never by dwell
    if key in DWELL_RULES:
        return DWELL_RULES[key]
    if is_counted_outside(badge):
        # A counted class inherits the outside-person rule.
        return DWELL_RULES.get("outside-person", 10)
    if key in INSIDE_BADGES:
        return None                      # tracked, no alarm
    return LOITER_MINUTES


# ─── IMAGE CLEANUP ────────────────────────────────────────────────────────────
# Camera images are kept this many days (today counts as one); older days are deleted at night.
# 0 = keep forever. The detections / alerts stay in the database either way.
IMAGE_RETENTION_DAYS = int(os.environ.get("TRINETRA_IMAGE_RETENTION_DAYS", "30"))
# Disk guard: under this much free space the oldest day of images is deleted first (never today), at any hour.
DISK_MIN_FREE_GB = float(os.environ.get("TRINETRA_DISK_MIN_FREE_GB", "15"))
# Nightly database backup on this device (HH:MM, empty = off) and how many daily copies are kept.
BACKUP_AT   = os.environ.get("TRINETRA_BACKUP_AT", "23:50")
BACKUP_KEEP = int(os.environ.get("TRINETRA_BACKUP_KEEP", "7"))

# ─── ACCOUNTS & SESSIONS (access.py) ──────────────────────────────────────────
# The Trinetra team: comma-separated user IDs, all signing in with SUPPORT_PASSWORD (empty = team login off).
# Admin accounts are made by the team on the Admin Accounts page; admins make users on the Users page.
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "trinetra")
SUPPORT_PASSWORD = os.environ.get("SUPPORT_PASSWORD", "")
# Session lifetime (hours): users + Trinetra team, admin accounts.
SESSION_HOURS       = int(os.environ.get("TRINETRA_SESSION_HOURS", "12"))
ADMIN_SESSION_HOURS = int(os.environ.get("TRINETRA_ADMIN_SESSION_HOURS", "24"))
# A session with no request for this many minutes is signed out (an open Live page keeps it alive).
IDLE_MINUTES = int(os.environ.get("TRINETRA_IDLE_MINUTES", "60"))
# Activity Logs (Trinetra team): rows older than this many days are removed. 0 = keep forever.
AUDIT_LOG_DAYS = int(os.environ.get("TRINETRA_AUDIT_RETENTION_DAYS", "365"))
# The client's address of this dashboard (sign-in links on Admin Accounts), e.g. https://<device>.ts.net:8443
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip()
# One line per web request in the log (off: the Live page polls every few seconds).
ACCESS_LOG = os.environ.get("TRINETRA_ACCESS_LOG", "0") == "1"

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
    print(f"[config] BREACH_BADGES = {sorted(BREACH_BADGES)} (instant alert)")
    print(f"[config] DWELL_RULES   = " +
          ", ".join(f"{b}:{m}min" for b, m in sorted(DWELL_RULES.items())))
    print(f"[config] INSIDE_BADGES = {sorted(INSIDE_BADGES) or '(none)'} (tracked, no alarm)")
    print(f"[config] BREACH_COOL   = {BREACH_COOLDOWN_SECONDS}s")
    print(f"[config] PERIM_GAP     = {PERIMETER_GAP_MINUTES} min (continuous-presence window)")
    print(f"[config] ENV_FILE      = {ENV_FILE}")
    print(f"[config] IMAGES KEPT   = {'forever' if IMAGE_RETENTION_DAYS <= 0 else f'{IMAGE_RETENTION_DAYS} days'} · disk guard under {DISK_MIN_FREE_GB:g} GB free")
    print(f"[config] BACKUP        = {BACKUP_AT or 'off'} (keep {BACKUP_KEEP})")
    print(f"[config] TEAM LOGIN    = {'on' if SUPPORT_PASSWORD else 'OFF (no SUPPORT_PASSWORD)'} · {len([u for u in SUPPORT_USERNAME.split(',') if u.strip()])} ID(s)")
    print(f"[config] SESSIONS      = idle {IDLE_MINUTES} min · users {SESSION_HOURS} h · admins {ADMIN_SESSION_HOURS} h")
    print(f"[config] ACTIVITY LOG  = keep {AUDIT_LOG_DAYS or 'forever'} days")
