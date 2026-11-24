"""Trinetra Perimeter — DeepStream control (the DeepStream page, Trinetra team only). Built on the Workplace
Analytics control module (epms_control.py), site-proven: the same probe / snapshot helpers, engine build, clean stop
and auto-restart supervisor.

Files (data folder, kept across upgrades):
  ds/config.json      cameras + pipeline settings (the page)        ds/zones.json   perimeter zones per camera
  ds/engines/         TensorRT engines (built here, per model and camera count — no rebuild after an upgrade)
  ds/models/          your own models: <name>.onnx + <name>.labels.txt (the image brings the stand-in detector)
  ds/frames/          the last frame of each camera (zone drawing while the pipeline is stopped)
  ds/run/             run configs, intent.json (the user wants it running), pid
  logs/pipeline_*.log, logs/engine_build_*.log
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

import config
from pipeline import zones as Z

ROOT = Path(os.environ.get("PERIMETER_ROOT", "/root/PERIMETER"))
DATA = Path(config.DATA_DIR)
DS = DATA / "ds"
RUN = DS / "run"
ENGINES = DS / "engines"
USER_MODELS = DS / "models"
FRAMES = DS / "frames"
LOGS = DATA / "logs"
IMAGE_MODELS = ROOT / "models"
SHM = Path(os.environ.get("PERIMETER_SHM", "/dev/shm"))
STATE = SHM / "perimeter_state.json"
SNAPS = SHM / "perimeter_snap"
CONFIG = DS / "config.json"
ZONES = DS / "zones.json"
INTENT = RUN / "intent.json"
PIDF = RUN / "pipeline.pid"
NIGHTLY_FILE = RUN / "nightly_restart.json"           # {"day": iso}: the planned night restart of that day is done
APP = Path(__file__).resolve().parent / "pipeline"
PY = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
YOLO_LIB = "/root/DeepStream-Yolo/nvdsinfer_custom_impl_Yolo/libnvdsinfer_custom_impl_Yolo.so"
BUILD_VIDEO = "/opt/nvidia/deepstream/deepstream/samples/streams/sample_1080p_h264.mp4"
SAMPLE_VIDEOS = "/opt/nvidia/deepstream/deepstream/samples/streams"
CAM_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")          # the dashboard's file names: <camera>_<class>_<time>.jpg
MAX_CAMERAS = 16
ENGINE_STEP = 4                                       # engines for 4, 8, 12, 16 cameras: adding one camera rarely rebuilds
SERIALIZE_RE = re.compile(r"serialize cuda engine to file:\s*(\S+)\s+successfully", re.I)
# Automatic restart (as Workplace Analytics, 19 Nov 2026): a stop nobody asked for → started again at once; after a
# memory (safety) stop RESTART_SETTLE_S later; still short of RESTART_MIN_GB FREE_MEM_AFTER_S after the stop → memory is
# freed once (a stray pipeline, stale helpers, the page cache); max MAX_RESTARTS_PER_HOUR. Only Stop on the page keeps it off.
MAX_RESTARTS_PER_HOUR, RESTART_MIN_GB = 5, 2.0
RESTART_SETTLE_S = 30
FREE_MEM_AFTER_S = 60
DEFAULTS = {"streams": [], "model": "", "threshold": 0.40, "save_every_s": 5, "frame_skip": 2, "loop_files": True,
            "nightly_restart": "00:00"}

router = APIRouter()
_lock = threading.Lock()


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def write_atomic(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _exists(p) -> bool:
    try:
        return Path(p).exists()
    except OSError:                                   # e.g. /root not readable outside the container
        return False


def available() -> bool:
    """DeepStream is here (the Jetson container): pyds + the YOLO parser"""
    return _exists(YOLO_LIB) and _exists("/opt/nvidia/deepstream/deepstream")


# ════════════════════════════════════════════════════════════════════════════
# models
# ════════════════════════════════════════════════════════════════════════════
def list_models() -> list:
    """every <name>.onnx with its <name>.labels.txt: the image's stand-in detector + the site's own (ds/models)"""
    out = []
    for d, kind in ((USER_MODELS, "site"), (IMAGE_MODELS, "built-in")):
        try:
            files = sorted(d.glob("*.onnx")) if d.is_dir() else []
        except OSError:
            files = []
        for f in files:
            lab = f.with_name(f.stem + ".labels.txt")
            labels = [x.strip() for x in lab.read_text().splitlines() if x.strip()] if lab.is_file() else []
            if labels:
                out.append({"name": f.name, "path": str(f), "labels": labels, "kind": kind,
                            "engines": engines_of(f.stem)})
    return out


def model_by_name(name: str) -> Optional[dict]:
    ms = list_models()
    return next((m for m in ms if m["name"] == name), None) or (ms[0] if ms and not name else None)


def engines_of(stem: str) -> list:
    rx = re.compile(re.escape(stem) + r"_b(\d+)_gpu0_fp16\.engine$")
    return sorted(int(m.group(1)) for e in ENGINES.glob(f"{stem}_b*_gpu0_fp16.engine") if (m := rx.match(e.name))) if ENGINES.is_dir() else []


def engine_plan(model: dict, n: int) -> dict:
    """the smallest built engine for >= n cameras, else the size to build (a multiple of 4)"""
    need = max(1, n)
    have = [b for b in model["engines"] if b >= need]
    size = min(have) if have else ((need + ENGINE_STEP - 1) // ENGINE_STEP) * ENGINE_STEP
    stem = Path(model["name"]).stem
    return {"size": size, "file": str(ENGINES / f"{stem}_b{size}_gpu0_fp16.engine"), "built": bool(have),
            "minutes": round(6 + size)}


PGIE_T = """[property]
gpu-id=0
net-scale-factor=0.0039215697906911373
model-color-format=0
onnx-file={onnx}
model-engine-file={engine}
labelfile-path={labels}
batch-size={batch}
network-mode=2
num-detected-classes={nc}
interval=0
gie-unique-id=1
process-mode=1
network-type=0
cluster-mode=2
infer-dims=3;640;640
maintain-aspect-ratio=1
symmetric-padding=1
parse-bbox-func-name=NvDsInferParseYoloCuda
custom-lib-path={lib}
engine-create-func-name=NvDsInferYoloCudaEngineGet

