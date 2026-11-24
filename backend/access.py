"""Trinetra Perimeter — sign-in, accounts and Activity Logs (the Workplace Analytics access model).

Who signs in:
  · the Trinetra team  — the IDs in SUPPORT_USERNAME (comma-separated), all with SUPPORT_PASSWORD from the settings
                         file; sees everything incl. Settings, Admin Accounts and Activity Logs
  · admin accounts     — made by the Trinetra team (Admin Accounts page); the password is also kept sealed so the
                         team can look it up again; an admin sees every client page and manages users
  · users              — made by an admin (Users page) with the permissions the admin picks

Kept from the perimeter VAPT hardening (security.py): PBKDF2 hashes, the password policy, sign-in throttling,
server-side sessions (only a SHA-256 of the token is stored) with an idle timeout, deny by default.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import config
import security

SESSION_COOKIE = "trn_session"

# (key, label, help) — what an admin can grant a user
PERMISSIONS = [
    ("live", "Live dashboard", "Live alerts, who is outside the perimeter now"),
    ("logs", "Logs & Charts", "Detection history, charts and visits"),
    ("manage_alerts", "Acknowledge alerts", "Acknowledge alerts and change the whitelist"),
    ("export", "Downloads", "Download detection logs as CSV"),
]
PERMISSION_KEYS = [k for k, _, _ in PERMISSIONS]
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
RESERVED_IDS = {"admin", "support", "trinetra", "root", "system"}

# Every page and what it needs (None = public). Pages not listed are refused.
PAGE_NEED = {
    "/login.html": None,
    "/": ("live",), "/index.html": ("live",), "/whitelist.html": ("live",),
    "/logs.html": ("logs",), "/visits.html": ("logs",),
    "/users.html": "admin",
    "/admins.html": "support", "/activity.html": "support", "/admin.html": "support", "/deepstream.html": "support",
}
PAGE_LABELS = {"/": "Live", "/index.html": "Live", "/whitelist.html": "Whitelist", "/logs.html": "Logs & Charts",
               "/visits.html": "Visits", "/users.html": "Users", "/admins.html": "Admin Accounts",
               "/activity.html": "Activity Logs", "/admin.html": "Settings",
               "/deepstream.html": "DeepStream"}
# where a signed-in person lands: the first page they may open
PAGE_ORDER = ["/index.html", "/logs.html", "/users.html", "/admin.html"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ════════════════════════════════════════════════════════════════════════════
# SCHEMA
# ════════════════════════════════════════════════════════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name          TEXT    NOT NULL DEFAULT '',
    password_hash TEXT    NOT NULL,                -- security.hash_password (PBKDF2-SHA256)
    permissions   TEXT    NOT NULL DEFAULT '[]',   -- JSON list of PERMISSION_KEYS (admins: all)
    active        INTEGER NOT NULL DEFAULT 1,
    role          TEXT    NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    created_by    TEXT,
    last_login    TEXT,
    password_enc  TEXT                             -- admin accounts: the password sealed for the Trinetra team
);
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash  TEXT    PRIMARY KEY,               -- SHA-256 of the cookie (the cookie itself is never stored)
    role        TEXT    NOT NULL,                  -- user | admin | support
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    username    TEXT    NOT NULL,
    created     TEXT    NOT NULL,
    expires     TEXT    NOT NULL,
    last_seen   TEXT    NOT NULL,
    ip          TEXT
);
CREATE INDEX IF NOT EXISTS idx_websess_user ON web_sessions(user_id);
CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,                  -- local time, ISO
    actor       TEXT    NOT NULL,                  -- user ID (a refused sign-in: the ID that was typed)
    actor_role  TEXT    NOT NULL,                  -- user | admin | support | anonymous | system
    category    TEXT    NOT NULL,                  -- sign-in | account | alerts | whitelist | settings | data | pages | reports | other
    action      TEXT    NOT NULL,
    target      TEXT,
    ip          TEXT,
    ok          INTEGER NOT NULL DEFAULT 1         -- 0 = refused
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(ts);
"""


