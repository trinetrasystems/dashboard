#!/usr/bin/env python3
"""Perimeter DeepStream pipeline: people / objects at the perimeter → camera images in the dashboard's incoming/.

  python3 perimeter_pipeline.py --config <ds/run/config_run.json> --pgie <ds/run/pgie_run.txt>

config_run.json (written by ds_control.py from the DeepStream page):
  {"streams": [{"name", "type": "rtsp"|"file", "uri", "transport": "tcp"|"udp"}], "zones": {camera: zone},
   "labels": [...], "save_every_s": 5, "frame_skip": 2, "loop_files": true, "incoming": "/root/PERIMETER/data/incoming"}
Pipeline: N x nvurisrcbin → nvstreammux 1920x1080 → nvinfer (YOLO) → nvtracker → nvvideoconvert RGBA → probe → fakesink.
Per frame, every detection's foot point is placed in the camera's zones (zones.py): alert region → Boundry-object
(breach), outside the perimeter line → outside-person / outside-object, inside → nothing. One whole frame per camera
and class at most every save_every_s seconds is written to incoming/ as <camera>_<class>_<YYYYMMDD-HHMMSS>.jpg (960x540,
boxes + zones drawn) — exactly what the dashboard ingests. Live view: every 2 s per camera a 960x540 frame (zones +
boxes) in /dev/shm/perimeter_snap/<camera>.jpg, and the same frame without drawings in <camera>.clean.jpg (zone drawing).
Throw / landing detection groundwork (PERIMETER_THROW_DETECTION_SPEC): a camera with "throw" on is decoded at every frame
(the frame skip does not apply: a thrown object is in the picture for only 5–15 frames); every camera has a day / night
mode (scene.py: colour saturation with hysteresis), in the state file and in every frame description; while a "normal
frames" recording runs (ds/run/sampling.json, from the Recordings page) one clean whole 1920x1080 frame per camera every
N s goes to that recording — never one with something detected in a landing zone (the PatchCore banks must hold only
normal scenes). Band / landing / near regions are drawn on the live view.
Throw over the wall (throw.py, spec §7.4): for a throw camera with a throw band + the perimeter line, every frame's band
(only that strip, grey) goes to the camera's own worker thread through a short queue (the oldest dropped and counted
when the worker is behind: the pipeline never waits). A throw → ds/throws/events.jsonl + a review picture
ds/throws/<YYYYmmdd>/<camera>_<time>.jpg + a THROW log line; no client alert yet (the alert logic comes later). Days older
than the image retention are deleted. Person crossed the wall (zones.LineCrossing): a tracked person outside the line
for 3 frames, then inside for 3 → that frame's person is a breach ("crossed", region "Perimeter line").
Robustness (as the Workplace Analytics pipeline, site-proven): a camera without frames for 10 s is re-created on its own
(an unreachable RTSP camera is port-checked first, every 60 s), source errors never stop the pipeline, the zones file is
re-read when it changes, RAM guard 700 MB / temperature 85 °C / disk 5 GB, stall watchdog, clean stop on SIGTERM.
"""
import argparse, collections, json, os, re, signal, socket, sys, threading, time
from datetime import datetime
from urllib.parse import urlparse
import cv2, numpy as np
import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst
import pyds

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zones as Z
from savepolicy import SavePolicy
from scene import ModeDetector, Sampler, busy, saturation
from throw import ThrowDetector

ROOT = os.environ.get("PERIMETER_ROOT", "/root/PERIMETER")
DATA = os.environ.get("TRINETRA_DATA", f"{ROOT}/data")
SHM = os.environ.get("PERIMETER_SHM", "/dev/shm")
STATE = f"{SHM}/perimeter_state.json"
SNAPS = f"{SHM}/perimeter_snap"
MUX_W, MUX_H = 1920, 1080
OUT_W, OUT_H = 960, 540
MIN_FREE_GB, MAX_TEMP_C, RAM_FLOOR_KB = 5.0, 85.0, 716_800
RECONNECT_S = 10
RETRY_S = int(os.environ.get("PERIMETER_RETRY_S", "60"))
HANG_S, EXIT_S, CLOSE_S, CLOSE_MAX = 60, 20, 60, 3   # watchdog(): main loop silent / shutdown / old connection closing
TRACKER = "/opt/nvidia/deepstream/deepstream/samples/configs/deepstream-app/config_tracker_NvDCF_perf.yml"
ZONE_COL = {"alert": (40, 40, 220), "watch": (220, 140, 40), "near": (247, 85, 168), "ignore": (90, 90, 90),
            "band": (21, 204, 250), "landing": (22, 204, 132)}                  # BGR, as the zone editor
COLORS = {Z.BREACH: (64, 64, 255), Z.OUTSIDE_PERSON: (0, 170, 255), Z.OUTSIDE_OBJECT: (0, 200, 255),
          Z.ZONE_PERSON: (230, 150, 40), Z.ZONE_OBJECT: (230, 170, 80)}   # BGR
_LOG_LOCK = threading.Lock()


def log(*a):
    with _LOG_LOCK:
        print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def setp(el, k, v):
    try: el.set_property(k, v)
    except TypeError: pass