[class-attrs-all]
nms-iou-threshold=0.45
pre-cluster-threshold={th}
topk=100
"""


def pgie_text(model: dict, engine: str, batch: int, th: float) -> str:
    lab = Path(model["path"]).with_name(Path(model["path"]).stem + ".labels.txt")
    return PGIE_T.format(onnx=model["path"], engine=engine, labels=lab, batch=batch, nc=len(model["labels"]), lib=YOLO_LIB, th=th)


# ════════════════════════════════════════════════════════════════════════════
# config + zones
# ════════════════════════════════════════════════════════════════════════════
def load_config() -> dict:
    return {**DEFAULTS, **(read_json(CONFIG, {}) or {})}


def check_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("invalid settings")
    cfg = {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}
    streams, seen = [], set()
    for s in cfg["streams"] or []:
        name, uri = str(s.get("name", "")).strip(), str(s.get("uri", "")).strip()
        if not CAM_RE.match(name):
            raise ValueError(f"camera name “{name}”: 1–40 letters, numbers or dashes (no spaces or underscores)")
        if name.lower() in seen:
            raise ValueError(f"two cameras are named {name}")
        seen.add(name.lower())
        if not re.match(r"^(rtsp|rtsps)://\S+$", uri) and not re.match(r"^file:///\S+$", uri):
            raise ValueError(f"{name}: the stream address starts with rtsp:// (a camera) or file:/// (a video on the Jetson)")
        if uri.startswith("file://") and available() and not _exists(uri[7:]):
            raise ValueError(f"{name}: no video file {uri[7:]} on the Jetson")
        tr = s.get("transport", "tcp")
        streams.append({"name": name, "uri": uri, "transport": "udp" if tr == "udp" else "tcp", "enabled": bool(s.get("enabled", True))})
    if len(streams) > MAX_CAMERAS:
        raise ValueError(f"at most {MAX_CAMERAS} cameras on one Jetson")
    cfg["streams"] = streams
    try:
        cfg["threshold"] = round(min(0.95, max(0.05, float(cfg["threshold"]))), 2)
        cfg["save_every_s"] = int(min(300, max(1, int(cfg["save_every_s"]))))
        cfg["frame_skip"] = int(min(10, max(0, int(cfg["frame_skip"]))))
    except (TypeError, ValueError):
        raise ValueError("threshold / image interval / frame skip must be numbers")
    cfg["loop_files"] = bool(cfg["loop_files"])
    nr = str(cfg.get("nightly_restart") or "").strip()
    if nr and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", nr):
        raise ValueError("nightly restart: a time HH:MM (empty = off)")
    cfg["nightly_restart"] = nr
    if cfg["model"] and not model_by_name(cfg["model"]):
        raise ValueError(f"model {cfg['model']} not found")
    return cfg


def load_zones() -> dict:
    return read_json(ZONES, {}) or {}


# ════════════════════════════════════════════════════════════════════════════
# probe + snapshot (the Workplace Analytics helpers: a separate process, never inside the dashboard)
# ════════════════════════════════════════════════════════════════════════════
PROBE_GI = r"""
import json, sys, time
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)
uri, timeout, transport = sys.argv[1], float(sys.argv[2]), sys.argv[3]
r = {"ok": False, "error": "no video within %d s" % timeout}
pipe = Gst.Pipeline()
src, sink = Gst.ElementFactory.make("uridecodebin"), Gst.ElementFactory.make("fakesink")
src.set_property("uri", uri)
src.set_property("caps", Gst.Caps.from_string("video/x-h264, parsed=true; video/x-h265, parsed=true; image/jpeg, parsed=true; video/x-raw; video/x-raw(ANY)"))
sink.set_property("sync", False)
pipe.add(src); pipe.add(sink)
def setup(_o, s):
    if s.find_property("protocols"): s.set_property("protocols", 7 if transport == "udp" else 4)
    if s.find_property("latency"): s.set_property("latency", 200)
