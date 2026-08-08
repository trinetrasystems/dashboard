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

-- NOTE: Loitering/gap/exit-camera settings now live in .env (TRINETRA_LOITER_MINUTES etc.)
-- No `config` table needed. Restart the server after editing .env.
