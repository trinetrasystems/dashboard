"""Alert beep on a Bluetooth speaker paired with the Jetson (Alerts page → Speaker).

The dashboard runs in a container; the Jetson's Bluetooth is on the host. The two talk through the data folder:

    data/speaker/config.json     the client's choice (this module writes it; the host agent reads the device)
    data/speaker/requests/*.json one file per job for the host agent: beep | test | scan | connect | forget
    data/speaker/status.json     written by the host agent (perimeter-speaker service) every few seconds:
                                 bluetooth on?, the speaker connected?, the last beep, the speakers found by a scan

The host agent (skill: template/speaker/perimeter-speaker.py) plays the beep with bluez-alsa, connects the speaker
when needed, and never takes anything from a request but a job name, a severity and a checked MAC address.
Without the agent (a laptop, a Jetson without the speaker service) the page says so; nothing else changes.
"""
import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

import config

DIR = Path(config.DATA_DIR) / "speaker"
CONFIG, STATUS, REQ = DIR / "config.json", DIR / "status.json", DIR / "requests"
MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
REPEATS = (0, 30, 60, 120, 300)                       # seconds; 0 = once per alert
DEFAULTS = {"enabled": False, "severities": "critical", "repeat_s": 0, "device": "", "device_name": ""}
AGENT_FRESH_S = 30                                    # the agent writes its status at least this often
MAX_PENDING = 20                                      # jobs waiting for the agent (an agent that is down never fills the disk)
_last_repeat = 0.0


def _write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _read(path: Path, default):
    try:
        v = json.loads(path.read_text())
        return v if isinstance(v, type(default)) else default
    except (OSError, ValueError):
        return default


def load() -> dict:
    return {**DEFAULTS, **{k: v for k, v in _read(CONFIG, {}).items() if k in DEFAULTS}}


def check(raw: dict, cur: dict) -> dict:
    """the client's settings, checked; raises ValueError"""
    out = dict(cur)
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    if "severities" in raw:
        if raw["severities"] not in ("critical", "all"):
            raise ValueError("Which alerts: critical or all")
        out["severities"] = raw["severities"]
    if "repeat_s" in raw:
        try:
            r = int(raw["repeat_s"])
        except (TypeError, ValueError):
            raise ValueError("Repeat: a number of seconds")
        if r not in REPEATS:
            raise ValueError("Repeat: once, or every 30 s / 1 / 2 / 5 min")
        out["repeat_s"] = r
    return out


def save(cfg: dict):
    _write(CONFIG, {k: cfg[k] for k in DEFAULTS})


def status() -> dict:
    """the host agent's report + whether it is alive"""
    st = _read(STATUS, {})
    age = time.time() - float(st.get("updated", 0) or 0)
    st["agent"] = bool(st) and age < AGENT_FRESH_S
    st["agent_age_s"] = round(age) if st.get("updated") else None
    st["pending"] = len(list(REQ.glob("*.json"))) if REQ.is_dir() else 0
    return st


def request(job: str, **kw) -> bool:
    """one job for the host agent; False when too many wait already (agent down)"""
    if job not in ("beep", "test", "scan", "connect", "forget"):
        raise ValueError("unknown job")
    REQ.mkdir(parents=True, exist_ok=True)
    pending = sorted(REQ.glob("*.json"))
    if len(pending) >= MAX_PENDING:
        if job != "beep":
            return False
        for p in pending[: len(pending) - MAX_PENDING + 1]:     # beeps: the newest counts, drop the oldest
            p.unlink(missing_ok=True)
    _write(REQ / f"{time.time():.3f}-{secrets.token_hex(3)}.json", {"job": job, "at": datetime.now().isoformat(timespec="seconds"), **kw})
    return True


def wants(alert: dict, cfg: dict) -> bool:
    return bool(cfg["enabled"] and cfg["device"] and alert.get("sound")
                and (cfg["severities"] == "all" or alert.get("severity") == "critical"))


def on_alert(alert: dict):
    """a new rule alert → a beep on the speaker when the client switched it on for this kind of alert"""
    try:
        cfg = load()
        if wants(alert, cfg):
            request("beep", severity=alert.get("severity") or "warning", alert_id=alert.get("alert_id"))
            mark_beeped()
    except Exception as e:
        print(f"[speaker] beep request failed: {e!r}")


def repeat_tick(open_alerts) -> bool:
    """called every few seconds with the open (unacknowledged) alerts [(severity, sound)]: beeps again every repeat_s
    while one the speaker is for is open"""
    global _last_repeat
    cfg = load()
    if not cfg["enabled"] or not cfg["repeat_s"] or not cfg["device"]:
        return False
    mine = [sev for sev, sound in open_alerts if sound and (cfg["severities"] == "all" or sev == "critical")]
    now = time.monotonic()
    if not mine or now - _last_repeat < cfg["repeat_s"]:
        return False
    _last_repeat = now
    request("beep", severity="critical" if "critical" in mine else "warning", repeat=True)
    return True


def mark_beeped():
    """a fresh alert just beeped: the repeat waits a full interval from now"""
    global _last_repeat
    _last_repeat = time.monotonic()


def clean_mac(v) -> str:
    mac = str(v or "").strip().upper().replace("-", ":")
    if not MAC_RE.match(mac):
        raise ValueError("A Bluetooth address looks like 12:34:56:78:9A:BC")
    return mac