def draw_zones(img, zone):
    h, w = img.shape[:2]
    P = lambda p: (int(p[0] * w), int(p[1] * h))
    over = img.copy()
    col = ZONE_COL
    for r in Z.regions(zone):
        if r["kind"] not in Z.DETECTION_KINDS:          # band / landing: outlined only (the picture stays readable)
            cv2.fillPoly(over, [np.array([P(p) for p in r["points"]], np.int32)], col.get(r["kind"], (160, 160, 160)))
    cv2.addWeighted(over, 0.28, img, 0.72, 0, img)
    for r in Z.regions(zone):
        pts = np.array([P(p) for p in r["points"]], np.int32)
        cv2.polylines(img, [pts], True, col.get(r["kind"], (160, 160, 160)), 2, cv2.LINE_AA)
        x, y = pts[:, 0].min(), pts[:, 1].min()
        cv2.putText(img, r["name"], (int(x) + 4, int(y) + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2, cv2.LINE_AA)
    line = zone.get("line") or []
    if len(line) >= 2:
        cv2.polylines(img, [np.array([P(p) for p in line], np.int32)], False, (0, 200, 255), 3, cv2.LINE_AA)
    if zone.get("outside"):
        c = P(zone["outside"]); cv2.circle(img, c, 7, (0, 200, 255), -1, cv2.LINE_AA)
        cv2.putText(img, "OUTSIDE", (c[0] + 10, c[1] + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 200, 255), 2, cv2.LINE_AA)


class App:
    def __init__(self, args):
        self.cfg = json.load(open(args.config))
        self.pgie_cfg = args.pgie
        self.srcs = [dict(s) for s in self.cfg["streams"]]
        if not self.srcs: sys.exit("no enabled cameras")
        self.cams = [s["name"] for s in self.srcs]
        self.labels = self.cfg.get("labels") or ["person"]
        self.incoming = self.cfg.get("incoming") or f"{DATA}/incoming"
        self.save_every = float(self.cfg.get("save_every_s", 5))
        self.zones_file = self.cfg.get("zones_file") or f"{DATA}/ds/zones.json"
        self.zones, self.zones_mtime = {}, 0.0
        self.reload_zones()
        mc = self.cfg.get("mode") or {}
        self.modes = {c: ModeDetector(mc.get("night_saturation_max", 12), mc.get("hysteresis_s", 30)) for c in self.cams}
        self.sampler = Sampler(self.cfg.get("sampling_file") or f"{DATA}/ds/run/sampling.json",
                               owner=int(os.environ.get("CAPTURES_UID", "1000")), log=log)
        self.sampler.reload()
        n = len(self.srcs)
        self.loop_files = bool(self.cfg.get("loop_files", True))
        self.drop = int(self.cfg.get("frame_skip", 2))
        self.any_live = any(s["type"] == "rtsp" for s in self.srcs)
        self.bins, self.mux_pads = [None] * n, [None] * n
        self.pad_cam = {i: i for i in range(n)}           # muxer input number -> camera (a reconnect alternates i / i+n)
        self.held = [None] * n                            # (old source, its muxer input) kept until the camera's new source links
        self.last_buf, self.started_at = [0.0] * n, [0.0] * n
        self.src_frames, self.online, self.gen, self.reconnects = [0] * n, [False] * n, [0] * n, [0] * n
        self.reach, self.reach_busy, self.outage = [None] * n, [False] * n, [0] * n
        self.last_snap, self.seq = {}, 0
        self.policy = SavePolicy(every=self.save_every)
        self.counts = collections.Counter()               # (camera, class) → images saved this run
        self.fps_cnt, self.fps = collections.Counter(), {}
        self.frames, self.started = 0, datetime.now().isoformat(timespec="seconds")
        self.disk_low, self.stopping, self.stall, self.fatal = False, False, False, False
        self.beat, self.quit_at = time.monotonic(), None         # fail-safe watchdog (watchdog()): main-loop heartbeat,
        self.closing = {}                                          # when the shutdown began; old sources being closed
        self.state_lock = threading.Lock()                         # closing an old source ↔ shutting the pipeline down
        self.exit_reason = "eos"
        self._wd = (0, 0, time.monotonic())
        os.makedirs(SNAPS, exist_ok=True); os.makedirs(self.incoming, exist_ok=True)
        self.crossing = Z.LineCrossing()
        self.throws_dir = self.cfg.get("throws_dir") or f"{DATA}/ds/throws"
        self.keep_days = int(self.cfg.get("keep_days", 30) or 0)
        self.throw_cfg = self.cfg.get("throw_cfg") or {}
        self.throw_cams = [s["name"] for s in self.srcs if s.get("throw")]
        self.throw_det, self.throw_q, self.throw_cv = {}, {}, threading.Condition()
        self.throw_n = collections.Counter()               # camera → throws, dropped frames, frames fed
        self.cam_frame = collections.Counter()
        self.throw_day = None
        self.build_throw_detectors()
        for c in self.throw_cams:
            self.throw_q[c] = collections.deque()
            threading.Thread(target=self.throw_worker, args=(c,), daemon=True, name=f"throw-{c}").start()
        self.loop = GLib.MainLoop(); self.build()

    def reload_zones(self):
        try:
            m = os.stat(self.zones_file).st_mtime
        except OSError:
            return
        if m == self.zones_mtime: return
        try:
            self.zones = json.load(open(self.zones_file)); self.zones_mtime = m
            if hasattr(self, "throw_det"):
                self.build_throw_detectors()
            log("zones loaded:", ", ".join(f"{c} ({len(Z.regions(z or {}))} region(s), line {'yes' if (z or {}).get('line') else 'no'})"
                                         for c, z in self.zones.items() if c in self.cams) or "none for these cameras")
        except Exception as e:
            log("zones file not read:", repr(e))

    # ---- throw over the wall: one detector + worker per throw camera
    def build_throw_detectors(self):
        for c in self.throw_cams:
            d = ThrowDetector(self.zones.get(c) or {}, self.throw_cfg, (MUX_W, MUX_H), camera=c)
            self.throw_det[c] = d
            log(f"THROW {c}: " + (f"watching the band {d.box} (every frame)" if d.ok else
                                  "throw detection on, but no throw band / perimeter line / outside point drawn: idle"))

    def throw_worker(self, cam):
        q = self.throw_q[cam]
        while True:
            with self.throw_cv:
                while not q:
                    self.throw_cv.wait(1.0)
                item = q.popleft()
            fno, t, wall, crop, persons, mode = item
            det = self.throw_det.get(cam)
            if det is None or not det.ok or crop.shape != (det.box[3] - det.box[1], det.box[2] - det.box[0]):
                continue                                     # zones changed while this frame waited
            try:
                for ev in det.feed(fno, t, crop, persons):
                    self.write_throw(cam, det, ev, wall, mode)
            except Exception as e:
                log(f"THROW {cam}: worker error (carries on): {e!r}")

    def write_throw(self, cam, det, ev, wall, mode):
        self.throw_n[(cam, "throws")] += 1
        day = wall.strftime("%Y%m%d")
        d = os.path.join(self.throws_dir, day)
        name = f"{cam}_{wall.strftime('%Y%m%d-%H%M%S')}-{wall.microsecond // 1000:03d}.jpg"
        ev = {**ev, "ts": wall.isoformat(timespec="milliseconds"), "mode": mode, "image": f"{day}/{name}"}
        try:
            os.makedirs(d, exist_ok=True)
            if self.throw_n[(cam, "images_" + day)] < 500:     # a storm of false ones never fills the disk
                ok, jpg = cv2.imencode(".jpg", det.review_image(ev), [cv2.IMWRITE_JPEG_QUALITY, 90])
                if ok:
                    with open(os.path.join(d, "." + name + ".part"), "wb") as f:
                        f.write(jpg.tobytes())
                    os.replace(os.path.join(d, "." + name + ".part"), os.path.join(d, name))
                    self.throw_n[(cam, "images_" + day)] += 1
                    self.throw_to_dashboard(cam, ev, jpg.tobytes(), wall, mode)
            with open(os.path.join(self.throws_dir, "events.jsonl"), "a") as f:
                f.write(json.dumps(ev) + "\n")
        except OSError as e:
            log(f"THROW {cam}: not written: {e!r}")
        log(f"THROW {cam}: {ev['curve']} over the line at {ev['crossing_xy']}, {ev['points']} points, "
            f"{ev['speed_px_f']} px/frame, fit {ev['fit_rms']} px → {ev['image']}")

    def throw_to_dashboard(self, cam, ev, jpg: bytes, wall, mode):
        """the throw → the dashboard's incoming/ like any camera image: <camera>_thrown-object_<time>.jpg (the review
        picture: the object zoomed frame by frame + its path over the line) and its frame description FIRST, one object
        marked "thrown" → the "Throw over the wall" rule (region *thrown) raises the alert. Runs in the throw worker."""
        ts = f"{wall.strftime('%Y%m%d-%H%M%S')}-{wall.microsecond // 1000:03d}"
        name = f"{cam}_thrown-object_{ts}.jpg"
        meta = json.dumps({"v": 1, "frame": f"{cam}|{wall.isoformat(timespec='milliseconds')}|throw{ev.get('track_id')}",
                           "camera": cam, "ts": wall.isoformat(timespec="milliseconds"), "mode": mode,
                           "objects": [{"label": "thrown object", "kind": "object", "regions": [], "alert": False,
                                        "outside": False, "near": False, "crossed": False, "thrown": True,
                                        "track": ev.get("track_id"), "conf": 1.0, "box": None}]})
        try:
            side = os.path.join(self.incoming, name[:-4] + ".json")
            with open(side + ".part", "w") as f:
                f.write(meta)
            os.replace(side + ".part", side)
            tmp = os.path.join(self.incoming, f".{name}.part")
            with open(tmp, "wb") as f:
                f.write(jpg)
            os.replace(tmp, os.path.join(self.incoming, name))
            self.counts[(cam, "thrown-object")] += 1
        except OSError as e:
            log(f"THROW {cam}: not sent to the dashboard: {e!r}")

    def prune_throws(self):
        """days of review pictures older than the image retention (events.jsonl keeps its last 20 000 lines)"""
        if self.keep_days <= 0 or not os.path.isdir(self.throws_dir):
            return
        today = datetime.now().strftime("%Y%m%d")
        if self.throw_day == today:
            return
        self.throw_day = today
        cut = (datetime.now().timestamp() - self.keep_days * 86400)
        for name in os.listdir(self.throws_dir):
            p = os.path.join(self.throws_dir, name)
            if re.fullmatch(r"\d{8}", name) and os.path.isdir(p):
                try:
                    if datetime.strptime(name, "%Y%m%d").timestamp() < cut:
                        for f in os.listdir(p):
                            os.remove(os.path.join(p, f))
                        os.rmdir(p); log(f"THROW: review pictures of {name} deleted (older than {self.keep_days} days)")
                except (OSError, ValueError) as e:
                    log("THROW: old day not deleted:", repr(e))
        ev = os.path.join(self.throws_dir, "events.jsonl")
        try:
            if os.path.getsize(ev) > 8_000_000:
                lines = open(ev).read().splitlines()[-20000:]
                with open(ev + ".tmp", "w") as f:
                    f.write("\n".join(lines) + "\n")
                os.replace(ev + ".tmp", ev)
        except OSError:
            pass

    def feed_throw(self, cam, buf, fm, boxes, now, wall):
        det = self.throw_det.get(cam)
        if det is None or not det.ok:
            return
        x0, y0, x1, y1 = det.box
        try:
            rgba = pyds.get_nvds_buf_surface(hash(buf), fm.batch_id)
            try: crop = cv2.cvtColor(np.ascontiguousarray(rgba[y0:y1, x0:x1]), cv2.COLOR_RGBA2GRAY)   # only the band
            finally: pyds.unmap_nvds_buf_surface(hash(buf), fm.batch_id)
        except Exception as e:
            self.throw_n[(cam, "copy_errors")] += 1
            if self.throw_n[(cam, "copy_errors")] <= 3: log(f"THROW {cam}: band copy failed: {e!r}")
            return
        persons = [b for b, _c, lab, _f in boxes if Z.target_of(lab) == "person"]
        self.cam_frame[cam] += 1
        q = self.throw_q[cam]
        with self.throw_cv:
            if len(q) >= 30:                                  # the worker is behind by a second: drop the oldest
                q.popleft(); self.throw_n[(cam, "dropped")] += 1
            q.append((self.cam_frame[cam], now, wall, crop, persons, self.modes[cam].mode))
            self.throw_cv.notify_all()

    # ---- pipeline (as Workplace Analytics)
    def build(self):
        Gst.init(None)
        mk = lambda f, n: Gst.ElementFactory.make(f, n) or sys.exit(f"cannot create {f}")
        self.pipe = Gst.Pipeline()
        self.mux = mux = mk("nvstreammux", "mux")
        n = len(self.srcs)
        for k, v in dict(batch_size=n, width=MUX_W, height=MUX_H, batched_push_timeout=40000,
                         live_source=1 if self.any_live else 0, enable_padding=0).items():
            mux.set_property(k.replace("_", "-"), v)
        self.pipe.add(mux)
        for i in range(n): self.make_source(i)
        pgie = mk("nvinfer", "pgie"); pgie.set_property("config-file-path", self.pgie_cfg); pgie.set_property("batch-size", n)
        trk = mk("nvtracker", "tracker")
        trk.set_property("ll-lib-file", "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so")
        trk.set_property("ll-config-file", self.tracker_config())
        trk.set_property("tracker-width", 640); trk.set_property("tracker-height", 384)
        conv = mk("nvvideoconvert", "conv")
        caps = mk("capsfilter", "caps"); caps.set_property("caps", Gst.Caps.from_string("video/x-raw(memory:NVMM), format=RGBA"))
        sink = mk("fakesink", "sink"); sink.set_property("sync", False); sink.set_property("async", False)
        for e in (pgie, trk, conv, caps, sink): self.pipe.add(e)
        self.core = {mux, pgie, trk, conv, caps, sink}
        mux.link(pgie); pgie.link(trk); trk.link(conv); conv.link(caps); caps.link(sink)
        sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self.probe, None)
        bus = self.pipe.get_bus(); bus.add_signal_watch(); bus.connect("message", self.on_msg)

    def tracker_config(self):
        """NVIDIA's NvDCF (perf) tracker settings with probationAge 0: a new object is reported from its FIRST frame —
        the default (2) holds back anything seen in only 1–2 frames, so a short / small appearance would be lost"""
        out = os.path.join(os.path.dirname(os.path.abspath(self.pgie_cfg)), "tracker_perimeter.yml")
        try:
            text = open(TRACKER).read()
            text = re.sub(r"(?m)^(\s*probationAge:\s*)\d+", r"\g<1>0", text)
            with open(out, "w") as f:
                f.write(text)
            return out
        except OSError as e:
            log("tracker config not written (NVIDIA default used):", repr(e)); return TRACKER

    def make_source(self, i):
        s = self.srcs[i]; self.gen[i] += 1
        b = Gst.ElementFactory.make("nvurisrcbin", f"src{i}_g{self.gen[i]}")
        b.set_property("uri", s["uri"]); setp(b, "drop-frame-interval", 0 if s.get("throw") else self.drop)
        if self.gen[i] == 1:                               # what the decoder really does (logged once per camera)
            try: d = b.get_property("drop-frame-interval")
            except TypeError: d = None
            log(f"camera {s['name']}: decoder gives " + ("every frame" if not d else f"every {d}{'nd' if d == 2 else 'rd' if d == 3 else 'th'} frame")
                + (" (throw detection)" if s.get("throw") else ""))
        setp(b, "cudadec-memtype", 0); setp(b, "disable-audio", True)
        if s["type"] == "file":
            setp(b, "file-loop", self.loop_files)
        else:
            setp(b, "latency", 300)
            setp(b, "select-rtp-protocol", 0 if s.get("transport") == "udp" else 4)
        b.connect("pad-added", self.on_pad, i)
        self.pipe.add(b); self.bins[i] = b; self.started_at[i] = time.monotonic()
        return b

    def on_pad(self, src, pad, i):
        caps = pad.get_current_caps() or pad.query_caps(None)
        if not caps.get_structure(0).get_name().startswith("video"): return
        live = self.srcs[i]["type"] == "rtsp"
        def ev(p, inf):
            if self.stopping: return Gst.PadProbeReturn.DROP
            t = inf.get_event().type
            if t == Gst.EventType.TAG: return Gst.PadProbeReturn.DROP
            if t == Gst.EventType.EOS and live and not self.stopping:
                log(f"camera {self.cams[i]} ended its stream - will reconnect"); self.last_buf[i] = 0.0
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK
        pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM | Gst.PadProbeType.EVENT_FLUSH, ev)
        pad.add_probe(Gst.PadProbeType.BUFFER, self.on_src_buffer, i)
        if self.mux_pads[i] is None:
            held = self.held[i][1].get_name() if self.held[i] else None   # never the input the old source still holds
            k = next(k for k in (i, i + len(self.srcs)) if f"sink_{k}" != held)
            self.mux_pads[i] = self.mux.request_pad_simple(f"sink_{k}"); self.pad_cam[k] = i
        if pad.link(self.mux_pads[i]) != Gst.PadLinkReturn.OK: log(f"ERROR linking camera {self.cams[i]}")
        if self.held[i]:                                  # the new source delivers: the old one goes 1.5 s later
            GLib.timeout_add(1500, self._dispose_source, *self.held[i], i); self.held[i] = None

    def on_src_buffer(self, pad, info, i):
        if self.stopping: return Gst.PadProbeReturn.DROP
        self.last_buf[i] = time.monotonic(); self.src_frames[i] += 1
        return Gst.PadProbeReturn.OK

    def restart_source(self, i):
        """re-create camera i. The old source stays linked to its muxer input (its data dropped) and is KEPT, input included, until
        the camera's new source delivers video (on_pad: it gets the other of the camera's two inputs i / i+n); 1.5 s
        after that the old input and source are released — no frame of theirs is on its way down the pipeline any more:
        the decoder's video memory and the muxer's own per-input copies of a camera it scales must outlive those frames.
        Released at once instead (until 28 Nov 2026) they freed memory a frame in flight still pointed to
        (NVMAP_IOC_DUP_HANDLE failed → NvVicCompose failed → nvinfer "Internal data stream error" → fatal, several scaled
        cameras reconnecting together), and released before the new source linked, the muxer was left with NO input
        → "No Sources found" → end of stream → the pipeline quit (a camera whose RTSP set-up takes > 1.5 s).
        A source that never delivered (no input) is closed at once. Errors an old source may still post are ignored
        (an older generation: on_msg)."""
        old, pad = self.bins[i], self.mux_pads[i]
        if pad is not None:
            peer = pad.get_peer()
            if peer is not None:                          # stays LINKED, its data dropped: unlinked for seconds, its
                peer.add_probe(Gst.PadProbeType.DATA_DOWNSTREAM,   # frames got "not linked" and the RTSP source began
                               lambda *_: Gst.PadProbeReturn.DROP)  # closing itself → deadlock with our close (28 Nov, gdb)
            self.mux_pads[i] = None
            if self.held[i]:                              # (cannot happen: a linked source released the older one)
                self._dispose_source(*self.held[i], i)
            self.held[i] = (old, pad)
        else:
            self._dispose_source(old, None, i)
        self.reconnects[i] += 1; self.outage[i] += 1
        self.make_source(i).sync_state_with_parent()

    def _dispose_source(self, old, pad=None, i=None):
        """the old source is closed in its OWN thread, never in the main loop: closing a connection to a camera that
        stopped sending without hanging up (cable cut, frozen camera) can wait a long time inside the RTSP source,
        and the main loop runs everything else (reconnects, state, stop). watchdog() restarts the pipeline cleanly
        when such closings pile up."""
        name = old.get_name(); self.closing[name] = (time.monotonic(), i)
        def run():
            out = False
            with self.state_lock:                         # taken out of the pipeline never at the same time as the
                try:                                      # pipeline's shutdown (the same element changed from two
                    if self.quit_at is None:              # threads aborted the process); shutting down: the
                        if pad is not None:               # pipeline's own NULL closes it
                            peer = pad.get_peer()
                            if peer is not None: peer.unlink(pad)
                            pad.send_event(Gst.Event.new_flush_start()); pad.send_event(Gst.Event.new_flush_stop(False))
                            self.mux.release_request_pad(pad)
                        self.pipe.remove(old); out = True
                except Exception as e:
                    log("old camera source not removed:", repr(e))
            if out:                                       # closed OUTSIDE the lock, no longer in the pipeline: one slow
                try:                                      # close never holds up the next ones or the shutdown
                    old.set_state(Gst.State.NULL)
                except Exception as e:
                    log("old camera source not closed:", repr(e))
            t0 = self.closing.pop(name, (time.monotonic(), i))[0]
            if time.monotonic() - t0 > 10: log(f"old connection {name} closed after {time.monotonic() - t0:.0f} s")
        threading.Thread(target=run, daemon=True, name=f"close-{name}").start()
        return False

    def watch_sources(self):
        self.beat = time.monotonic()
        if self.stopping: return True
        now = time.monotonic()
        for i, s in enumerate(self.srcs):
            alive = now - self.last_buf[i] < RECONNECT_S
            if alive != self.online[i]:
                self.online[i] = alive
                log(f"CAMERA {'ONLINE' if alive else 'OFFLINE'}: {s['name']}")
                if alive: self.outage[i] = 0
            if s["type"] == "rtsp" and not alive and now - self.started_at[i] >= RETRY_S - 1 \
                    and (self.reach[i] is None or now - self.reach[i][0] >= RETRY_S - 1):
                self.check_reachable(i)
        self.reload_zones()
        self.sampler.reload()
        return True

    def check_reachable(self, i):
        if self.reach_busy[i]: return
        u = urlparse(self.srcs[i]["uri"])
        try: host, port = u.hostname, u.port or 554
        except ValueError: host, port = None, 554
        if not host: GLib.idle_add(self._reconnect, i); return
        self.reach_busy[i] = True
        def run():
            ok = False
            try:
                with socket.create_connection((host, port), timeout=3): ok = True
            except OSError: pass
            prev = self.reach[i]
            self.reach[i] = (time.monotonic(), ok); self.reach_busy[i] = False
            if ok: GLib.idle_add(self._reconnect, i)
            elif prev is None or prev[1]: log(f"camera {self.cams[i]} not reachable ({host}:{port}) - checked every {RETRY_S} s")
        threading.Thread(target=run, daemon=True, name=f"reach{i}").start()

    def _reconnect(self, i):
        if self.stopping or time.monotonic() - self.last_buf[i] < RECONNECT_S: return False
        try:
            self.restart_source(i)
            if self.outage[i] <= 3 or self.outage[i] % 30 == 0: log(f"reconnecting {self.cams[i]} (attempt {self.reconnects[i]})")
        except Exception as e: log(f"reconnect {self.cams[i]} failed: {e!r}")
        return False

    def source_index(self, el):
        while el is not None:
            m = re.match(r"src(\d+)_g(\d+)$", el.get_name() or "")
            if m: return int(m.group(1)), int(m.group(2))
            el = el.get_parent()
        return None

    def on_msg(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.EOS:
            log("EOS - stopped" if self.stopping else "EOS - all sources finished")
            self.quit_at = self.quit_at or time.monotonic(); self.loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            if msg.src in self.core or msg.src is self.pipe:
                log("FATAL", msg.src.get_name(), err.message, (dbg or "")[:300]); self.fatal = True; self.exit_reason = "fatal"
                self.quit_at = self.quit_at or time.monotonic(); self.loop.quit()
                return
            si = self.source_index(msg.src)
            if si is not None and si[1] == self.gen[si[0]]:
                log(f"camera {self.cams[si[0]]} error (pipeline keeps running): {err.message}"); self.last_buf[si[0]] = 0.0
        elif t == Gst.MessageType.WARNING:
            w, _ = msg.parse_warning(); log("WARN", msg.src.get_name(), w.message)

    # ---- per batch: zones → images
    def probe(self, pad, info, _):
        buf = info.get_buffer()
        batch = pyds.gst_buffer_get_nvds_batch_meta(hash(buf)); lf = batch.frame_meta_list
        while lf:
            fm = pyds.NvDsFrameMeta.cast(lf.data)
            try: self.frame(buf, fm)
            except Exception as e: log("probe error:", repr(e))
            lf = lf.next
        return Gst.PadProbeReturn.OK

    def frame(self, buf, fm):
        cam = self.cams[self.pad_cam.get(fm.pad_index, fm.pad_index % len(self.cams))]; self.frames += 1; self.fps_cnt[cam] += 1
        zone = self.zones.get(cam) or {}
        objs, boxes = [], []
        lo = fm.obj_meta_list
        while lo:
            o = pyds.NvDsObjectMeta.cast(lo.data); r = o.rect_params
            lab = self.labels[o.class_id] if 0 <= o.class_id < len(self.labels) else str(o.class_id)
            b = (r.left, r.top, r.width, r.height)
            d = Z.describe(lab, b, zone, MUX_W, MUX_H)
            tid = int(o.object_id) if o.object_id < 2**53 else None
            if Z.target_of(lab) == "person" and self.crossing.update(
                    cam, tid, ((b[0] + b[2] / 2) / MUX_W, (b[1] + b[3]) / MUX_H), zone, time.monotonic()):
                d = dict(d or {"kind": "person", "regions": [], "alert": False, "outside": False, "near": False})
                d.update(alert=True, crossed=True, regions=d["regions"] + [Z.CROSSED_REGION])
                log(f"CROSSED {cam}: person (track {tid}) crossed the perimeter line")
            c = Z.cls(d) if d else None
            boxes.append((b, c, lab, o.confidence))
            if d:
                objs.append({"crossed": False, **d, "label": lab, "cls": c, "track": tid,
                             "conf": round(float(o.confidence), 3),
                             "box": [round(b[0] / MUX_W, 4), round(b[1] / MUX_H, 4), round(b[2] / MUX_W, 4), round(b[3] / MUX_H, 4)]})
            lo = lo.next
        now, wall = time.monotonic(), datetime.now()
        if cam in self.throw_q:
            self.feed_throw(cam, buf, fm, boxes, now, wall)
        rel = self.policy.release(cam, now)                    # a held frame (news that had to wait): written now
        if rel:
            r_objs, (r_jpg, r_wall), r_cls = rel
            self.write_images(cam, r_objs, r_cls, r_jpg, r_wall)
            self.policy.saved(cam, r_objs, r_cls, now)
        due, hold = self.policy.decide(cam, objs, now)         # savepolicy.py: nothing short / small / new is skipped
        if (due or hold) and self.disk_low:                    # saving paused (disk low): no frame copies for nothing
            self.policy.saved(cam, objs, due, now); due, hold = [], False
        snap = now - self.last_snap.get(cam, -1e9) >= 2.0
        samples = [] if self.disk_low else self.sampler.due(cam, now, wall)
        if samples and busy([b for b, _c, _l, _f in boxes], zone, MUX_W, MUX_H):
            for j in samples: self.sampler.busy_now(j, cam, now)   # not a normal scene: tried again in 5 s
            samples = []
        if not (due or hold or snap or samples): return
        full = self.frame_copy(buf, fm)
        if full is None: return
        if snap or samples:                                    # day / night from a small copy (every 2 s)
            md = self.modes[cam]
            sw = md.update(saturation(cv2.resize(full, (160, 90), interpolation=cv2.INTER_AREA)), now)
            if sw: log(f"MODE {cam}: {sw} (colour saturation {md.sat})")
        for j in samples:
            try:
                name = self.sampler.save(j, cam, full, self.modes[cam].mode, self.modes[cam].sat, wall, now)
                log("NORMAL FRAME", j["id"], name)
            except OSError as e:
                log("normal frame not written:", repr(e)); self.sampler.busy_now(j, cam, now)
        if not (due or hold or snap): return
        img = self.frame_image(full, zone, boxes)
        if snap:
            self.last_snap[cam] = now
            clean = cv2.resize(full, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)       # for drawing zones: no drawings on it
            tmp = f"{SNAPS}/{cam}.clean.tmp.jpg"; cv2.imwrite(tmp, clean, [cv2.IMWRITE_JPEG_QUALITY, 85]); os.replace(tmp, f"{SNAPS}/{cam}.clean.jpg")
            tmp = f"{SNAPS}/{cam}.tmp.jpg"; cv2.imwrite(tmp, img, [cv2.IMWRITE_JPEG_QUALITY, 80]); os.replace(tmp, f"{SNAPS}/{cam}.jpg")
        if due or hold:
            ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])   # encoded in memory: the temp name has no .jpg
            if not ok:
                log("IMAGE not encoded"); return
            if hold:
                self.policy.hold(cam, objs, (jpg.tobytes(), wall), now)
            else:
                self.write_images(cam, objs, due, jpg.tobytes(), wall)
                self.policy.saved(cam, objs, due, now)

    def write_images(self, cam, objs, classes, jpg: bytes, wall: datetime):
        """one image per class (+ the frame description, written FIRST: the image triggers the dashboard's ingest);
        the time in the name to the millisecond — images of the same second never collide"""
        ts = f"{wall.strftime('%Y%m%d-%H%M%S')}-{wall.microsecond // 1000:03d}"
        self.seq += 1
        meta = json.dumps({"v": 1, "frame": f"{cam}|{wall.isoformat(timespec='milliseconds')}|{self.seq}", "camera": cam,
                           "ts": wall.isoformat(timespec="milliseconds"), "mode": self.modes[cam].mode,
                           "objects": [{k: o.get(k) for k in ("label", "kind", "regions", "alert", "outside", "near", "crossed",
                                                              "track", "conf", "box")} for o in objs]})
        for c in classes:
            name = f"{cam}_{c}_{ts}.jpg"
            side = os.path.join(self.incoming, name[:-4] + ".json")
            with open(side + ".part", "w") as f:
                f.write(meta)
            os.replace(side + ".part", side)
            tmp = os.path.join(self.incoming, f".{name}.part")          # renamed when complete: the dashboard sees a finished file
            with open(tmp, "wb") as f:
                f.write(jpg)
            os.replace(tmp, os.path.join(self.incoming, name))
            self.counts[(cam, c)] += 1
            log("IMAGE", name, f"({len(objs)} object(s))")

    def flush_held(self):
        """on stop: frames still held are written (a stop right after a short appearance must not lose it)"""
        for cam, q in list(self.policy.held.items()):
            while q:
                objs, (jpg, wall) = q.popleft()
                try:
                    self.write_images(cam, objs, list(dict.fromkeys(o["cls"] for o in objs)), jpg, wall)
                except OSError as e:
                    log("held frame not written:", repr(e))

    @staticmethod
    def frame_copy(buf, fm):
        """this frame, whole (streammux size 1920x1080), BGR — a copy: the buffer is handed back at once"""
        try:
            rgba = pyds.get_nvds_buf_surface(hash(buf), fm.batch_id)
            try: full = np.array(rgba, copy=True, order="C")
            finally: pyds.unmap_nvds_buf_surface(hash(buf), fm.batch_id)
        except Exception as e:
            log("frame copy failed:", repr(e)); return None
        return cv2.cvtColor(full, cv2.COLOR_RGBA2BGR)

    def frame_image(self, full, zone, boxes):
        """the frame at 960x540 with the zones and the boxes drawn"""
        img = cv2.resize(full, (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
        draw_zones(img, zone)
        sx, sy = OUT_W / MUX_W, OUT_H / MUX_H
        for (l, t, w, h), cls, lab, conf in boxes:
            col = COLORS.get(cls, (160, 160, 160))
            p1, p2 = (int(l * sx), int(t * sy)), (int((l + w) * sx), int((t + h) * sy))
            cv2.rectangle(img, p1, p2, col, 2 if cls else 1, cv2.LINE_AA)
            cv2.putText(img, f"{cls or lab} {conf:.2f}", (p1[0], max(12, p1[1] - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1, cv2.LINE_AA)
        cv2.putText(img, datetime.now().strftime("%d %b %Y %H:%M:%S"), (10, OUT_H - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)
        return img

    # ---- safety (every 5 s)
    @staticmethod
    def max_temp():
        t = []
        for p in os.listdir("/sys/class/thermal"):
            try: t.append(int(open(f"/sys/class/thermal/{p}/temp").read()) / 1000)
            except Exception: pass
        return max(t) if t else None

    def safety(self):
        free = os.statvfs(self.incoming); gb = free.f_bavail * free.f_frsize / 1e9
        if gb < MIN_FREE_GB and not self.disk_low: log(f"SAFETY: free disk {gb:.1f} GB < {MIN_FREE_GB} GB -> image saving paused")
        self.disk_low = gb < MIN_FREE_GB or (self.disk_low and gb < MIN_FREE_GB + 1)
        mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo") if ":" in l}
        if mem.get("MemAvailable", 0) < RAM_FLOOR_KB and not self.stopping:
            log(f"SAFETY: only {mem.get('MemAvailable', 0)//1024} MB RAM available (< 700 MB) -> stopping cleanly"); self.stop("safety")
        temp = self.max_temp()
        if temp is not None and temp > MAX_TEMP_C and not self.stopping:
            log(f"SAFETY: temperature {temp:.0f} C > {MAX_TEMP_C} C -> stopping cleanly"); self.stop("safety")
        return gb, temp

    def stop(self, reason="stop"):
        """clean stop: EOS straight into every mux input, then shut down (as Workplace Analytics)"""
        if self.stopping: return
        self.stopping, self.exit_reason = True, reason
        self.quit_at = self.quit_at or time.monotonic()
        pads = [p for p in self.mux_pads if p is not None] + [h[1] for h in self.held if h]
        if not pads: self.loop.quit(); return
        for p in pads: p.send_event(Gst.Event.new_eos())
        GLib.timeout_add(2500, lambda: (self.loop.quit() if self.loop.is_running() else None, False)[1])

    def write_state(self):
        try:
            gb, temp = self.safety()
            pf, sf, tc = self._wd; nsf = sum(self.src_frames)
            if self.frames != pf: self._wd = (self.frames, nsf, time.monotonic())
            elif nsf - sf > 50 and time.monotonic() - tc > 60 and not self.stopping:
                log("STALL: cameras deliver frames but none processed for 60 s - exiting (code 3)"); self.stall = True; self.stop("stall")
                GLib.timeout_add_seconds(15, lambda: (log("STALL: EOS did not finish - hard exit"), os._exit(3)))
            elif nsf == sf: self._wd = (pf, nsf, time.monotonic())
            self.fps = {c: round(self.fps_cnt[c] / 5.0, 2) for c in self.cams}; self.fps_cnt.clear()
            mono = time.monotonic()
            streams = [{"name": s["name"], "type": s["type"], "online": self.online[i], "throw": bool(s.get("throw")),
                        "mode": self.modes[s["name"]].mode, "saturation": self.modes[s["name"]].sat,
                        "reachable": None if self.reach[i] is None or self.online[i] else self.reach[i][1],
                        "last_frame_s": None if not self.last_buf[i] else round(mono - self.last_buf[i], 1),
                        "fps": self.fps.get(s["name"], 0), "reconnects": self.reconnects[i],
                        "images": {c: n for (cam, c), n in self.counts.items() if cam == s["name"]},
                        "throw_stats": self.throw_stats(s["name"])}
                       for i, s in enumerate(self.srcs)]
            self.crossing.prune(mono)
            self.prune_throws()
            st = {"running": True, "started": self.started, "pid": os.getpid(), "updated": datetime.now().isoformat(timespec="seconds"),
                  "frames": self.frames, "streams": streams, "free_disk_gb": round(gb, 1), "temp_c": temp,
                  "images_paused_low_disk": self.disk_low}
            tmp = STATE + ".tmp"; json.dump(st, open(tmp, "w")); os.replace(tmp, STATE)
        except Exception as e:
            log("state write failed:", repr(e))
        return True

    def throw_stats(self, cam):
        det = self.throw_det.get(cam)
        if det is None:
            return None
        return {"watching": det.ok, "throws": self.throw_n[(cam, "throws")], "dropped": self.throw_n[(cam, "dropped")],
                "queue": len(self.throw_q.get(cam) or ()), **(det.summary() if det.ok else {})}

    def watchdog(self):
        """fail-safe, in its own thread (works while the main loop is blocked): the pipeline never stays half-alive.
        - the main loop silent for HANG_S (watch_sources beats every RECONNECT_S) → hard exit, code 6
        - a shutdown (stop / fatal / stall) not finished within EXIT_S → hard exit (code 4 fatal, 3 stall, 6, 0 stop)
        - old camera connections still closing after CLOSE_S: logged; CLOSE_MAX of them at once → clean restart
        A hard exit marks the state file (exit reason) first; the dashboard supervisor starts the pipeline again
        (the user still wants it running) — only Stop on the page keeps it off."""
        said = set()
        while True:
            time.sleep(5)
            now = time.monotonic()
            if self.quit_at is not None and now - self.quit_at > EXIT_S:
                code = 4 if self.fatal else 3 if self.stall else 0 if self.exit_reason == "stop" else 6
                self._hard_exit(f"shutdown not finished after {EXIT_S} s", self.exit_reason, code)
            if self.quit_at is None and now - self.beat > HANG_S:
                self._hard_exit(f"main loop not responding for {int(now - self.beat)} s", "hang", 6)
            stuck = {n: v for n, v in list(self.closing.items()) if now - v[0] > CLOSE_S}
            for n in stuck:
                if n not in said:
                    said.add(n); log(f"old connection {n} still closing after {CLOSE_S} s (the pipeline carries on)")
            if len(stuck) >= CLOSE_MAX and not self.stopping:
                log(f"{len(stuck)} old camera connections will not close - restarting the pipeline cleanly")
                GLib.idle_add(lambda: (self.stop("reconnect_stuck"), False)[1])
                self.quit_at = self.quit_at or now

    def _hard_exit(self, why, reason, code):
        log(f"WATCHDOG: {why} - exiting (code {code}); the dashboard starts the pipeline again")
        try:
            st = json.load(open(STATE)) if os.path.exists(STATE) else {}
            st.update(running=False, pid=os.getpid(), exit_reason=reason, stopped=datetime.now().isoformat(timespec="seconds"))
            json.dump(st, open(STATE + ".tmp", "w")); os.replace(STATE + ".tmp", STATE)
            self.flush_held()
        except Exception:
            pass
        try: os.sync()
        except Exception: pass
        os._exit(code)

    def run(self):
        mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo") if ":" in l}
        if mem.get("MemAvailable", 0) < 1_500_000:
            sys.exit(f"SAFETY: only {mem.get('MemAvailable', 0)//1024} MB RAM available (< 1.5 GB) - not starting")
        signal.signal(signal.SIGTERM, lambda *a: (log("SIGTERM - stopping cleanly"), self.stop()))
        signal.signal(signal.SIGINT, lambda *a: (log("SIGINT - stopping cleanly"), self.stop()))
        self.safety()
        GLib.timeout_add_seconds(5, self.write_state)
        GLib.timeout_add_seconds(RECONNECT_S, self.watch_sources)
        log(f"starting {len(self.srcs)} camera(s): " + ", ".join(f"{s['name']} ({s['type']}{', throw detection: every frame' if s.get('throw') else ''})" for s in self.srcs)
            + f"; images every {self.save_every:g} s per camera and class → {self.incoming}")
        threading.Thread(target=self.watchdog, daemon=True, name="watchdog").start()
        self.pipe.set_state(Gst.State.PLAYING)
        try: self.loop.run()
        finally:
            self.quit_at = self.quit_at or time.monotonic()
            got = self.state_lock.acquire(timeout=EXIT_S)  # an old source still closing first (watchdog() exits if stuck)
            self.pipe.set_state(Gst.State.NULL)
            if got: self.state_lock.release()
            self.flush_held()
            try:
                s = json.load(open(STATE)) if os.path.exists(STATE) else {}
                s.update(running=False, pid=os.getpid(), exit_reason=self.exit_reason, stopped=datetime.now().isoformat(timespec="seconds"))
                json.dump(s, open(STATE + ".tmp", "w")); os.replace(STATE + ".tmp", STATE)
            except Exception: pass
            os.sync(); log(f"stopped (exit reason: {self.exit_reason})")
            if self.stall: os._exit(3)
            if self.fatal: os._exit(4)
            if self.exit_reason == "safety": os._exit(5)
            if self.closing:                              # an old connection still closing in its thread: leave now
                sys.stdout.flush(); sys.stderr.flush(); os._exit(0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--pgie", required=True)
    App(ap.parse_args()).run()
