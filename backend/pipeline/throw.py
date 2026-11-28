"""Throw over the wall (PERIMETER_THROW_DETECTION_SPEC §2.1, §7.4): motion in the throw band → small moving spots →
tracks → path test. No GStreamer here: the pipeline feeds it every frame's band crop from a worker thread, the replay tool
(throw_replay.py) feeds it video files, the tests feed it made-up pictures — the same code everywhere.

    det = ThrowDetector(zone, cfg)            zone: the camera's zones (fractions: line, outside, band regions)
    events = det.feed(frame_no, t, gray_crop, persons)
        gray_crop  the band's bounding box (det.box, streammux pixels) of this frame, 8-bit grey
        persons    [(left, top, width, height)] person boxes of this frame in streammux pixels (their motion is ignored)
        → [event] for every track that passed the path test on this frame (ThrowCandidate = ThrowConfirmed while the
          optional classifier is off, spec §7.4.6)

1 motion      MOG2 (history 300, var 25, no shadows) on the crop, only inside the band polygon(s); the first `warmup`
              frames only teach the background
2 scene gate  more than scene_change_frac of the band moving at once (headlights, IR switching, shake) → frame skipped,
              tracks kept (a throw is not lost to one bright frame)
3 spots       median 3, open + close, connected components; area blob_area_px; spots on a person (IoA > 0.2) dropped
4 tracks      constant-velocity prediction, greedy nearest neighbour within gate_px of the prediction; a 2-point track
              may jump up to max_step_px (no velocity known yet — a throw near the camera moves fast); max_missed_frames
5 path test   when a track ends, or has min_track_points after crossing the line (an early decision = a fast alert):
              crosses the perimeter line from the outside side to the inside · median speed ≥ min_speed · x(t) linear and
              y(t) quadratic fit within max_fit_rms with gravity (y accelerates downwards) — or a straight line within
              the limit (accept_straight_line) · no reversal of direction · not on the same spot (min path length)
Every decided track is counted by its reason (state file: what the site's normal motion looks like).
"""
import math
import time
from collections import Counter, deque

import zones as Z

DEFAULTS = {"history": 300, "var_threshold": 25, "warmup": 30, "scene_change_frac": 0.30, "blob_area_px": (3, 600),
            "person_ioa": 0.2, "gate_px": 60, "max_step_px": 150, "max_missed_frames": 1, "min_track_points": 3,
            "min_fit_points": 5, "min_side_points": 2, "min_speed_px_per_frame": 8.0, "max_fit_rms_px": 5.5,
            "fit_rms_per_size": 0.35, "accept_straight_line": True, "trim_from_points": (7, 10),
            "x_degree": 2,
            "max_spots": 60, "max_tracks": 120, "min_path_px": 40, "stationary_after": 8, "crop_px": 96, "keep_frames": 16}
# fit_rms_per_size: the fit tolerance grows with the object (max_fit_rms_px or this x its typical width, √area): the
# centre of a big motion-blurred streak wobbles a few pixels, a small dot's does not.
# blob_area_px: the spec's (3, 150) is the area of a still object; a thrown one is a motion-blurred streak (a 10 px
# object moving 30 px a frame covers ~300 px): 600 (measured on the test videos: 4 of 16 found at 150, 15 at 600).
# min_side_points: seen at least this often on each side of the line (outside before, inside after the crossing).
# trim_from_points (a, b): a long path whose fit fails may set aside its worst-fitting point (a path of >= a points) or
# two (>= b points) and is fitted again with the SAME tolerance: one wrong spot picked up next to the object (tracking
# noise) or the hand still holding it at the start no longer fails a clean arc. Only after the crossing / speed / travel
# / zigzag tests passed on all points, and the rest (>= min_fit_points) must fit. Added 28 Nov 2026 after the user's
# real throw clips 2 and 3 failed "bad_fit" on exactly that (one stray point; two hand points before the release).
# x_degree: the sideways position x(t) is fitted with this degree (y(t) is always quadratic, gravity). 2 since 28 Nov
# 2026: an object flying towards / away from the camera speeds up / slows down sideways in the picture (perspective),
# and the first point is often still the accelerating hand, so a straight x(t) failed real throws (clip 3 on the
# Jetson). Direction reversals still fail (zigzag test). Measured: same throws found, 0 false alarms on the six
# site-like test videos (birds, insects, leaves, a walking person, headlights, grass in wind).
# max_fit_rms_px 5.5 (was 4.0) since 28 Nov 2026: the user's real clip 1 (thrown from right at the gate) fitted at 5.2 px
# on the Jetson's frames — its first point is the hand and the object still blurred together at the release. Margin
# measured: even 7.0 raises 0 false alarms on the six site-like test videos and no extra event on the three real clips.
# min_fit_points: a curve through 3 or 4 points always "fits" (a parabola has 3 numbers): the fit test means something
# from 5 points (the spec's 3 is the shortest track looked at; a thrown object is in view 5–15 frames).
# max_missed_frames: the spec's 1. A track that already has a speed is followed across that many frames without a
# spot (an object passing a dark pillar or a pole at the wall top), the search area growing with the gap.


