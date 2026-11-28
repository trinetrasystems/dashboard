"""Day / night per camera and the normal-scene frame sampler — the perimeter pipeline's helpers for the throw and landing
detection (PERIMETER_THROW_DETECTION_SPEC §7.3 mode detector, §8.1 normal-scene images for the PatchCore banks).
No GStreamer here: the pipeline calls these with numbers and numpy pictures, the tests with made-up ones.

ModeDetector  night when the mean colour saturation (HSV S, 0–255, of a small copy of the frame) stays under
              `night_max` for `hysteresis_s` seconds, day when it stays above for as long (an IR picture is grey: S ≈ 0;
              a colour day picture: S ≈ 40–120). The first reading sets the mode at once. A switch is reported (the
              landing detection pauses after one, spec pause_after_switch_s).

Sampler       one whole, clean frame per camera every `every_s` seconds while a "normal frames" recording runs
              (captures.py writes ds/run/sampling.json): full streammux size (1920x1080 — the zones' and the banks'
              coordinates), no drawings, JPEG quality 95, named <camera>_<YYYYmmdd-HHMMSS>_<day|night>.jpg.
              A frame with anything detected in a landing zone (no landing zone drawn: anywhere in the picture) is not
              normal: it waits — tried again every `retry_s` — up to half the interval, then that turn is skipped.
              Progress per camera in <capture>/sample.state.json (kept, skipped, day, night, last).
"""
import json
import os
import time
from datetime import datetime

import zones as Z


def saturation(bgr_small) -> float:
    import cv2
    return float(cv2.cvtColor(bgr_small, cv2.COLOR_BGR2HSV)[:, :, 1].mean())


class ModeDetector:
    def __init__(self, night_max: float = 12, hysteresis_s: float = 30):
        self.night_max, self.hyst = float(night_max), float(hysteresis_s)
        self.mode, self.since, self.sat, self.pending = None, None, None, None   # pending = (mode, first time seen)
        self.switches = 0                          # real switches this run (not the first reading)

    def update(self, sat: float, now: float):
        """→ the new mode when it switched (also the first reading), else None"""
        self.sat = round(sat, 1)
        want = "night" if sat < self.night_max else "day"
        if self.mode is None:
            self.mode, self.since = want, now
            return want
        if want == self.mode:
            self.pending = None
            return None
        if self.pending is None or self.pending[0] != want:
            self.pending = (want, now)
            return None
        if now - self.pending[1] >= self.hyst:
            self.mode, self.since, self.pending = want, now, None
            self.switches += 1
            return want
        return None


def busy(objs_boxes, zone, w: float, h: float) -> bool:
    """anything detected in a landing zone (corners, centre or foot point of its box inside), or — no landing zone —
    anything at all. objs_boxes: [(left, top, width, height) in pixels of a w x h frame]"""
    lands = Z.regions(zone or {}, "landing")
    objs_boxes = [b for b in objs_boxes if not Z.ignored(((b[0] + b[2] / 2) / w, (b[1] + b[3]) / h), zone)]  # no-detection
    if not lands:
        return bool(objs_boxes)
    for (l, t, bw, bh) in objs_boxes:
        pts = [(l / w, t / h), ((l + bw) / w, t / h), (l / w, (t + bh) / h), ((l + bw) / w, (t + bh) / h),
               ((l + bw / 2) / w, (t + bh / 2) / h), ((l + bw / 2) / w, (t + bh) / h)]
        if any(Z.in_polygon(p, r["points"]) for r in lands for p in pts):
            return True
    return False


class Parked:
    """things (not people) standing still in the same place for after_s seconds — a parked bike or car is part of the
    normal scene, so it does not keep the normal-frames sampler from ever saving a frame (28 Nov 2026: bikes parked at
    a landing zone's edge made the sampler skip 2 of 3 turns). People always count, standing or not.
    update() with every frame looked at (a few a second is enough); parked(box) after that."""

    def __init__(self, after_s: float = 120, iou: float = 0.7, forget_s: float = 30):
        self.after_s, self.iou, self.forget_s = after_s, iou, forget_s
        self.items = {}                                        # camera → [[box, first seen, last seen]]

    @staticmethod
    def _iou(a, b):
        ix = max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
        i = ix * iy
        return i / max(1e-9, a[2] * a[3] + b[2] * b[3] - i)

    def update(self, cam, boxes, now):
        its = self.items.setdefault(cam, [])
        for b in boxes:
            m = max(its, key=lambda it: self._iou(it[0], b), default=None)
            if m is not None and self._iou(m[0], b) >= self.iou:
                m[2] = now
            else:
                its.append([tuple(b), now, now])
        self.items[cam] = [it for it in its if now - it[2] <= self.forget_s][-200:]

    def parked(self, cam, box, now) -> bool:
        return any(self._iou(it[0], box) >= self.iou and now - it[1] >= self.after_s for it in self.items.get(cam, []))


