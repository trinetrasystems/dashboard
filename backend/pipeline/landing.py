"""Landing / left-object detection worker (PERIMETER_THROW_DETECTION_SPEC §7.5 hot spots, §7.6 fusion, §7.8 feedback) —
step E, 28 Nov 2026. Its OWN process, started by the pipeline when a camera with a landing zone has a memory (bank.py):
PyTorch and the GPU search never run inside the DeepStream process, and a crash here never stops the cameras (the
pipeline starts it again). Spec §2.7: the pipeline only copies a picture once a second.

    python3 landing.py --config ds/run/landing_run.json

Input (from the pipeline, /dev/shm/perimeter_landing/): per camera <camera>.npy = the landing tiles' part of the frame
(1920x1080 pixels, BGR), <camera>.view.npy = the whole frame at 960x540, <camera>.json = its time, day / night (+ how
long since the switch) and the boxes of the people and vehicles in it. About once a second.

Per check (one camera): the score map against the bank of the current mode (bank.Scorer) → hot cells (over the bank's
threshold) → more than scene_change_frac of the zones hot = a scene change (lights, rain on the lens): nothing counted
→ hot spots (connected, at least min_hot_cells) → Candidates:
  a spot under a person / vehicle box (IoA > person_ioa_suppress) never starts a candidate and never counts for one;
  a candidate seen in confirm_checks clean checks is a NEW OBJECT; it is dropped clear_s after it was last seen.
  NEW OBJECT within landing_window_s after a throw of this camera in the direction of its zone's side ("in" → an
  inside zone, "out" → an outside zone) → "landed": the throw's alert is upgraded to Critical (alert rule *landed).
  NEW OBJECT still there after unattended_s, nobody within owner_px of it for owner_s, not from a throw →
  "unattended" (alert rule *unattended, Critical).
No detection for pause_after_switch_s after a day / night switch (the bank changes; spec §7.6).
Alerts go to the dashboard's incoming/ like any camera image (<camera>_landed-object_… / <camera>_left-object_…jpg + the
frame description FIRST). Kept: ds/landing/events.jsonl, the review picture and the spot's tiles (for False alarm)
under ds/landing/<YYYYmmdd>/; days older than the image retention deleted.
False alarm (data/feedback/feedback.jsonl, learn = "bank"): the alert's tiles' patches join the bank of that mode at once
(bank.add_feedback; patches the bank already has are skipped); its undo takes them out again. Never on a timer (§7.8).
State for the dashboard: /dev/shm/perimeter_landing_state.json (every 2 s).
"""
import argparse
import collections
import json
import math
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bank as BK
import zones as Z

DEFAULTS = {"check_interval_s": 1.0, "scene_change_frac": 0.35, "person_ioa_suppress": 0.3, "match_px": 80,
            "confirm_checks": 3, "unattended_s": 30, "clear_s": 10, "owner_px": 250, "owner_s": 10,
            "landing_window_s": 10, "pause_after_switch_s": 60, "max_candidates": 20, "max_alerts_per_day": 300}
SHM = os.environ.get("PERIMETER_SHM", "/dev/shm")
IN_DIR = f"{SHM}/perimeter_landing"
STATE = f"{SHM}/perimeter_landing_state.json"
SIDE_OF_DIR = {"in": "inside", "out": "outside"}


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), "LANDING", *a, flush=True)


def ioa(a, b):
    """intersection of box a (l, t, r, b) with box b (l, t, w, h) over a's area"""
    ix = max(0.0, min(a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[1] + b[3]) - max(a[1], b[1]))
    return ix * iy / max(1.0, (a[2] - a[0]) * (a[3] - a[1]))


def box_gap(cx, cy, b):
    """distance from a point to a box (l, t, w, h); 0 inside"""
    dx = max(b[0] - cx, 0.0, cx - (b[0] + b[2]))
    dy = max(b[1] - cy, 0.0, cy - (b[1] + b[3]))
    return math.hypot(dx, dy)