src.connect("source-setup", setup)
def pad_added(_e, pad):
    c = pad.get_current_caps() or pad.query_caps(None)
    if c.to_string().startswith(("video/", "image/")) and not sink.get_static_pad("sink").is_linked():
        pad.link(sink.get_static_pad("sink"))
src.connect("pad-added", pad_added)
seen = {}
def on_buf(pad, info):
    if "caps" not in seen:
        c = pad.get_current_caps()
        if c: seen["caps"] = c.get_structure(0)
    return Gst.PadProbeReturn.OK
sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, on_buf)
pipe.set_state(Gst.State.PLAYING)
bus, end = pipe.get_bus(), time.time() + timeout
while time.time() < end and "caps" not in seen:
    m = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
    if m:
        if m.type == Gst.MessageType.ERROR: r["error"] = m.parse_error()[0].message
        break
pipe.set_state(Gst.State.NULL)
if "caps" in seen:
    st = seen["caps"]; name = st.get_name()
    okw, w = st.get_int("width"); okh, h = st.get_int("height")
    r = {"ok": True, "width": w if okw else 0, "height": h if okh else 0,
         "codec": {"video/x-h264": "H.264", "video/x-h265": "H.265", "image/jpeg": "MJPEG"}.get(name, name.split("/")[-1])}
    okf, n, d = st.get_fraction("framerate")
    if okf and n > 0 and d > 0: r["fps"] = round(n / d, 2)
print(json.dumps(r))
"""

SNAP_GI = r"""
import json, os, sys, time
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
import numpy as np, cv2
Gst.init(None)
uri, transport, out, timeout = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
skip = 3 if uri.startswith("rtsp") else 12
pipe = Gst.Pipeline()
mk = Gst.ElementFactory.make
src, conv, caps, sink = mk("uridecodebin"), mk("nvvideoconvert"), mk("capsfilter"), mk("appsink")
src.set_property("uri", uri)
caps.set_property("caps", Gst.Caps.from_string("video/x-raw, format=BGRx"))
sink.set_property("sync", False); sink.set_property("max-buffers", 2); sink.set_property("drop", True)
for e in (src, conv, caps, sink): pipe.add(e)
conv.link(caps); caps.link(sink)
def setup(_o, s):
    if s.find_property("protocols"): s.set_property("protocols", 7 if transport == "udp" else 4)
    if s.find_property("latency"): s.set_property("latency", 300)
src.connect("source-setup", setup)
def pad_added(_e, pad):
    c = pad.get_current_caps() or pad.query_caps(None)
    if c.to_string().startswith("video/") and not conv.get_static_pad("sink").is_linked(): pad.link(conv.get_static_pad("sink"))
src.connect("pad-added", pad_added)
pipe.set_state(Gst.State.PLAYING)
r, n, end, bus = {"ok": False, "error": "no frame within %d s" % timeout}, 0, time.time() + timeout, pipe.get_bus()
while time.time() < end:
    m = bus.pop_filtered(Gst.MessageType.ERROR)
    if m: r["error"] = m.parse_error()[0].message; break
    smp = sink.emit("try-pull-sample", 500 * Gst.MSECOND)
    if smp is None: continue
    n += 1
    if n < skip: continue
    st = smp.get_caps().get_structure(0); w, h = st.get_value("width"), st.get_value("height")
    buf = smp.get_buffer(); ok, mi = buf.map(Gst.MapFlags.READ)
    a = np.frombuffer(mi.data, np.uint8); stride = len(a) // h
    img = a[: stride * h].reshape(h, stride)[:, : w * 4].reshape(h, w, 4)[:, :, :3].copy()
    buf.unmap(mi)
    img = cv2.resize(img, (960, int(960 * h / w)), interpolation=cv2.INTER_AREA)
    tmp = out + ".tmp.jpg"; cv2.imwrite(tmp, img, [cv2.IMWRITE_JPEG_QUALITY, 88]); os.replace(tmp, out)
    r = {"ok": True, "width": w, "height": h}; break
