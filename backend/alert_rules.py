"""Trinetra Perimeter — alert rules (the Alerts page): what the client wants to be alerted about.

Every camera image the pipeline saves comes with a description of the frame (<image>.json: each object's kind —
person / object —, the named regions it stands in, whether it is in an alert region or outside the perimeter line, its
tracking id). Images without one (an edge device, a test image) are read from their file-name class. Each frame is
one OBSERVATION; the rules run on the observations:

  presence   anything matching in the region → an alert at once, once per presence (a presence ends after
             presence_gap seconds without a match)
  dwell      matching for `minutes` without a break longer than presence_gap → an alert, once per presence
  count      `count` or more matching objects in one frame → an alert (then not again within the cooldown)
  offline    a camera without frames for `minutes` (or the pipeline down while it should run) → an alert, once per outage

Gaps (intermittent tracking): a presence continues while matching objects are seen with breaks no longer than the
rule's `gap` seconds (default 60) — tracked 1–10 s, missed 11–20 s, tracked 21–25 s is ONE presence of 25 s; a new
tracking id, a pipeline restart or an object jittering on a region border does not split it either (presence is per
rule, camera and region, not per tracking id). One alert per presence (also when images arrive late or out of order);
`repeat` minutes re-alerts while the presence goes on (0 = once).
Offline: a camera that blinks back for a few seconds does not reset its outage — only 60 s of video in a row does.

Each rule: cameras ([] = all), region (*alert = any alert region, *outside = outside the perimeter line, *any = any
region or outside, *near = any near-wall zone, *crossed = a person who crossed the perimeter line from outside to inside
(the pipeline's LineCrossing; such a frame is also an alert-region breach), or one region's name), target (person / object / any), active days + hours (overnight allowed),
cooldown, severity (critical / warning), sound. Whitelisted labels never alert.
"""
import json
import re
import threading
from datetime import datetime, timedelta
from typing import Callable, Optional

RULE_TYPES = ("presence", "dwell", "count", "offline")
TARGETS = ("person", "object", "any")
SEVERITIES = ("critical", "warning")