# ── hot spots → candidates → events (pure Python: the tests call it with made-up spots) ──────────────────────────
class Candidates:
    def __init__(self, cfg=None):
        self.c = {**DEFAULTS, **(cfg or {})}
        self.items, self.next_id = [], 1

    def clear(self):
        self.items = []

    def update(self, t, spots, people, paused=False) -> list:
        """t = seconds (monotonic); spots = [{"box": (l, t, r, b) frame px, "level", "zone", "side"}];
        people = [(l, t, w, h)] person / vehicle boxes → events [{"type": "new" | "unattended" | "gone", ...}]"""
        c, ev = self.c, []
        pairs = []
        for j, s in enumerate(spots):
            sx, sy = (s["box"][0] + s["box"][2]) / 2, (s["box"][1] + s["box"][3]) / 2
            for i, cd in enumerate(self.items):
                d = math.hypot(sx - cd["cx"], sy - cd["cy"])
                if d <= c["match_px"]:
                    pairs.append((d, i, j))
        used_c, used_s = set(), set()
        for d, i, j in sorted(pairs):
            if i in used_c or j in used_s:
                continue
            used_c.add(i); used_s.add(j)
            cd, s = self.items[i], spots[j]
            cd["last_seen"] = t
            cd["level"] = max(cd["level"], s["level"])
            if max((ioa(s["box"], p) for p in people), default=0) <= c["person_ioa_suppress"] and not paused:
                cd["seen"] += 1
                cd["box"] = s["box"]
        for j, s in enumerate(spots):
            if j in used_s or paused or len(self.items) >= c["max_candidates"]:
                continue
            if max((ioa(s["box"], p) for p in people), default=0) > c["person_ioa_suppress"]:
                continue                                           # a person / vehicle itself: never a candidate
            self.items.append({"id": self.next_id, "box": s["box"], "cx": (s["box"][0] + s["box"][2]) / 2,
                               "cy": (s["box"][1] + s["box"][3]) / 2, "first_seen": t, "last_seen": t, "seen": 1,
                               "level": s["level"], "zone": s["zone"], "side": s["side"], "confirmed": False,
                               "unattended": False, "linked": False, "owner_last": t})
            self.next_id += 1
        keep = []
        for cd in self.items:
            if any(box_gap(cd["cx"], cd["cy"], p) <= c["owner_px"] for p in people):
                cd["owner_last"] = t                               # someone is with it
            if t - cd["last_seen"] > c["clear_s"]:
                if cd["confirmed"]:
                    ev.append({"type": "gone", **self._pub(cd, t)})
                continue
            if not cd["confirmed"] and cd["seen"] >= c["confirm_checks"]:
                cd["confirmed"] = True
                ev.append({"type": "new", **self._pub(cd, t)})
            if cd["confirmed"] and not cd["unattended"] and not cd["linked"] and t - cd["first_seen"] >= c["unattended_s"] \
                    and t - cd["owner_last"] >= c["owner_s"] and t - cd["last_seen"] <= 1.5 * c["check_interval_s"] + 0.5:
                cd["unattended"] = True
                ev.append({"type": "unattended", **self._pub(cd, t)})
            keep.append(cd)
        self.items = keep
        return ev

    @staticmethod
    def _pub(cd, t):
        return {"cand": cd["id"], "box": [round(v) for v in cd["box"]], "zone": cd["zone"], "side": cd["side"],
                "level": round(cd["level"], 3), "first_seen": cd["first_seen"], "t": t, "age_s": round(t - cd["first_seen"], 1)}

    def link(self, cand_id):
        for cd in self.items:
            if cd["id"] == cand_id:
                cd["linked"] = True


def hot_spots(smap, mask, threshold, min_cells, origin, zones, ignore=None):
    """score map → (spots, hot fraction of the zones). origin = the map's top-left corner in frame pixels.
    ignore: the no-detection cells — a spot whose peak is there (or mostly there) is dropped: an object under a
    no-detection region never alerts, even though its halo reaches past the region's edge."""
    import cv2
    import numpy as np
    inside = mask > 0
    hot = (smap >= threshold) & inside
    counted = inside if ignore is None else inside & ~ignore
    frac = float((hot & counted).sum()) / max(1, int(counted.sum()))
    n, lab, st, _c = cv2.connectedComponentsWithStats(hot.astype(np.uint8), connectivity=8)
    out = []
    for k in range(1, n):
        if st[k, cv2.CC_STAT_AREA] < min_cells:
            continue
        x, y, w, h = (int(v) for v in st[k, :4])
        part = lab[y:y + h, x:x + w] == k
        if ignore is not None:
            ig = ignore[y:y + h, x:x + w][part]
            vals = smap[y:y + h, x:x + w][part]
            if ig[int(np.argmax(vals))] or ig.mean() >= 0.5:
                continue                                           # its peak is in a no-detection region
        zi = np.bincount(mask[y:y + h, x:x + w][part].ravel(), minlength=len(zones) + 1)[1:]
        z = int(np.argmax(zi)) if zi.size else 0
        s = BK.MAP_SCALE
        out.append({"box": (origin[0] + x * s, origin[1] + y * s, origin[0] + (x + w) * s, origin[1] + (y + h) * s),
                    "level": float(smap[y:y + h, x:x + w][part].max()), "zone": zones[z]["name"] if zones else None,
                    "zone_i": z, "side": zones[z]["side"] if zones else None, "cells": int(st[k, cv2.CC_STAT_AREA])})
    return out, frac


def throw_match(throws, wall_ts, side, window_s):
    """the latest throw of this camera that fits a new object first seen at wall_ts in a zone on `side`"""
    best = None
    for tt, d, ev in throws:
        if wall_ts - window_s <= tt <= wall_ts + 2 and (side is None or SIDE_OF_DIR.get(d) == side):
            if best is None or tt > best[0]:
                best = (tt, d, ev)
    return best