pipe.set_state(Gst.State.NULL)
print(json.dumps(r))
"""
_SNAP_SEM = threading.BoundedSemaphore(2)


def _helper(script: str, args: list, timeout: float) -> dict:
    if not available():
        return {"ok": False, "error": "DeepStream is not installed here (only on the Jetson)"}
    try:
        p = subprocess.run([PY, "-c", script, *args], capture_output=True, text=True, timeout=timeout)
        lines = [l for l in p.stdout.splitlines() if l.startswith("{")]
        return json.loads(lines[-1]) if lines else {"ok": False, "error": (p.stderr.strip().splitlines() or ["no answer"])[-1][:200]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"no answer within {timeout:.0f} s"}


def probe(uri: str, transport: str) -> dict:
    with _SNAP_SEM:
        return _helper(PROBE_GI, [uri, "10", transport], 25)


def grab_frame(cam: dict) -> dict:
    FRAMES.mkdir(parents=True, exist_ok=True)
    with _SNAP_SEM:
        return _helper(SNAP_GI, [cam["uri"], cam.get("transport", "tcp"), str(FRAMES / f"{cam['name']}.jpg"), "12"], 30)


# ════════════════════════════════════════════════════════════════════════════
# the pipeline process
# ════════════════════════════════════════════════════════════════════════════
PROC = None
RESTARTS: list = []
DOWN = {"since": None, "said": 0.0, "freed": False}


def intent() -> bool:
    return bool((read_json(INTENT, {}) or {}).get("running"))


def set_intent(v: bool, by: str = ""):
    write_atomic(INTENT, json.dumps({"running": bool(v), "at": datetime.now().isoformat(timespec="seconds"), "by": by}))


def pid_alive() -> Optional[int]:
    global PROC
    if PROC is not None and PROC.poll() is not None:
        PROC = None                                   # reaped: never a zombie
    try:
        pid = int(PIDF.read_text())
        os.kill(pid, 0)
        return pid if Path(f"/proc/{pid}/cmdline").read_bytes().find(b"perimeter_pipeline.py") >= 0 else None
    except Exception:
        return None


def host_health() -> dict:
    mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo") if ":" in l}
    temps = []
    for p in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            temps.append(int(p.read_text()) / 1000)
        except Exception:                             # some Jetson zones cannot be read (as Workplace Analytics)
            pass
    du = shutil.disk_usage(DATA)
    return {"mem_available_gb": round(mem.get("MemAvailable", 0) / 1048576, 2), "mem_total_gb": round(mem.get("MemTotal", 0) / 1048576, 1),
            "temp_c": round(max(temps)) if temps else None, "free_disk_gb": round(du.free / 1e9, 1)}


def pipeline_logs() -> list:
    return sorted(LOGS.glob("pipeline_*.log"))


def read_tail(path: Path, n: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - n))
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def launch():
    global PROC
    LOGS.mkdir(parents=True, exist_ok=True)
    for old in pipeline_logs()[:-9]:
        old.unlink()                                  # the last 10 run logs
    lf = LOGS / f"pipeline_{_ts()}.log"
    with open(lf, "a") as f:
        PROC = subprocess.Popen([PY, "-u", str(APP / "perimeter_pipeline.py"), "--config", str(RUN / "config_run.json"),
                                 "--pgie", str(RUN / "pgie_run.txt")], stdout=f, stderr=subprocess.STDOUT,
                                cwd=str(APP), start_new_session=True, env={**os.environ, "TRINETRA_DATA": str(DATA)})
    PIDF.write_text(str(PROC.pid))
    return PROC, lf


def stop_pipeline(say=print):
    pid = pid_alive()
    if not pid:
        say("pipeline not running")
        return
    say(f"stopping the pipeline (pid {pid}): clean stop, up to 30 s")
    os.kill(pid, signal.SIGTERM)
    for _ in range(60):
        time.sleep(0.5)
        if not pid_alive():
            say("pipeline stopped cleanly")
            return
    os.kill(pid, signal.SIGKILL)
    time.sleep(1)
    say("pipeline did not stop in 30 s - killed")


def exit_reason() -> Optional[str]:
    if pid_alive():
        return None
    st = read_json(STATE, {}) or {}
    return (st.get("exit_reason") or ("crash" if st.get("running") else None)) if st else None


def running_state() -> dict:
    st = read_json(STATE, {}) or {}
    pid = pid_alive()
    return st if pid and st.get("running") and st.get("pid") == pid else {}


# ─── jobs (start / build engine / stop): one at a time, step by step, shown on the page ───────────────────────────
class Job:
    def __init__(self, action: str):
        self.action, self.status, self.lines, self.error = action, "running", [], None
        self.started, self.ended, self.cancel = datetime.now().isoformat(timespec="seconds"), None, False

    def log(self, msg: str):
        line = f"{datetime.now():%H:%M:%S} {msg}"
        self.lines.append(line)
        del self.lines[:-400]
        print(f"[ds] {msg}", flush=True)

    def summary(self) -> dict:
        return {"action": self.action, "status": self.status, "started": self.started, "ended": self.ended,
                "error": self.error, "lines": self.lines[-120:]}


JOB: Optional[Job] = None


def start_job(action: str, fn) -> Job:
    global JOB
    with _lock:
        if JOB and JOB.status == "running":
            raise HTTPException(409, f"Busy: {JOB.action} is still running")
        JOB = job = Job(action)

    def run():
        try:
            fn(job)
            job.status = "done"
        except Exception as e:
            job.status, job.error = "failed", str(e)
            job.log(f"FAILED: {e}")
        job.ended = datetime.now().isoformat(timespec="seconds")
    threading.Thread(target=run, daemon=True, name=f"ds-{action}").start()
    return job


def build_engine(job: Job, model: dict, plan: dict):
    """as Workplace Analytics: the pipeline stopped, one video, nvinfer with batch N writes the engine; renamed when done"""
    h = host_health()
    if h["mem_available_gb"] < 2.5:
        raise RuntimeError(f"only {h['mem_available_gb']} GB RAM available (< 2.5 GB) - not building")
    ENGINES.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    work = ENGINES / "build_tmp"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    target = Path(plan["file"])
    (work / "build_infer.txt").write_text(pgie_text(model, str(target), plan["size"], 0.4))
    argv = ["gst-launch-1.0", "-e", "uridecodebin", f"uri=file://{BUILD_VIDEO}", "!", "m.sink_0", "nvstreammux", "name=m",
            "batch-size=1", "width=1920", "height=1080", "!", "nvinfer", f"config-file-path={work / 'build_infer.txt'}",
            f"batch-size={plan['size']}", "!", "fakesink", "sync=false"]
    log_path = LOGS / f"engine_build_b{plan['size']}_{_ts()}.log"
    for old in sorted(LOGS.glob("engine_build_*.log"))[:-5]:
        old.unlink()
    job.log(f"building the FP16 TensorRT engine of {model['name']} for {plan['size']} cameras (one-time, ~{plan['minutes']} min)")
    t0, pos, note, done = time.time(), 0, time.time(), None
    with open(log_path, "w") as f:
        p = subprocess.Popen(argv, cwd=work, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        while True:
            with open(log_path, errors="ignore") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            for line in chunk.splitlines():
                m = SERIALIZE_RE.search(line)
                if m:
                    done = Path(m.group(1)) if Path(m.group(1)).is_absolute() else work / m.group(1)
            if done and done.is_file():
                break
            if job.cancel:
                raise RuntimeError("stopped by the user during the engine build")
            if p.poll() is not None and not done:
                raise RuntimeError(f"the engine build stopped (exit code {p.returncode}) before the engine was saved — see {log_path.name}")
            if time.time() - t0 > plan["minutes"] * 2.5 * 60:
                raise RuntimeError("the engine build took too long")
            if time.time() - note > 60:
                note = time.time()
                job.log(f"... still building ({(time.time() - t0) / 60:.0f} of ~{plan['minutes']} min)")
            time.sleep(1)
    finally:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGINT)
                time.sleep(3)
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
    tmp = target.with_name(target.name + ".tmp")
    shutil.move(str(done), str(tmp))
    os.sync()
    os.replace(tmp, target)
    shutil.rmtree(work, ignore_errors=True)
    job.log(f"engine built in {(time.time() - t0) / 60:.1f} min → {target.name}")


def run_plan(cfg: dict):
    streams = [s for s in cfg["streams"] if s["enabled"]]
    if not streams:
        raise RuntimeError("no camera switched on: add one on the Cameras tab")
    model = model_by_name(cfg["model"])
    if not model:
        raise RuntimeError("no model: put <name>.onnx + <name>.labels.txt in the data folder's ds/models")
    return streams, model, engine_plan(model, len(streams))


def write_run_configs(cfg: dict, streams: list, model: dict, plan: dict):
    run = {"streams": [{"name": s["name"], "type": "file" if s["uri"].startswith("file://") else "rtsp", "uri": s["uri"],
                        "transport": s["transport"]} for s in streams],
           "labels": model["labels"], "save_every_s": cfg["save_every_s"], "frame_skip": cfg["frame_skip"],
           "loop_files": cfg["loop_files"], "incoming": config.INCOMING_DIR, "zones_file": str(ZONES)}
    write_atomic(RUN / "config_run.json", json.dumps(run, indent=1))
    write_atomic(RUN / "pgie_run.txt", pgie_text(model, plan["file"], len(streams), cfg["threshold"]))


def wait_ready(job: Job, n: int):
    t0 = time.time()
    while time.time() - t0 < 180:
        st = running_state()
        if st and st.get("frames", 0) > 0:
            on = sum(1 for s in st.get("streams", []) if s.get("online"))
            job.log(f"pipeline running: {on} of {n} camera(s) delivering frames")
            return
        if not pid_alive():
            tail = read_tail(pipeline_logs()[-1], 3000).strip().splitlines()[-6:] if pipeline_logs() else []
            raise RuntimeError("the pipeline stopped while starting: " + " | ".join(tail)[-400:])
        time.sleep(2)
    job.log("pipeline started; no frames yet after 3 min (cameras are retried every 10 s)")


def do_start(job: Job):
    cfg = load_config()
    streams, model, plan = run_plan(cfg)
    if pid_alive():
        stop_pipeline(job.log)
    if not Path(plan["file"]).is_file():
        build_engine(job, model, plan)
    else:
        job.log(f"engine ready: {Path(plan['file']).name}")
    if job.cancel:
        raise RuntimeError("cancelled")
    write_run_configs(cfg, streams, model, plan)
    h = host_health()
    if h["mem_available_gb"] < 1.5:
        raise RuntimeError(f"only {h['mem_available_gb']} GB RAM available (< 1.5 GB) - not starting")
    _, lf = launch()
    set_intent(True, "started on the DeepStream page")
    job.log(f"pipeline started ({len(streams)} camera(s), model {model['name']}, log {lf.name})")
    wait_ready(job, len(streams))


def do_stop(job: Job):
    set_intent(False, "stopped on the DeepStream page")
    stop_pipeline(job.log)


def do_build(job: Job):
    cfg = load_config()
    streams, model, plan = run_plan(cfg)
    if Path(plan["file"]).is_file():
        job.log(f"engine already built: {Path(plan['file']).name}")
        return
    was = pid_alive()
    if was:
        stop_pipeline(job.log)
    build_engine(job, model, plan)
    if was and intent():
        write_run_configs(cfg, streams, model, plan)
        launch()
        job.log("pipeline started again")


# ─── supervisor: the user wants it running but it is not (crash, stall, safety, reboot) → start it again ──────────
def free_memory() -> str:
    """memory held while the pipeline is stopped (as Workplace Analytics): a stray perimeter_pipeline.py the pid file does
    not point to (one that hung while exiting), helper processes of this dashboard (probes / frame grabs) older than
    2 min; then the page cache (best effort). The dashboard and a running pipeline are never touched."""
    done, me, cur = [], os.getpid(), pid_alive()
    try:
        btime = next(int(l.split()[1]) for l in open("/proc/stat") if l.startswith("btime"))
    except (OSError, StopIteration):
        btime = 0
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) in (1, me, cur):
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            st = (d / "stat").read_text().rsplit(")", 1)[1].split()          # after "(comm)": state ppid … starttime (#20)
            ppid, age = int(st[1]), time.time() - (btime + int(st[19]) / os.sysconf("SC_CLK_TCK"))
        except (OSError, ValueError, IndexError):
            continue
        if "perimeter_pipeline.py" in cmd or (ppid == me and age > 120 and " -c " in cmd):
            try:
                os.kill(int(d.name), signal.SIGKILL)
                done.append(f"killed pid {d.name} ({cmd[:60].strip()}…)")
            except OSError:
                pass
    try:
        os.sync()
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("1")
        done.append("page cache dropped")
    except OSError:
        pass
    return ", ".join(done) or "nothing to free"


def supervise_once(now: Optional[float] = None):
    now = now or time.time()
    if not intent() or pid_alive() or (JOB and JOB.status == "running"):
        DOWN.update(since=None, freed=False)
        return None
    if not (RUN / "config_run.json").exists():
        return None
    if DOWN["since"] is None:
        DOWN.update(since=now, said=now - 480, freed=False)
    why = exit_reason() or "dashboard restart"
    down = now - DOWN["since"]
    if why == "safety" and down < RESTART_SETTLE_S:
        return None                                   # a memory stop: let the memory come back first
    RESTARTS[:] = [t for t in RESTARTS if now - t < 3600]
    h, wait = host_health(), None
    if len(RESTARTS) >= MAX_RESTARTS_PER_HOUR:
        wait = f"{MAX_RESTARTS_PER_HOUR} restarts in the last hour"
    elif h["mem_available_gb"] < RESTART_MIN_GB:
        wait = f"only {h['mem_available_gb']} GB RAM available (needs {RESTART_MIN_GB} GB)"
        if down >= FREE_MEM_AFTER_S and not DOWN["freed"]:
            DOWN["freed"] = True
            print(f"[ds] supervisor: {wait} - freeing memory: {free_memory()}", flush=True)
            h = host_health()
            if h["mem_available_gb"] >= RESTART_MIN_GB:
                wait = None                           # enough now: start at once
    elif (h["temp_c"] or 0) > 80:
        wait = f"Jetson too hot ({h['temp_c']} °C)"
    elif h["free_disk_gb"] < 5:
        wait = f"only {h['free_disk_gb']} GB free disk"
    if wait:
        if now - DOWN["said"] >= 600:
            DOWN["said"] = now
            print(f"[ds] supervisor: pipeline down {int(down // 60)} min after '{why}', waiting: {wait}", flush=True)
        return None
    RESTARTS.append(now)
    p, lf = launch()
    DOWN.update(since=None, freed=False)
    print(f"[ds] supervisor: pipeline started again after '{why}' (down {int(down)} s, "
          f"{h['mem_available_gb']} GB RAM available; pid {p.pid}, {lf.name})", flush=True)
    return p


def nightly_restart(now: Optional[datetime] = None) -> bool:
    """once a night at the time set on the Pipeline tab (within 10 min, also after a dashboard restart): a running
    pipeline is stopped cleanly and started again — frees the memory a long run collects (camera reconnects). Never
    during an action, never when the user stopped it (as Workplace Analytics)."""
    now = now or datetime.now()
    at_s = (load_config().get("nightly_restart") or "").strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at_s):
        return False
    at = datetime.combine(now.date(), datetime.strptime(at_s, "%H:%M").time())
    if not at <= now < at + timedelta(minutes=10):
        return False
    if (read_json(NIGHTLY_FILE, {}) or {}).get("day") == now.date().isoformat():
        return False
    write_atomic(NIGHTLY_FILE, json.dumps({"day": now.date().isoformat(), "at": now.isoformat(timespec="seconds")}))
    if not intent() or not pid_alive() or (JOB and JOB.status == "running"):
        return False
    print(f"[ds] nightly restart: stopping the pipeline cleanly", flush=True)
    stop_pipeline()
    p, lf = launch()
    print(f"[ds] nightly restart: pipeline started again (pid {p.pid}, {lf.name})", flush=True)
    return True


def supervisor():
    while True:
        time.sleep(10)
        try:
            nightly_restart()
        except Exception as e:
            print(f"[ds] nightly restart: {e!r}", flush=True)
        try:
            supervise_once()
        except Exception as e:
            print(f"[ds] supervisor: {e!r}", flush=True)


def start():
    for d in (DS, RUN, ENGINES, USER_MODELS, FRAMES, LOGS):
        d.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=supervisor, daemon=True, name="ds-supervisor").start()


def local_stop():
    """the container / Jetson shuts down: stop the pipeline cleanly, keep the intent (it starts again afterwards)"""
    stop_pipeline()


# ════════════════════════════════════════════════════════════════════════════
# API (/api/ds/*: the Trinetra team only — access.py)
# ════════════════════════════════════════════════════════════════════════════
async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except ValueError:
        raise HTTPException(422, "Invalid request")
    if not isinstance(b, dict):
        raise HTTPException(422, "Invalid request")
    return b


def _cfg_out(cfg: dict) -> dict:
    return {**cfg, "models": list_models(), "max_cameras": MAX_CAMERAS,
            "sample_videos": sorted(str(p) for p in Path(SAMPLE_VIDEOS).glob("*.mp4")) if _exists(SAMPLE_VIDEOS) else []}


@router.get("/api/ds/status")
def api_status():
    cfg = load_config()
    st, pid = running_state(), pid_alive()
    streams = [s for s in cfg["streams"] if s["enabled"]]
    model = model_by_name(cfg["model"])
    plan = engine_plan(model, len(streams)) if model and streams else None
    if plan:
        plan["ready"] = Path(plan["file"]).is_file()
    return {"available": available(), "running": bool(pid), "pid": pid, "intent": intent(), "state": st,
            "exit_reason": exit_reason(), "health": host_health(), "job": JOB.summary() if JOB else None,
            "model": model["name"] if model else None, "engine": plan, "cameras": len(streams),
            "zones_drawn": sorted(c for c, z in load_zones().items() if z and (z.get("line") or z.get("alert")))}


@router.get("/api/ds/config")
def api_config():
    return _cfg_out(load_config())


@router.put("/api/ds/config")
async def api_config_save(request: Request):
    try:
        cfg = check_config(await _body(request))
    except ValueError as e:
        raise HTTPException(400, str(e))
    write_atomic(CONFIG, json.dumps(cfg, indent=1))
    names = {s["name"] for s in cfg["streams"]}
    z = load_zones()
    if any(k not in names for k in z):                # zones of cameras that are gone
        write_atomic(ZONES, json.dumps({k: v for k, v in z.items() if k in names}, indent=1))
    request.state.audit_target = f"{len(cfg['streams'])} camera(s)"
    return {**_cfg_out(cfg), "restart_needed": bool(pid_alive())}


@router.post("/api/ds/probe")
async def api_probe(request: Request):
    body = await _body(request)
    uri, tr = str(body.get("uri", "")).strip(), "udp" if body.get("transport") == "udp" else "tcp"
    if not re.match(r"^(rtsp|rtsps)://\S+$|^file:///\S+$", uri):
        raise HTTPException(400, "The address starts with rtsp:// or file:///")
    import asyncio
    return await asyncio.to_thread(probe, uri, tr)


@router.get("/api/ds/zones/{cam}")
def api_zone(cam: str):
    return load_zones().get(cam) or {"line": [], "outside": None, "alert": [], "ignore": []}


@router.put("/api/ds/zones/{cam}")
async def api_zone_save(cam: str, request: Request):
    if cam not in {s["name"] for s in load_config()["streams"]}:
        raise HTTPException(404, "No such camera")
    try:
        zone = Z.clean(await _body(request))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    z = load_zones()
    z[cam] = zone
    write_atomic(ZONES, json.dumps(z, indent=1))     # a running pipeline reads it again within 10 s
    return {"ok": True, "zone": zone, "applied": "within 10 s" if pid_alive() else "at the next start"}


@router.get("/api/ds/frame/{cam}")
async def api_frame(cam: str, refresh: int = 0):
    """the camera's picture for drawing / Live view: the running pipeline's frame (2 s old at most), else the saved
    one, else (or refresh=1 while stopped) a new one grabbed from the camera"""
    s = next((x for x in load_config()["streams"] if x["name"] == cam), None)
    if not s:
        raise HTTPException(404, "No such camera")
    live = SNAPS / f"{cam}.jpg"
    if pid_alive() and live.is_file() and time.time() - live.stat().st_mtime < 30:
        return FileResponse(live, headers={"Cache-Control": "no-store"})
    saved = FRAMES / f"{cam}.jpg"
    if refresh or not saved.is_file():
        import asyncio
        r = await asyncio.to_thread(grab_frame, s)
        if not r.get("ok") and not saved.is_file():
            raise HTTPException(502, f"No picture from {cam}: {r.get('error')}")
    return FileResponse(saved, headers={"Cache-Control": "no-store"})


@router.get("/api/ds/live/{cam}")
def api_live(cam: str):
    """Live view: the running pipeline's annotated frame (zones + boxes)"""
    live = SNAPS / f"{cam}.jpg"
    if not CAM_RE.match(cam) or not live.is_file():
        raise HTTPException(404, "No live frame (the pipeline is not running, or this camera has no frames yet)")
    return FileResponse(live, headers={"Cache-Control": "no-store"})


