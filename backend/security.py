"""Trinetra Perimeter — security primitives.

Passwords · sign-in throttling · server-side sessions · request guards.
Everything here is deliberately framework-light so it can be unit-tested.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from typing import Optional

# ─── Passwords: PBKDF2-HMAC-SHA256, unique random salt per password ──────────
PBKDF2_ITERATIONS = 200_000
PASSWORD_MIN, PASSWORD_MAX = 12, 128

# Passwords that must never be accepted, whatever their length. Compared
# case-insensitively, with the product's own names folded in.
_COMMON = {
    "password", "password1", "password12", "password123", "password1234", "password12345",
    "passw0rd", "p@ssw0rd", "p@ssword", "123456789012", "1234567890123", "12345678901234",
    "qwertyuiop", "qwertyuiop12", "qwerty123456", "1q2w3e4r5t6y", "1qaz2wsx3edc", "zaq12wsxcde3",
    "iloveyou1234", "letmein12345", "welcome12345", "welcome@1234", "admin1234567", "admin@123456",
    "administrator", "administrator1", "changeme1234", "abcdefghijkl", "abc123456789", "aaaaaaaaaaaa",
    "trinetra1234", "trinetra@123", "trinetra@1234", "trinetrasystems", "perimeter123", "perimeter1234",
    "security1234", "security@123", "india@123456", "monkey123456", "football1234", "sunshine1234",
    "princess1234", "dragon123456", "master123456", "superman1234", "baseball1234", "starwars1234",
}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: Optional[str]) -> bool:
    """Constant-time check. A missing/garbled hash still costs one PBKDF2 run,
    so an unknown account answers as slowly as a wrong password."""
    try:
        algo, iters, salt_b64, dk_b64 = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            raise ValueError
        salt, expected, n = base64.b64decode(salt_b64), base64.b64decode(dk_b64), int(iters)
    except (ValueError, TypeError):
        salt, expected, n = b"\0" * 16, b"\0" * 32, PBKDF2_ITERATIONS
        hashlib.pbkdf2_hmac("sha256", password.encode(), salt, n)
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, n)
    return hmac.compare_digest(dk, expected)


def password_problem(password: str, user_id: str = "") -> Optional[str]:
    """None if acceptable, else a short reason shown to the user."""
    if not isinstance(password, str):
        return "Password is required"
    if len(password) < PASSWORD_MIN:
        return f"Password must be at least {PASSWORD_MIN} characters"
    if len(password) > PASSWORD_MAX:
        return f"Password must be at most {PASSWORD_MAX} characters"
    low = password.lower()
    if low in _COMMON or len(set(low)) <= 3:
        return "That password is too common — choose another"
    uid = (user_id or "").lower()
    for part in {uid, uid.split("@")[0]}:
        if len(part) >= 3 and part in low:
            return "Password must not contain the user ID"
    return None


USER_ID_RE = re.compile(r"^[A-Za-z0-9._@-]{3,64}$")
ROLES = ("admin", "operator")


# ─── Sign-in throttling ───────────────────────────────────────────────────────
class LoginLimiter:
    """5 failures for one user ID, or 20 from one address, inside the window
    lock further sign-in for that user ID / address for LOCK_SECONDS."""
    PER_USER, PER_IP = 5, 20
    WINDOW_SECONDS = LOCK_SECONDS = 15 * 60

    def __init__(self):
        self._fails: dict = {}     # key → [timestamps]
        self._locked: dict = {}    # key → unlock time
        self._lock = threading.Lock()

    def _prune(self, key, now):
        self._fails[key] = [t for t in self._fails.get(key, []) if now - t < self.WINDOW_SECONDS]

    def locked(self, user_id: str, ip: str) -> bool:
        now = time.time()
        with self._lock:
            for key in (f"u:{user_id}", f"ip:{ip}"):
                until = self._locked.get(key)
                if until and until > now:
                    return True
                if until:
                    self._locked.pop(key, None)
        return False

    def fail(self, user_id: str, ip: str) -> bool:
        """Record a failure; return True if this failure caused a lock."""
        now, newly = time.time(), False
        with self._lock:
            for key, limit in ((f"u:{user_id}", self.PER_USER), (f"ip:{ip}", self.PER_IP)):
                self._prune(key, now)
                self._fails[key].append(now)
                if len(self._fails[key]) >= limit and key not in self._locked:
                    self._locked[key] = now + self.LOCK_SECONDS
                    newly = True
        return newly

    def success(self, user_id: str):
        with self._lock:
            self._fails.pop(f"u:{user_id}", None)


# ─── Server-side sessions ─────────────────────────────────────────────────────
class SessionStore:
    """256-bit random tokens. Only a SHA-256 of each token is kept in memory,
    so a memory dump does not yield usable cookies. Absolute lifetime plus an
    idle timeout; deleted on sign-out."""

    def __init__(self, idle_minutes_fn, lifetime_hours_fn):
        self._s: dict = {}
        self._lock = threading.Lock()
        self._idle = idle_minutes_fn
        self._life = lifetime_hours_fn

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, user_id: str, role: str, ip: str = "", user_agent: str = "") -> tuple:
        token = secrets.token_urlsafe(32)
        now = time.time()
        max_age = int(self._life(role) * 3600)
        with self._lock:
            self._s[self._key(token)] = {"user_id": user_id, "role": role, "created": now,
                                         "expires": now + max_age, "last_seen": now,
                                         "ip": ip, "user_agent": user_agent[:300]}
        return token, max_age

    def active(self) -> list:
        """Sessions still valid right now (no tokens), most recently active first."""
        now, out = time.time(), []
        with self._lock:
            for k in list(self._s):
                s = self._s[k]
                if now > s["expires"] or now - s["last_seen"] > self._idle() * 60:
                    self._s.pop(k, None)
                    continue
                out.append({kk: s.get(kk) for kk in ("user_id", "role", "created", "last_seen", "ip", "user_agent")})
        return sorted(out, key=lambda x: -x["last_seen"])

    def get(self, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        k, now = self._key(token), time.time()
        with self._lock:
            s = self._s.get(k)
            if not s:
                return None
            if now > s["expires"] or now - s["last_seen"] > self._idle() * 60:
                self._s.pop(k, None)
                return None
            s["last_seen"] = now
            return dict(s)

    def delete(self, token: Optional[str]):
        if token:
            with self._lock:
                self._s.pop(self._key(token), None)

    def delete_user(self, user_id: str, keep_token: Optional[str] = None):
        keep = self._key(keep_token) if keep_token else None
        with self._lock:
            for k in [k for k, v in self._s.items() if v["user_id"] == user_id and k != keep]:
                self._s.pop(k, None)


# ─── Request helpers ─────────────────────────────────────────────────────────
class BadRequest(Exception):
    """Raised for unparseable or wrongly-typed request bodies."""


def parse_json_object(raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise BadRequest()
    if not isinstance(data, dict):
        raise BadRequest()
    return data


def has_control_chars(s: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in s)


def security_headers(is_https: bool) -> dict:
    h = {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data: blob:; "
            "connect-src 'self' ws: wss:; media-src 'self' blob:; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'self'"),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
        "Referrer-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    }
    if is_https:
        h["Strict-Transport-Security"] = "max-age=31536000"
    return h
