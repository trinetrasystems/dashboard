"""Trinetra — user-scoped push notifications over Firebase Cloud Messaging.

Deliberately transport-agnostic and free of any FastAPI/web coupling, so the
same service can back the web dashboard today and a React Native app later:
a device is just (user_id, fcm_token, platform), and platform is opaque here.

    NotificationService.send_to_user("TRN_1001", {
        "title": "Vehicle Movement Detected",
        "body":  "CAR-KA01AB1234 moved from Washing into Delivery",
        "data":  {"eventType": "BAY_MOVEMENT", "eventId": "MOV_42"},
    })

If Firebase credentials are absent the service runs in LOG-ONLY mode: every
call is validated, resolved to real tokens and recorded in notification_log,
but nothing is transmitted. That keeps the whole pipeline testable before the
service-account key exists.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Optional

import config

# ─── Firebase Admin SDK (optional at import time) ────────────────────────────
try:
    import firebase_admin
    from firebase_admin import credentials as _fb_credentials, messaging as _fb_messaging
    _HAS_FIREBASE = True
except ImportError:                                     # pragma: no cover
    firebase_admin = None
    _fb_messaging = None
    _HAS_FIREBASE = False

_fb_app = None
_fb_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ════════════════════════════════════════════════════════════════════════════
# PASSCODES — stdlib PBKDF2, no new dependency
# ════════════════════════════════════════════════════════════════════════════

def hash_passcode(passcode: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", passcode.encode(), bytes.fromhex(salt), 120_000)
    return f"pbkdf2$120000${salt}${dk.hex()}"


def verify_passcode(passcode: str, stored: Optional[str]) -> bool:
    if not stored:
        return False
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", passcode.encode(), bytes.fromhex(salt), int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), digest)      # constant-time


# ════════════════════════════════════════════════════════════════════════════
# USERS
# ════════════════════════════════════════════════════════════════════════════

def list_users() -> list[dict]:
    """Every user with their devices attached — the admin panel renders both,
    so returning counts alone would leave the device table permanently empty."""
    conn = _db()
    try:
        rows = conn.execute("""
            SELECT u.*,
                   (SELECT COUNT(*) FROM user_devices d
                     WHERE d.user_id = u.user_id AND d.is_active = 1) AS active_devices,
                   (SELECT COUNT(*) FROM user_devices d
                     WHERE d.user_id = u.user_id) AS total_devices
            FROM users u ORDER BY u.user_id
        """).fetchall()
        devices = {}
        for d in conn.execute(
            "SELECT id, user_id, platform, browser, device_id, is_active, created_at, "
            "       last_seen_at, substr(fcm_token, 1, 12) || '…' AS token_preview "
            "FROM user_devices ORDER BY last_seen_at DESC"
        ).fetchall():
            devices.setdefault(d["user_id"], []).append(dict(d))

        out = []
        for r in rows:
            u = _user_dict(r)
            u["devices"] = devices.get(u["user_id"], [])
            out.append(u)
        return out
    finally:
        conn.close()


def get_user(user_id: str) -> Optional[dict]:
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return None
        user = _user_dict(row)
        user["devices"] = [dict(d) for d in conn.execute(
            "SELECT id, platform, browser, device_id, is_active, created_at, last_seen_at, "
            "       substr(fcm_token, 1, 12) || '…' AS token_preview "
            "FROM user_devices WHERE user_id=? ORDER BY last_seen_at DESC", (user_id,)
        ).fetchall()]
        return user
    finally:
        conn.close()


def _user_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d.pop("passcode_hash", None)                       # never leaves the backend
    d["notification_enabled"] = bool(d.get("notification_enabled"))
    d["has_passcode"] = bool(r["passcode_hash"]) if "passcode_hash" in r.keys() else False
    return d


def upsert_user(user_id: str, name: str = "", email: str = "",
                passcode: Optional[str] = None,
                notification_enabled: Optional[bool] = None) -> dict:
    """Create or update a notification user. Only fields provided are changed."""
    user_id = user_id.strip()
    if not user_id:
        raise ValueError("userId required")

    conn = _db()
    try:
        with conn:
            existing = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO users(user_id, name, email, passcode_hash, notification_enabled) "
                    "VALUES (?,?,?,?,?)",
                    (user_id, name, email,
                     hash_passcode(passcode) if passcode else None,
                     1 if notification_enabled is not False else 0)
                )
            else:
                sets, params = [], []
                if name:
                    sets.append("name=?"); params.append(name)
                if email:
                    sets.append("email=?"); params.append(email)
                if passcode:
                    sets.append("passcode_hash=?"); params.append(hash_passcode(passcode))
                if notification_enabled is not None:
                    sets.append("notification_enabled=?"); params.append(1 if notification_enabled else 0)
                if sets:
                    sets.append("updated_at=CURRENT_TIMESTAMP")
                    params.append(user_id)
                    conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id=?", params)
    finally:
        conn.close()
    return get_user(user_id)


def rename_user(old_id: str, new_id: str) -> dict:
    """Change a userId, carrying its devices, sessions and history with it.

    user_devices/user_sessions/notification_log reference the user by its
    string id, so all four tables move together inside one transaction —
    otherwise a rename would orphan every registered device.
    """
    old_id, new_id = old_id.strip(), new_id.strip()
    if not new_id:
        raise ValueError("new userId required")
    if old_id == new_id:
        return get_user(old_id)

    conn = _db()
    try:
        if conn.execute("SELECT 1 FROM users WHERE user_id=?", (new_id,)).fetchone():
            raise ValueError(f"{new_id} already exists")
        if not conn.execute("SELECT 1 FROM users WHERE user_id=?", (old_id,)).fetchone():
            raise ValueError(f"{old_id} not found")
        with conn:
            conn.execute("UPDATE users            SET user_id=?, updated_at=CURRENT_TIMESTAMP "
                         "WHERE user_id=?", (new_id, old_id))
            conn.execute("UPDATE user_devices     SET user_id=? WHERE user_id=?", (new_id, old_id))
            conn.execute("UPDATE user_sessions    SET user_id=? WHERE user_id=?", (new_id, old_id))
            conn.execute("UPDATE notification_log SET user_id=? WHERE user_id=?", (new_id, old_id))
    finally:
        conn.close()
    print(f"[push] renamed user {old_id} → {new_id} (devices and history carried over)")
    return get_user(new_id)


def delete_user(user_id: str) -> dict:
    conn = _db()
    try:
        with conn:
            devices = conn.execute("DELETE FROM user_devices WHERE user_id=?", (user_id,)).rowcount
            conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
            users = conn.execute("DELETE FROM users WHERE user_id=?", (user_id,)).rowcount
        return {"deleted_user": users, "deleted_devices": devices}
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════════
# SESSIONS — DB-backed so a restart doesn't break token refresh
# ════════════════════════════════════════════════════════════════════════════

def login(user_id: str, passcode: str) -> Optional[str]:
    """Return a fresh session token, or None if credentials are wrong."""
    conn = _db()
    try:
        row = conn.execute("SELECT passcode_hash FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row or not verify_passcode(passcode, row["passcode_hash"]):
            return None
        token = secrets.token_hex(32)
        expires = datetime.utcnow() + timedelta(days=config.USER_SESSION_DAYS)
        with conn:
            conn.execute("DELETE FROM user_sessions WHERE expires_at < ?",
                         (datetime.utcnow().isoformat(),))
            conn.execute("INSERT INTO user_sessions(token, user_id, expires_at) VALUES (?,?,?)",
                         (token, user_id, expires.isoformat()))
        return token
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════════
# FIREBASE AUTH SIGN-IN — one login for web dashboard, website and mobile app
#
# The client authenticates with Firebase (as the website and mobile app already
# do), then hands us the resulting ID token. We verify it with the same service
# account used for sending, so no second password exists anywhere.
# ════════════════════════════════════════════════════════════════════════════

def _slug_from_email(email: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_\-]", "_", (email or "").split("@")[0])[:32]
    return base or "USER"


def verify_firebase_token(id_token: str) -> Optional[dict]:
    """Validate a Firebase ID token. Returns its claims, or None if invalid.

    Requires the service account (same file used for sending) — without it we
    cannot verify signatures, and accepting an unverified token would let
    anyone impersonate any user.
    """
    if not _HAS_FIREBASE or not id_token:
        return None
    if _firebase() is None:
        return None
    try:
        from firebase_admin import auth as _fb_auth
        return _fb_auth.verify_id_token(id_token)
    except Exception as e:
        print(f"[push] firebase token rejected: {e}")
        return None


def login_with_firebase(id_token: str) -> Optional[tuple[str, str]]:
    """Exchange a Firebase ID token for a Trinetra session.

    Returns (session_token, user_id) or None.

    Resolution order:
      1. a user already linked to this firebase_uid
      2. a user with the same email but no link yet  → link it (lets an admin
         pre-create TRN_1001 with an email and have it adopt the account)
      3. otherwise auto-provision a new user
    """
    claims = verify_firebase_token(id_token)
    if not claims:
        return None

    uid = claims.get("uid") or claims.get("user_id")
    email = (claims.get("email") or "").strip()
    name = (claims.get("name") or "").strip()
    if not uid:
        return None

    conn = _db()
    try:
        row = conn.execute("SELECT user_id FROM users WHERE firebase_uid=?", (uid,)).fetchone()

        if not row and email:
            row = conn.execute(
                "SELECT user_id FROM users WHERE lower(email)=lower(?) "
                "AND (firebase_uid IS NULL OR firebase_uid='')", (email,)
            ).fetchone()
            if row:
                with conn:
                    conn.execute("UPDATE users SET firebase_uid=?, updated_at=CURRENT_TIMESTAMP "
                                 "WHERE user_id=?", (uid, row["user_id"]))
                print(f"[push] linked Firebase account to existing user {row['user_id']}")

        if not row:
            if not config.FCM_AUTO_PROVISION:
                print(f"[push] no Trinetra user for firebase uid {uid} and auto-provision is off")
                return None
            # Human-readable id derived from the email, uniquified if needed
            base = _slug_from_email(email) if email else f"USER_{uid[:8]}"
            candidate, n = base, 1
            while conn.execute("SELECT 1 FROM users WHERE user_id=?", (candidate,)).fetchone():
                n += 1
                candidate = f"{base}_{n}"
            with conn:
                conn.execute(
                    "INSERT INTO users(user_id, name, email, firebase_uid, notification_enabled) "
                    "VALUES (?,?,?,?,1)", (candidate, name, email, uid))
            print(f"[push] auto-provisioned notification user {candidate} for {email or uid}")
            row = {"user_id": candidate}

        user_id = row["user_id"]
        token = secrets.token_hex(32)
        expires = datetime.utcnow() + timedelta(days=config.USER_SESSION_DAYS)
        with conn:
            conn.execute("INSERT INTO user_sessions(token, user_id, expires_at) VALUES (?,?,?)",
                         (token, user_id, expires.isoformat()))
        return token, user_id
    finally:
        conn.close()


def user_for_session(token: str) -> Optional[str]:
    """Resolve a session cookie to a userId. This is the ONLY way the backend
    learns who a caller is — a userId in a request body is never trusted."""
    if not token:
        return None
    conn = _db()
    try:
        row = conn.execute(
            "SELECT user_id, expires_at FROM user_sessions WHERE token=?", (token,)
        ).fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
            with conn:
                conn.execute("DELETE FROM user_sessions WHERE token=?", (token,))
            return None
        return row["user_id"]
    finally:
        conn.close()


def logout(token: str):
    conn = _db()
    try:
        with conn:
            conn.execute("DELETE FROM user_sessions WHERE token=?", (token,))
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════════
# DEVICES
# ════════════════════════════════════════════════════════════════════════════

def register_device(user_id: str, fcm_token: str, platform: str = "WEB",
                    browser: str = "", device_id: str = "") -> dict:
    """Bind a token to a user.

    fcm_token is UNIQUE, so INSERT ... ON CONFLICT re-points an existing row at
    the new owner. That is what makes the hand-off safe when user A logs out of
    a browser and user B logs in on the same one: the token moves to B rather
    than existing twice, so A can never receive on that device again.
    """
    if not fcm_token:
        raise ValueError("fcmToken required")
    platform = (platform or "WEB").upper()

    conn = _db()
    try:
        with conn:
            conn.execute("""
                INSERT INTO user_devices(user_id, fcm_token, platform, browser, device_id,
                                         is_active, last_seen_at, updated_at)
                VALUES (?,?,?,?,?,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                ON CONFLICT(fcm_token) DO UPDATE SET
                    user_id      = excluded.user_id,
                    platform     = excluded.platform,
                    browser      = excluded.browser,
                    device_id    = excluded.device_id,
                    is_active    = 1,
                    last_seen_at = CURRENT_TIMESTAMP,
                    updated_at   = CURRENT_TIMESTAMP
            """, (user_id, fcm_token, platform, browser, device_id))
        row = conn.execute(
            "SELECT id, user_id, platform, browser, is_active FROM user_devices WHERE fcm_token=?",
            (fcm_token,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def deactivate_token(fcm_token: str, user_id: Optional[str] = None) -> int:
    """Deactivate one token. When user_id is given the row must belong to them."""
    conn = _db()
    try:
        with conn:
            if user_id:
                cur = conn.execute(
                    "UPDATE user_devices SET is_active=0, updated_at=CURRENT_TIMESTAMP "
                    "WHERE fcm_token=? AND user_id=?", (fcm_token, user_id))
            else:
                cur = conn.execute(
                    "UPDATE user_devices SET is_active=0, updated_at=CURRENT_TIMESTAMP "
                    "WHERE fcm_token=?", (fcm_token,))
        return cur.rowcount
    finally:
        conn.close()


def delete_device(device_id_row: int) -> int:
    conn = _db()
    try:
        with conn:
            return conn.execute("DELETE FROM user_devices WHERE id=?", (device_id_row,)).rowcount
    finally:
        conn.close()


def active_tokens(user_id: str) -> list[str]:
    conn = _db()
    try:
        return [r["fcm_token"] for r in conn.execute(
            "SELECT fcm_token FROM user_devices WHERE user_id=? AND is_active=1", (user_id,)
        ).fetchall()]
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════════
# SENDING
# ════════════════════════════════════════════════════════════════════════════

def _firebase():
    """Initialise the Admin SDK once. Returns None when unconfigured."""
    global _fb_app
    if not _HAS_FIREBASE:
        return None
    cred_path = config.FIREBASE_CREDENTIALS_FILE
    if not cred_path or not os.path.isfile(cred_path):
        return None
    with _fb_lock:
        if _fb_app is None:
            try:
                _fb_app = firebase_admin.initialize_app(
                    _fb_credentials.Certificate(cred_path))
                print(f"[push] firebase-admin initialised · project "
                      f"{config.FIREBASE_WEB_CONFIG.get('projectId')}")
            except Exception as e:
                print(f"[push] firebase init failed: {e}")
                return None
    return _fb_app


def _log(user_id, notification, sent, failed, status, detail=""):
    conn = _db()
    try:
        with conn:
            conn.execute(
                "INSERT INTO notification_log(user_id,title,body,data_json,sent_count,"
                "failed_count,status,detail) VALUES (?,?,?,?,?,?,?,?)",
                (user_id, notification.get("title"), notification.get("body"),
                 json.dumps(notification.get("data") or {}), sent, failed, status, detail[:500])
            )
    finally:
        conn.close()


class NotificationService:
    """Send a notification to every active device belonging to one user."""

    @staticmethod
    def send_to_user(user_id: str, notification: dict) -> dict:
        title = notification.get("title") or "Trinetra"
        body = notification.get("body") or ""
        data = {str(k): str(v) for k, v in (notification.get("data") or {}).items()}
        link = notification.get("link") or data.get("link") or "/index.html"

        # 1. user must exist
        user = get_user(user_id)
        if not user:
            _log(user_id, notification, 0, 0, "skipped", "no such user")
            return {"ok": False, "reason": "unknown_user", "sent": 0, "failed": 0}

        # 2. notifications must be enabled for them
        if not user["notification_enabled"]:
            _log(user_id, notification, 0, 0, "skipped", "notifications disabled")
            return {"ok": False, "reason": "notifications_disabled", "sent": 0, "failed": 0}

        # 3. they must have at least one live device
        tokens = active_tokens(user_id)
        if not tokens:
            _log(user_id, notification, 0, 0, "skipped", "no active devices")
            return {"ok": False, "reason": "no_devices", "sent": 0, "failed": 0}

        # 4. transmit (or record intent when Firebase isn't configured yet)
        app = _firebase()
        if app is None:
            _log(user_id, notification, 0, 0, "skipped",
                 f"log-only mode · would target {len(tokens)} token(s)")
            print(f"[push] LOG-ONLY → {user_id} × {len(tokens)} device(s): {title} — {body}")
            return {"ok": True, "mode": "log_only", "sent": 0, "failed": 0,
                    "targeted": len(tokens)}

        # FCM rejects a webpush link that isn't absolute HTTPS, and it rejects
        # the WHOLE message — so only attach it when we have a real public
        # origin. Without it the service worker still routes the click using
        # data.link / data.sessionId, which are carried in `data` regardless.
        data.setdefault("link", link)
        abs_link = config.public_link(link)
        webpush = (_fb_messaging.WebpushConfig(
            fcm_options=_fb_messaging.WebpushFCMOptions(link=abs_link)
        ) if abs_link else None)

        # Android: pin the channel the app registered with notifee. Without it
        # background notifications land in Android's default channel, where they
        # can be silent. Must match the id the app creates.
        android = _fb_messaging.AndroidConfig(
            priority="high",
            notification=_fb_messaging.AndroidNotification(
                channel_id=config.ANDROID_CHANNEL_ID,
                click_action="TRINETRA_EVENT",
            ),
        )
        # iOS: sound + badge so the alert isn't delivered silently, and
        # content-available so the app can process data while backgrounded.
        apns = _fb_messaging.APNSConfig(
            headers={"apns-priority": "10"},
            payload=_fb_messaging.APNSPayload(
                aps=_fb_messaging.Aps(sound="default", content_available=True)
            ),
        )

        message = _fb_messaging.MulticastMessage(
            notification=_fb_messaging.Notification(title=title, body=body),
            data=data,
            tokens=tokens,
            webpush=webpush,
            android=android,
            apns=apns,
        )
        try:
            resp = _fb_messaging.send_each_for_multicast(message)
        except Exception as e:
            _log(user_id, notification, 0, len(tokens), "error", str(e))
            print(f"[push] send failed for {user_id}: {e}")
            return {"ok": False, "reason": "send_error", "error": str(e),
                    "sent": 0, "failed": len(tokens)}

        # 5/6. retire tokens FCM says are dead, so they stop being retried
        dead = []
        for token, r in zip(tokens, resp.responses):
            if r.success:
                continue
            err = r.exception
            if isinstance(err, (_fb_messaging.UnregisteredError,
                                _fb_messaging.SenderIdMismatchError)):
                dead.append(token)
        for t in dead:
            deactivate_token(t)
        if dead:
            print(f"[push] deactivated {len(dead)} invalid token(s) for {user_id}")

        # 7. audit
        _log(user_id, notification, resp.success_count, resp.failure_count,
             "sent" if resp.success_count else "error",
             f"pruned {len(dead)} invalid token(s)" if dead else "")
        print(f"[push] {user_id}: {resp.success_count} sent, {resp.failure_count} failed")
        return {"ok": resp.success_count > 0, "sent": resp.success_count,
                "failed": resp.failure_count, "deactivated": len(dead)}

    @staticmethod
    def send_to_users(user_ids: list[str], notification: dict) -> dict:
        return {uid: NotificationService.send_to_user(uid, notification) for uid in user_ids}

    @staticmethod
    def broadcast(notification: dict) -> dict:
        """Every user with notifications enabled (used by movement alerts)."""
        return NotificationService.send_to_users(
            [u["user_id"] for u in list_users() if u["notification_enabled"]], notification)