class Sampler:
    def __init__(self, path: str, owner: int = 1000, retry_s: float = 5.0, log=print):
        self.path, self.owner, self.retry_s, self.log = path, owner, retry_s, log
        self.mtime, self.jobs = None, []           # [{"id", "dir", "cameras", "every_s", "until"}]
        self.next = {}                             # (capture, camera) → monotonic time due
        self.slot = {}                             # (capture, camera) → monotonic time the current turn began
        self.state = {}                            # capture → {camera: {...}}

    def reload(self):
        try:
            m = os.stat(self.path).st_mtime
        except OSError:
            if self.jobs:
                self.log("normal frames: no recording running any more")
            self.jobs, self.mtime = [], None
            return
        if m == self.mtime:
            return
        try:
            d = json.load(open(self.path))
            jobs = [j for j in d.get("captures") or [] if j.get("dir") and j.get("cameras") and float(j.get("every_s", 0)) >= 10]
        except (OSError, ValueError, TypeError) as e:
            self.log("normal frames: sampling file not read:", repr(e))
            return
        self.mtime, self.jobs = m, jobs
        for j in jobs:
            st = self.state.setdefault(j["id"], self._read_state(j))
            for c in j["cameras"]:
                st.setdefault(c, {"kept": 0, "skipped": 0, "day": 0, "night": 0, "last": None})
        self.log("normal frames:", "; ".join(f"{j['id']} {', '.join(j['cameras'])} every {float(j['every_s']):g} s" for j in jobs) or "none")

    @staticmethod
    def _read_state(j) -> dict:
        try:
            return json.load(open(os.path.join(j["dir"], "sample.state.json"))).get("cameras", {})
        except (OSError, ValueError, AttributeError):
            return {}

    def due(self, cam: str, now: float, wall: datetime):
        """→ the jobs that want a frame of this camera now"""
        out = []
        for j in self.jobs:
            if cam not in j["cameras"]:
                continue
            try:
                if j.get("until") and wall >= datetime.fromisoformat(j["until"]):
                    continue
            except ValueError:
                continue
            k = (j["id"], cam)
            if now >= self.next.get(k, 0):
                self.slot.setdefault(k, now)
                out.append(j)
        return out

    def busy_now(self, j, cam: str, now: float):
        """this turn's frame was not normal: try again shortly, or give the turn up after half the interval"""
        k, every = (j["id"], cam), float(j["every_s"])
        if now - self.slot.get(k, now) >= every / 2:
            self.state[j["id"]][cam]["skipped"] += 1
            self.slot.pop(k, None)
            self.next[k] = now + every / 2           # the next turn: one interval after this one began
            self._save(j)
        else:
            self.next[k] = now + self.retry_s

    def save(self, j, cam: str, bgr_full, mode: str, sat, wall: datetime, now: float) -> str:
        import cv2
        d = os.path.join(j["dir"], "frames", cam)
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            for p in (os.path.dirname(d), d):
                self._own(p)
        name = f"{cam}_{wall.strftime('%Y%m%d-%H%M%S')}_{mode or 'day'}.jpg"
        ok, jpg = cv2.imencode(".jpg", bgr_full, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise OSError("JPEG not encoded")
        tmp = os.path.join(d, f".{name}.part")
        with open(tmp, "wb") as f:
            f.write(jpg.tobytes())
        os.replace(tmp, os.path.join(d, name))
        self._own(os.path.join(d, name))
        st = self.state[j["id"]][cam]
        st["kept"] += 1
        st[mode if mode in ("day", "night") else "day"] += 1
        st["last"] = wall.isoformat(timespec="seconds")
        st["saturation"] = sat
        k = (j["id"], cam)
        began = self.slot.pop(k, now)
        self.next[k] = began + float(j["every_s"])
        self._save(j)
        return name

    def _own(self, p):
        try:
            os.chown(p, self.owner, self.owner)
        except OSError:
            pass

    def _save(self, j):
        p = os.path.join(j["dir"], "sample.state.json")
        try:
            with open(p + ".tmp", "w") as f:
                json.dump({"updated": datetime.now().isoformat(timespec="seconds"), "cameras": self.state[j["id"]]}, f)
            os.replace(p + ".tmp", p)
            self._own(p)
        except OSError as e:
            self.log("normal frames: progress not written:", repr(e))


if __name__ == "__main__":                            # python3 scene.py: self-test
    import tempfile
    import numpy as np
    m = ModeDetector(12, 30)
    assert m.update(60, 0) == "day"
    assert m.update(3, 10) is None and m.update(3, 39) is None      # 29 s of grey: not yet
    assert m.update(70, 41) is None and m.pending is None            # back to colour: the count starts again
    assert m.update(2, 50) is None and m.update(2, 80) == "night"
    assert m.update(50, 90) is None and m.update(50, 121) == "day"
    z = {"line": [], "outside": None, "regions": [{"name": "Landing 1", "kind": "landing",
                                                   "points": [[0.1, 0.6], [0.9, 0.6], [0.9, 0.95], [0.1, 0.95]]}]}
    assert not busy([(100, 100, 50, 100)], z, 1000, 1000)                # a person above the landing zone
    assert busy([(500, 550, 50, 100)], z, 1000, 1000)                    # feet in it
    assert busy([(1, 1, 5, 5)], {}, 1000, 1000) and not busy([], {}, 1000, 1000)
    pk = Parked(after_s=120)
    bike = (500, 550, 80, 60)
    for t in range(0, 125, 2):
        pk.update("c", [(bike[0] + t % 3, bike[1], bike[2], bike[3])], float(t))       # still (a pixel of jitter)
    assert pk.parked("c", bike, 124.0) and not pk.parked("c", (100, 100, 50, 50), 124.0)
    pk2 = Parked(after_s=120)
    for t in range(0, 125, 2):
        pk2.update("c", [(100 + 8 * t, 550, 80, 60)], float(t))                        # driving through: never parked
    assert not pk2.parked("c", (100 + 8 * 124, 550, 80, 60), 124.0)
    pk.update("c", [], 200.0)                                                           # gone for > 30 s: forgotten
    assert not pk.parked("c", bike, 200.0)
    tmp = tempfile.mkdtemp()
    cap = os.path.join(tmp, "20261127-100000")
    os.makedirs(cap)
    sf = os.path.join(tmp, "sampling.json")
    json.dump({"captures": [{"id": "20261127-100000", "dir": cap, "cameras": ["north-wall"], "every_s": 60,
                             "until": "2099-01-01T00:00:00"}]}, open(sf, "w"))
    s = Sampler(sf, owner=os.getuid(), log=lambda *a: None)
    s.reload()
    wall, img = datetime(2026, 11, 27, 10, 0, 0), np.zeros((1080, 1920, 3), np.uint8)
    j = s.due("north-wall", 0, wall)[0]
    s.busy_now(j, "north-wall", 0)                                       # someone in the landing zone
    assert not s.due("north-wall", 3, wall) and s.due("north-wall", 5, wall)
    name = s.save(j, "north-wall", img, "night", 2.0, wall, 6)
    assert name == "north-wall_20261127-100000_night.jpg" and os.path.isfile(os.path.join(cap, "frames", "north-wall", name))
    assert not s.due("north-wall", 59, wall) and s.due("north-wall", 60, wall)   # one interval after the turn began
    for t in (60, 65, 70, 89):
        s.busy_now(j, "north-wall", t)
    s.busy_now(j, "north-wall", 90)                                      # busy for half the interval: turn skipped
    st = json.load(open(os.path.join(cap, "sample.state.json")))["cameras"]["north-wall"]
    assert (st["kept"], st["skipped"], st["night"]) == (1, 1, 1), st
    assert not s.due("north-wall", 119, wall) and s.due("north-wall", 120, wall)
    assert not s.due("north-wall", 200, datetime(2100, 1, 1))           # over
    print("scene self-test ok")