# "False alarm" on an alert (client admins + the Trinetra team): what the detection learns from it
# (PERIMETER_THROW_DETECTION_SPEC §7.8). The alert is written to data/feedback/feedback.jsonl; the detection workers
# read it (on_feedback): "bank" = the landing / left-object detection adds that picture patch to the normal-scene bank
# at once; "negatives" = the throw detection keeps the crops for the next training of its throw checker; "none" =
# kept for the false-alarm counts only (person rules: tune the region / times instead). An undo writes an "undo" line:
# the worker takes back what it added for that alert.
FEEDBACK_LEARN = {"landing": "bank", "unattended": "bank", "anomaly": "bank", "throw": "negatives"}
NO_FALSE_ALARM_KINDS = ("offline",)                 # a camera without video is a fact, not a detection
SPECIAL_REGIONS = {"*alert": "any alert region", "*outside": "outside the perimeter line", "*any": "any region or outside",
                   "*near": "any near-wall zone", "*crossed": "crossed the perimeter line"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CLASS_META = {                                       # file-name class → observation (images without a description)
    "boundry-object": {"kind": "object", "alert": True, "outside": False},
    "outside-person": {"kind": "person", "alert": False, "outside": True},
    "outside-object": {"kind": "object", "alert": False, "outside": True},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    type          TEXT    NOT NULL,                -- presence | dwell | count | offline
    cameras       TEXT    NOT NULL DEFAULT '[]',   -- JSON list; [] = every camera
    region        TEXT    NOT NULL DEFAULT '*alert',
    target        TEXT    NOT NULL DEFAULT 'any',
    minutes       REAL    NOT NULL DEFAULT 0,
    count         INTEGER NOT NULL DEFAULT 0,
    days          TEXT    NOT NULL DEFAULT '[0,1,2,3,4,5,6]',   -- Mon = 0
    start         TEXT    NOT NULL DEFAULT '',     -- HH:MM ('' = all day); start > end = overnight
    "end"         TEXT    NOT NULL DEFAULT '',
    cooldown      REAL    NOT NULL DEFAULT 0,      -- minutes
    gap           REAL    NOT NULL DEFAULT 60,     -- seconds unseen before a presence ends
    repeat        REAL    NOT NULL DEFAULT 0,      -- minutes: re-alert while the presence goes on (0 = once)
    severity      TEXT    NOT NULL DEFAULT 'warning',
    sound         INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TEXT,
    created_by    TEXT
);
CREATE TABLE IF NOT EXISTS observations (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    frame    TEXT    NOT NULL UNIQUE,              -- camera|time|seq: a frame saved as several images counts once
    camera   TEXT    NOT NULL,
    ts       TEXT    NOT NULL,                     -- local time, ISO
    image    TEXT,
    objects  TEXT    NOT NULL                      -- JSON list
);
CREATE INDEX IF NOT EXISTS idx_obs_cam_ts ON observations(camera, ts);
"""

DEFAULT_RULES = [
    {"name": "Perimeter breach", "type": "presence", "region": "*alert", "target": "any", "severity": "critical",
     "cooldown": 1},
    {"name": "Person outside the wall", "type": "dwell", "region": "*outside", "target": "person", "minutes": 10,
     "severity": "warning"},
    {"name": "Object left outside the wall", "type": "dwell", "region": "*outside", "target": "object", "minutes": 10,
     "severity": "warning"},
    {"name": "Camera offline", "type": "offline", "minutes": 5, "severity": "warning", "sound": False},
]


def migrate(conn):
    """alert_rules + observations; the alerts table also holds rule alerts (session optional, rule columns)"""
    conn.executescript(SCHEMA)
    info = conn.execute("PRAGMA table_info(alerts)").fetchall()
    cols = {r[1]: r for r in info}
    if cols and cols["session_id"][3]:               # session_id NOT NULL (first versions): rebuild the table
        print("[migrate] alerts: session_id optional (alert rules)")
        conn.executescript("""
            ALTER TABLE alerts RENAME TO alerts_old;
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER REFERENCES sessions(id), badge TEXT NOT NULL,
                triggered_at DATETIME NOT NULL, duration_seconds INTEGER, telegram_sent INTEGER DEFAULT 0,
                email_sent INTEGER DEFAULT 0, acknowledged INTEGER DEFAULT 0, acknowledged_at DATETIME,
                acknowledged_by TEXT, kind TEXT DEFAULT 'dwell', camera TEXT, zone TEXT, image TEXT);
            INSERT INTO alerts(id, session_id, badge, triggered_at, duration_seconds, telegram_sent, email_sent, acknowledged,
                               acknowledged_at, acknowledged_by, kind, camera, zone, image)
                SELECT id, session_id, badge, triggered_at, duration_seconds, telegram_sent, email_sent, acknowledged,
                       acknowledged_at, acknowledged_by, kind, camera, zone, image FROM alerts_old;
            DROP TABLE alerts_old;
            CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(acknowledged, triggered_at DESC);
            CREATE INDEX IF NOT EXISTS idx_alerts_kind ON alerts(kind, triggered_at DESC);""")
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    for col, typ in (("rule_id", "INTEGER"), ("rule_name", "TEXT"), ("severity", "TEXT"), ("message", "TEXT"),
                     ("presence_start", "TEXT"), ("object_count", "INTEGER"), ("sound", "INTEGER DEFAULT 1"),
                     ("verdict", "TEXT"), ("verdict_by", "TEXT"), ("verdict_at", "TEXT"), ("verdict_note", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_rule ON alerts(rule_id, camera, triggered_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(triggered_at)")       # history by date (worst case: 100k+)
    conn.execute("UPDATE alerts SET acknowledged=0 WHERE acknowledged IS NULL")           # open = acknowledged=0 (the index)
    rcols = {r[1] for r in conn.execute("PRAGMA table_info(alert_rules)")}
    for col, typ in (("gap", "REAL NOT NULL DEFAULT 60"), ("repeat", "REAL NOT NULL DEFAULT 0")):
        if col not in rcols:
            conn.execute(f"ALTER TABLE alert_rules ADD COLUMN {col} {typ}")
    if not conn.execute("SELECT 1 FROM alert_rules LIMIT 1").fetchone() and \
            not conn.execute("SELECT 1 FROM sqlite_master WHERE name='alert_rules_seeded'").fetchone():
        for r in DEFAULT_RULES:
            c = check({**r, "enabled": True})
            conn.execute(f"INSERT INTO alert_rules({', '.join(_q(k) for k in c)}) VALUES({', '.join('?' * len(c))})",
                         [json.dumps(v) if isinstance(v, list) else v for v in c.values()])
        conn.execute("CREATE TABLE alert_rules_seeded (x)")   # the defaults once: a client who deletes them keeps it so


def _q(k):
    return '"end"' if k == "end" else k


# ─── rules ───────────────────────────────────────────────────────────────────
def check(body: dict, known_cameras: Optional[set] = None) -> dict:
    """a rule from the page, validated → the columns to store; raises ValueError with a message for the page"""
    if not isinstance(body, dict):
        raise ValueError("invalid rule")
    name = str(body.get("name", "")).strip()
    if not 1 <= len(name) <= 60 or any(ord(c) < 32 for c in name):
        raise ValueError("Name: 1–60 characters")
    typ = body.get("type")
    if typ not in RULE_TYPES:
        raise ValueError("Type: presence, dwell, count or offline")
    cams = body.get("cameras") or []
    if not isinstance(cams, list) or len(cams) > 64 or not all(isinstance(c, str) and re.fullmatch(r"[A-Za-z0-9-]{1,40}", c) for c in cams):
        raise ValueError("Cameras: a list of camera names (empty = every camera)")
    if known_cameras is not None and any(c not in known_cameras for c in cams):
        raise ValueError(f"Unknown camera: {', '.join(c for c in cams if c not in known_cameras)}")
    region = str(body.get("region") or "*alert").strip()
    if region not in SPECIAL_REGIONS and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}", region):
        raise ValueError("Region: a region name, any alert region, outside the line, any region, any near-wall zone or crossed the line")
    target = body.get("target") or "any"
    if target not in TARGETS:
        raise ValueError("Who: person, object or any")
    try:
        minutes = round(float(body.get("minutes") or 0), 2)
        count = int(body.get("count") or 0)
        cooldown = round(float(body.get("cooldown") or 0), 2)
        gap = round(float(body.get("gap") if body.get("gap") not in (None, "") else 60), 1)
        repeat = round(float(body.get("repeat") or 0), 2)
    except (TypeError, ValueError):
        raise ValueError("Minutes, count, cooldown, gap and repeat are numbers")
    if not 10 <= gap <= 3600:
        raise ValueError("Gone after: 10–3600 seconds unseen")
    if not 0 <= repeat <= 24 * 60:
        raise ValueError("Repeat: 0–1440 minutes (0 = once per presence)")
    if typ in ("dwell", "offline") and not 0.5 <= minutes <= 24 * 60:
        raise ValueError("Minutes: 0.5–1440")
    if typ == "count" and not 1 <= count <= 1000:
        raise ValueError("Count: 1–1000")
    if not 0 <= cooldown <= 24 * 60:
        raise ValueError("Cooldown: 0–1440 minutes")
    days = body.get("days", list(range(7)))
    if not isinstance(days, list) or not days or not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
        raise ValueError("Days: at least one day")
    start, end = str(body.get("start") or "").strip(), str(body.get("end") or "").strip()
    if bool(start) != bool(end) or (start and not (TIME_RE.match(start) and TIME_RE.match(end))) or (start and start == end):
        raise ValueError("Active hours: both times HH:MM (different), or both empty for all day")
    sev = body.get("severity") or "warning"
    if sev not in SEVERITIES:
        raise ValueError("Severity: critical or warning")
    return {"name": name, "enabled": int(bool(body.get("enabled", True))), "type": typ, "cameras": sorted(set(cams)),
            "region": region if typ != "offline" else "", "target": target if typ != "offline" else "any",
            "minutes": minutes if typ in ("dwell", "offline") else 0, "count": count if typ == "count" else 0,
            "days": sorted(set(days)), "start": start, "end": end, "cooldown": cooldown, "severity": sev,
            "gap": gap if typ in ("presence", "dwell") else 60, "repeat": repeat if typ in ("presence", "dwell") else 0,
            "sound": int(bool(body.get("sound", True)))}


def row_out(r) -> dict:
    d = dict(r)
    d["cameras"], d["days"] = json.loads(d["cameras"] or "[]"), json.loads(d["days"] or "[]")
    d["enabled"], d["sound"] = bool(d["enabled"]), bool(d["sound"])
    d["region_label"] = SPECIAL_REGIONS.get(d["region"], d["region"])
    return d


def active_at(rule: dict, t: datetime) -> bool:
    """inside the rule's days and hours (an overnight window belongs to the day it starts)"""
    if not rule["start"]:
        return t.weekday() in rule["days"]
    hm = t.strftime("%H:%M")
    if rule["start"] < rule["end"]:
        return t.weekday() in rule["days"] and rule["start"] <= hm < rule["end"]
    if hm >= rule["start"]:
        return t.weekday() in rule["days"]
    if hm < rule["end"]:
        return (t - timedelta(days=1)).weekday() in rule["days"]
    return False


