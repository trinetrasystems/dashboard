#!/usr/bin/env python3
"""Replay a video through the throw detection — the same ThrowDetector the live pipeline runs (spec §9 replay.py).
    python3 throw_replay.py VIDEO --zone ZONE.json [--camera NAME] [--stabilize] [--out DIR] [--set key=value ...]
ZONE.json: one camera's zone (fractions: line, outside, regions with a "band"), or the dashboard's ds/zones.json with
--camera. Every frame is scaled to 1920x1080 first (as nvstreammux), so pixel thresholds mean what they mean live.
--stabilize: for a HANDHELD clip only — each frame is moved onto the first one (feature tracking + a similarity
transform) so the background stands still; a site camera is fixed and never needs it.
Writes DIR/events.jsonl (one line per throw), DIR/throw_<n>.jpg (the review picture) and prints a JSON summary:
{"video", "frames", "fps", "events": [...], "stats": {reason: count, ms per frame}}.
"""
import argparse
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from throw import ThrowDetector  # noqa: E402

W, H = 1920, 1080


def _value(v: str):
    try:
        return json.loads(v)
    except ValueError:
        return v


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--zone", required=True)
    ap.add_argument("--camera")
    ap.add_argument("--stabilize", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args(argv)
    z = json.load(open(a.zone))
    if a.camera:
        z = z[a.camera]
    cfg = {k: _value(v) for k, v in (s.split("=", 1) for s in a.set)}
    det = ThrowDetector(z, cfg, (W, H), camera=a.camera or os.path.basename(a.video))
    if not det.ok:
        sys.exit("the zone needs the perimeter line, the outside point and a throw band")
    cap = cv2.VideoCapture(a.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if a.out:
        os.makedirs(a.out, exist_ok=True)
    x0, y0, x1, y1 = det.box
    ref = None                                           # stabilise: the first frame's grey (small), the running transform
    acc = np.eye(3)
    prev = None
    events, n = [], 0
    while True:
        ok, f = cap.read()
        if not ok:
            break
        f = cv2.resize(f, (W, H), interpolation=cv2.INTER_LINEAR)
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        if a.stabilize:
            small = cv2.resize(g, (W // 2, H // 2))
            if prev is not None:
                p = cv2.goodFeaturesToTrack(prev, 400, 0.01, 10)
                if p is not None:
                    q, st, _ = cv2.calcOpticalFlowPyrLK(prev, small, p, None)
                    good = st.ravel() == 1
                    if good.sum() >= 10:
                        m, _ = cv2.estimateAffinePartial2D(q[good], p[good], method=cv2.RANSAC, ransacReprojThreshold=2.0)
                        if m is not None:
                            m[:, 2] *= 2                 # half size → full size
                            acc = acc @ np.vstack([m, [0, 0, 1]])    # this frame → the previous → … → the first
            prev = small
            g = cv2.warpAffine(g, acc[:2], (W, H), borderMode=cv2.BORDER_REPLICATE)
        evs = det.feed(n, n / fps, g[y0:y1, x0:x1].copy(), [])
        for ev in evs:
            ev["video"] = os.path.basename(a.video)
            ev["t_start_s"], ev["t_end_s"] = round(ev["t_start"], 2), round(ev["t_end"], 2)
            events.append(ev)
            if a.out:
                cv2.imwrite(os.path.join(a.out, f"throw_{len(events)}.jpg"), det.review_image(ev))
        n += 1
    if a.out and det.debug is not None:
        with open(os.path.join(a.out, "tracks.jsonl"), "w") as fo:
            for d in det.debug:
                fo.write(json.dumps(d) + "\n")
    if a.out:
        with open(os.path.join(a.out, "events.jsonl"), "w") as fo:
            for ev in events:
                fo.write(json.dumps(ev) + "\n")
    out = {"video": os.path.basename(a.video), "frames": n, "fps": round(fps, 2), "box": det.box,
           "events": [{k: ev[k] for k in ("t_start_s", "t_end_s", "crossing_xy", "speed_px_f", "fit_rms", "curve", "points",
                                         "area_px", "direction")} for ev in events], "stats": det.summary()}
    print(json.dumps(out))
    return out


if __name__ == "__main__":
    main()
