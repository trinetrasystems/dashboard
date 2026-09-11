-- Trinetra Systems Monitor — SQLite schema
-- Apply once: sqlite3 /data/trinetra.db < schema.sql

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- Every detection that arrived from edge Jetsons
CREATE TABLE IF NOT EXISTS detections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL UNIQUE,
    camera      TEXT NOT NULL,
    badge       TEXT NOT NULL,
    timestamp   DATETIME NOT NULL,
    type        TEXT DEFAULT 'sighting',        -- 'entry' | 'sighting' | 'exit'
    session_id  INTEGER REFERENCES sessions(id),-- which "visit" this belongs to
    received_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_det_ts      ON detections(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_det_badge   ON detections(badge);
CREATE INDEX IF NOT EXISTS idx_det_camera  ON detections(camera);
CREATE INDEX IF NOT EXISTS idx_det_session ON detections(session_id);

-- Presence sessions (for loitering detection + "currently in premises")
CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    badge           TEXT NOT NULL,
    first_seen      DATETIME NOT NULL,
    last_seen       DATETIME NOT NULL,
    last_camera     TEXT,
    total_sightings INTEGER DEFAULT 1,
    alert_sent      INTEGER DEFAULT 0,
    closed          INTEGER DEFAULT 0,                  -- 1 = badge exited or session stale
    closed_at       DATETIME,
    cameras_json    TEXT DEFAULT '{}'                   -- {"cam-a": 12, "cam-b": 3}
);
CREATE INDEX IF NOT EXISTS idx_sess_open ON sessions(badge, closed);

-- Loitering alerts fired (one row per session)
CREATE TABLE IF NOT EXISTS alerts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        INTEGER NOT NULL REFERENCES sessions(id),
    badge             TEXT NOT NULL,
    triggered_at      DATETIME NOT NULL,
    duration_seconds  INTEGER,
    telegram_sent     INTEGER DEFAULT 0,
    email_sent        INTEGER DEFAULT 0,
    acknowledged      INTEGER DEFAULT 0,
    acknowledged_at   DATETIME,
    acknowledged_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(acknowledged, triggered_at DESC);

-- Camera-to-camera movement events (one row per transition, no time threshold).
-- Written on ingest whenever a badge appears at a different camera than the one
-- it was last seen at — plus its first appearance (entry) and exit-gate hits.
-- Repeat sightings at the SAME camera are not movements and are not recorded.
CREATE TABLE IF NOT EXISTS movements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    badge         TEXT NOT NULL,
    session_id    INTEGER REFERENCES sessions(id),
    detection_id  INTEGER REFERENCES detections(id),
    from_camera   TEXT,                       -- NULL when this is the first sighting
    to_camera     TEXT NOT NULL,
    event         TEXT NOT NULL,              -- 'entry' | 'move' | 'exit'
    statement     TEXT NOT NULL,              -- human-readable sentence, as alerted
    timestamp     DATETIME NOT NULL,
    filename      TEXT,                       -- image captured at the destination
    notified      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_move_ts      ON movements(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_move_badge   ON movements(badge);
CREATE INDEX IF NOT EXISTS idx_move_session ON movements(session_id);

-- Badges exempt from loitering alerts (staff, etc.)
CREATE TABLE IF NOT EXISTS whitelist (
    badge      TEXT PRIMARY KEY,
    reason     TEXT,
    added_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ════════════════════════════════════════════════════════════════════════════
-- PUSH NOTIFICATIONS (Firebase Cloud Messaging)
--
-- A "user" here is a notification recipient (TRN_1001), created by the admin —
-- distinct from a badge, which is a vehicle seen by a camera. The dashboard
-- itself stays open; a user signs in only to bind their device to their userId.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              TEXT NOT NULL UNIQUE,          -- TRN_1001 (human-readable)
    name                 TEXT,
    email                TEXT,
    passcode_hash        TEXT,                          -- optional; only for non-Firebase sign-in
    firebase_uid         TEXT UNIQUE,                   -- links this row to a Firebase Auth account
    notification_enabled INTEGER DEFAULT 1,
    created_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at           DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_users_uid ON users(user_id);
CREATE INDEX IF NOT EXISTS idx_users_fb  ON users(firebase_uid);

-- One row per browser/device. fcm_token is UNIQUE, which is what makes the
-- logout → different-user-logs-in case safe: re-registering the same token
-- REPLACES the row, moving it to the new owner instead of duplicating it.
CREATE TABLE IF NOT EXISTS user_devices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    fcm_token    TEXT NOT NULL UNIQUE,
    platform     TEXT DEFAULT 'WEB',                    -- WEB | ANDROID | IOS
    browser      TEXT,
    device_id    TEXT,
    is_active    INTEGER DEFAULT 1,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_dev_user   ON user_devices(user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_dev_token  ON user_devices(fcm_token);

-- Sessions live in the DB (not memory) so a server restart doesn't break a
-- device's ability to refresh its FCM token.
CREATE TABLE IF NOT EXISTS user_sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usess_exp ON user_sessions(expires_at);

-- Delivery audit trail
CREATE TABLE IF NOT EXISTS notification_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    title        TEXT,
    body         TEXT,
    data_json    TEXT,
    sent_count   INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    status       TEXT,                                  -- sent | skipped | error
    detail       TEXT,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_notiflog_ts ON notification_log(created_at DESC);

-- NOTE: Loitering/gap/exit-camera settings now live in .env (TRINETRA_LOITER_MINUTES etc.)
-- No `config` table needed. Restart the server after editing .env.
