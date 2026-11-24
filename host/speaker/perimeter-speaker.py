#!/usr/bin/env python3
"""perimeter-speaker — the alert beep on a Bluetooth speaker paired with the Jetson (runs on the HOST, as root).

The perimeter dashboard (in its container) writes jobs into <data>/speaker/requests/*.json:
    beep {severity}   an alert: critical = 6 fast high tones, warning = 2 tones
    test {severity}   the Test button
    scan              find speakers nearby (12 s)
    connect {mac}     pair + trust + connect a speaker (it must be in pairing mode)
    forget {mac}      remove a speaker
and reads <data>/speaker/status.json, which this agent rewrites every 5 s and after every job.

Bluetooth: bluez (bluetoothctl) + bluez-alsa (bluealsa daemon, aplay -D bluealsa). Only a job name, a severity and a
checked MAC address are taken from a request; every command runs without a shell. Standard library only.
"""
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

DATA = Path(os.environ.get("PERIMETER_DATA", "/home/nvidia/PERIMETER_data"))
DIR = DATA / "speaker"
REQ, STATUS, CONFIG = DIR / "requests", DIR / "status.json", DIR / "config.json"
SOUNDS = Path(os.environ.get("SPEAKER_SOUNDS", "/var/lib/perimeter-speaker"))
MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
VERSION = 1
state = {"last_beep": None, "scan": {"at": None, "running": False, "devices": []}, "job": None, "error": None}


def log(*a):
    print("[speaker]", *a, flush=True)


def run(args, timeout=15, inp=None) -> tuple:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=inp)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out
    except OSError as e:
        return 127, str(e)


def ctl(*args, timeout=15) -> tuple:
    return run(["bluetoothctl", *args], timeout=timeout)


# ── the beep sounds (made once, same pattern as the dashboard's browser beep) ──
def make_sounds():
    SOUNDS.mkdir(parents=True, exist_ok=True)
    rate = 44100

    def write(name, tones, gap_start=0.7):
        path = SOUNDS / f"{name}.wav"
        if path.is_file():
            return
        frames = bytearray(struct.pack("<h", 0) * int(rate * gap_start))       # A2DP drops the first moment: start quiet
        t_end = max(t for t, _ in tones) + 0.3
        n = int(rate * t_end)
        buf = [0.0] * n
        for start, f in tones:
            s0 = int(rate * start)
            for i in range(int(rate * 0.22)):
                if s0 + i < n:
                    env = min(1.0, i / (rate * 0.01), (rate * 0.22 - i) / (rate * 0.02))
                    buf[s0 + i] += 0.6 * env * (1 if math.sin(2 * math.pi * f * i / rate) >= 0 else -1) * 0.5
        frames += b"".join(struct.pack("<h", int(max(-1, min(1, v)) * 32000)) for v in buf)
        tmp = path.with_suffix(".tmp")
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(bytes(frames))
        os.replace(tmp, path)

    write("critical", [(t, 1040 if i % 2 else 880) for i, t in enumerate((0, 0.28, 0.56, 1.1, 1.38, 1.66))])
    write("warning", [(0, 660), (0.35, 660)])


# ── Bluetooth ──
def device() -> str:
    try:
        mac = str(json.loads(CONFIG.read_text()).get("device") or "").upper()
    except (OSError, ValueError):
        return ""
    return mac if MAC_RE.match(mac) else ""


def info(mac) -> dict:
    code, out = ctl("info", mac, timeout=8)
    get = lambda k: (re.search(rf"^\s*{k}: (.+)$", out, re.M) or [None, ""])[1].strip()
    return {"mac": mac, "known": code == 0 and "Device " + mac in out, "name": get("Name") or get("Alias"),
            "paired": get("Paired") == "yes", "trusted": get("Trusted") == "yes", "connected": get("Connected") == "yes"}


def powered() -> bool:
    code, out = ctl("show", timeout=8)
    return code == 0 and bool(re.search(r"^\s*Powered: yes", out, re.M))


def connect(mac, pair=False) -> tuple:
    ctl("power", "on")
    d = info(mac)
    if pair and not d["paired"]:
        ctl("--timeout", "15", "scan", "on", timeout=20)                 # the speaker must be seen before pairing
        code, out = run(["bluetoothctl", "--agent", "NoInputNoOutput", "pair", mac], timeout=30)
        if code != 0 and "AlreadyExists" not in out:
            return False, f"pairing failed — is the speaker in pairing mode? ({out.strip().splitlines()[-1] if out.strip() else code})"
    if not info(mac)["trusted"]:
        ctl("trust", mac)
    if not info(mac)["connected"]:
        code, out = ctl("connect", mac, timeout=25)
        for _ in range(10):
            if info(mac)["connected"]:
                break
            time.sleep(1)
    d = info(mac)
    return d["connected"], "connected" if d["connected"] else "not connected — switch the speaker on, near the Jetson"


