#!/usr/bin/env python3
"""Trinetra Perimeter — the Recordings page's worker (Trinetra team): record cameras on this Jetson, cut the video into
frames, compress + zip them for the laptop. One process per job, started by captures.py in its own session: a dashboard
restart never stops it, and every step resumes where it stopped (power cut, crash, the worker killed).

  capture_worker.py record <capture-dir> <camera>   the camera address comes on stdin (JSON {"uri", "transport"}), never
                                                    in argv or in the capture folder (it holds the camera's password)
  capture_worker.py frames <capture-dir>            settings: <capture-dir>/frames.json → progress frames.state.json
  capture_worker.py pack   <capture-dir>            settings: <capture-dir>/pack.json   → progress pack.state.json

Video is copied as it comes from the camera (no re-encoding, almost no CPU) into 60 s Matroska pieces written as a stream
(nothing rewritten at the end): a power cut loses at most the last seconds, a cut-off piece still plays. (MPEG-TS refused
cameras that send B-frames: "Timestamping error on input streams".) Frames: the Jetson's video decoder; only the frames kept are
converted. Package: zip parts of ~50 MB (JPEGs stored, not deflated: they are compressed already) + manifest.json +
SHA256SUMS, each part checksummed so the laptop can resume and verify part by part.
"""
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

OWNER = int(os.environ.get("CAPTURES_UID", "1000"))      # the Jetson's login user: the laptop fetches over ssh as it
STOP_GB = float(os.environ.get("CAPTURE_STOP_GB", "20"))  # free disk under this → the job stops (the disk guard is lower)
SEG_S = int(os.environ.get("CAPTURE_SEGMENT_S", "60"))
SEG_RE = re.compile(r"^seg_(\d{5})\.mkv$")
TERM = {"now": False}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def own(p: Path):
    try:
        os.chown(p, OWNER, OWNER)
    except OSError:
        pass