# ── one camera ─────────────────────────────────────────────────────────────────────────────────────────────────
class Cam:
    """one camera. layout = all its landing zones now; act = the zones its bank of this mode covers (a removed zone is
    simply not looked at; an added / moved one waits for the next build — bank.covered)"""

    def __init__(self, w, name):
        self.w, self.name = w, name
        self.zone, self.layout, self.act, self.scorer, self.bank = None, None, None, None, None
        self.key, self.missing = None, []
        self.cands = Candidates(w.c)
        self.status, self.error, self.mode, self.last_t = "starting", None, None, 0.0
        self.stats = collections.Counter()
        self.ms = collections.deque(maxlen=120)
        self.levels = collections.deque(maxlen=120)
        self.retry_at = 0.0

    def set_zone(self, zone):
        lay = BK.landing_layout(zone)
        look = lambda l: (l["tiles"], [(z["name"], z["points"], z["side"]) for z in l["zones"]], l.get("ignore"))
        if self.layout and look(lay) == look(self.layout):
            self.zone = zone
            return
        self.zone, self.layout, self.key = zone, lay, None          # the bank is matched to the zones again (ensure_bank)
        self.retry_at = 0.0

    def ensure_bank(self, mode):
        """→ True when the bank of this mode is loaded for the zones it covers"""
        root = self.w.banks
        st = BK.Bank.stamp(root, self.name, mode)
        if st is None:
            self.status, self.bank, self.key = f"no {mode} memory yet (Recordings → Normal frames, then Build landing memory)", None, None
            return False
        key = (mode, st, json.dumps(self.layout["tiles"]), json.dumps([(z["points"], z["side"]) for z in self.layout["zones"]]),
               json.dumps(self.layout.get("ignore")))
        if self.bank and self.key == key:
            return True
        if time.monotonic() < self.retry_at:
            return False
        meta = BK.read_meta(root, self.name, mode) or {}
        ok, missing = BK.covered(self.layout, meta.get("tiles"))
        if not ok:
            self.status, self.bank, self.key = (f"the landing zones changed since the {mode} memory was built: "
                                                f"it is built again by itself (or Build landing memory)"), None, None
            self.retry_at = time.monotonic() + 30
            return False
        try:
            self.bank = None
            self.w.free_gpu()
            act = BK.sub_layout(self.layout, ok)
            bank_tiles = [tuple(t) for t in meta["tiles"]]
            self.bank = BK.Bank(root, self.name, mode, self.w.device, only_tiles=[bank_tiles.index(t) for t in act["tiles"]])
            self.act, self.missing, self.key = act, missing, key
            self.scorer = BK.Scorer(act, bb=self.w.backbone())
            self.cands.clear()
            log(f"{self.name}: {mode} memory loaded for {', '.join(z['name'] for z in act['zones'])} "
                f"({self.bank.patches} patches per tile, threshold {self.bank.threshold:.2f}"
                f"{', ' + str(len(self.bank.extra_ids)) + ' false alarm(s) learnt' if self.bank.extra_ids else ''})"
                + (f"; not in it yet: {', '.join(missing)}" if missing else ""))
            return True
        except Exception as e:
            self.status, self.error, self.bank, self.key = f"memory not loaded: {e}", repr(e), None, None
            self.retry_at = time.monotonic() + 60
            log(f"{self.name}: {mode} memory not loaded: {e!r}")
            return False

    def check(self, meta, crop, view):
        if not self.layout or not self.layout["tiles"]:
            self.status = "no landing zone"
            return
        mode = meta.get("mode")
        if mode not in ("day", "night"):
            return
        if mode != self.mode:
            if self.mode is not None:
                log(f"{self.name}: {self.mode} → {mode}")
            self.mode = mode
            self.cands.clear()
        if not self.ensure_bank(mode):
            return
        ox, oy, ox1, oy1 = (list(meta.get("crop") or []) + [0, 0, 0, 0])[:4]
        x0, y0, x1, y1 = self.act["crop"]
        if not (ox <= x0 and oy <= y0 and ox1 >= x1 and oy1 >= y1) or crop.shape[:2] != (oy1 - oy, ox1 - ox):
            return                                                 # the zones changed while the picture was on its way
        t0 = time.perf_counter()
        smap = self.scorer.score(crop, self.bank.prepared, origin=(ox, oy))
        self.ms.append((time.perf_counter() - t0) * 1000)
        self.stats["checks"] += 1
        lvl = self.scorer.level(smap)
        self.levels.append(lvl)
        paused = (meta.get("mode_age_s") or 0) < self.w.c["pause_after_switch_s"] and meta.get("mode_switched")
        spots, frac = hot_spots(smap, self.scorer.zmask, self.bank.threshold, self.w.c.get("min_hot_cells", 3),
                                (x0, y0), self.act["zones"], ignore=self.scorer.ignore)
        if frac > self.w.c["scene_change_frac"]:
            self.stats["scene_change"] += 1
            spots = []
            paused = True
        self.status = "paused (day / night switch)" if paused and meta.get("mode_switched") else \
            "scene change: not counted" if frac > self.w.c["scene_change_frac"] else "watching"
        people = [tuple(b[:4]) for b in meta.get("people") or []]
        t = float(meta.get("mono") or time.monotonic())
        for ev in self.cands.update(t, spots, people, paused=bool(paused)):
            self.on_event(ev, meta, crop, view, smap)

    def on_event(self, ev, meta, crop, view, smap):
        wall = datetime.fromisoformat(meta["ts"])
        first_wall = wall - timedelta(seconds=ev["t"] - ev["first_seen"])
        self.stats[ev["type"]] += 1
        if ev["type"] == "gone":
            log(f"{self.name}: object {ev['cand']} in {ev['zone']} gone after {ev['age_s']} s")
            return
        if ev["type"] == "new":
            m = throw_match(self.w.throws.get(self.name, ()), first_wall.timestamp(), ev["side"], self.w.c["landing_window_s"])
            log(f"{self.name}: NEW OBJECT {ev['cand']} in {ev['zone']} ({ev['side'] or 'no line'}) at {ev['box']}, level "
                f"{ev['level']:.2f}" + (f" — {m[0] - first_wall.timestamp():+.1f} s from a throw {m[1]}" if m else ""))
            if not m:
                return                                             # left there? decided after unattended_s
            self.cands.link(ev["cand"])
            self.w.alert(self, "landed", ev, meta, crop, view, throw=m[2])
        elif ev["type"] == "unattended":
            log(f"{self.name}: UNATTENDED object {ev['cand']} in {ev['zone']} for {ev['age_s']:.0f} s at {ev['box']}")
            self.w.alert(self, "unattended", ev, meta, crop, view)

    def summary(self):
        ms = sorted(self.ms)
        lv = sorted(self.levels)
        b = self.bank
        return {"status": self.status, "mode": self.mode, "checks": self.stats["checks"],
                "ms_median": round(ms[len(ms) // 2]) if ms else None,
                "level_median": round(lv[len(lv) // 2], 3) if lv else None,
                "threshold": round(b.threshold, 3) if b else None, "patches": b.patches if b else None,
                "learnt": len(b.extra_ids) if b else 0, "candidates": len(self.cands.items),
                "new": self.stats["new"], "landed": self.stats["landed_alerts"], "unattended": self.stats["unattended_alerts"],
                "scene_change": self.stats["scene_change"], "error": self.error,
                "zones": [{"name": z["name"], "side": z["side"]} for z in (self.layout or {}).get("zones", [])],
                "watching_zones": [z["name"] for z in (self.act or {}).get("zones", [])] if b else [],
                "not_in_memory": self.missing if b else []}


# ── the worker ─────────────────────────────────────────────────────────────────────────────────────────────────
class Worker:
    def __init__(self, run):
        import numpy as np
        self.np = np
        self.run = run
        self.c = {**DEFAULTS, **(run.get("cfg") or {})}
        self.cams = {c: Cam(self, c) for c in run["cameras"]}
        self.banks = run["banks_dir"]
        self.landing_dir = run["landing_dir"]
        self.incoming = run["incoming"]
        self.throws_file = os.path.join(run["throws_dir"], "events.jsonl")
        self.feedback_file = run["feedback_file"]
        self.zones_file = run["zones_file"]
        self.keep_days = int(run.get("keep_days", 30) or 0)
        self.device = os.environ.get("PATCHCORE_DEVICE", "cuda")
        self._bb = None
        self.throws = collections.defaultdict(lambda: collections.deque(maxlen=50))
        self.tail_pos = {}
        self.start_size = {}                                       # a file that appears later is read from its start
        for f in (run["feedback_file"], os.path.join(run["throws_dir"], "events.jsonl")):
            try:
                self.start_size[f] = os.path.getsize(f)
            except OSError:
                self.start_size[f] = 0
        self.events_by_image = {}
        self.zones_mtime = None
        self.seen = {}
        self.day, self.n_day = None, collections.Counter()
        self.stop = False
        self.parent = os.getppid()
        os.makedirs(self.landing_dir, exist_ok=True)
        self._load_events()

    def backbone(self):
        if self._bb is None:
            import patchcore as P
            t0 = time.time()
            self._bb = P.Backbone(self.device)
            log(f"backbone ready on {self.device} ({time.time() - t0:.1f} s)")
        return self._bb

    def free_gpu(self):
        if self.device.startswith("cuda"):
            import torch
            torch.cuda.empty_cache()

    # files the worker follows ------------------------------------------------------------------------------------
    def _tail(self, path, resume=False):
        """new complete lines of an append-only file since the last call; the first call starts at its end, or —
        resume — where the last worker stopped"""
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        pos = self.tail_pos.get(path)
        if pos is None:
            pos = self._offset(path, size) if resume else min(size, self.start_size.get(path, size))
        if size < pos:
            pos = 0                                                # rotated / trimmed
        if size == pos:
            self.tail_pos[path] = pos
            return []
        with open(path, "rb") as f:
            f.seek(pos)
            data = f.read(size - pos)
        cut = data.rfind(b"\n") + 1
        self.tail_pos[path] = pos + cut
        return [l for l in data[:cut].decode(errors="replace").splitlines() if l.strip()]

    def _offset(self, path, size):
        """feedback: where the last worker stopped (a False alarm pressed while the worker was down is not lost)"""
        try:
            st = json.load(open(os.path.join(self.landing_dir, "feedback.offset")))
            if st.get("path") == path:
                return min(int(st.get("pos", size)), size)
        except (OSError, ValueError):
            pass
        return min(size, self.start_size.get(path, size))   # no offset kept: from where it was when the worker started

    def _save_offset(self, path):
        try:
            tmp = os.path.join(self.landing_dir, ".feedback.offset.part")
            with open(tmp, "w") as f:
                json.dump({"path": path, "pos": self.tail_pos.get(path, 0)}, f)
            os.replace(tmp, os.path.join(self.landing_dir, "feedback.offset"))
        except OSError:
            pass

    def follow_throws(self):
        for line in self._tail(self.throws_file):
            try:
                ev = json.loads(line)
                cam = ev.get("camera")
                if cam in self.cams:
                    self.throws[cam].append((datetime.fromisoformat(ev["ts"]).timestamp(), ev.get("dir") or "in", ev))
            except (ValueError, KeyError, TypeError):
                pass
        cut = time.time() - 120
        for q in self.throws.values():
            while q and q[0][0] < cut:
                q.popleft()

    def follow_feedback(self):
        lines = self._tail(self.feedback_file, resume=True)
        for line in lines:
            try:
                fb = json.loads(line)
            except ValueError:
                continue
            if fb.get("learn") != "bank" or fb.get("camera") not in self.cams:
                continue
            try:
                if fb.get("verdict") == "false_alarm":
                    self.learn(fb)
                elif fb.get("verdict") == "undo":
                    if BK.remove_feedback(self.banks, fb["camera"], int(fb["alert_id"])):
                        log(f"{fb['camera']}: false alarm #{fb['alert_id']} taken back: its patches left the memory")
            except Exception as e:
                log(f"feedback #{fb.get('alert_id')} not learnt: {e!r}")
        if lines:
            self._save_offset(self.feedback_file)

    def learn(self, fb):
        """a False alarm on a landing alert → its tiles' patches into the bank of the mode it was seen in"""
        np, cam = self.np, self.cams[fb["camera"]]
        ev = self.events_by_image.get(os.path.basename(fb.get("image") or ""))
        if not ev or not ev.get("tiles_file"):
            log(f"{fb['camera']}: false alarm #{fb['alert_id']}: no landing picture of it kept (nothing to learn)")
            return
        meta = BK.read_meta(self.banks, cam.name, ev["mode"]) or {}
        d = np.load(os.path.join(self.landing_dir, ev["tiles_file"]))
        bank_tiles = [tuple(t) for t in meta.get("tiles") or []]
        pairs = [(bank_tiles.index(tuple(int(v) for v in p)), k) for k, p in enumerate(d["xy"]) if tuple(int(v) for v in p) in bank_tiles]
        if not pairs or not cam.scorer:
            log(f"{fb['camera']}: false alarm #{fb['alert_id']}: those tiles are not in the memory any more (nothing to learn)")
            return
        ids, tiles = [p[0] for p in pairs], [d["tiles"][p[1]] for p in pairs]
        feats = cam.scorer.features(tiles).flatten(2).transpose(1, 2)       # [n, P, D]
        import patchcore as P
        b = BK.Bank(self.banks, cam.name, ev["mode"], self.device, only_tiles=ids)   # just these tiles (small)
        keep = []
        for k in range(len(ids)):
            one = (b.prepared[0][k:k + 1], b.prepared[1][k:k + 1], b.prepared[2][k:k + 1])
            dist = P.nn_distance_tiles(feats[k].t().reshape(1, -1, P.TILE_PX // 8, P.TILE_PX // 8), *one).flatten()
            keep.append(feats[k][dist >= 0.25 * b.threshold])                # what the bank does not have yet
        del b
        n = BK.add_feedback(self.banks, cam.name, ev["mode"], int(fb["alert_id"]), ids, keep)
        log(f"{cam.name}: false alarm #{fb['alert_id']} learnt: {n} patch(es) of {len(ids)} tile(s) added to the {ev['mode']} memory")

    def _load_events(self):
        f = os.path.join(self.landing_dir, "events.jsonl")
        try:
            for line in open(f).read().splitlines()[-5000:]:
                ev = json.loads(line)
                if ev.get("image_name"):
                    self.events_by_image[ev["image_name"]] = ev
        except (OSError, ValueError):
            pass

    def reload_zones(self):
        try:
            m = os.stat(self.zones_file).st_mtime
        except OSError:
            return
        if m == self.zones_mtime:
            return
        try:
            zs = json.load(open(self.zones_file))
        except (OSError, ValueError) as e:
            log("zones not read:", repr(e))
            return
        self.zones_mtime = m
        for c in self.cams.values():
            c.set_zone(zs.get(c.name) or {})

    # alerts ---------------------------------------------------------------------------------------------------------
    def alert(self, cam, what, ev, meta, crop, view, throw=None):
        import cv2
        np = self.np
        wall = datetime.fromisoformat(meta["ts"])
        day = wall.strftime("%Y%m%d")
        cam.stats[what + "_alerts"] += 1
        if self.n_day[(cam.name, day)] >= self.c["max_alerts_per_day"]:
            log(f"{cam.name}: {what} not sent (more than {self.c['max_alerts_per_day']} today: check the zones / memory)")
            return
        self.n_day[(cam.name, day)] += 1
        ts = f"{wall.strftime('%Y%m%d-%H%M%S')}-{wall.microsecond // 1000:03d}"
        cls = "landed-object" if what == "landed" else "left-object"
        name = f"{cam.name}_{cls}_{ts}.jpg"
        d = os.path.join(self.landing_dir, day)
        os.makedirs(d, exist_ok=True)
        x0, y0 = meta["crop"][0], meta["crop"][1]                 # the picture's own top-left corner
        l, t, r, b = ev["box"]
        xy = [(tx, ty, s) for (tx, ty, s) in cam.act["tiles"] if tx < r and tx + s > l and ty < b and ty + s > t]
        tiles = np.stack([crop[ty - y0:ty - y0 + s, tx - x0:tx - x0 + s] for tx, ty, s in xy])
        tf = f"{day}/{cam.name}_{ts}.tiles.npz"
        np.savez(os.path.join(self.landing_dir, tf), xy=np.array(xy), tiles=tiles)   # tiles by position: zones may change
        img = self.review(cam, what, ev, crop, view, throw, (x0, y0))
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        rec = {"type": what, "camera": cam.name, "ts": wall.isoformat(timespec="milliseconds"), "mode": meta.get("mode"),
               "zone": ev["zone"], "side": ev["side"], "box": ev["box"], "level": ev["level"], "age_s": ev["age_s"],
               "threshold": round(cam.bank.threshold, 3), "image_name": name, "picture": f"{day}/{name}", "tiles_file": tf,
               "throw": {k: throw.get(k) for k in ("ts", "dir", "crossing_xy", "track_id")} if throw else None}
        if ok:
            with open(os.path.join(d, name), "wb") as f:
                f.write(jpg.tobytes())
        with open(os.path.join(self.landing_dir, "events.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        self.events_by_image[name] = rec
        bx = [round(ev["box"][0] / 1920, 4), round(ev["box"][1] / 1080, 4), round((ev["box"][2] - ev["box"][0]) / 1920, 4),
              round((ev["box"][3] - ev["box"][1]) / 1080, 4)]
        obj = {"label": "landed object" if what == "landed" else "left object", "kind": "object",
               "regions": [ev["zone"]] if ev["zone"] else [], "alert": False, "outside": ev["side"] == "outside",
               "near": False, "crossed": False, "thrown": what == "landed", "landed": what == "landed",
               "unattended": what == "unattended", "throw_dir": (throw or {}).get("dir"), "age_s": ev["age_s"],
               "track": None, "conf": 1.0, "box": bx}
        meta_out = json.dumps({"v": 1, "frame": f"{cam.name}|{wall.isoformat(timespec='milliseconds')}|{what}{ev['cand']}",
                               "camera": cam.name, "ts": wall.isoformat(timespec="milliseconds"), "mode": meta.get("mode"),
                               "objects": [obj]})
        if not ok:
            return
        try:
            side = os.path.join(self.incoming, name[:-4] + ".json")
            with open(side + ".part", "w") as f:
                f.write(meta_out)
            os.replace(side + ".part", side)
            tmp = os.path.join(self.incoming, f".{name}.part")
            with open(tmp, "wb") as f:
                f.write(jpg.tobytes())
            os.replace(tmp, os.path.join(self.incoming, name))
            log(f"{cam.name}: {what.upper()} alert → {name}")
        except OSError as e:
            log(f"{cam.name}: alert not sent to the dashboard: {e!r}")

    def review(self, cam, what, ev, crop, view, throw, origin):
        """the whole frame (960x540) with the landing zones and the object's box + a 3x zoom of it"""
        import cv2
        np = self.np
        img = view.copy() if view is not None and view.ndim == 3 else np.zeros((540, 960, 3), np.uint8)
        sx, sy = img.shape[1] / 1920, img.shape[0] / 1080
        for z in cam.layout["zones"]:
            pts = np.array([(p[0] * img.shape[1], p[1] * img.shape[0]) for p in z["points"]], np.int32)
            cv2.polylines(img, [pts], True, (132, 204, 22), 2, cv2.LINE_AA)
        line = (cam.zone or {}).get("line") or []
        if len(line) >= 2:
            cv2.polylines(img, [np.array([(p[0] * img.shape[1], p[1] * img.shape[0]) for p in line], np.int32)], False,
                          (0, 200, 255), 2, cv2.LINE_AA)
        if throw and throw.get("track"):
            cv2.polylines(img, [np.array([(x * sx, y * sy) for _f, x, y in throw["track"]], np.int32)], False, (0, 0, 255), 2, cv2.LINE_AA)
        l, t, r, b = ev["box"]
        cv2.rectangle(img, (int(l * sx) - 4, int(t * sy) - 4), (int(r * sx) + 4, int(b * sy) + 4), (0, 0, 255), 2, cv2.LINE_AA)
        x0, y0 = origin
        cx, cy, s = (l + r) // 2 - x0, (t + b) // 2 - y0, 64
        part = crop[max(0, cy - s):cy + s, max(0, cx - s):cx + s]
        if part.size:
            zoom = cv2.resize(part, (part.shape[1] * 2, part.shape[0] * 2), interpolation=cv2.INTER_NEAREST)
            zh, zw = min(zoom.shape[0], img.shape[0] - 10), min(zoom.shape[1], img.shape[1] - 10)
            img[img.shape[0] - zh - 10:img.shape[0] - 10, img.shape[1] - zw - 10:img.shape[1] - 10] = zoom[:zh, :zw]
            cv2.rectangle(img, (img.shape[1] - zw - 11, img.shape[0] - zh - 11), (img.shape[1] - 9, img.shape[0] - 9), (255, 255, 255), 1)
        text = (f"Thrown {(throw or {}).get('dir', 'in')} and landed: {ev['zone']}" if what == "landed"
                else f"Object left {ev['age_s']:.0f} s: {ev['zone']}")
        cv2.putText(img, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(img, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
        return img

    def prune(self):
        today = datetime.now().strftime("%Y%m%d")
        if self.day == today:
            return
        self.day = today
        if self.keep_days <= 0:
            return
        import shutil
        cut = time.time() - self.keep_days * 86400
        for n in os.listdir(self.landing_dir):
            p = os.path.join(self.landing_dir, n)
            if re.fullmatch(r"\d{8}", n) and os.path.isdir(p):
                try:
                    if datetime.strptime(n, "%Y%m%d").timestamp() < cut:
                        shutil.rmtree(p)
                        log(f"pictures of {n} deleted (older than {self.keep_days} days)")
                except (OSError, ValueError):
                    pass
        ev = os.path.join(self.landing_dir, "events.jsonl")
        try:
            if os.path.getsize(ev) > 8_000_000:
                lines = open(ev).read().splitlines()[-20000:]
                with open(ev + ".tmp", "w") as f:
                    f.write("\n".join(lines) + "\n")
                os.replace(ev + ".tmp", ev)
        except OSError:
            pass

    def write_state(self):
        st = {"pid": os.getpid(), "updated": datetime.now().isoformat(timespec="seconds"),
              "cameras": {n: c.summary() for n, c in self.cams.items()}}
        try:
            with open(STATE + ".tmp", "w") as f:
                json.dump(st, f)
            os.replace(STATE + ".tmp", STATE)
        except OSError:
            pass

    def loop(self):
        np = self.np
        log(f"watching {', '.join(self.cams)} (one check per camera about every {self.c['check_interval_s']:g} s)")
        self.reload_zones()
        self.backbone()
        last_state = last_files = 0.0
        while not self.stop:
            if os.getppid() != self.parent:
                log("the pipeline is gone: stopping")
                break
            did = False
            for name, cam in self.cams.items():
                jf = os.path.join(IN_DIR, f"{name}.json")
                try:
                    m = os.stat(jf).st_mtime
                except OSError:
                    continue
                if self.seen.get(name) == m:
                    continue
                self.seen[name] = m
                try:
                    meta = json.load(open(jf))
                    crop = np.load(os.path.join(IN_DIR, f"{name}.npy"))
                    view = np.load(os.path.join(IN_DIR, f"{name}.view.npy")) if os.path.exists(os.path.join(IN_DIR, f"{name}.view.npy")) else None
                except (OSError, ValueError) as e:
                    continue                                       # the next one comes in a second
                try:
                    cam.check(meta, crop, view)
                    did = True
                except Exception as e:
                    cam.error = repr(e)
                    cam.stats["errors"] += 1
                    if cam.stats["errors"] <= 5 or cam.stats["errors"] % 100 == 0:
                        log(f"{name}: check failed (carries on): {e!r}")
                    if "out of memory" in str(e).lower():
                        cam.bank = None
                        self.free_gpu()
                        cam.retry_at = time.monotonic() + 30
            now = time.monotonic()
            if now - last_files >= 1.0:
                last_files = now
                self.reload_zones()
                self.follow_throws()
                self.follow_feedback()
                self.prune()
            if now - last_state >= 2.0:
                last_state = now
                self.write_state()
            if not did:
                time.sleep(0.1)
        self.write_state()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--parent", type=int)
    a = ap.parse_args()
    try:                                                           # SIGTERM when the pipeline dies (never outlives it)
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)
        os.nice(5)                                                 # the cameras first
    except Exception:
        pass
    if a.parent and os.getppid() != a.parent:
        sys.exit("the pipeline is gone")
    run = json.load(open(a.config))
    w = Worker(run)
    if a.parent:
        w.parent = a.parent
    signal.signal(signal.SIGTERM, lambda *_: setattr(w, "stop", True))
    signal.signal(signal.SIGINT, lambda *_: setattr(w, "stop", True))
    try:
        w.loop()
    except Exception as e:
        log(f"FATAL: {e!r}")
        raise
    log("stopped")


def _selftest():
    c = Candidates({"confirm_checks": 3, "unattended_s": 30, "clear_s": 10, "owner_s": 10})
    spot = lambda x, y: {"box": (x - 10, y - 10, x + 10, y + 10), "level": 2.0, "zone": "Inside", "side": "inside"}
    ev = []
    for t in range(0, 45):                                         # an object appears at t=0 and stays, nobody near
        ev += c.update(float(t), [spot(500, 800)], [])
    kinds = [e["type"] for e in ev]
    assert kinds == ["new", "unattended"], kinds
    assert ev[0]["t"] == 2.0 and ev[1]["t"] == 30.0, ev
    c = Candidates()                                               # a person standing there: never a candidate
    ev = []
    for t in range(0, 40):
        ev += c.update(float(t), [spot(500, 800)], [(470, 700, 60, 130)])
    assert not ev and not c.items, ev
    c = Candidates()                                               # left by someone who stays next to it: new, never unattended
    ev = []
    for t in range(0, 60):
        ev += c.update(float(t), [spot(500, 800)], [(700, 650, 50, 150)])
    assert [e["type"] for e in ev] == ["new"], ev
    c = Candidates()                                               # flicker for 2 checks: nothing
    assert not c.update(0.0, [spot(500, 800)], []) and not c.update(1.0, [spot(500, 800)], []) and not c.update(2.0, [], [])
    assert not any(c.update(float(t), [], []) for t in range(3, 15)) and not c.items
    c = Candidates()                                               # a thrown object: linked → no unattended later
    ev = []
    for t in range(0, 45):
        for e in c.update(float(t), [spot(900, 700)], []):
            ev.append(e)
            if e["type"] == "new":
                c.link(e["cand"])
    assert [e["type"] for e in ev] == ["new"], ev
    c = Candidates()                                               # paused (day / night switch): nothing starts
    assert not any(c.update(float(t), [spot(500, 800)], [], paused=True) for t in range(10)) and not c.items
    c = Candidates()                                               # removed after 40 s: new, unattended, gone
    ev = []
    for t in range(0, 60):
        ev += c.update(float(t), [spot(500, 800)] if t < 40 else [], [])
    assert [e["type"] for e in ev] == ["new", "unattended", "gone"], ev
    th = [(100.0, "in", {"dir": "in"}), (130.0, "out", {"dir": "out"})]
    assert throw_match(th, 105.0, "inside", 10)[1] == "in" and throw_match(th, 135.0, "inside", 10) is None
    assert throw_match(th, 135.0, "outside", 10)[1] == "out" and throw_match(th, 150.0, "outside", 10) is None
    assert throw_match(th, 101.0, None, 10)[1] == "in"
    try:
        import numpy as np
    except ImportError:
        print("candidates ok (no numpy here)")
        return
    zones = [{"name": "Inside", "side": "inside"}, {"name": "Outside", "side": "outside"}]
    mask = np.zeros((50, 80), np.uint8); mask[25:, :] = 1; mask[:20, :] = 2
    smap = np.zeros((50, 80), np.float32); smap[30:33, 10:14] = 3.0; smap[5:6, 40:41] = 3.0; smap[10:13, 60:63] = 2.5
    sp, frac = hot_spots(smap, mask, 2.0, 3, (100, 200), zones)
    assert len(sp) == 2 and {s["zone"] for s in sp} == {"Inside", "Outside"}, sp          # the 1-cell spot is noise
    assert sp[0]["box"][0] >= 100 and frac < 0.05, (sp, frac)
    ign = np.zeros_like(mask, bool); ign[29:34, 9:13] = True                                # no detection over the spot's peak
    sp2, _f = hot_spots(smap, mask, 2.0, 3, (100, 200), zones, ignore=ign)
    assert [s["zone"] for s in sp2] == ["Outside"], sp2
    smap[:] = 3.0
    assert hot_spots(smap, mask, 2.0, 3, (0, 0), zones)[1] > 0.9                           # everything hot: a scene change
    print("landing self-test ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _selftest()
    else:
        main()