def obj_class(obj: dict) -> str:
    """the file-name class of a described object (as pipeline/zones.py cls())"""
    if obj.get("alert"):
        return "Boundry-object"
    side = "outside" if obj.get("outside") else "zone"
    return f"{side}-{'person' if obj.get('kind') == 'person' else 'object'}"


def matches(rule: dict, obj: dict, whitelist: set) -> bool:
    if whitelist and ((obj.get("label") or "").lower() in whitelist or obj_class(obj).lower() in whitelist):
        return False                                  # whitelisted: by detector label or by class
    if rule["target"] != "any" and obj.get("kind") != rule["target"]:
        return False
    reg = rule["region"]
    if reg == "*alert":
        return bool(obj.get("alert"))
    if reg == "*outside":
        return bool(obj.get("outside"))
    if reg == "*any":
        return bool(obj.get("alert") or obj.get("outside") or obj.get("regions"))
    if reg == "*near":
        return bool(obj.get("near"))
    if reg == "*crossed":
        return bool(obj.get("crossed"))
    return reg.lower() in [n.lower() for n in obj.get("regions") or []]


def for_camera(rule: dict, camera: str) -> bool:
    return not rule["cameras"] or camera in rule["cameras"]


# ─── the engine ──────────────────────────────────────────────────────────────
class Engine:
    """thread-safe; `db` opens a connection, `lock` serialises writes (the app's), `notify(alert)` is called for each
    new alert (outside the lock), `gap()` = presence gap in seconds"""

    def __init__(self, db: Callable, lock, notify: Callable[[dict], None], gap: Callable[[], float]):
        self.db, self.lock, self.notify, self.gap = db, lock, notify, gap
        self.outage: dict = {}                       # (rule id, camera) → since (datetime) for offline rules
        self._mu = threading.Lock()
        self._pres: dict = {}                        # (rule id, camera) → (rule signature, start, end): the presence last found

    # observations ----------------------------------------------------------
    @staticmethod
    def from_class(badge: str) -> Optional[dict]:
        m = CLASS_META.get((badge or "").lower())
        return {**m, "label": badge, "regions": [], "track": None} if m else None

    def ingest(self, camera: str, ts_local: datetime, image: str, meta: Optional[dict], badge: str) -> list:
        """one saved image (+ its frame description) → the alerts it raises (already stored; notify() called)"""
        if meta and isinstance(meta.get("objects"), list):
            objs = [o for o in meta["objects"][:200] if isinstance(o, dict)]
            frame = str(meta.get("frame") or f"{camera}|{ts_local.isoformat()}|{image}")[:200]
        else:
            o = self.from_class(badge)
            if not o:
                return []
            objs, frame = [o], f"{camera}|{ts_local.isoformat()}|{image}"
        clean = [{"label": str(o.get("label", ""))[:40], "kind": "person" if o.get("kind") == "person" else "object",
                  "regions": [str(n)[:40] for n in (o.get("regions") or [])][:20], "alert": bool(o.get("alert")),
                  "outside": bool(o.get("outside")), "near": bool(o.get("near")), "crossed": bool(o.get("crossed")),
                  "track": o.get("track") if isinstance(o.get("track"), int) else None}
                 for o in objs]
        fired = []
        with self._mu:
            conn = self.db()
            try:
                with self.lock, conn:
                    cur = conn.execute("INSERT OR IGNORE INTO observations(frame, camera, ts, image, objects) VALUES(?,?,?,?,?)",
                                       (frame, camera, ts_local.isoformat(timespec="milliseconds"), image, json.dumps(clean)))
                    if not cur.rowcount:
                        return []                    # the same frame, saved as another class's image: counted once
                    wl = {r[0].lower() for r in conn.execute("SELECT badge FROM whitelist")}
                    rules = [row_out(r) for r in conn.execute("SELECT * FROM alert_rules WHERE enabled=1 AND type!='offline'")]
                    for rule in rules:
                        if not for_camera(rule, camera) or not active_at(rule, ts_local):
                            continue
                        hit = [o for o in clean if matches(rule, o, wl)]
                        if not hit:
                            continue
                        a = self._evaluate(conn, rule, camera, ts_local, image, hit, wl)
                        if a:
                            fired.append(a)
            finally:
                conn.close()
        for a in fired:
            self.notify(a)
        return fired

    def _presence(self, conn, rule, camera, ts_local, wl) -> tuple:
        """(start, end) of the presence this frame belongs to: the connected run of matching observations around it —
        backwards AND forwards — with no break longer than the rule's gap (frames can arrive late or out of order).

        Fast path (the usual case, frames in order): the presence last found for this rule + camera is kept; a frame
        inside it, or at most one gap after its end, belongs to it. Anything else (a late frame, a new presence, the
        rule or the whitelist changed) walks the observations outwards from the frame in chunks and stops at the first
        break longer than the gap — the work is the presence's length, never a fixed window of hours."""
        gap = timedelta(seconds=rule.get("gap") or self.gap())
        sig = (gap, rule["region"], rule["target"], frozenset(wl))
        key = (rule["id"], camera)
        c = self._pres.get(key)
        if c and c[0] == sig:
            s, e = c[1], c[2]
            if s <= ts_local <= e:
                return s, e
            if e < ts_local <= e + gap and not conn.execute(
                    "SELECT 1 FROM observations WHERE camera=? AND ts>? AND ts<=? LIMIT 1",
                    (camera, ts_local.isoformat(timespec="milliseconds"), (ts_local + gap).isoformat(timespec="milliseconds"))).fetchone():
                # in order: nothing stored after this frame yet (a frame that came early could join a later presence)
                self._pres[key] = (sig, s, ts_local)
                return s, ts_local
        s = self._walk(conn, rule, camera, ts_local, wl, gap, -1)
        e = self._walk(conn, rule, camera, ts_local, wl, gap, +1)
        if len(self._pres) > 5000:
            self._pres.clear()
        self._pres[key] = (sig, s, e)
        return s, e

    def _walk(self, conn, rule, camera, ts_local, wl, gap, way, chunk=400) -> datetime:
        """the farthest matching observation reachable from ts_local in one direction without a break > gap"""
        last, cur_ts, cur_id = ts_local, ts_local.isoformat(timespec="milliseconds"), (2 ** 62 if way < 0 else -1)
        sql = ("SELECT id, ts, objects FROM observations WHERE camera=? AND (ts<? OR (ts=? AND id<?)) ORDER BY ts DESC, id DESC LIMIT ?"
               if way < 0 else
               "SELECT id, ts, objects FROM observations WHERE camera=? AND (ts>? OR (ts=? AND id>?)) ORDER BY ts, id LIMIT ?")
        while True:
            rows = conn.execute(sql, (camera, cur_ts, cur_ts, cur_id, chunk)).fetchall()
            for r in rows:
                t = datetime.fromisoformat(r["ts"])
                if abs(t - last) > gap:
                    return last
                if any(matches(rule, o, wl) for o in json.loads(r["objects"])):
                    last = t
            if len(rows) < chunk:
                return last
            cur_ts, cur_id = rows[-1]["ts"], rows[-1]["id"]

    def reset_cache(self):
        """observations deleted (data reset): forget the kept presences"""
        self._pres.clear()

    @staticmethod
    def _what(rule, hit) -> str:
        kinds = {o["kind"] for o in hit}
        return "Person" if kinds == {"person"} else ("Object" if kinds == {"object"} else "Person / object")

    def _evaluate(self, conn, rule, camera, ts_local, image, hit, wl) -> Optional[dict]:
        now_utc = datetime.utcnow()
        last = conn.execute("SELECT triggered_at, presence_start FROM alerts WHERE rule_id=? AND camera=? "
                            "ORDER BY id DESC LIMIT 1", (rule["id"], camera)).fetchone()
        if last and rule["cooldown"] and now_utc - datetime.fromisoformat(last["triggered_at"]) < timedelta(minutes=rule["cooldown"]):
            return None
        where = SPECIAL_REGIONS.get(rule["region"], rule["region"])
        if rule["type"] == "count":
            if len(hit) < rule["count"]:
                return None
            noun = "people" if {o["kind"] for o in hit} == {"person"} else "objects"
            return self._store(conn, rule, camera, image, hit, None, 0, f"{len(hit)} {noun} in {where} (limit {rule['count']})")
        start, end = self._presence(conn, rule, camera, ts_local, wl)
        dur = (end - start).total_seconds()
        if rule["type"] == "dwell" and dur < rule["minutes"] * 60:
            return None
        # this presence alerted already? (an alert whose presence started anywhere inside it — also when images came
        # late or out of order and the presence turned out to start earlier)
        prev = conn.execute("SELECT triggered_at, presence_start FROM alerts WHERE rule_id=? AND camera=? AND presence_start>=? "
                            "AND presence_start<=? ORDER BY id DESC LIMIT 1",
                            (rule["id"], camera, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))).fetchone()
        if prev:
            if not rule.get("repeat") or now_utc - datetime.fromisoformat(prev["triggered_at"]) < timedelta(minutes=rule["repeat"]):
                return None
        what = self._what(rule, hit)
        if rule["type"] == "dwell":
            msg = f"{what} in {where} for {int(dur // 60)} min {int(dur % 60)} s"
        else:
            msg = f"{what} in {where}" + (f" — still there after {int(dur // 60)} min {int(dur % 60)} s" if prev else "")
        return self._store(conn, rule, camera, image, hit, start, int(dur), msg)

    def _store(self, conn, rule, camera, image, hit, start, dur, msg) -> dict:
        labels = sorted({o["label"] for o in hit if o["label"]})
        regions = sorted({n for o in hit for n in o["regions"]}) or ([SPECIAL_REGIONS[rule["region"]]] if rule["region"] in SPECIAL_REGIONS else [rule["region"]])
        kind = "breach" if rule["severity"] == "critical" and rule["type"] == "presence" else rule["type"]
        cur = conn.execute(
            "INSERT INTO alerts(session_id, badge, triggered_at, duration_seconds, kind, camera, zone, image, rule_id, "
            "rule_name, severity, message, presence_start, object_count, sound) VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (", ".join(labels) or rule["target"], datetime.utcnow().isoformat(), dur, kind, camera, ", ".join(regions)[:200],
             image, rule["id"], rule["name"], rule["severity"], msg[:300],
             start.isoformat(timespec="seconds") if start else None, len(hit), int(rule["sound"])))
        return {"alert_id": cur.lastrowid, "rule_id": rule["id"], "rule_name": rule["name"], "severity": rule["severity"],
                "kind": kind, "camera": camera, "zone": ", ".join(regions), "message": msg, "image": image,
                "badge": ", ".join(labels), "sound": bool(rule["sound"]), "count": len(hit), "duration_seconds": dur}

    # offline ------------------------------------------------------------------
    STABLE_S = 60                                   # video this long in a row ends an outage (a blinking camera does not)

    def check_offline(self, now: datetime, cameras: list, running: bool, wanted: bool, streams: dict) -> list:
        """cameras: the enabled cameras; streams: name → seconds since its last frame (None = none yet) of a running
        pipeline. A camera is offline when the pipeline should run but is down, or its stream sends nothing for 15 s.
        An outage ends only after STABLE_S of video in a row (state per rule and camera: since, fired, online_since)."""
        fired = []
        with self._mu:
            conn = self.db()
            try:
                rules = [row_out(r) for r in conn.execute("SELECT * FROM alert_rules WHERE enabled=1 AND type='offline'")]
                live = set()
                for rule in rules:
                    for cam in cameras:
                        if not for_camera(rule, cam):
                            continue
                        key = (rule["id"], cam)
                        live.add(key)
                        st = self.outage.get(key)
                        down = wanted and (not running or streams.get(cam) is None or streams[cam] > 15)
                        if not down:
                            if st:
                                st["online_since"] = st["online_since"] or now
                                if (now - st["online_since"]).total_seconds() >= self.STABLE_S:
                                    self.outage.pop(key, None)        # back for good: the next outage alerts again
                            continue
                        if not st:
                            lag = streams.get(cam) if running and streams.get(cam) else 0
                            st = self.outage[key] = {"since": now - timedelta(seconds=lag), "fired": False, "online_since": None}
                        st["online_since"] = None                     # down again: the blink did not end the outage
                        if st["fired"] or not active_at(rule, now):
                            continue
                        mins = (now - st["since"]).total_seconds() / 60
                        if mins < rule["minutes"]:
                            continue
                        why = "the pipeline is not running" if not running else "no video from the camera"
                        with self.lock, conn:
                            a = self._store(conn, rule, cam, None, [], st["since"], int(mins * 60),
                                            f"Camera {cam} offline for {int(mins)} min — {why}")
                        st["fired"] = True
                        fired.append(a)
                for key in [k for k in self.outage if k not in live]:
                    self.outage.pop(key, None)                # rule / camera removed
            finally:
                conn.close()
        for a in fired:
            self.notify(a)
        return fired
