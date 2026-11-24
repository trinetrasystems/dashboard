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
boxes) in /dev/shm/perimeter_snap/<camera>.jpg.
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
TRACKER = "/opt/nvidia/deepstream/deepstream/samples/configs/deepstream-app/config_tracker_NvDCF_perf.yml"
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
    col = {"alert": (40, 40, 220), "watch": (220, 140, 40), "ignore": (90, 90, 90)}
    for r in Z.regions(zone):
        cv2.fillPoly(over, [np.array([P(p) for p in r["points"]], np.int32)], col[r["kind"]])
    cv2.addWeighted(over, 0.28, img, 0.72, 0, img)
    for r in Z.regions(zone):
        pts = np.array([P(p) for p in r["points"]], np.int32)
        cv2.polylines(img, [pts], True, col[r["kind"]], 2, cv2.LINE_AA)
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
        n = len(self.srcs)
        self.loop_files = bool(self.cfg.get("loop_files", True))
        self.drop = int(self.cfg.get("frame_skip", 2))
        self.any_live = any(s["type"] == "rtsp" for s in self.srcs)
        self.bins, self.mux_pads = [None] * n, [None] * n
        self.last_buf, self.started_at = [0.0] * n, [0.0] * n
        self.src_frames, self.online, self.gen, self.reconnects = [0] * n, [False] * n, [0] * n, [0] * n
        self.reach, self.reach_busy, self.outage = [None] * n, [False] * n, [0] * n
        self.last_snap, self.seq = {}, 0
        self.policy = SavePolicy(every=self.save_every)
        self.counts = collections.Counter()               # (camera, class) → images saved this run
        self.fps_cnt, self.fps = collections.Counter(), {}
        self.frames, self.started = 0, datetime.now().isoformat(timespec="seconds")
        self.disk_low, self.stopping, self.stall, self.fatal = False, False, False, False
        self.exit_reason = "eos"
        self._wd = (0, 0, time.monotonic())
        os.makedirs(SNAPS, exist_ok=True); os.makedirs(self.incoming, exist_ok=True)
        self.loop = GLib.MainLoop(); self.build()

    def reload_zones(self):
        try:
            m = os.stat(self.zones_file).st_mtime
        except OSError:
            return
        if m == self.zones_mtime: return
        try:
            self.zones = json.load(open(self.zones_file)); self.zones_mtime = m
            log("zones loaded:", ", ".join(f"{c} ({len(Z.regions(z or {}))} region(s), line {'yes' if (z or {}).get('line') else 'no'})"
                                         for c, z in self.zones.items() if c in self.cams) or "none for these cameras")
        except Exception as e:
            log("zones file not read:", repr(e))

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
        b.set_property("uri", s["uri"]); setp(b, "drop-frame-interval", self.drop)
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
        if self.mux_pads[i] is None: self.mux_pads[i] = self.mux.request_pad_simple(f"sink_{i}")
        if pad.link(self.mux_pads[i]) != Gst.PadLinkReturn.OK: log(f"ERROR linking camera {self.cams[i]}")

    def on_src_buffer(self, pad, info, i):
        if self.stopping: return Gst.PadProbeReturn.DROP
        self.last_buf[i] = time.monotonic(); self.src_frames[i] += 1
        return Gst.PadProbeReturn.OK

    def restart_source(self, i):
        old, pad = self.bins[i], self.mux_pads[i]
        old.set_state(Gst.State.NULL); self.pipe.remove(old)
        if pad is not None:
            pad.send_event(Gst.Event.new_flush_stop(False)); self.mux.release_request_pad(pad); self.mux_pads[i] = None
        self.reconnects[i] += 1; self.outage[i] += 1
        self.make_source(i).sync_state_with_parent()

    def watch_sources(self):
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
        if t == Gst.MessageType.EOS: log("EOS - stopped" if self.stopping else "EOS - all sources finished"); self.loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            if msg.src in self.core or msg.src is self.pipe:
                log("FATAL", msg.src.get_name(), err.message, (dbg or "")[:300]); self.fatal = True; self.exit_reason = "fatal"; self.loop.quit()
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
        cam = self.cams[fm.pad_index]; self.frames += 1; self.fps_cnt[cam] += 1
        zone = self.zones.get(cam) or {}
        objs, boxes = [], []
        lo = fm.obj_meta_list
        while lo:
            o = pyds.NvDsObjectMeta.cast(lo.data); r = o.rect_params
            lab = self.labels[o.class_id] if 0 <= o.class_id < len(self.labels) else str(o.class_id)
            b = (r.left, r.top, r.width, r.height)
            d = Z.describe(lab, b, zone, MUX_W, MUX_H)
            c = Z.cls(d) if d else None
            boxes.append((b, c, lab, o.confidence))
            if d:
                objs.append({**d, "label": lab, "cls": c, "track": int(o.object_id) if o.object_id < 2**53 else None,
                             "conf": round(float(o.confidence), 3),
                             "box": [round(b[0] / MUX_W, 4), round(b[1] / MUX_H, 4), round(b[2] / MUX_W, 4), round(b[3] / MUX_H, 4)]})
            lo = lo.next
        now, wall = time.monotonic(), datetime.now()
        rel = self.policy.release(cam, now)                    # a held frame (news that had to wait): written now
        if rel:
            r_objs, (r_jpg, r_wall), r_cls = rel
            self.write_images(cam, r_objs, r_cls, r_jpg, r_wall)
            self.policy.saved(cam, r_objs, r_cls, now)
        due, hold = self.policy.decide(cam, objs, now)         # savepolicy.py: nothing short / small / new is skipped
        if (due or hold) and self.disk_low:                    # saving paused (disk low): no frame copies for nothing
            self.policy.saved(cam, objs, due, now); due, hold = [], False
        snap = now - self.last_snap.get(cam, -1e9) >= 2.0
        if not (due or hold or snap): return
        img = self.frame_image(buf, fm, zone, boxes)
        if img is None: return
        if snap:
            self.last_snap[cam] = now
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
                           "ts": wall.isoformat(timespec="milliseconds"),
                           "objects": [{k: o[k] for k in ("label", "kind", "regions", "alert", "outside", "track", "conf", "box")}
                                       for o in objs]})
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

    def frame_image(self, buf, fm, zone, boxes):
        """this frame at 960x540 BGR with the zones and the boxes drawn"""
        try:
            rgba = pyds.get_nvds_buf_surface(hash(buf), fm.batch_id)
            try: full = np.array(rgba, copy=True, order="C")
            finally: pyds.unmap_nvds_buf_surface(hash(buf), fm.batch_id)
        except Exception as e:
            log("frame copy failed:", repr(e)); return None
        img = cv2.resize(cv2.cvtColor(full, cv2.COLOR_RGBA2BGR), (OUT_W, OUT_H), interpolation=cv2.INTER_AREA)
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
        pads = [p for p in self.mux_pads if p is not None]
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
            streams = [{"name": s["name"], "type": s["type"], "online": self.online[i],
                        "reachable": None if self.reach[i] is None or self.online[i] else self.reach[i][1],
                        "last_frame_s": None if not self.last_buf[i] else round(mono - self.last_buf[i], 1),
                        "fps": self.fps.get(s["name"], 0), "reconnects": self.reconnects[i],
                        "images": {c: n for (cam, c), n in self.counts.items() if cam == s["name"]}}
                       for i, s in enumerate(self.srcs)]
            st = {"running": True, "started": self.started, "pid": os.getpid(), "updated": datetime.now().isoformat(timespec="seconds"),
                  "frames": self.frames, "streams": streams, "free_disk_gb": round(gb, 1), "temp_c": temp,
                  "images_paused_low_disk": self.disk_low}
            tmp = STATE + ".tmp"; json.dump(st, open(tmp, "w")); os.replace(tmp, STATE)
        except Exception as e:
            log("state write failed:", repr(e))
        return True

    def run(self):
        mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo") if ":" in l}
        if mem.get("MemAvailable", 0) < 1_500_000:
            sys.exit(f"SAFETY: only {mem.get('MemAvailable', 0)//1024} MB RAM available (< 1.5 GB) - not starting")
        signal.signal(signal.SIGTERM, lambda *a: (log("SIGTERM - stopping cleanly"), self.stop()))
        signal.signal(signal.SIGINT, lambda *a: (log("SIGINT - stopping cleanly"), self.stop()))
        self.safety()
        GLib.timeout_add_seconds(5, self.write_state)
        GLib.timeout_add_seconds(RECONNECT_S, self.watch_sources)
        log(f"starting {len(self.srcs)} camera(s): " + ", ".join(f"{s['name']} ({s['type']})" for s in self.srcs)
            + f"; images every {self.save_every:g} s per camera and class → {self.incoming}")
        self.pipe.set_state(Gst.State.PLAYING)
        try: self.loop.run()
        finally:
            self.pipe.set_state(Gst.State.NULL)
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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--pgie", required=True)
    App(ap.parse_args()).run()