def write_json(path: Path, obj, sync: bool = False):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
        if sync:
            f.flush()
            os.fsync(f.fileno())
    os.replace(tmp, path)
    own(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def mkdir(p: Path) -> Path:
    if not p.is_dir():
        p.mkdir(parents=True, exist_ok=True)
        own(p)
    return p


class Log:
    def __init__(self, cap: Path, tag: str):
        self.path, self.tag = cap / "log.txt", tag

    def __call__(self, msg: str):
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [{self.tag}] {msg}\n"
        try:
            with open(self.path, "a") as f:                 # O_APPEND: one line from each worker stays whole
                f.write(line)
            own(self.path)
        except OSError:
            pass
        print(line, end="", flush=True)


def free_gb(p: Path) -> float:
    try:
        return shutil.disk_usage(p).free / 1e9
    except OSError:
        return 1e9


def on_term(*_):
    TERM["now"] = True


signal.signal(signal.SIGTERM, on_term)
signal.signal(signal.SIGINT, on_term)
signal.signal(signal.SIGHUP, signal.SIG_IGN)


def gst():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    return Gst


# ════════════════════════════════════════════════════════════════════════════
# record
# ════════════════════════════════════════════════════════════════════════════
def cmd_record(cap: Path, cam: str):
    src = json.loads(sys.stdin.read() or "{}")
    uri, transport = str(src.get("uri", "")), "udp" if src.get("transport") == "udp" else "tcp"
    log = Log(cap, f"record {cam}")
    meta = read_json(cap / "capture.json", {}) or {}
    end = datetime.fromisoformat(meta["end"])
    vdir = mkdir(cap / "video" / cam)
    stf, segf = vdir / "status.json", vdir / "segments.jsonl"
    old = read_json(stf, {}) or {}
    st = {"camera": cam, "pid": os.getpid(), "state": "connecting", "codec": old.get("codec"), "since": None,
          "reconnects": old.get("reconnects", 0), "restarts": old.get("restarts", -1) + 1, "recorded_s": old.get("recorded_s", 0.0),
          "bytes": 0, "segments": 0, "last_error": old.get("last_error"), "end": meta["end"], "updated": now_iso()}

    def disk_bytes():
        segs = [p for p in vdir.iterdir() if SEG_RE.match(p.name)]
        return len(segs), sum(p.stat().st_size for p in segs), max([int(SEG_RE.match(p.name).group(1)) for p in segs] or [0])

    def save():
        st["segments"], st["bytes"], _ = disk_bytes()
        st["updated"] = now_iso()
        write_json(stf, st)

    def why_stop():
        if TERM["now"]:
            return "the worker was told to stop"
        if (cap / "STOP").exists():
            return "stopped on the Recordings page"
        if datetime.now() >= end:
            return "the planned time is over"
        if free_gb(cap) < STOP_GB:
            return f"the disk is nearly full (under {STOP_GB:g} GB free)"
        return None

    if not uri:
        st.update(state="failed", last_error="no camera address")
        save()
        log("FAILED: no camera address")
        return 1
    log(f"recording until {end:%H:%M:%S} ({'resumed' if st['restarts'] else 'start'}; {transport.upper()})")
    Gst = gst()
    backoff = 5
    reason = None
    while True:
        reason = why_stop()
        if reason:
            break
        _, _, last = disk_bytes()
        res, had_video = record_once(Gst, uri, transport, vdir, segf, last + 1, st, save, why_stop, log)
        reason = why_stop()
        if reason:
            break
        if had_video:
            backoff = 5                                  # it was recording: try again soon
        st["reconnects"] += 1
        st.update(state="retrying", since=None, last_error=res)
        save()
        log(f"camera: {res} — trying again in {backoff} s")
        t_end = time.time() + backoff
        while time.time() < t_end and not why_stop():
            time.sleep(0.5)
        backoff = min(30, backoff * 2)
    if TERM["now"]:                                  # container stop / shutdown: the dashboard carries on after it
        st.update(state="interrupted", since=None, pid=None)
        save()
        log(f"recorder stopped by a shutdown — the recording carries on when the dashboard is back (until {end:%H:%M:%S})")
        return 0
    st.update(state="done", since=None, stop_reason=reason, pid=None)
    save()
    log(f"recording ended: {reason} · {st['segments']} piece(s), {st['bytes'] / 1e6:.1f} MB, "
        f"{st['recorded_s'] / 60:.1f} min of video")
    return 0


def record_once(Gst, uri, transport, vdir, segf, start_index, st, save, why_stop, log) -> str:
    """one connection: camera → (depay, parse) → 60 s .mkv pieces; returns why it ended (error, end of stream, stall)"""
    mk = Gst.ElementFactory.make
    pipe = Gst.Pipeline()
    mux = mk("matroskamux")
    mux.set_property("streamable", True)              # written front to back: a piece cut off still plays
    smux = mk("splitmuxsink")
    smux.set_property("location", str(vdir / "seg_%05d.mkv"))
    smux.set_property("max-size-time", SEG_S * Gst.SECOND)
    smux.set_property("muxer", mux)
    smux.set_property("start-index", start_index)
    pipe.add(smux)
    vpad = smux.request_pad_simple("video") if hasattr(smux, "request_pad_simple") else smux.get_request_pad("video")
    q = mk("queue")
    q.set_property("max-size-time", 4 * Gst.SECOND)
    pipe.add(q)
    q.get_static_pad("src").link(vpad)
    flow = {"linked": False, "last": 0.0, "first": 0.0, "bytes": 0, "err": None}

    def counter(_pad, info):
        b = info.get_buffer()
        t = time.time()
        if not flow["first"]:
            flow["first"] = t
        flow["last"], flow["bytes"] = t, flow["bytes"] + (b.get_size() if b else 0)
        return Gst.PadProbeReturn.OK

    def video_branch(codec: str, depay_name=None):
        els = []
        if depay_name:
            els.append(mk(depay_name))
        parse = mk("h264parse" if codec == "H.264" else "h265parse")
        parse.set_property("config-interval", -1)       # SPS/PPS with every key frame: each piece decodes on its own
        els.append(parse)
        if not uri.startswith("rtsp"):
            ident = mk("identity")
            ident.set_property("sync", True)            # a test video plays at camera speed, not as fast as the disk
            els.append(ident)
        for e in els:
            pipe.add(e)
            e.sync_state_with_parent()
        for a, b in zip(els, els[1:]):
            a.link(b)
        els[-1].link(q)
        els[-1].get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, counter)
        st["codec"] = codec
        flow["linked"] = True
        return els[0].get_static_pad("sink")

    def sink_away(pad):                                  # audio / metadata: not recorded, never "not linked"
        fs = mk("fakesink")
        fs.set_property("sync", False)
        fs.set_property("async", False)
        pipe.add(fs)
        fs.sync_state_with_parent()
        pad.link(fs.get_static_pad("sink"))

    if uri.startswith("rtsp"):
        src = mk("rtspsrc")
        src.set_property("location", uri)
        src.set_property("protocols", 7 if transport == "udp" else 4)
        src.set_property("latency", 300)
        src.set_property("tcp-timeout", 10_000_000)

        def pad_added(_e, pad):
            s = (pad.get_current_caps() or pad.query_caps(None)).get_structure(0)
            enc = (s.get_string("encoding-name") or "").upper()
            if s.get_string("media") == "video" and not flow["linked"] and enc in ("H264", "H265"):
                pad.link(video_branch("H.264" if enc == "H264" else "H.265", "rtph264depay" if enc == "H264" else "rtph265depay"))
            else:
                if s.get_string("media") == "video" and not flow["linked"]:
                    flow["err"] = f"the camera sends {enc or 'an unknown codec'}: only H.264 / H.265 can be recorded"
                sink_away(pad)
        src.connect("pad-added", pad_added)
        pipe.add(src)
    else:
        src, pb = mk("filesrc"), mk("parsebin")
        src.set_property("location", uri[7:] if uri.startswith("file://") else uri)
        pipe.add(src)
        pipe.add(pb)
        src.link(pb)

        def pad_added(_e, pad):
            name = (pad.get_current_caps() or pad.query_caps(None)).get_structure(0).get_name()
            if name in ("video/x-h264", "video/x-h265") and not flow["linked"]:
                pad.link(video_branch("H.264" if name == "video/x-h264" else "H.265"))
            else:
                sink_away(pad)
        pb.connect("pad-added", pad_added)

    bus = pipe.get_bus()
    st.update(state="connecting", since=None)
    save()
    pipe.set_state(Gst.State.PLAYING)
    play_wall = datetime.now()                         # running time 0 ≈ now: a piece starts at play_wall + its running time
    t0, last_save, result = time.time(), 0.0, None
    rec_base = st["recorded_s"]
    while result is None:
        m = bus.timed_pop_filtered(500 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.ELEMENT)
        if m is not None:
            if m.type == Gst.MessageType.ERROR:
                err, dbg = m.parse_error()
                result = flow["err"] or f"error: {err.message}"
            elif m.type == Gst.MessageType.EOS:
                result = "the stream ended"
            elif m.get_structure() and m.get_structure().get_name() == "splitmuxsink-fragment-opened":
                ms = m.get_structure()
                loc = ms.get_string("location") or ""
                ok_rt, rt = ms.get_uint64("running-time")
                begin = play_wall + timedelta(seconds=rt / 1e9) if ok_rt else datetime.now()
                with open(segf, "a") as f:
                    f.write(json.dumps({"file": Path(loc).name, "start": begin.isoformat(timespec="milliseconds")}) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                own(segf)
        t = time.time()
        if flow["err"]:
            result = flow["err"]
        if flow["first"] and st["state"] != "recording":
            st.update(state="recording", since=now_iso(), last_error=None)
            log(f"camera connected ({st['codec']}) — recording")
            save()
            last_save = t
        if flow["first"]:
            st["recorded_s"] = round(rec_base + (flow["last"] - flow["first"]), 1)
        if result is None and not flow["first"] and t - t0 > 30:
            result = "no video within 30 s (camera not reachable, wrong address or password?)"
        if result is None and flow["first"] and t - flow["last"] > 20:
            result = "no video for 20 s (the camera or the network dropped)"
        if result is None and why_stop():
            break
        if t - last_save > 2:
            save()
            last_save = t
    # a clean end: EOS lets the last piece close properly (a piece cut off still plays)
    if result is None:
        pipe.send_event(Gst.Event.new_eos())
        bus.timed_pop_filtered(5 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipe.set_state(Gst.State.NULL)
    save()
    return result or "stopped", bool(flow["first"])


# ════════════════════════════════════════════════════════════════════════════
# frames
# ════════════════════════════════════════════════════════════════════════════
def seg_starts(vdir: Path) -> dict:
    out = {}
    try:
        for line in (vdir / "segments.jsonl").read_text().splitlines():
            try:
                d = json.loads(line)
                out[d["file"]] = datetime.fromisoformat(d["start"])
            except (ValueError, KeyError):
                pass
    except OSError:
        pass
    return out


def cmd_frames(cap: Path):
    import cv2
    import numpy as np
    log = Log(cap, "frames")
    cfg = read_json(cap / "frames.json", {}) or {}
    every, skip_similar = float(cfg.get("every_s", 1)), bool(cfg.get("skip_similar"))
    sf = cap / "frames.state.json"
    st = read_json(sf, {}) or {}
    fdir = cap / "frames"
    if st.get("run") != cfg.get("run"):
        shutil.rmtree(fdir, ignore_errors=True)
        st = {"run": cfg.get("run"), "done": {}, "kept": 0, "skipped": 0, "bytes": 0, "started": now_iso()}
        log(f"making frames: one every {every:g} s{', near-identical frames skipped' if skip_similar else ''}")
    else:
        log(f"making frames: resumed ({len(st['done'])} piece(s) already done)")
    st.update(state="running", pid=os.getpid(), error=None)
    meta = read_json(cap / "capture.json", {}) or {}
    work = []
    for cam in meta.get("cameras", []):
        vdir = cap / "video" / cam
        if vdir.is_dir():
            starts = seg_starts(vdir)
            for p in sorted(vdir.iterdir()):
                if SEG_RE.match(p.name) and p.stat().st_size > 0:
                    work.append((cam, p, starts.get(p.name)))
    st["total"] = len(work)
    write_json(sf, st, sync=True)
    Gst = gst()
    nv = Gst.ElementFactory.find("nvvideoconvert") is not None
    for cam, seg, start in work:
        key = f"{cam}/{seg.name}"
        if key in st["done"]:
            continue
        if TERM["now"]:
            return 1
        if free_gb(cap) < STOP_GB:
            st.update(state="failed", error=f"the disk is nearly full (under {STOP_GB:g} GB free)")
            write_json(sf, st, sync=True)
            log(f"FAILED: {st['error']}")
            return 1
        out = mkdir(fdir / cam)
        start = start or (datetime.fromtimestamp(seg.stat().st_mtime) - timedelta(seconds=SEG_S))
        kept, skipped, nbytes, err = extract(Gst, cv2, np, nv, seg, out, cam, start, every, skip_similar)
        st["done"][key] = kept
        st["kept"] += kept
        st["skipped"] += skipped
        st["bytes"] += nbytes
        st["updated"] = now_iso()
        write_json(sf, st, sync=True)
        if err:
            log(f"{key}: {kept} frame(s), ended early: {err}")
    n = sum(1 for _ in fdir.rglob("*.jpg")) if fdir.is_dir() else 0
    st.update(state="done", kept=n, ended=now_iso(), pid=None)
    write_json(sf, st, sync=True)
    skipped = f" ({st['skipped']} near-identical skipped)" if st["skipped"] else ""
    log(f"frames done: {n} frame(s), {st['bytes'] / 1e6:.0f} MB{skipped}")
    return 0


def extract(Gst, cv2, np, nv, seg: Path, out: Path, cam: str, start: datetime, every: float, skip_similar: bool):
    """one video piece → a JPEG every `every` s of video (named by the real time it shows); only kept frames are converted"""
    mk = Gst.ElementFactory.make
    pipe = Gst.Pipeline()
    src, conv, caps, sink = mk("uridecodebin"), mk("nvvideoconvert" if nv else "videoconvert"), mk("capsfilter"), mk("appsink")
    src.set_property("uri", seg.resolve().as_uri())
    caps.set_property("caps", Gst.Caps.from_string("video/x-raw, format=BGRx"))
    sink.set_property("sync", False)
    sink.set_property("max-buffers", 4)
    for e in (src, conv, caps, sink):
        pipe.add(e)
    conv.link(caps)
    caps.link(sink)
    pick = {"first": None, "next": 0.0}

    def choose(_pad, info):                            # decide before the conversion: dropped frames cost nothing
        b = info.get_buffer()
        if b is None or b.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.DROP
        if pick["first"] is None:
            pick["first"] = b.pts
        t = (b.pts - pick["first"]) / Gst.SECOND
        if t + 1e-3 < pick["next"]:
            return Gst.PadProbeReturn.DROP
        pick["next"] = (int((t + 1e-3) // every) + 1) * every      # one frame per `every` seconds of video
        return Gst.PadProbeReturn.OK

    conv.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, choose)

    def pad_added(_e, pad):
        c = pad.get_current_caps() or pad.query_caps(None)
        if c.to_string().startswith("video/") and not conv.get_static_pad("sink").is_linked():
            pad.link(conv.get_static_pad("sink"))
    src.connect("pad-added", pad_added)
    pipe.set_state(Gst.State.PLAYING)
    bus = pipe.get_bus()
    kept = skipped = nbytes = 0
    last_small, err = None, None
    while True:
        smp = sink.emit("try-pull-sample", 1 * Gst.SECOND)
        if smp is None:
            m = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if m is not None:
                if m.type == Gst.MessageType.ERROR:
                    err = m.parse_error()[0].message
                break
            if sink.get_property("eos") or TERM["now"]:
                break
            continue
        buf = smp.get_buffer()
        stc = smp.get_caps().get_structure(0)
        w, h = stc.get_value("width"), stc.get_value("height")
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            continue
        a = np.frombuffer(mi.data, np.uint8)
        stride = len(a) // h
        img = a[: stride * h].reshape(h, stride)[:, : w * 4].reshape(h, w, 4)[:, :, :3].copy()
        buf.unmap(mi)
        if skip_similar:
            small = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (64, 36), interpolation=cv2.INTER_AREA).astype(np.int16)
            if last_small is not None and float(np.abs(small - last_small).mean()) < 2.5:
                skipped += 1
                continue
            last_small = small
        when = start + timedelta(seconds=(buf.pts - pick["first"]) / Gst.SECOND)
        name = f"{cam}_{when:%Y%m%d-%H%M%S}-{when.microsecond // 1000:03d}.jpg"
        okj, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if okj:
            tmp = out / (name + ".part")
            tmp.write_bytes(jpg.tobytes())
            os.replace(tmp, out / name)
            kept += 1
            nbytes += len(jpg)
    pipe.set_state(Gst.State.NULL)
    return kept, skipped, nbytes, err


# ════════════════════════════════════════════════════════════════════════════
# pack
# ════════════════════════════════════════════════════════════════════════════
def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_pack(cap: Path):
    import cv2
    import numpy as np
    log = Log(cap, "pack")
    cfg = read_json(cap / "pack.json", {}) or {}
    max_px, quality = int(cfg.get("max_px", 1280)), int(cfg.get("quality", 85))
    part_bytes = int(cfg.get("part_mb", 50)) * 1_000_000
    sf, pkg = cap / "pack.state.json", cap / "package"
    st = read_json(sf, {}) or {}
    if st.get("run") != cfg.get("run"):
        shutil.rmtree(pkg, ignore_errors=True)
        st = {"run": cfg.get("run"), "parts": [], "frames_done": 0, "started": now_iso()}
        log(f"compress + zip: {'original size' if not max_px else f'longest side {max_px} px'}, JPEG quality {quality}, "
            f"parts of {part_bytes // 1_000_000} MB")
    else:
        log(f"compress + zip: resumed after part {len(st['parts'])}")
    mkdir(pkg)
    for p in pkg.glob("*.tmp"):
        p.unlink()                                     # a part cut off half-way is made again
    frames = sorted(cap.glob("frames/*/*.jpg"), key=lambda p: (p.parent.name, p.name))
    st.update(state="running", pid=os.getpid(), error=None, total=len(frames))
    write_json(sf, st, sync=True)
    i, part = st["frames_done"], None
    sizes = {}

    def close_part(zf, tmp, n_in, first_i):
        zf.close()
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        name = tmp.name[:-4]
        os.replace(tmp, pkg / name)
        own(pkg / name)
        st["parts"].append({"file": name, "bytes": (pkg / name).stat().st_size, "sha256": sha256(pkg / name), "frames": n_in})
        st["frames_done"] = first_i + n_in
        st["updated"] = now_iso()
        write_json(sf, st, sync=True)
        log(f"{name}: {n_in} frame(s), {st['parts'][-1]['bytes'] / 1e6:.1f} MB")

    while i < len(frames):
        if TERM["now"]:
            return 1
        if free_gb(cap) < STOP_GB:
            st.update(state="failed", error=f"the disk is nearly full (under {STOP_GB:g} GB free)")
            write_json(sf, st, sync=True)
            log(f"FAILED: {st['error']}")
            return 1
        if part is None:
            tmp = pkg / f"part-{len(st['parts']) + 1:03d}.zip.tmp"
            zf = zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True)
            part, n_in, first_i = (zf, tmp), 0, i
        p = frames[i]
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            i += 1
            continue
        h, w = img.shape[:2]
        sizes[f"{w}x{h}"] = sizes.get(f"{w}x{h}", 0) + 1
        if max_px and max(w, h) > max_px:
            s = max_px / max(w, h)
            img = cv2.resize(img, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        data = jpg.tobytes()
        zf, tmp = part
        if n_in and zf.fp.tell() + len(data) + 200 > part_bytes:
            close_part(zf, tmp, n_in, first_i)
            part = None
            continue                                   # this frame starts the next part
        m = re.search(r"_(\d{8})-(\d{6})", p.name)
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").timetuple()[:6] if m else (2026, 1, 1, 0, 0, 0)
        zi = zipfile.ZipInfo(f"{p.parent.name}/{p.name}", date_time=dt)
        zi.compress_type = zipfile.ZIP_STORED
        zf.writestr(zi, data)
        n_in += 1
        i += 1
        if i % 200 == 0:
            st["frames_now"] = i
            write_json(sf, st)
    if part is not None:
        close_part(part[0], part[1], n_in, first_i)
    meta = read_json(cap / "capture.json", {}) or {}
    fst = read_json(cap / "frames.state.json", {}) or {}
    per_cam = {}
    for p in frames:
        per_cam[p.parent.name] = per_cam.get(p.parent.name, 0) + 1
    manifest = {"format": "trinetra-capture/1", "site": meta.get("site"), "capture": meta.get("id"), "name": meta.get("name"),
                "cameras": meta.get("cameras"), "recorded_from": meta.get("start"), "recorded_until": meta.get("ended") or meta.get("end"),
                "frames": len(frames), "frames_per_camera": per_cam, "frame_every_s": (read_json(cap / "frames.json", {}) or {}).get("every_s"),
                "near_identical_skipped": fst.get("skipped", 0), "original_sizes": sizes,
                "max_px": max_px, "jpeg_quality": quality, "parts": st["parts"],
                "bytes": sum(p["bytes"] for p in st["parts"]), "packed": now_iso()}
    write_json(pkg / "manifest.json", manifest, sync=True)
    sums = "".join(f"{p['sha256']}  {p['file']}\n" for p in st["parts"]) + f"{sha256(pkg / 'manifest.json')}  manifest.json\n"
    (pkg / "SHA256SUMS.tmp").write_text(sums)
    os.replace(pkg / "SHA256SUMS.tmp", pkg / "SHA256SUMS")
    own(pkg / "SHA256SUMS")
    st.update(state="done", bytes=manifest["bytes"], ended=now_iso(), pid=None)
    write_json(sf, st, sync=True)
    log(f"package ready: {len(st['parts'])} part(s), {manifest['bytes'] / 1e6:.1f} MB, {len(frames)} frame(s)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("record", "frames", "pack"):
        print(__doc__)
        sys.exit(2)
    c = Path(sys.argv[2])
    try:
        if sys.argv[1] == "record":
            sys.exit(cmd_record(c, sys.argv[3]))
        sys.exit(cmd_frames(c) if sys.argv[1] == "frames" else cmd_pack(c))
    except SystemExit:
        raise
    except Exception as e:                             # the dashboard shows it; the supervisor may try once more
        Log(c, sys.argv[1])(f"FAILED: {e!r}")
        sf = c / ("frames.state.json" if sys.argv[1] == "frames" else "pack.state.json")
        if sys.argv[1] != "record":
            s = read_json(sf, {}) or {}
            s.update(state="failed", error=str(e)[:300], pid=None)
            write_json(sf, s, sync=True)
        sys.exit(1)