def _fit(ts, vs, deg):
    """least-squares polynomial (numpy) → (coefficients highest first, RMS)"""
    import numpy as np
    t = np.asarray(ts, float)
    v = np.asarray(vs, float)
    if len(t) <= deg:
        return None, 0.0
    c = np.polyfit(t - t[0], v, deg)
    return c, float(np.sqrt(np.mean((np.polyval(c, t - t[0]) - v) ** 2)))


def _trimmed_fit(fr, xs, ys, k, keep, xdeg=1):
    """drop up to k points, one at a time, each time the one farthest from the current fit (x linear, y quadratic) →
    (frames, xs, ys) left, or None when fewer than keep would be left"""
    import numpy as np
    fr, xs, ys = list(fr), list(xs), list(ys)
    for _ in range(k):
        if len(fr) - 1 < keep:
            return None
        cx, _ = _fit(fr, xs, xdeg)
        cy, _ = _fit(fr, ys, 2)
        t = np.asarray(fr, float) - fr[0]
        r = np.hypot(np.polyval(cx, t) - np.asarray(xs), np.polyval(cy, t) - np.asarray(ys))
        j = int(np.argmax(r))
        del fr[j], xs[j], ys[j]
    return fr, xs, ys


def path_test(pts, line, outside, cfg=None, areas=None):
    """pts = [(frame, x, y)] in pixels; line / outside in the same pixels; areas = the spots' areas (optional) →
    (ok, reason, metrics). Pure geometry (no pictures): the unit tests call this with made-up tracks."""
    c = {**DEFAULTS, **(cfg or {})}
    tol = c["max_fit_rms_px"]
    if areas:
        tol = max(tol, c["fit_rms_per_size"] * math.sqrt(sorted(areas)[len(areas) // 2]))
    n = len(pts)
    m = {"points": n}
    if n < max(c["min_track_points"], c["min_fit_points"]):
        return False, "too_short", m
    zone = {"line": line, "outside": outside}
    sides = [Z.is_outside((x, y), zone) for _f, x, y in pts]
    cross = next((i for i in range(1, n) if sides[i - 1] and not sides[i]), None)
    if cross is None:
        return False, ("inside_to_outside" if any(not a and b for a, b in zip(sides, sides[1:])) else "no_crossing"), m
    m["crossing_xy"] = [round((pts[cross - 1][1] + pts[cross][1]) / 2), round((pts[cross - 1][2] + pts[cross][2]) / 2)]
    # really over the line: seen on both sides, not one spot jittering on the line (grass at the wall foot)
    if sum(sides[:cross]) < c["min_side_points"] or sum(1 for s in sides[cross:] if not s) < c["min_side_points"]:
        return False, "on_the_line", m
    steps = [math.hypot(pts[i][1] - pts[i - 1][1], pts[i][2] - pts[i - 1][2]) / max(1, pts[i][0] - pts[i - 1][0])
             for i in range(1, n)]
    speed = sorted(steps)[len(steps) // 2]
    m["speed_px_f"] = round(speed, 1)
    length = math.hypot(pts[-1][1] - pts[0][1], pts[-1][2] - pts[0][2])
    m["path_px"] = round(length)
    if length < c["min_path_px"]:
        return False, "no_travel", m
    if speed < c["min_speed_px_per_frame"]:
        return False, "slow", m
    fr = [f for f, _x, _y in pts]
    xs = [x for _f, x, _y in pts]
    ys = [y for _f, _x, y in pts]
    # no reversal: the direction of travel (x and y separately, ignoring tiny steps) changes sign at most once in y
    # (up then down: a lob) and never in x
    dx = [b - a for a, b in zip(xs, xs[1:]) if abs(b - a) > 1.5]
    dy = [b - a for a, b in zip(ys, ys[1:]) if abs(b - a) > 1.5]
    flips = lambda d: sum(1 for a, b in zip(d, d[1:]) if (a > 0) != (b > 0))
    m["x_flips"], m["y_flips"] = flips(dx), flips(dy)
    if m["x_flips"] > 0 or m["y_flips"] > 1:
        return False, "zigzag", m
    cx, rx = _fit(fr, xs, c["x_degree"])
    cy2, ry2 = _fit(fr, ys, 2)
    cy1, ry1 = _fit(fr, ys, 1)
    m["fit_rms"], m["fit_tol"] = round(max(rx, ry2), 2), round(tol, 2)
    if cy2 is not None and rx <= tol and ry2 <= tol and cy2[0] > -0.05:
        m["curve"] = "parabola" if cy2[0] > 0.05 else "line"
        return True, "throw", m
    if c["accept_straight_line"] and rx <= tol and ry1 <= tol:
        m["curve"], m["fit_rms"] = "line", round(max(rx, ry1), 2)
        return True, "throw", m
    a, b = c["trim_from_points"]
    k = 2 if n >= b else 1 if n >= a else 0
    for kk in range(1, k + 1):                                   # the fewest points set aside that make it fit
        t = _trimmed_fit(fr, xs, ys, kk, c["min_fit_points"], c["x_degree"])
        if t is None:
            break
        tcx, trx = _fit(t[0], t[1], c["x_degree"])
        tcy2, try2 = _fit(t[0], t[2], 2)
        tcy1, try1 = _fit(t[0], t[2], 1)
        if tcy2 is not None and trx <= tol and try2 <= tol and tcy2[0] > -0.05:
            m.update(curve="parabola" if tcy2[0] > 0.05 else "line", fit_rms=round(max(trx, try2), 2), trimmed=kk)
            return True, "throw", m
        if c["accept_straight_line"] and trx <= tol and try1 <= tol:
            m.update(curve="line", fit_rms=round(max(trx, try1), 2), trimmed=kk)
            return True, "throw", m
    return False, ("against_gravity" if cy2 is not None and cy2[0] <= -0.05 and ry2 <= tol else "bad_fit"), m


class ThrowDetector:
    def __init__(self, zone, cfg=None, frame=(1920, 1080), camera: str = ""):
        import cv2
        import numpy as np
        self.cv2, self.np = cv2, np
        self.c = {**DEFAULTS, **(cfg or {})}
        self.camera = camera
        W, H = frame
        bands = Z.regions(zone or {}, "band")
        line = (zone or {}).get("line") or []
        out = (zone or {}).get("outside")
        self.ok = bool(bands and len(line) >= 2 and out)
        self.line = [(p[0] * W, p[1] * H) for p in line]
        self.outside = (out[0] * W, out[1] * H) if out else None
        if not self.ok:
            self.box = None
            return
        pts = [(p[0] * W, p[1] * H) for b in bands for p in b["points"]]
        x0, y0 = max(0, int(min(p[0] for p in pts))), max(0, int(min(p[1] for p in pts)))
        x1, y1 = min(W, int(math.ceil(max(p[0] for p in pts)))), min(H, int(math.ceil(max(p[1] for p in pts))))
        self.box = (x0, y0, x1, y1)                                    # what the pipeline crops (streammux pixels)
        self.mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
        for b in bands:
            cv2.fillPoly(self.mask, [np.array([(p[0] * W - x0, p[1] * H - y0) for p in b["points"]], np.int32)], 255)
        self.mask_px = max(1, int(cv2.countNonZero(self.mask)))
        self.mog = cv2.createBackgroundSubtractorMOG2(history=self.c["history"], varThreshold=self.c["var_threshold"],
                                                      detectShadows=False)
        self.k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.tracks, self.next_id, self.n = [], 1, 0
        self.recent = deque(maxlen=self.c["keep_frames"])                   # (frame_no, crop) for the review crops
        self.stats = Counter()
        self.ms = deque(maxlen=300)
        self.debug = [] if self.c.get("debug") else None

    # ── spots ─────────────────────────────────────────────────────────────────────────────────────────────
    def _spots(self, crop, persons):
        cv2, np = self.cv2, self.np
        fg = self.mog.apply(crop)
        fg = cv2.bitwise_and(fg, self.mask)
        if self.n <= self.c["warmup"]:
            return None
        frac = cv2.countNonZero(fg) / self.mask_px
        if frac > self.c["scene_change_frac"]:
            self.stats["scene_change_frames"] += 1
            return None
        fg = cv2.medianBlur(fg, 3)
        fg = cv2.morphologyEx(cv2.morphologyEx(fg, cv2.MORPH_OPEN, self.k), cv2.MORPH_CLOSE, self.k)
        nlab, _lab, st, cent = cv2.connectedComponentsWithStats(fg)
        x0, y0 = self.box[0], self.box[1]
        lo, hi = self.c["blob_area_px"]
        spots = []
        for j in range(1, nlab):
            a = int(st[j, cv2.CC_STAT_AREA])
            if not lo <= a <= hi:
                continue
            bx, by, bw, bh = st[j, 0] + x0, st[j, 1] + y0, st[j, 2], st[j, 3]
            if persons and any(self._ioa((bx, by, bw, bh), p) > self.c["person_ioa"] for p in persons):
                self.stats["spots_on_person"] += 1
                continue
            spots.append((float(cent[j][0] + x0), float(cent[j][1] + y0), a))
        if len(spots) > self.c["max_spots"]:                         # grass in a storm: keep the smallest (a thrown
            self.stats["frames_too_many_spots"] += 1                 # object is small), never thousands of tracks
            spots = sorted(spots, key=lambda s: s[2])[: self.c["max_spots"]]
        return spots

    @staticmethod
    def _ioa(a, b):
        """intersection over a's area (a = spot box, b = person box)"""
        ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
        iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
        return ix * iy / max(1, a[2] * a[3])

    # ── tracks ────────────────────────────────────────────────────────────────────────────────────────────
    def feed(self, frame_no: int, t: float, crop, persons=None):
        if not self.ok:
            return []
        t0 = time.perf_counter()
        self.n += 1
        self.recent.append((frame_no, crop))
        spots = self._spots(crop, persons or [])
        events = []
        if spots is not None:
            events = self._track(frame_no, t, spots)
        self.ms.append((time.perf_counter() - t0) * 1000)
        return events

    def _predict(self, tr, frame_no):
        """constant velocity from the last (up to) 3 steps; the gate grows with the frames missed"""
        (f1, x1, y1), k = tr["pts"][-1], frame_no - tr["pts"][-1][0]
        if len(tr["pts"]) < 2:
            return x1, y1, self.c["max_step_px"] * k
        f0, x0, y0 = tr["pts"][-min(4, len(tr["pts"]))]
        d = max(1, f1 - f0)
        return x1 + (x1 - x0) / d * k, y1 + (y1 - y0) / d * k, self.c["gate_px"] * (1 + 0.5 * (k - 1))

    def _track(self, frame_no, t, spots):
        pairs = []
        for i, tr in enumerate(self.tracks):
            px, py, gate = self._predict(tr, frame_no)
            for j, (x, y, _a) in enumerate(spots):
                d = math.hypot(x - px, y - py)
                if d <= gate:
                    pairs.append((d, i, j))
        used_t, used_s = set(), set()
        for d, i, j in sorted(pairs):                                 # greedy nearest neighbour
            if i in used_t or j in used_s:
                continue
            used_t.add(i); used_s.add(j)
            x, y, a = spots[j]
            self.tracks[i]["pts"].append((frame_no, x, y)); self.tracks[i]["area"].append(a)
            self.tracks[i]["t"].append(t); self.tracks[i]["missed"] = 0
        events, keep = [], []
        for i, tr in enumerate(self.tracks):
            if i not in used_t:
                tr["missed"] += 1
            # a track with one point has no speed: no gap allowed; stationary flicker (grass) is let go early
            ended = tr["missed"] > (self.c["max_missed_frames"] if len(tr["pts"]) >= 2 else 0)
            if not ended and not tr["decided"] and len(tr["pts"]) >= self.c["stationary_after"]:
                (_a, xa, ya), (_b, xb, yb) = tr["pts"][0], tr["pts"][-1]
                if math.hypot(xb - xa, yb - ya) < self.c["min_path_px"]:
                    tr["decided"] = True
                    self.stats["stationary"] += 1
                    ended = True
            if not tr["decided"] and (ended or self._crossed_enough(tr)):
                ev = self._decide(tr)
                if ev:
                    events.append(ev)
            if not ended:
                keep.append(tr)
        for j, (x, y, a) in enumerate(spots):                          # unmatched spots start tracks
            if j not in used_s and len(keep) < self.c["max_tracks"]:
                keep.append({"id": self.next_id, "pts": [(frame_no, x, y)], "area": [a], "t": [t], "missed": 0, "decided": False})
                self.next_id += 1
        self.tracks = keep
        return events

    def _crossed_enough(self, tr):
        """early decision: crossed already and min_track_points since the crossing"""
        if len(tr["pts"]) < 2 * self.c["min_track_points"] - 1:
            return False
        zone = {"line": self.line, "outside": self.outside}
        sides = [Z.is_outside((x, y), zone) for _f, x, y in tr["pts"]]
        cross = next((i for i in range(1, len(sides)) if sides[i - 1] and not sides[i]), None)
        return cross is not None and len(sides) - cross >= self.c["min_track_points"]

    def _decide(self, tr):
        tr["decided"] = True
        if len(tr["pts"]) < 2:
            self.stats["single_spots"] += 1
            return None
        ok, reason, m = path_test(tr["pts"], self.line, self.outside, self.c, tr["area"])
        self.stats[reason] += 1
        if self.debug is not None and len(tr["pts"]) >= 3:            # replay --debug: every decided track, and why
            self.debug.append({"reason": reason, **m, "track": [[f, round(x), round(y)] for f, x, y in tr["pts"]],
                               "area": tr["area"]})
        if not ok:
            return None
        return {"type": "throw", "camera": self.camera, "track_id": tr["id"], "frame": tr["pts"][-1][0],
                "t_start": tr["t"][0], "t_end": tr["t"][-1],
                "track": [[f, round(x, 1), round(y, 1)] for f, x, y in tr["pts"]],
                "area_px": sorted(tr["area"])[len(tr["area"]) // 2], **m,
                "direction": [round(tr["pts"][-1][1] - tr["pts"][0][1]), round(tr["pts"][-1][2] - tr["pts"][0][2])]}

    # ── review picture: 3 crops around the track (t-1, t, t+1 at the crossing) + the band with the track ─────────
    def review_image(self, ev):
        cv2, np = self.cv2, self.np
        frames = dict(self.recent)
        cx, cy = ev["crossing_xy"]
        s = self.c["crop_px"]
        x0, y0 = self.box[0], self.box[1]
        fno = next((f for f, x, y in ev["track"] if not Z.is_outside((x, y), {"line": self.line, "outside": self.outside})),
                   ev["track"][-1][0])
        tiles = []
        for f in (fno - 1, fno, fno + 1):
            g = frames.get(f)
            tile = np.zeros((s, s), np.uint8)
            if g is not None:
                lx, ly = int(cx - x0 - s // 2), int(cy - y0 - s // 2)
                lx, ly = max(0, min(lx, g.shape[1] - s)), max(0, min(ly, g.shape[0] - s))
                part = g[ly:ly + s, lx:lx + s]
                tile[:part.shape[0], :part.shape[1]] = part
            tiles.append(cv2.resize(tile, (s * 2, s * 2), interpolation=cv2.INTER_NEAREST))
        top = np.hstack(tiles)
        last = frames.get(ev["track"][-1][0])
        band = cv2.cvtColor(last if last is not None else np.zeros((self.box[3] - y0, self.box[2] - x0), np.uint8), cv2.COLOR_GRAY2BGR)
        pts = np.array([(int(x - x0), int(y - y0)) for _f, x, y in ev["track"]], np.int32)
        cv2.polylines(band, [pts], False, (0, 0, 255), 2, cv2.LINE_AA)
        for p in pts:
            cv2.circle(band, tuple(int(v) for v in p), 3, (0, 255, 255), -1)
        ln = np.array([(int(x - x0), int(y - y0)) for x, y in self.line], np.int32)
        cv2.polylines(band, [ln], False, (0, 200, 255), 1, cv2.LINE_AA)
        w = top.shape[1]
        band = cv2.resize(band, (w, max(1, int(band.shape[0] * w / band.shape[1]))))
        return np.vstack([cv2.cvtColor(top, cv2.COLOR_GRAY2BGR), band])

    def summary(self):
        ms = sorted(self.ms)
        return {"frames": self.n, "ms_median": round(ms[len(ms) // 2], 2) if ms else None,
                "ms_p95": round(ms[int(len(ms) * 0.95)], 2) if ms else None, "tracks_open": len(self.tracks),
                **{k: v for k, v in self.stats.items()}}


if __name__ == "__main__":                                              # python3 throw.py: self-test
    import random
    line, out = [(0, 500), (1920, 500)], (960, 200)                    # outside = above the line (the far side)
    arc = lambda: [(f, 600 + 18 * f, 420 - 20 * f + 2 * f * f) for f in range(0, 18)]   # up, over the top, down: a lob
    ok, reason, m = path_test(arc(), line, out)
    assert ok and m["curve"] == "parabola" and m["crossing_xy"][1] > 450, (reason, m)
    ok, reason, m = path_test([(f, 300 + 25 * f, 420 + 12 * f) for f in range(10)], line, out)       # straight throw
    assert ok and m["curve"] == "line", (reason, m)
    zig = [(f, 900 + (12 if f % 2 else -12) + 3 * f, 430 + 15 * f) for f in range(10)]           # insect / bird
    assert path_test(zig, line, out)[1] == "zigzag"
    assert path_test([(f, 900 + 2 * f, 470 + 2 * f) for f in range(20)], line, out)[1] == "slow"  # drifting leaf
    assert path_test([(f, 700 + 20 * f, 380 + 0 * f) for f in range(8)], line, out)[1] == "no_crossing"   # along the top
    assert path_test([(f, 600 + 18 * f, 600 - 20 * f) for f in range(10)], line, out)[1] == "inside_to_outside"
    up = [(f, 600 + 15 * f, 420 + 30 * f - 2.5 * f * f) for f in range(12)]                      # curving UP: no gravity
    assert not path_test(up, line, out)[0], path_test(up, line, out)
    random.seed(3)
    noisy = [(f, 600 + 18 * f + random.uniform(-1, 1), 420 - 20 * f + 2 * f * f + random.uniform(-1, 1)) for f in range(18)]
    assert path_test(noisy, line, out)[0], path_test(noisy, line, out)                          # real jitter: still a throw
    assert path_test([(0, 900, 400), (1, 910, 520)], line, out)[1] == "too_short"
    # the detector on pictures: a still band with noise, a 7 px dark object thrown across → one event; noise alone → none
    import numpy as np
    import cv2
    zone = {"line": [[0, 0.5], [1, 0.5]], "outside": [0.5, 0.1],
            "regions": [{"name": "Band 1", "kind": "band", "points": [[0, 0.3], [1, 0.3], [1, 0.7], [0, 0.7]]}]}
    rng = np.random.default_rng(1)
    for case in ("throw", "noise", "flash"):
        det = ThrowDetector(zone, {"warmup": 20}, camera="test")
        x0, y0, x1, y1 = det.box
        base = rng.integers(90, 130, (y1 - y0, x1 - x0)).astype(np.uint8)
        evs = []
        for f in range(80):
            img = cv2.add(base, rng.integers(0, 4, base.shape, dtype=np.uint8))
            if case == "throw" and 40 <= f < 59:                          # rises to y≈370, falls over the line (540)
                k = f - 40
                x, y = 500 + 22 * k, 420 + (-20 * k + 2.0 * k * k)
                cv2.circle(img, (int(x - x0), int(y - y0)), 4, 20, -1)
            if case == "flash" and f == 50:
                img = cv2.add(img, 120)                                    # headlights: the whole band at once
            evs += det.feed(f, f / 30, img, [])
        if case == "throw":
            assert len(evs) == 1 and evs[0]["curve"] == "parabola", (evs, det.summary())
            rv = det.review_image(evs[0])
            assert rv.shape[1] == 6 * det.c["crop_px"], rv.shape
        else:
            assert not evs, (case, evs)
        if case == "flash":
            assert det.stats["scene_change_frames"] >= 1, det.summary()
    # a person walking through the band: its motion is ignored (spots on a person box)
    det = ThrowDetector(zone, {"warmup": 20}, camera="test")
    x0, y0, x1, y1 = det.box
    evs = []
    for f in range(80):
        img = base.copy()
        px = 300 + 12 * f
        box = (px, 420, 40, 120)
        if f > 30:
            cv2.rectangle(img, (px - x0 + 15, 420 - y0), (px - x0 + 22, 420 - y0 + 7), 10, -1)   # a hand-sized spot on him
        evs += det.feed(f, f / 30, img, [box] if f > 30 else [])
    assert not evs and det.stats["spots_on_person"] > 0, det.summary()
    print("throw self-test ok", det.summary())