class Access:
    def __init__(self, db: Callable[[], sqlite3.Connection], db_lock):
        self.db, self.lock = db, db_lock
        self.limiter = security.LoginLimiter()
        self._audit_n = 0
        self._page_seen: dict = {}
        self._cred_key: Optional[bytes] = None

    def init_schema(self, conn: sqlite3.Connection):
        conn.executescript(SCHEMA)

    # ─── team ───────────────────────────────────────────────────────────────
    @staticmethod
    def team_ids() -> list:
        return [u.strip() for u in config.SUPPORT_USERNAME.split(",") if u.strip()]

    def team_member(self, username: str) -> Optional[str]:
        return next((u for u in self.team_ids() if u.lower() == username.lower()), None)

    # ─── sealed admin passwords (HMAC-SHA256 keystream + tag; key file next to the database) ───────────────────
    def _key(self) -> bytes:
        if self._cred_key is None:
            f = Path(config.DATA_DIR) / ".cred_key"
            if not f.exists():
                f.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(secrets.token_bytes(32))
            self._cred_key = f.read_bytes()[:32]
        return self._cred_key

    @staticmethod
    def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
        return b"".join(hmac.new(key, b"enc" + nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
                        for i in range(n // 32 + 1))[:n]

    def seal(self, pw: str) -> str:
        key, nonce, data = self._key(), secrets.token_bytes(16), pw.encode()
        ct = bytes(a ^ b for a, b in zip(data, self._stream(key, nonce, len(data))))
        tag = hmac.new(key, b"mac" + nonce + ct, hashlib.sha256).digest()[:16]
        return "v1$" + base64.urlsafe_b64encode(nonce + ct + tag).decode()

    def unseal(self, tok: Optional[str]) -> Optional[str]:
        if not tok or not tok.startswith("v1$"):
            return None
        try:
            raw, key = base64.urlsafe_b64decode(tok[3:]), self._key()
            nonce, ct, tag = raw[:16], raw[16:-16], raw[-16:]
            if not hmac.compare_digest(tag, hmac.new(key, b"mac" + nonce + ct, hashlib.sha256).digest()[:16]):
                return None
            return bytes(a ^ b for a, b in zip(ct, self._stream(key, nonce, len(ct)))).decode()
        except Exception:
            return None

    _PW_CHARS = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/l/I

    @classmethod
    def generate_password(cls) -> str:
        """e.g. 'k7Rm2-xQp9W-4hTzc' (~87 bits)"""
        return "-".join("".join(secrets.choice(cls._PW_CHARS) for _ in range(5)) for _ in range(3))

    # ─── sessions ───────────────────────────────────────────────────────────
    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def session_hours(self, role: str) -> int:
        return config.ADMIN_SESSION_HOURS if role == "admin" else config.SESSION_HOURS

    def create_session(self, role: str, user_id: Optional[int], username: str, ip: str) -> tuple:
        token, now = secrets.token_urlsafe(32), datetime.now()
        hours = self.session_hours(role)
        conn = self.db()
        try:
            with self.lock, conn:
                conn.execute("DELETE FROM web_sessions WHERE expires<?", (_now(),))
                conn.execute("INSERT INTO web_sessions(token_hash,role,user_id,username,created,expires,last_seen,ip) "
                             "VALUES(?,?,?,?,?,?,?,?)",
                             (self._hash(token), role, user_id, username, now.isoformat(timespec="seconds"),
                              (now + timedelta(hours=hours)).isoformat(timespec="seconds"),
                              now.isoformat(timespec="seconds"), ip[:64]))
                if user_id:
                    conn.execute("UPDATE users SET last_login=? WHERE id=?", (now.isoformat(timespec="seconds"), user_id))
        finally:
            conn.close()
        return token, hours * 3600

    def delete_session(self, token: Optional[str]):
        if not token:
            return
        conn = self.db()
        try:
            with self.lock, conn:
                conn.execute("DELETE FROM web_sessions WHERE token_hash=?", (self._hash(token),))
        finally:
            conn.close()

    def delete_user_sessions(self, conn, user_id: int, keep_token: Optional[str] = None):
        conn.execute("DELETE FROM web_sessions WHERE user_id=? AND token_hash<>?",
                     (user_id, self._hash(keep_token) if keep_token else ""))

    def identity(self, token: Optional[str]) -> dict:
        """Who is calling. No / unknown / expired / idle cookie, or a disabled / deleted account = anonymous."""
        anon = {"role": "anonymous", "name": "", "username": "", "permissions": []}
        if not token:
            return anon
        h, now = self._hash(token), datetime.now()
        conn = self.db()
        try:
            s = conn.execute("SELECT * FROM web_sessions WHERE token_hash=?", (h,)).fetchone()
            if not s:
                return {**anon, "expired": True}
            idle = now - datetime.fromisoformat(s["last_seen"]) > timedelta(minutes=config.IDLE_MINUTES)
            if now.isoformat() > s["expires"] or idle:
                with self.lock, conn:
                    conn.execute("DELETE FROM web_sessions WHERE token_hash=?", (h,))
                return {**anon, "expired": True}
            ident = None
            if s["role"] == "support" and self.team_member(s["username"]) and config.SUPPORT_PASSWORD:
                ident = {"role": "support", "name": "Trinetra team", "username": s["username"], "permissions": PERMISSION_KEYS}
            elif s["role"] in ("admin", "user"):
                u = conn.execute("SELECT * FROM users WHERE id=? AND active=1 AND role=?", (s["user_id"], s["role"])).fetchone()
                if u:
                    perms = PERMISSION_KEYS if u["role"] == "admin" else \
                        [p for p in json.loads(u["permissions"] or "[]") if p in PERMISSION_KEYS]
                    ident = {"role": u["role"], "name": u["name"] or u["username"], "username": u["username"],
                             "user_id": u["id"], "permissions": perms}
            if not ident:
                return {**anon, "expired": True}
            # last_seen at most once a minute: no database write on every poll
            if now - datetime.fromisoformat(s["last_seen"]) > timedelta(seconds=60):
                with self.lock, conn:
                    conn.execute("UPDATE web_sessions SET last_seen=? WHERE token_hash=?", (now.isoformat(timespec="seconds"), h))
            return ident
        finally:
            conn.close()

    # ─── who may open what ───────────────────────────────────────────────────
    @staticmethod
    def route_permission(method: str, path: str):
        """None = public · 'any' = signed in · a tuple of permission keys (any one of them) · 'admin' · 'support'.
        Deny by default: anything not listed needs the Trinetra team."""
        if path in PAGE_NEED:
            return PAGE_NEED[path]
        if path.startswith(("/css/", "/assets/", "/img/", "/fonts/")) or path in ("/favicon.ico", "/js/login.js"):
            return None
        if path.startswith("/js/"):
            return "any"                                   # page scripts: signed in (no data in them)
        if path.endswith(".html"):
            return "support"
        if path in ("/api/login", "/api/logout", "/api/me"):
            return None
        if path.startswith("/images/"):
            return ("live", "logs")
        if path == "/ws":
            return ("live",)
        if not path.startswith("/api/"):
            return "support"
        if path == "/api/me/password":
            return "any"
        if path in ("/api/stats", "/api/perimeter/rules", "/api/jetsons/status"):
            return "any"                                   # the top bar of every page
        if path.startswith("/api/users"):
            return "admin"
        if path.startswith(("/api/admins", "/api/audit", "/api/admin/")):
            return "support"
        if path == "/api/detections/export":
            return ("export",)
        if path in ("/api/detections/summary",) or path.startswith("/api/visits"):
            return ("logs",)
        if path in ("/api/sessions/active", "/api/alerts/recent"):
            return ("live",)
        if re.fullmatch(r"/api/alerts/\d+/ack", path) and method == "POST":
            return ("manage_alerts",)
        if path.startswith("/api/whitelist"):
            return ("live", "logs") if method == "GET" else ("manage_alerts",)
        if path == "/api/detections" or path.startswith(("/api/badges", "/api/sessions/")):
            return ("live", "logs")
        return "support"

    @staticmethod
    def allowed(ident: dict, need) -> bool:
        role = ident["role"]
        if need is None or role == "support":
            return True
        if role == "anonymous" or need == "support":
            return False
        if role == "admin":
            return True
        if need == "admin":
            return False
        if need == "any":
            return True
        return any(p in ident["permissions"] for p in need)

    def first_page(self, ident: dict) -> Optional[str]:
        return next((p for p in PAGE_ORDER if self.allowed(ident, PAGE_NEED[p])), None)

    # ─── Activity Logs ───────────────────────────────────────────────────────
    def audit(self, actor: str, role: str, category: str, action: str, target: str = "", ip: str = "", ok: bool = True):
        """One row per sign-in, change, page opened or download. Never raises."""
        try:
            conn = self.db()
            try:
                with self.lock, conn:
                    conn.execute("INSERT INTO activity_log(ts,actor,actor_role,category,action,target,ip,ok) "
                                 "VALUES(?,?,?,?,?,?,?,?)",
                                 (_now(), (actor or "")[:64], role, category, action[:120], (target or "")[:200],
                                  (ip or "")[:64], int(ok)))
                    self._audit_n += 1
                    if self._audit_n % 500 == 1 and config.AUDIT_LOG_DAYS > 0:
                        conn.execute("DELETE FROM activity_log WHERE ts<?",
                                     ((datetime.now() - timedelta(days=config.AUDIT_LOG_DAYS)).isoformat(),))
            finally:
                conn.close()
        except Exception as e:
            print(f"[audit] not written: {e!r}")

    def page_opened(self, ident: dict, path: str) -> bool:
        """a page opened again by the same person within 10 minutes is one row"""
        k, now = (ident.get("username"), path), time.monotonic()
        if now - self._page_seen.get(k, -1e9) <= 600:
            return False
        if len(self._page_seen) > 5000:
            self._page_seen.clear()
        self._page_seen[k] = now
        return True

    def prune(self) -> int:
        if config.AUDIT_LOG_DAYS <= 0:
            return 0
        conn = self.db()
        try:
            with self.lock, conn:
                n = conn.execute("DELETE FROM activity_log WHERE ts<?",
                                 ((datetime.now() - timedelta(days=config.AUDIT_LOG_DAYS)).isoformat(),)).rowcount
        finally:
            conn.close()
        if n:
            print(f"[audit] removed {n} activity-log rows older than {config.AUDIT_LOG_DAYS} days")
        return n


# (method, path pattern, category, action); group 1 of the pattern = what it was done to (an id is resolved to a
# name before the change, so a deleted row is still named)
AUDIT_RULES = [(m, re.compile(p + "$"), c, a) for m, p, c, a in [
    ("POST", r"/api/users", "account", "Created user"), ("PUT", r"/api/users/(\d+)", "account", "Edited user"),
    ("DELETE", r"/api/users/(\d+)", "account", "Deleted user"),
    ("POST", r"/api/admins", "account", "Created admin account"), ("PUT", r"/api/admins/(\d+)", "account", "Edited admin account"),
    ("POST", r"/api/admins/(\d+)/reset-password", "account", "Set admin password"),
    ("DELETE", r"/api/admins/(\d+)", "account", "Deleted admin account"),
    ("POST", r"/api/alerts/(\d+)/ack", "alerts", "Acknowledged an alert"),
    ("DELETE", r"/api/whitelist/([^/]+)", "whitelist", "Removed from whitelist"),
    ("DELETE", r"/api/admin/database/([^/]+/[^/]+)", "data", "Deleted a record"),
    ("DELETE", r"/api/admin/database/([^/]+)", "data", "Cleared a table"),
    ("POST", r"/api/admin/reset", "data", "Deleted all detection data"),
    ("POST", r"/api/ds/(start|stop|build)", "pipeline", "DeepStream"),
    ("PUT", r"/api/ds/config", "pipeline", "Changed cameras / pipeline settings"),
    ("PUT", r"/api/ds/zones/([^/]+)", "pipeline", "Drew perimeter zones"),
]]


def audit_rule(method: str, path: str):
    for m, rx, cat, act in AUDIT_RULES:
        if m == method:
            g = rx.fullmatch(path)
            if g:
                return cat, act, (g.group(1) if g.groups() else "")
    return None