@router.post("/api/ds/start")
def api_start(request: Request):
    request.state.audit_target = "start"
    return start_job("start", do_start).summary()


@router.post("/api/ds/stop")
def api_stop(request: Request):
    request.state.audit_target = "stop"
    if JOB and JOB.status == "running":           # e.g. an engine build: cancelled, and the pipeline stays off
        JOB.cancel = True
        set_intent(False, "stopped on the DeepStream page")
        return {"action": JOB.action, "status": "cancelling", "lines": JOB.lines[-20:] + ["cancelling …"]}
    return start_job("stop", do_stop).summary()


@router.post("/api/ds/build")
def api_build(request: Request):
    request.state.audit_target = "build engine"
    return start_job("build engine", do_build).summary()


@router.get("/api/ds/job")
def api_job():
    return JOB.summary() if JOB else {"status": None, "lines": []}


@router.get("/api/ds/log")
def api_log(kind: str = "pipeline", lines: int = 200):
    files = sorted(LOGS.glob("engine_build_*.log")) if kind == "engine" else pipeline_logs()
    if not files:
        return {"file": None, "text": ""}
    text = read_tail(files[-1], 120_000).splitlines()[-max(10, min(lines, 2000)):]
    return {"file": files[-1].name, "text": "\n".join(text)}