def play(severity) -> tuple:
    mac = device()
    if not mac:
        return False, "no speaker chosen"
    ok, why = connect(mac)
    if not ok:
        return False, why
    wav = SOUNDS / f"{'critical' if severity == 'critical' else 'warning'}.wav"
    code, out = run(["aplay", "-q", "-D", f"bluealsa:DEV={mac},PROFILE=a2dp", str(wav)], timeout=20)
    if code != 0:
        time.sleep(2)                                                   # the A2DP stream can need a moment after connect
        code, out = run(["aplay", "-q", "-D", f"bluealsa:DEV={mac},PROFILE=a2dp", str(wav)], timeout=20)
    return code == 0, "played" if code == 0 else f"could not play: {out.strip()[-160:]}"


def scan():
    state["scan"] = {"at": None, "running": True, "devices": []}
    write_status()
    ctl("power", "on")
    ctl("--timeout", "12", "scan", "on", timeout=18)
    _, out = ctl("devices", timeout=8)
    devs = []
    for mac, name in re.findall(r"^Device ([0-9A-F:]{17}) (.*)$", out, re.M):
        d = info(mac)
        raw = ctl("info", mac, timeout=8)[1]
        audio = "Audio Sink" in raw or "0000110b" in raw.lower() or "audio-" in raw.lower()
        devs.append({"mac": mac, "name": name.strip()[:60], "paired": d["paired"], "connected": d["connected"], "audio": audio})
    devs.sort(key=lambda d: (not d["audio"], not d["paired"], d["name"] == d["mac"].replace(":", "-"), d["name"]))
    state["scan"] = {"at": time.time(), "running": False, "devices": devs[:30]}


def write_status():
    mac = device()
    st = {"updated": time.time(), "version": VERSION, "bluetooth": powered(),
          "bluealsa": any(run(["systemctl", "is-active", u], timeout=5)[1].strip() == "active" for u in ("bluez-alsa", "bluealsa")),
          "device": info(mac) if mac else None, "last_beep": state["last_beep"], "scan": state["scan"],
          "job": state["job"], "error": state["error"]}
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_name(".status.tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, STATUS)


def take_jobs() -> list:
    if not REQ.is_dir():
        return []
    jobs = []
    for p in sorted(REQ.glob("*.json")):
        try:
            if p.stat().st_size < 4096:
                j = json.loads(p.read_text())
                if isinstance(j, dict):
                    jobs.append(j)
        except (OSError, ValueError):
            pass
        p.unlink(missing_ok=True)
    beeps = [j for j in jobs if j.get("job") == "beep"]
    rest, seen = [], set()
    for j in jobs:                                # the same job asked twice (Find speakers clicked again) runs once
        k = (j.get("job"), j.get("mac"), j.get("severity"))
        if j.get("job") != "beep" and k not in seen:
            seen.add(k); rest.append(j)
    if beeps:                                     # many alerts at once → one beep, the most urgent
        rest.append({"job": "beep", "severity": "critical" if any(b.get("severity") == "critical" for b in beeps) else "warning"})
    return rest


def do(job):
    kind = job.get("job")
    sev = "critical" if job.get("severity") == "critical" else "warning"
    state["job"], state["error"] = kind, None
    write_status()
    if kind in ("beep", "test"):
        ok, why = play(sev)
        state["last_beep"] = {"at": time.time(), "ok": ok, "message": why, "severity": sev, "test": kind == "test"}
        log(kind, sev, why)
    elif kind == "scan":
        scan()
        log("scan:", len(state["scan"]["devices"]), "devices")
    elif kind in ("connect", "forget"):
        mac = str(job.get("mac") or "").upper()
        if not MAC_RE.match(mac):
            state["error"] = "bad address"
        elif kind == "connect":
            ok, why = connect(mac, pair=True)
            state["error"] = None if ok else why
            log("connect", mac, why)
        else:
            ctl("disconnect", mac); ctl("remove", mac)
            log("forget", mac)
    state["job"] = None


def main():
    make_sounds()
    REQ.mkdir(parents=True, exist_ok=True)
    ctl("power", "on")
    log(f"running · data {DIR} · speaker {device() or '(none chosen)'}")
    last = 0.0
    while True:
        jobs = take_jobs()
        for j in jobs:
            try:
                do(j)
            except Exception as e:                      # one bad job never stops the agent
                state["job"], state["error"] = None, repr(e)[:200]
                log("job failed:", repr(e))
        if jobs or time.time() - last > 5:
            try:
                write_status()
            except Exception as e:
                log("status not written:", repr(e))
            last = time.time()
        time.sleep(0.5)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "sounds":
        make_sounds(); print(SOUNDS)
    else:
        main()
