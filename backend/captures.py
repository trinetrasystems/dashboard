"""Trinetra Perimeter — Recordings (the Recordings page, Trinetra team only). Record chosen cameras on this Jetson for a
set time, turn the video into frames (one every N s), compress + zip them, and send the package to the Trinetra laptop
for labelling. Fail-safe and resumable at every step: each job is its own process (capture_worker.py) that a dashboard
restart does not stop; the supervisor here (every 5 s, and at start-up after a reboot or power cut) carries on whatever
was cut off; the laptop fetches the package part by part (rsync, resumable, SHA-256 checked) and reports back.

Folder: PERIMETER_CAPTURES (host /home/nvidia/PERIMETER_captures, owned by the Jetson's login user so the laptop can
fetch over ssh without root). Camera addresses and passwords are never written there (they stay in ds/config.json).
  <id>/capture.json                      what was asked: cameras, minutes, start / end, state       (this module)
  <id>/video/<camera>/seg_NNNNN.mkv      the camera's own video, 60 s pieces + segments.jsonl + status.json (recorder)
  <id>/frames.json · frames.state.json   asked · progress;  <id>/frames/<camera>/*.jpg              (frames worker)
  <id>/pack.json · pack.state.json       asked · progress;  <id>/package/part-NNN.zip + manifest.json + SHA256SUMS
  <id>/package/SEND                      "send to the laptop" (this module) → PROGRESS.json, RECEIVED.json (the laptop)
  <id>/log.txt                           what every step did (shown on the page)
  .receiver.json                         the laptop's last visit (scripts/receive_captures.sh of the perimeter skill)
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

import config
import ds_control

ROOT = Path(os.environ.get("PERIMETER_CAPTURES") or (Path(config.DATA_DIR) / "captures"))
OWNER = int(os.environ.get("CAPTURES_UID", "1000"))
WORKER = Path(__file__).resolve().parent / "capture_worker.py"
ID_RE = re.compile(r"^\d{8}-\d{6}$")
START_MIN_GB = config.DISK_MIN_FREE_GB + 10          # a new recording needs this much free disk
STOP_GB = config.DISK_MIN_FREE_GB + 5               # every job stops here: the image disk guard (lower) never has to act
MAX_MINUTES = 24 * 60
MAX_AHEAD_DAYS = 7
REC_RESTARTS = 30                                    # a recorder that keeps dying is given up on (per camera)
JOB_ATTEMPTS = 3                                     # frames / pack: tries after a crash or a reboot
FRAME_EVERY = (1, 2, 3, 5, 10, 15, 30, 60)
SIZES = (0, 640, 960, 1280, 1920)                    # longest side of the packed frames; 0 = as recorded
router = APIRouter()
_lock = threading.RLock()
PROCS: dict = {}                                     # (capture, job) → Popen: reaped here, never a zombie


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _own(p: Path):
    try:
        os.chown(p, OWNER, OWNER)
    except OSError:
        pass


def _mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    _own(p)
    return p


def _write(p: Path, obj):
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    _own(p)


def _read(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _free_gb() -> float:
    try:
        return shutil.disk_usage(ROOT if ROOT.exists() else ROOT.parent).free / 1e9
    except OSError:
        return 0.0


def _log(cap: Path, msg: str):
    try:
        with open(cap / "log.txt", "a") as f:
            f.write(f"{_now():%Y-%m-%d %H:%M:%S} [dashboard] {msg}\n")
        _own(cap / "log.txt")
    except OSError:
        pass
    print(f"[recordings] {cap.name}: {msg}", flush=True)


def _cap(cid: str) -> Path:
    if not ID_RE.match(cid or ""):
        raise HTTPException(404, "No such recording")
    p = ROOT / cid
    if not (p / "capture.json").is_file():
        raise HTTPException(404, "No such recording")
    return p


def _caps() -> list:
    try:
        return sorted((p for p in ROOT.iterdir() if ID_RE.match(p.name) and (p / "capture.json").is_file()), reverse=True)
    except OSError:
        return []


# ─── worker processes ────────────────────────────────────────────────────────────────────────────────────────────────
def _alive(cap: Path, job: str) -> Optional[int]:
    k = (cap.name, job)
    if k in PROCS and PROCS[k].poll() is not None:
        PROCS.pop(k)                                  # reaped
    try:
        pid = int((cap / "run" / f"{job}.pid").read_text())
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
        return pid if b"capture_worker.py" in cmd and str(cap).encode() in cmd else None
    except (OSError, ValueError):
        return None


def _launch(cap: Path, job: str, args: list, stdin: Optional[dict] = None):
    run = _mkdir(cap / "run")
    env = {**os.environ, "CAPTURES_UID": str(OWNER), "CAPTURE_STOP_GB": f"{STOP_GB:g}"}
    with open(run / f"{job}.out", "wb") as out:      # a crash's traceback (the log.txt has the readable lines)
        p = subprocess.Popen([ds_control.PY, "-u", str(WORKER), *args], stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                             stdout=out, stderr=subprocess.STDOUT, cwd=str(WORKER.parent), start_new_session=True, env=env)
    if stdin:                                         # the camera address: on stdin, never in argv / on disk here
        p.stdin.write(json.dumps(stdin).encode())
        p.stdin.close()
    (run / f"{job}.pid").write_text(str(p.pid))
    PROCS[(cap.name, job)] = p
    return p


def _start_recorder(cap: Path, cam: str) -> bool:
    s = next((x for x in ds_control.load_config()["streams"] if x["name"] == cam), None)
    if not s:
        _log(cap, f"{cam}: this camera is no longer on the DeepStream page — not recorded")
        return False
    _launch(cap, f"rec-{cam}", ["record", str(cap), cam], {"uri": s["uri"], "transport": s.get("transport", "tcp")})
    return True


def _stop_workers(cap: Path):
    for f in (cap / "run").glob("*.pid") if (cap / "run").is_dir() else []:
        pid = _alive(cap, f.stem)
        if pid:
            try:
                os.killpg(pid, 15)
            except OSError:
                pass


# ─── the supervisor: start what is due, carry on what was cut off ────────────────────────────────────────────────────
def supervise_once(now: Optional[datetime] = None):
    now = now or _now()
    with _lock:
        for cap in _caps():
            m = _read(cap / "capture.json", {}) or {}
            st = m.get("state")
            if st == "scheduled" and now >= datetime.fromisoformat(m["start"]):
                if _free_gb() < START_MIN_GB:
                    m.update(state="failed", error=f"not started: under {START_MIN_GB:g} GB free on the disk")
                    _write(cap / "capture.json", m)
                    _log(cap, m["error"])
                    continue
                m.update(state="recording", start=_iso(now), end=_iso(now + timedelta(minutes=m["minutes"])))
                _write(cap / "capture.json", m)
                _log(cap, f"planned recording started: {', '.join(m['cameras'])} for {m['minutes']} min")
                for cam in m["cameras"]:
                    _start_recorder(cap, cam)
            elif st == "recording":
                stop, over = (cap / "STOP").exists(), now >= datetime.fromisoformat(m["end"])
                running = False
                for cam in m["cameras"]:
                    if _alive(cap, f"rec-{cam}"):
                        running = True
                        continue
                    cs = _read(cap / "video" / cam / "status.json", {}) or {}
                    if stop or over or cs.get("state") in ("done", "failed"):
                        continue                      # finished by itself (time over, stop, disk) — or never could
                    n = m.setdefault("restarts", {}).get(cam, 0)
                    if n >= REC_RESTARTS:
                        continue
                    m["restarts"][cam] = n + 1
                    _write(cap / "capture.json", m)
                    _log(cap, f"{cam}: the recorder was not running (dashboard restart, reboot or crash) — carrying on")
                    running = _start_recorder(cap, cam) or running
                if not running:
                    reasons = {(_read(cap / "video" / c / "status.json", {}) or {}).get("stop_reason") for c in m["cameras"]}
                    m.update(state="stopped" if stop else "recorded", ended=_iso(now))
                    if any(r and "disk" in r for r in reasons):
                        m["note"] = "stopped early: the disk is nearly full"
                    _write(cap / "capture.json", m)
                    _log(cap, f"recording {'stopped' if stop else 'finished'}")
            for job in ("frames", "pack"):
                req = _read(cap / f"{job}.json")
                if not req or req.get("state") != "running":
                    continue
                prog = _read(cap / f"{job}.state.json", {}) or {}
                if prog.get("run") == req.get("run") and prog.get("state") in ("done", "failed"):
                    req.update(state=prog["state"], ended=prog.get("ended") or _iso(now), error=prog.get("error"))
                    _write(cap / f"{job}.json", req)
                    continue
                if _alive(cap, job):
                    continue
                if req.get("attempts", 0) >= JOB_ATTEMPTS:
                    req.update(state="failed", error=f"stopped {JOB_ATTEMPTS} times (see the log)")
                    _write(cap / f"{job}.json", req)
                    _log(cap, f"{job}: given up after {JOB_ATTEMPTS} tries")
                    continue
                req["attempts"] = req.get("attempts", 0) + 1
                _write(cap / f"{job}.json", req)
                if req["attempts"] > 1:
                    _log(cap, f"{job}: was cut off (dashboard restart, reboot or crash) — carrying on (try {req['attempts']})")
                _launch(cap, job, [job, str(cap)])


def supervisor():
    while True:
        try:
            supervise_once()
        except Exception as e:                        # never stops: the next round tries again
            print(f"[recordings] supervisor: {e!r}", flush=True)
        time.sleep(5)


def start():
    try:
        _mkdir(ROOT)
        os.chmod(ROOT, 0o700)
    except OSError as e:
        print(f"[recordings] folder {ROOT}: {e}", flush=True)
    for d in ROOT.glob(".del-*"):                     # a delete cut off by a restart: finish it
        threading.Thread(target=shutil.rmtree, args=(d,), kwargs={"ignore_errors": True}, daemon=True).start()
    threading.Thread(target=supervisor, daemon=True, name="recordings").start()
    print(f"[recordings] on: {ROOT} (stops under {STOP_GB:g} GB free, new ones need {START_MIN_GB:g} GB)", flush=True)


# ─── what the page shows ─────────────────────────────────────────────────────────────────────────────────────────────
def _send_info(cap: Path, pack_run) -> dict:
    pkg = cap / "package"
    send = _read(pkg / "SEND")
    if not send or send.get("run") != pack_run:
        return {"state": "none"}
    rec = _read(pkg / "RECEIVED.json")
    if rec and rec.get("run") == pack_run:
        return {"state": "received", "at": rec.get("at"), "host": rec.get("host"), "path": rec.get("path"), "asked": send.get("at")}
    prog = _read(pkg / "PROGRESS.json") or {}
    if prog.get("run") == pack_run and prog.get("parts"):
        return {"state": "sending", "asked": send.get("at"), "parts_done": prog.get("parts_done", 0), "parts": prog["parts"],
                "bytes_done": prog.get("bytes_done", 0), "bytes": prog.get("bytes", 0), "at": prog.get("at"), "host": prog.get("host")}
    return {"state": "waiting", "asked": send.get("at")}


def summary(cap: Path) -> dict:
    m = _read(cap / "capture.json", {}) or {}
    cams = []
    for c in m.get("cameras", []):
        s = _read(cap / "video" / c / "status.json", {}) or {}
        cams.append({"name": c, "state": s.get("state") or ("waiting" if m.get("state") in ("scheduled", "recording") else "none"),
                     "codec": s.get("codec"), "bytes": s.get("bytes", 0), "seconds": s.get("recorded_s", 0),
                     "reconnects": s.get("reconnects", 0), "error": s.get("last_error"), "stop_reason": s.get("stop_reason")})
    fr, fs = _read(cap / "frames.json") or {}, _read(cap / "frames.state.json") or {}
    pr, ps = _read(cap / "pack.json") or {}, _read(cap / "pack.state.json") or {}
    if fs.get("run") != fr.get("run"):
        fs = {}
    if ps.get("run") != pr.get("run"):
        ps = {}
    frames = None
    if fr:
        frames = {"state": fr.get("state"), "every_s": fr.get("every_s"), "skip_similar": fr.get("skip_similar"),
                  "count": fs.get("kept", 0), "skipped": fs.get("skipped", 0), "bytes": fs.get("bytes", 0),
                  "done": len(fs.get("done", {})), "total": fs.get("total", 0), "error": fr.get("error") or fs.get("error"),
                  "attempts": fr.get("attempts", 0)}
    pack = None
    if pr:
        parts = ps.get("parts", [])
        pack = {"state": pr.get("state"), "max_px": pr.get("max_px"), "quality": pr.get("quality"), "part_mb": pr.get("part_mb"),
                "parts": len(parts), "bytes": sum(p["bytes"] for p in parts), "frames_done": max(ps.get("frames_done", 0), ps.get("frames_now", 0)),
                "total": ps.get("total", 0), "error": pr.get("error") or ps.get("error"), "attempts": pr.get("attempts", 0)}
    return {"id": cap.name, "name": m.get("name") or "", "cameras": cams, "minutes": m.get("minutes"), "state": m.get("state"),
            "start": m.get("start"), "end": m.get("end"), "ended": m.get("ended"), "created": m.get("created"), "by": m.get("by"),
            "note": m.get("note") or m.get("error"), "video_bytes": sum(c["bytes"] for c in cams),
            "frames": frames, "pack": pack,
            "send": _send_info(cap, pr.get("run")) if pack and pack["state"] == "done" else {"state": "none"}}


def _busy_job() -> Optional[str]:
    for cap in _caps():
        for job in ("frames", "pack"):
            if (_read(cap / f"{job}.json") or {}).get("state") == "running":
                return f"{'making frames' if job == 'frames' else 'packing'} of {cap.name}"
    return None


def _sending(cap: Path) -> bool:
    pr = _read(cap / "pack.json") or {}
    return pr.get("state") == "done" and _send_info(cap, pr.get("run"))["state"] in ("waiting", "sending")


def _by(request: Request) -> str:
    i = getattr(request.state, "ident", None) or {}
    return i.get("username") or i.get("name") or ""


# ════════════════════════════════════════════════════════════════════════════
# API (/api/captures*: the Trinetra team only — access.py: anything not listed there needs the team)
# ════════════════════════════════════════════════════════════════════════════
@router.get("/api/captures")
def api_list():
    rcv = _read(ROOT / ".receiver.json") or {}
    age = None
    if rcv.get("at"):
        try:
            age = max(0, int((_now() - datetime.fromisoformat(rcv["at"])).total_seconds()))
        except ValueError:
            pass
    cfg = ds_control.load_config()
    return {"cameras": [{"name": s["name"], "enabled": s["enabled"]} for s in cfg["streams"]],
            "disk": {"free_gb": round(_free_gb(), 1), "start_min_gb": START_MIN_GB, "stop_gb": STOP_GB},
            "receiver": {"host": rcv.get("host"), "at": rcv.get("at"), "age_s": age},
            "max_minutes": MAX_MINUTES, "frame_every": FRAME_EVERY, "sizes": SIZES,
            "folder": os.environ.get("PERIMETER_CAPTURES_HOST", str(ROOT)),
            "captures": [summary(c) for c in _caps()]}


@router.post("/api/captures")
async def api_create(request: Request):
    b = await ds_control._body(request)
    names = {s["name"] for s in ds_control.load_config()["streams"]}
    cams = b.get("cameras")
    if not isinstance(cams, list) or not cams or len(cams) > ds_control.MAX_CAMERAS:
        raise HTTPException(400, "Choose at least one camera")
    cams = list(dict.fromkeys(str(c) for c in cams))
    bad = [c for c in cams if c not in names]
    if bad:
        raise HTTPException(400, f"Not a camera on the DeepStream page: {', '.join(bad)[:120]}")
    try:
        minutes = int(b.get("minutes"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Duration: a number of minutes")
    if not 1 <= minutes <= MAX_MINUTES:
        raise HTTPException(400, f"Duration: 1 to {MAX_MINUTES} minutes")
    name = re.sub(r"\s+", " ", str(b.get("name") or "")).strip()[:60]
    now = _now()
    start = None
    if b.get("start_at"):
        try:
            start = datetime.fromisoformat(str(b["start_at"])[:19]).replace(microsecond=0)
        except ValueError:
            raise HTTPException(400, "Start time: a date and time")
        if start <= now + timedelta(seconds=30):
            start = None                              # "at" a time already here: now
        elif start > now + timedelta(days=MAX_AHEAD_DAYS):
            raise HTTPException(400, f"Start time: within {MAX_AHEAD_DAYS} days")
    free = _free_gb()
    if free < START_MIN_GB:
        raise HTTPException(409, f"Only {free:.0f} GB free on the disk: a recording needs {START_MIN_GB:g} GB (delete old recordings first)")
    with _lock:
        for c in _caps():
            st = (_read(c / "capture.json") or {}).get("state")
            if st in ("scheduled", "recording"):
                raise HTTPException(409, f"Recording {c.name} is {'planned' if st == 'scheduled' else 'running'}: stop it first "
                                         "(one recording at a time; it can hold several cameras)")
        cid = now.strftime("%Y%m%d-%H%M%S")
        while (ROOT / cid).exists():
            now += timedelta(seconds=1)
            cid = now.strftime("%Y%m%d-%H%M%S")
        cap = _mkdir(ROOT / cid)
        site = os.environ.get("TRINETRA_SITE_NAME") or os.environ.get("CLIENT_NAME") or os.uname().nodename
        m = {"id": cid, "name": name, "site": site, "cameras": cams, "minutes": minutes, "created": _iso(_now()), "by": _by(request)}
        if start:
            m.update(state="scheduled", start=_iso(start), end=_iso(start + timedelta(minutes=minutes)))
        else:
            m.update(state="recording", start=_iso(_now()), end=_iso(_now() + timedelta(minutes=minutes)))
        _write(cap / "capture.json", m)
        _log(cap, f"{'planned for ' + m['start'] if start else 'recording'}: {', '.join(cams)} for {minutes} min (by {m['by']})")
        if not start:
            for cam in cams:
                _start_recorder(cap, cam)
    request.state.audit_target = f"{cid}: {', '.join(cams)} for {minutes} min"[:200]
    return summary(cap)


@router.post("/api/captures/{cid}/stop")
def api_stop(cid: str, request: Request):
    cap = _cap(cid)
    with _lock:
        m = _read(cap / "capture.json", {})
        if m.get("state") == "scheduled":
            m.update(state="cancelled", ended=_iso(_now()))
            _write(cap / "capture.json", m)
            _log(cap, f"planned recording cancelled (by {_by(request)})")
        elif m.get("state") == "recording":
            (cap / "STOP").write_text(_iso(_now()))   # each recorder closes its last piece cleanly within ~2 s
            _log(cap, f"stopped on the page (by {_by(request)})")
        else:
            raise HTTPException(409, "This recording is not running")
    request.state.audit_target = cid
    return summary(cap)


def _ready_for_next(cap: Path, what: str):
    m = _read(cap / "capture.json", {})
    if m.get("state") not in ("recorded", "stopped"):
        raise HTTPException(409, "The recording is not finished yet" if m.get("state") in ("recording", "scheduled") else "Nothing was recorded")
    busy = _busy_job()
    if busy:
        raise HTTPException(409, f"Busy: {busy} — one at a time (it keeps the Jetson free for the live pipeline)")
    if _sending(cap):
        raise HTTPException(409, f"The package is being sent to the laptop: cancel sending first, then {what}")


@router.post("/api/captures/{cid}/frames")
async def api_frames(cid: str, request: Request):
    cap = _cap(cid)
    b = await ds_control._body(request)
    try:
        every = int(b.get("every_s", 1))
    except (TypeError, ValueError):
        every = 0
    if every not in FRAME_EVERY:
        raise HTTPException(400, f"One frame every: {', '.join(map(str, FRAME_EVERY))} s")
    with _lock:
        _ready_for_next(cap, "make the frames again")
        if not any((cap / "video").glob("*/seg_*.mkv")):
            raise HTTPException(409, "No video was recorded (see the log: camera not reachable?)")
        for f in ("pack.json", "pack.state.json"):
            (cap / f).unlink(missing_ok=True)         # new frames → the old package is out of date
        shutil.rmtree(cap / "package", ignore_errors=True)
        _write(cap / "frames.json", {"run": uuid.uuid4().hex[:12], "every_s": every, "skip_similar": bool(b.get("skip_similar")),
                                     "state": "running", "attempts": 0, "asked": _iso(_now()), "by": _by(request)})
    supervise_once()                                  # starts it now (not in up to 5 s)
    request.state.audit_target = f"{cid}: one every {every} s"
    return summary(cap)


@router.post("/api/captures/{cid}/pack")
async def api_pack(cid: str, request: Request):
    cap = _cap(cid)
    b = await ds_control._body(request)
    try:
        max_px, quality, part_mb = int(b.get("max_px", 1280)), int(b.get("quality", 85)), int(b.get("part_mb", 50))
    except (TypeError, ValueError):
        raise HTTPException(400, "Size, quality and part size are numbers")
    if max_px not in SIZES or not 50 <= quality <= 95 or not 10 <= part_mb <= 200:
        raise HTTPException(400, "Size: as recorded / 640 / 960 / 1280 / 1920 px · quality 50–95 · parts of 10–200 MB")
    with _lock:
        _ready_for_next(cap, "pack again")
        if (_read(cap / "frames.json") or {}).get("state") != "done":
            raise HTTPException(409, "Make the frames first")
        for f in ("SEND", "PROGRESS.json", "RECEIVED.json"):
            (cap / "package" / f).unlink(missing_ok=True)
        _write(cap / "pack.json", {"run": uuid.uuid4().hex[:12], "max_px": max_px, "quality": quality, "part_mb": part_mb,
                                   "state": "running", "attempts": 0, "asked": _iso(_now()), "by": _by(request)})
    supervise_once()
    request.state.audit_target = f"{cid}: {max_px or 'as recorded'} px, quality {quality}"
    return summary(cap)


@router.post("/api/captures/{cid}/send")
def api_send(cid: str, request: Request):
    cap = _cap(cid)
    with _lock:
        pr = _read(cap / "pack.json") or {}
        if pr.get("state") != "done" or not (cap / "package" / "manifest.json").is_file():
            raise HTTPException(409, "Compress + zip first")
        for f in ("PROGRESS.json", "RECEIVED.json"):
            (cap / "package" / f).unlink(missing_ok=True)
        _write(cap / "package" / "SEND", {"run": pr["run"], "at": _iso(_now()), "by": _by(request)})
        _log(cap, f"sending to the laptop asked (by {_by(request)}): the laptop fetches it at its next check (every minute)")
    request.state.audit_target = cid
    return summary(cap)


@router.post("/api/captures/{cid}/cancel-send")
def api_cancel_send(cid: str, request: Request):
    cap = _cap(cid)
    with _lock:
        for f in ("SEND", "PROGRESS.json"):
            (cap / "package" / f).unlink(missing_ok=True)
        _log(cap, f"sending cancelled (by {_by(request)})")
    request.state.audit_target = cid
    return summary(cap)


@router.delete("/api/captures/{cid}")
def api_delete(cid: str, request: Request):
    cap = _cap(cid)
    with _lock:
        m = _read(cap / "capture.json", {})
        if m.get("state") == "recording":
            raise HTTPException(409, "Stop the recording first")
        for job in ("frames", "pack"):
            if (_read(cap / f"{job}.json") or {}).get("state") == "running":
                raise HTTPException(409, f"Wait until {'the frames are made' if job == 'frames' else 'packing is done'}")
        _stop_workers(cap)
        gone = ROOT / f".del-{cid}"
        os.replace(cap, gone)                         # off the list at once; the files go in the background
    threading.Thread(target=shutil.rmtree, args=(gone,), kwargs={"ignore_errors": True}, daemon=True).start()
    request.state.audit_target = f"{cid}{' (' + m.get('name') + ')' if m.get('name') else ''}"
    return {"ok": True}


@router.get("/api/captures/{cid}/log")
def api_log(cid: str, lines: int = 300):
    cap = _cap(cid)
    text = ds_control.read_tail(cap / "log.txt", 200_000).splitlines()[-max(20, min(lines, 2000)):]
    return {"text": "\n".join(text)}


@router.get("/api/captures/{cid}/frame/{cam}")
def api_frame(cid: str, cam: str):
    """a frame from the middle of the camera's recording (to see what was captured)"""
    cap = _cap(cid)
    if not ds_control.CAM_RE.match(cam):
        raise HTTPException(404, "No such camera")
    files = sorted((cap / "frames" / cam).glob("*.jpg")) if (cap / "frames" / cam).is_dir() else []
    if not files:
        raise HTTPException(404, "No frames yet")
    return FileResponse(files[len(files) // 2], headers={"Cache-Control": "no-store"})
