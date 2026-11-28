"""PatchCore memory of the normal scene for the landing / left-object detection (PERIMETER_THROW_DETECTION_SPEC §2.3–2.5,
§7.5, §8.1) — step E, 28 Nov 2026. Builds, stores, loads and scores against the banks; the landing worker (landing.py)
and the dashboard's "Build landing memory" job (ds_control.py) use it. patchcore.py holds the backbone and the search.

Layout      every landing zone of a camera (up to 4, on either side of the perimeter line) is cut into the FIXED tile grid
            of patchcore.tile_grid (288 px, 32 px overlap, 1920x1080 streammux pixels); a tile shared by two zones is
            one tile. Each zone knows its side ("inside" / "outside", zones.region_side): a throw "in" lands in an
            inside zone, "out" in an outside zone. No-detection (ignore) regions are cut out of the zones' map.
Zones edited (28 Nov 2026, the user's wish): a bank stores its tiles; a zone whose tiles are all in it keeps working
            with no new build (covered()) — a zone REMOVED simply stops being looked at, the others carry on; a zone
            added or moved is "not in the memory yet" until the next build (ds_control builds it by itself a minute
            after the zones are saved, from the camera's Normal frames — whole frames, so they fit any new zone).
Banks       per camera and day / night: banks/<camera>/<mode>/bank.pt = FP16 [tiles, N, 1536] (every tile the SAME N,
            the batched search wants it) + meta.json (layout signature, images, thresholds, the check on held-out
            normal frames and on made-up objects). Built from the camera's Normal frames (Recordings): at most
            max_images per mode, evenly spread in time; holdout_frac of them kept aside for the thresholds.
Coreset     greedy k-centre (PatchCore) on a random projection of the patches to proj_dim numbers, coreset_ratio of
            them kept (2 %: the user's choice of 27 Nov — 10 % took ~1.2 s per check on the Orin Nano). Two passes over
            the pictures per group of tiles (pass 1 projections → coreset, pass 2 the chosen patches' full features):
            the GPU never holds all patches at once (a tile of 300 pictures = 388 800 patches, 1.2 GB in FP16).
Score map   per tile the distance of each 8x8 px patch to its nearest bank patch → at 1/4 resolution (MAP_SCALE) →
            Gaussian blur (σ = 4 px of the frame) → the tiles stitched (max where they overlap) → only inside the zones.
            Level of a picture = the k-th highest value inside the zones (k = min_hot_cells: a spot must have an area,
            one hot cell is noise).
Thresholds  map_threshold = the highest level of a held-out normal frame x the LARGEST margin (of margins, at least
            margin) that still finds min_recall_pct of made-up objects pasted into the held-out frames (12–48 px,
            dark / bright / coloured) — no held-out normal frame alarms, and as far above the normal scene as the
            objects allow (fewer false alarms from wind, shadows, light). The trade-off table (margin → normal frames
            over it, objects found) is in meta.json. Staged objects at the site (step G) check it for real.
Feedback    "False alarm" on a landing alert → that spot's tiles' patches are added to the bank of that mode at once
            (banks/<camera>/<mode>/extra/<alert id>.pt, patches already in the bank skipped); undo deletes the file.

    python3 bank.py build --camera C --zones ZONES.json --captures DIR [--out DIR] [--frames DIR] [--max-images N]
    python3 bank.py status --camera C --zones ZONES.json --captures DIR [--out DIR]
    python3 bank.py selftest          (layout, coreset, map; with a GPU: a real build + score on made-up pictures)
"""
import argparse
import glob
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import patchcore as P
import zones as Z

FRAME = (1920, 1080)
MAP_SCALE = 4                       # the score map's pixel = 4x4 frame pixels (a patch = 8x8)
PATCHES = (P.TILE_PX // 8) ** 2     # 36 x 36 per tile
DEFAULTS = {"coreset_ratio": 0.02, "proj_dim": 64, "max_images": 300, "min_images": 20, "holdout_frac": 0.15,
            "holdout_min": 5, "holdout_max": 60, "margin": 1.10, "margins": (1.1, 1.2, 1.3, 1.5, 1.75, 2.0),
            "min_recall_pct": 95.0, "min_hot_cells": 3, "blur_sigma_px": 4.0,
            "group_budget_mb": 256, "images_per_batch": 4, "synthetic_per_image": 3, "seed": 7}
FRAME_RE = re.compile(r"^(?P<cam>[A-Za-z0-9-]+)_(?P<ts>\d{8}-\d{6})_(?P<mode>day|night)\.jpg$")
VERSION = 1


def _log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ── layout ──────────────────────────────────────────────────────────────────────────────────────────────────────
def landing_layout(zone, frame=FRAME, tile_px=P.TILE_PX, overlap_px=P.OVERLAP_PX) -> dict:
    """the camera's landing zones → {"tiles": [(x, y, s)], "zones": [{"name", "side", "points", "tiles": [i]}],
    "crop": (x0, y0, x1, y1) the part of the frame the tiles cover (x0, y0 multiples of MAP_SCALE), "sig"}"""
    tiles, zs = [], []
    for r in Z.regions(zone or {}, "landing"):
        ids = []
        for t in P.tile_grid(P.zone_box(r["points"], frame), frame, tile_px, overlap_px):
            if t not in tiles:
                tiles.append(t)
            ids.append(tiles.index(t))
        zs.append({"name": r["name"], "side": Z.region_side(r, zone), "points": r["points"], "tiles": ids})
    ignore = [r["points"] for r in Z.regions(zone or {}, "ignore")]
    if not tiles:
        return {"tiles": [], "zones": zs, "crop": None, "sig": None, "ignore": ignore}
    x0 = min(x for x, _y, _s in tiles) // MAP_SCALE * MAP_SCALE
    y0 = min(y for _x, y, _s in tiles) // MAP_SCALE * MAP_SCALE
    x1 = max(x + s for x, _y, s in tiles)
    y1 = max(y + s for _x, y, s in tiles)
    sig = hashlib.sha1(json.dumps({"v": VERSION, "tiles": tiles, "frame": list(frame), "weights": P.WEIGHTS}).encode()).hexdigest()[:16]
    return {"tiles": [tuple(t) for t in tiles], "zones": zs, "crop": (x0, y0, x1, y1), "sig": sig, "ignore": ignore}


def sub_layout(layout, keep):
    """the layout of only the zones `keep` (indices): their tiles (re-numbered), crop, the same no-detection regions"""
    tiles, zs = [], []
    for k in keep:
        z = layout["zones"][k]
        ids = []
        for i in z["tiles"]:
            t = tuple(layout["tiles"][i])
            if t not in tiles:
                tiles.append(t)
            ids.append(tiles.index(t))
        zs.append({**z, "tiles": ids})
    if not tiles:
        return {"tiles": [], "zones": zs, "crop": None, "sig": None, "ignore": layout.get("ignore", [])}
    x0 = min(x for x, _y, _s in tiles) // MAP_SCALE * MAP_SCALE
    y0 = min(y for _x, y, _s in tiles) // MAP_SCALE * MAP_SCALE
    return {"tiles": tiles, "zones": zs, "crop": (x0, y0, max(x + s for x, _y, s in tiles), max(y + s for _x, y, s in tiles)),
            "sig": layout["sig"], "ignore": layout.get("ignore", [])}


def covered(layout, bank_tiles):
    """which zones the bank covers (every tile of theirs in it) → (zone indices covered, names not covered)"""
    have = {tuple(t) for t in bank_tiles or []}
    ok = [k for k, z in enumerate(layout["zones"]) if all(tuple(layout["tiles"][i]) in have for i in z["tiles"])]
    return ok, [z["name"] for k, z in enumerate(layout["zones"]) if k not in ok]


def zone_masks(layout, cut_ignore=True):
    """→ uint8 [h, w] at 1/MAP_SCALE over the layout's crop: 0 = outside every landing zone (or in a no-detection
    region), k + 1 = in zone k"""
    import cv2
    import numpy as np
    x0, y0, x1, y1 = layout["crop"]
    h, w = math.ceil((y1 - y0) / MAP_SCALE), math.ceil((x1 - x0) / MAP_SCALE)
    m = np.zeros((h, w), np.uint8)
    for k, z in enumerate(layout["zones"]):
        pts = np.array([((p[0] * FRAME[0] - x0) / MAP_SCALE, (p[1] * FRAME[1] - y0) / MAP_SCALE) for p in z["points"]], np.float32)
        cv2.fillPoly(m, [np.round(pts).astype(np.int32)], k + 1)
    for ig in (layout.get("ignore") or []) if cut_ignore else []:   # no-detection regions: never looked at
        pts = np.array([((p[0] * FRAME[0] - x0) / MAP_SCALE, (p[1] * FRAME[1] - y0) / MAP_SCALE) for p in ig], np.float32)
        cv2.fillPoly(m, [np.round(pts).astype(np.int32)], 0)
    return m


def tile_crops(img, layout, ids=None, origin=(0, 0)):
    """BGR tiles of a picture (the whole frame, or the layout's crop with origin = its top-left corner)"""
    ox, oy = origin
    return [img[y - oy:y - oy + s, x - ox:x - ox + s] for i, (x, y, s) in enumerate(layout["tiles"]) if ids is None or i in ids]


# ── normal frames ───────────────────────────────────────────────────────────────────────────────────────────────
def normal_frames(captures_root, cam, extra_dirs=()) -> list:
    """every Normal frame of this camera (Recordings, any normal-frames recording; + extra_dirs) →
    [{"path", "mode", "ts"}] in time order"""
    dirs = []
    for cj in glob.glob(os.path.join(captures_root or "/nonexistent", "*", "capture.json")):
        try:
            if json.load(open(cj)).get("kind") == "normal":
                dirs.append(os.path.join(os.path.dirname(cj), "frames", cam))
        except (OSError, ValueError, AttributeError):
            pass
    out = []
    for d in list(dirs) + list(extra_dirs or ()):
        for f in glob.glob(os.path.join(d, "*.jpg")):
            m = FRAME_RE.match(os.path.basename(f))
            if m and m["cam"] == cam:
                out.append({"path": f, "mode": m["mode"], "ts": m["ts"]})
    return sorted(out, key=lambda x: (x["ts"], x["path"]))


def split_frames(frames, c) -> tuple:
    """evenly over time: holdout (every k-th) and the bank's pictures (at most max_images)"""
    n = len(frames)
    nh = max(c["holdout_min"], min(c["holdout_max"], int(round(n * c["holdout_frac"])))) if n >= 2 * c["holdout_min"] else 0
    hold_idx = set(int(round((i + 0.5) * n / nh)) for i in range(nh)) if nh else set()
    hold = [f for i, f in enumerate(frames) if i in hold_idx]
    train = [f for i, f in enumerate(frames) if i not in hold_idx]
    if len(train) > c["max_images"]:
        step = len(train) / c["max_images"]
        train = [train[int(i * step)] for i in range(c["max_images"])]
    return train, hold


# ── coreset ─────────────────────────────────────────────────────────────────────────────────────────────────────
def coreset_idx(z, k: int):
    """greedy k-centre on z [M, d] (any device; FP16 fine) → LongTensor [k] of the chosen rows. No host sync per step."""
    torch = sys.modules["torch"]
    m = z.shape[0]
    k = max(1, min(k, m))
    zz = (z.float() ** 2).sum(1)
    mind = torch.full((m,), float("inf"), device=z.device)
    last = torch.zeros((), dtype=torch.long, device=z.device)          # start: row 0 (the order is the pictures' order)
    chosen = torch.empty((k,), dtype=torch.long, device=z.device)
    for i in range(k):
        chosen[i] = last
        d = zz - 2 * (z @ z[last]).float() + zz[last]
        mind = torch.minimum(mind, d)
        last = torch.argmax(mind)
    return chosen


# ── score map ───────────────────────────────────────────────────────────────────────────────────────────────────
class Scorer:
    """the backbone + the layout's fixed parts (mask, blur) → score maps of pictures against a bank"""

    def __init__(self, layout, bb=None, device=None, c=None):
        import numpy as np
        import torch
        self.torch, self.np, self.layout = torch, np, layout
        self.c = {**DEFAULTS, **(c or {})}
        self.bb = bb or P.Backbone(device or os.environ.get("PATCHCORE_DEVICE", "cuda"))
        self.device = self.bb.device
        self.mask = zone_masks(layout)                     # the zones, no-detection regions cut out (levels)
        self.zmask = zone_masks(layout, cut_ignore=False)  # the zones whole: a spot whose PEAK is in a no-detection
        self.ignore = (self.zmask > 0) & (self.mask == 0)  # region is dropped (its halo spills a few px past the edge)
        x0, y0, _x1, _y1 = layout["crop"]
        self.off = [((x - x0) // MAP_SCALE, (y - y0) // MAP_SCALE, s // MAP_SCALE) for x, y, s in layout["tiles"]]
        sg = self.c["blur_sigma_px"] / MAP_SCALE
        r = max(1, int(math.ceil(2.5 * sg)))
        g = torch.exp(-(torch.arange(-r, r + 1, dtype=torch.float32) ** 2) / (2 * sg * sg))
        g = (g / g.sum()).to(self.device)
        self.kx, self.ky, self.r = g.view(1, 1, 1, -1), g.view(1, 1, -1, 1), r
        self.mask_t = torch.from_numpy(self.zmask > 0).to(self.device)

    def features(self, tiles):
        return self.bb.features(tiles)

    def dist(self, feats, bank):
        """feats [T, D, 36, 36], bank = (banks, sq, mean) prepared → [T, 36, 36] nearest-neighbour distances"""
        return P.nn_distance_tiles(feats, *bank)

    def to_map(self, dist):
        """[T, 36, 36] → the zones' score map, float32 numpy [h, w] at 1/MAP_SCALE (0 outside the zones)"""
        torch, F = self.torch, self.torch.nn.functional
        s4 = P.TILE_PX // MAP_SCALE
        up = F.interpolate(dist[:, None].float(), size=(s4, s4), mode="bilinear", align_corners=False)
        up = F.conv2d(F.pad(up, (self.r, self.r, 0, 0), mode="replicate"), self.kx)
        up = F.conv2d(F.pad(up, (0, 0, self.r, self.r), mode="replicate"), self.ky)[:, 0]
        h, w = self.mask.shape
        canvas = torch.zeros((h, w), device=up.device)
        for i, (ox, oy, s) in enumerate(self.off):
            hh, ww = min(s, h - oy), min(s, w - ox)
            region = canvas[oy:oy + hh, ox:ox + ww]
            torch.maximum(region, up[i, :hh, :ww], out=region)
        return (canvas * self.mask_t).cpu().numpy()

    def score(self, img, bank, origin=(0, 0)):
        """a picture (whole frame, or the layout's crop at origin) → its score map"""
        f = self.features(tile_crops(img, self.layout, origin=origin))
        return self.to_map(self.dist(f, bank))

    def level(self, smap, region=None):
        """the k-th highest value inside the zones (or inside region: a bool mask of the map)"""
        np = self.np
        v = smap[(self.mask > 0) if region is None else (region & (self.mask > 0))]
        k = self.c["min_hot_cells"]
        if v.size == 0:
            return 0.0
        if v.size <= k:
            return float(v.min())
        return float(np.partition(v, -k)[-k])


# ── build ───────────────────────────────────────────────────────────────────────────────────────────────────────
def _read(path):
    import cv2
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None or img.shape[:2] != (FRAME[1], FRAME[0]):
        return None
    return img


def build_mode(sc, frames, c, log=_log):
    """the bank of one mode: FP16 [T, K, D] on the CPU (K = coreset_ratio of every tile's patches)"""
    torch, np = sc.torch, sc.np
    layout = sc.layout
    T, n = len(layout["tiles"]), len(frames)
    M = n * PATCHES
    K = max(1, int(M * c["coreset_ratio"]))
    proj = c["proj_dim"]
    G = max(1, min(T, int(c["group_budget_mb"] * 1e6 // (M * proj * 2))))
    gen = torch.Generator(device="cpu").manual_seed(c["seed"])
    dt = torch.float16 if sc.bb.half else torch.float32                 # FP16 on the GPU (the CPU: tests)
    Pm = (torch.randn(1536, proj, generator=gen) / math.sqrt(proj)).to(sc.device).to(dt)
    out = torch.empty((T, K, 1536), dtype=torch.float16)
    B = c["images_per_batch"]
    t0 = time.time()
    for g0 in range(0, T, G):
        ids = list(range(g0, min(T, g0 + G)))
        z = torch.empty((len(ids), M, proj), dtype=dt, device=sc.device)
        bad = 0
        for b0 in range(0, n, B):                                   # pass 1: projections of every patch
            imgs = [_read(f["path"]) for f in frames[b0:b0 + B]]
            tiles = []
            for im in imgs:
                if im is None:
                    bad += 1
                    im = np.zeros((FRAME[1], FRAME[0], 3), np.uint8)
                tiles += tile_crops(im, layout, set(ids))
            f = sc.features(tiles).flatten(2).transpose(1, 2)        # [len(imgs) * G, P, D]
            f = f.reshape(len(imgs), len(ids), PATCHES, 1536)
            for j in range(len(imgs)):
                z[:, (b0 + j) * PATCHES:(b0 + j + 1) * PATCHES] = f[j] @ Pm
        if bad:
            log(f"   {bad} picture(s) not readable (counted as black)")
        chosen = [coreset_idx(z[g], K) for g in range(len(ids))]
        del z
        idx = [torch.sort(ch).values.cpu() for ch in chosen]
        for b0 in range(0, n, B):                                   # pass 2: the chosen patches' full features
            imgs = [_read(f["path"]) for f in frames[b0:b0 + B]]
            tiles = []
            for im in imgs:
                tiles += tile_crops(im if im is not None else np.zeros((FRAME[1], FRAME[0], 3), np.uint8), layout, set(ids))
            f = sc.features(tiles).flatten(2).transpose(1, 2).reshape(len(imgs), len(ids), PATCHES, 1536)
            lo, hi = b0 * PATCHES, (b0 + len(imgs)) * PATCHES
            for gi, t in enumerate(ids):
                sel = idx[gi][(idx[gi] >= lo) & (idx[gi] < hi)]
                if sel.numel():
                    pos = torch.searchsorted(idx[gi], sel)
                    rows = f[:, gi].reshape(-1, 1536)[(sel - lo).to(sc.device)]
                    out[t, pos] = rows.half().cpu()
        log(f"   tiles {ids[0] + 1}–{ids[-1] + 1} of {T} done ({time.time() - t0:.0f} s)")
    return out


def paste_object(img, mask_full, rng):
    """a made-up object (12–48 px: a packet, a bottle, a phone — dark, bright, coloured or low contrast, slightly blurred) at a random
    place inside the landing zones → (picture, its box (l, t, r, b) in frame pixels) or (picture, None)"""
    import cv2
    import numpy as np
    ys, xs = np.nonzero(mask_full)
    if not len(xs):
        return img, None
    k = rng.integers(len(xs))
    cx, cy = int(xs[k]), int(ys[k])
    w, h = int(rng.integers(12, 49)), int(rng.integers(12, 49))
    tone = rng.integers(4)
    if tone == 3:                              # low contrast: a grey packet on grey ground (±30 of the ground there)
        y0, x0 = max(0, cy - 30), max(0, cx - 30)
        ground = img[y0:cy + 30, x0:cx + 30].reshape(-1, 3).mean(0)
        sign = 1 if ground.mean() < 128 else -1
        col = tuple(int(np.clip(v + sign * 30, 0, 255)) for v in ground)
    else:
        col = (int(rng.integers(15, 60)),) * 3 if tone == 0 else (int(rng.integers(200, 250)),) * 3 if tone == 1 else \
            tuple(int(v) for v in rng.integers(0, 255, 3))
    out = img.copy()
    obj = np.zeros_like(out)
    m = np.zeros(out.shape[:2], np.uint8)
    if rng.random() < 0.5:
        cv2.ellipse(m, (cx, cy), (w // 2, h // 2), float(rng.integers(0, 180)), 0, 360, 255, -1)
    else:
        cv2.rectangle(m, (cx - w // 2, cy - h // 2), (cx + w // 2, cy + h // 2), 255, -1)
    obj[:] = col
    m = cv2.GaussianBlur(m, (3, 3), 0)
    a = (m.astype(np.float32) / 255)[..., None]
    out = (out * (1 - a) + obj * a).astype(np.uint8)
    return out, (cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2)


def calibrate(sc, bank, hold, c, log=_log):
    """held-out normal frames → their levels; made-up objects pasted into them → found at which threshold"""
    import cv2
    np = sc.np
    rng = np.random.default_rng(c["seed"])
    x0, y0, x1, y1 = sc.layout["crop"]
    full_mask = np.zeros((FRAME[1], FRAME[0]), np.uint8)
    full_mask[y0:y0 + sc.mask.shape[0] * MAP_SCALE, x0:x0 + sc.mask.shape[1] * MAP_SCALE] = \
        cv2.resize((sc.mask > 0).astype(np.uint8), (sc.mask.shape[1] * MAP_SCALE, sc.mask.shape[0] * MAP_SCALE),
                   interpolation=cv2.INTER_NEAREST)[:FRAME[1] - y0, :FRAME[0] - x0]
    full_mask = cv2.erode(full_mask, np.ones((25, 25), np.uint8))           # objects wholly inside a zone
    normal, objs = [], []
    for f in hold:
        img = _read(f["path"])
        if img is None:
            continue
        normal.append(sc.level(sc.score(img, bank)))
        pic = img
        boxes = []
        for _ in range(c["synthetic_per_image"]):
            pic, b = paste_object(pic, full_mask, rng)
            if b:
                boxes.append(b)
        smap = sc.score(pic, bank)
        for (l, t, r, b) in boxes:
            reg = np.zeros(sc.mask.shape, bool)
            reg[max(0, (t - y0) // MAP_SCALE - 1):(b - y0) // MAP_SCALE + 2, max(0, (l - x0) // MAP_SCALE - 1):(r - x0) // MAP_SCALE + 2] = True
            objs.append(sc.level(smap, reg))
    if not normal:
        raise RuntimeError("no held-out normal frame readable")
    top = max(normal)
    found = lambda th: round(100 * sum(1 for v in objs if v >= th) / max(1, len(objs)), 1)
    table = [{"margin": mg, "threshold": round(top * mg, 3), "normal_over": sum(1 for v in normal if v >= top * mg),
              "objects_found_pct": found(top * mg)} for mg in sorted({1.0, c["margin"], *c["margins"]})]
    ok = [mg for mg in c["margins"] if mg >= c["margin"] and found(top * mg) >= c["min_recall_pct"]]
    margin = max(ok) if ok else c["margin"]
    thr = top * margin
    recall = found(thr)
    log(f"   held-out normal frames: {len(normal)}, highest level {top:.2f} → threshold {thr:.2f} (x{margin}); "
        f"made-up objects found: {recall} % of {len(objs)}" + ("" if ok else f" (under {c['min_recall_pct']:g} %: more / better normal frames help)"))
    return {"map_threshold": round(thr, 4), "margin": margin, "normal_levels": [round(v, 3) for v in sorted(normal)],
            "normal_max": round(top, 4), "objects": len(objs), "objects_found_pct": recall,
            "object_levels_median": round(float(np.median(objs)), 3) if objs else None, "tradeoff": table}


def mode_dir(out_root, cam, mode):
    return os.path.join(out_root, cam, mode)


def read_meta(out_root, cam, mode):
    try:
        return json.load(open(os.path.join(mode_dir(out_root, cam, mode), "meta.json")))
    except (OSError, ValueError):
        return None


def save_mode(out_root, cam, mode, bank, meta):
    """atomic: written next to it, then swapped in; the False-alarm additions are kept when the tiles are the same"""
    import torch
    d = mode_dir(out_root, cam, mode)
    tmp = d + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    torch.save({"banks": bank, "tiles": meta["tiles"]}, os.path.join(tmp, "bank.pt"))
    old = read_meta(out_root, cam, mode)
    if old and old.get("sig") == meta["sig"] and os.path.isdir(os.path.join(d, "extra")):
        shutil.copytree(os.path.join(d, "extra"), os.path.join(tmp, "extra"))
        meta["extra_kept"] = len(os.listdir(os.path.join(tmp, "extra")))
    with open(os.path.join(tmp, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    if os.path.isdir(d):
        shutil.rmtree(d + ".old", ignore_errors=True)
        os.replace(d, d + ".old")
    os.replace(tmp, d)
    shutil.rmtree(d + ".old", ignore_errors=True)


def build(cam, zone, frames, out_root, cfg=None, log=_log) -> dict:
    c = {**DEFAULTS, **(cfg or {})}
    layout = landing_layout(zone)
    if not layout["tiles"]:
        raise RuntimeError(f"{cam}: no landing zone drawn (DeepStream → Perimeter zones → Landing zone)")
    import torch
    sc = Scorer(layout, c=c)
    log(f"{cam}: {len(layout['zones'])} landing zone(s) "
        f"({', '.join(z['name'] + ' (' + (z['side'] or 'no line') + ')' for z in layout['zones'])}), {len(layout['tiles'])} tiles")
    res = {"camera": cam, "sig": layout["sig"], "modes": {}}
    for mode in ("day", "night"):
        fs = [f for f in frames if f["mode"] == mode]
        if len(fs) < c["min_images"]:
            log(f"{mode}: {len(fs)} normal frame(s) — at least {c['min_images']} needed: no {mode} memory built")
            res["modes"][mode] = {"built": False, "frames": len(fs)}
            continue
        train, hold = split_frames(fs, c)
        log(f"{mode}: {len(fs)} normal frames → {len(train)} for the memory, {len(hold)} kept aside for the threshold")
        t0 = time.time()
        bank = build_mode(sc, train, c, log)
        prep = P.prepare_banks(bank.to(sc.device, copy=True))   # centred in place: never the bank that is saved
        cal = calibrate(sc, prep, hold, c, log) if hold else {"map_threshold": None}
        del prep
        if sc.device.startswith("cuda"):
            torch.cuda.empty_cache()
        meta = {"version": VERSION, "camera": cam, "mode": mode, "sig": layout["sig"], "tiles": layout["tiles"],
                "zones": [{k: z[k] for k in ("name", "side", "tiles")} for z in layout["zones"]],
                "built": datetime.now().isoformat(timespec="seconds"), "images": len(train), "holdout": len(hold),
                "first": train[0]["ts"], "last": train[-1]["ts"], "patches_per_tile": int(bank.shape[1]),
                "coreset_ratio": c["coreset_ratio"], "build_s": round(time.time() - t0), **cal}
        if not meta.get("map_threshold"):
            log(f"{mode}: too few frames to set the threshold — not saved")
            res["modes"][mode] = {"built": False, "frames": len(fs)}
            continue
        save_mode(out_root, cam, mode, bank, meta)
        log(f"{mode}: memory saved ({meta['patches_per_tile']} patches per tile, {bank.numel() * 2 / 1e6:.0f} MB, "
            f"{meta['build_s']} s)")
        res["modes"][mode] = {"built": True, **{k: meta[k] for k in ("images", "holdout", "map_threshold", "objects_found_pct")}}
    return res


# ── load (the landing worker) ───────────────────────────────────────────────────────────────────────────────────
class Bank:
    """one camera's bank of one mode on the GPU, the False-alarm additions included, ready for the batched search"""

    def __init__(self, out_root, cam, mode, device="cuda", only_tiles=None):
        """only_tiles: just these tiles (in this order) — learning a False alarm needs a few, not the whole bank"""
        import torch
        self.dir = mode_dir(out_root, cam, mode)
        self.meta = json.load(open(os.path.join(self.dir, "meta.json")))
        self.mtime = self.stamp(out_root, cam, mode)
        base = torch.load(os.path.join(self.dir, "bank.pt"), map_location="cpu", weights_only=True)["banks"]
        order = list(only_tiles) if only_tiles is not None else list(range(base.shape[0]))
        if only_tiles is not None:
            base = base[order].clone()
        pos = {t: k for k, t in enumerate(order)}
        extra = [[] for _ in range(base.shape[0])]
        self.extra_ids = []
        for f in sorted(glob.glob(os.path.join(self.dir, "extra", "*.pt"))):
            try:
                e = torch.load(f, map_location="cpu", weights_only=True)
                for t, feats in zip(e["tiles"], e["feats"]):
                    if t in pos and feats.numel():
                        extra[pos[t]].append(feats.half())
                self.extra_ids.append(os.path.basename(f)[:-3])
            except Exception as ex:                                # a damaged addition never stops the detection
                print(f"LANDING bank addition {f} not read: {ex!r}", flush=True)
        n_extra = max([sum(x.shape[0] for x in e) for e in extra] + [0])
        if n_extra:
            pad = []
            for t, e in enumerate(extra):
                rows = torch.cat(e) if e else base[t, :0]
                fill = base[t, :1].expand(n_extra - rows.shape[0], -1)   # duplicates of a bank patch: no distance changes
                pad.append(torch.cat([rows, fill]))
            base = torch.cat([base, torch.stack(pad)], 1)
        self.patches = int(base.shape[1])
        self.prepared = P.prepare_banks(base.to(device))
        self.threshold = float(self.meta["map_threshold"])

    @staticmethod
    def stamp(out_root, cam, mode):
        """changes when the bank or its additions change (the worker reloads)"""
        d = mode_dir(out_root, cam, mode)
        try:
            ex = os.path.join(d, "extra")
            return (os.stat(os.path.join(d, "meta.json")).st_mtime,
                    tuple(sorted(os.listdir(ex))) if os.path.isdir(ex) else ())
        except OSError:
            return None


def add_feedback(out_root, cam, mode, alert_id, tiles, feats) -> int:
    """False alarm: these tiles' patches join the bank (extra/<alert id>.pt) → patches added"""
    import torch
    d = os.path.join(mode_dir(out_root, cam, mode), "extra")
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{alert_id}.pt.part")
    torch.save({"tiles": list(tiles), "feats": [f.half().cpu() for f in feats]}, tmp)
    os.replace(tmp, os.path.join(d, f"{alert_id}.pt"))
    return sum(int(f.shape[0]) for f in feats)


def remove_feedback(out_root, cam, alert_id) -> bool:
    gone = False
    for mode in ("day", "night"):
        f = os.path.join(mode_dir(out_root, cam, mode), "extra", f"{alert_id}.pt")
        if os.path.isfile(f):
            os.remove(f)
            gone = True
    return gone


def status(cam, zone, captures_root, out_root, extra_dirs=()) -> dict:
    """what the dashboard shows per camera: zones, normal frames, the memory of each mode and whether it still fits"""
    layout = landing_layout(zone)
    frames = normal_frames(captures_root, cam, extra_dirs)
    out = {"camera": cam, "zones": [{"name": z["name"], "side": z["side"]} for z in layout["zones"]],
           "tiles": len(layout["tiles"]), "frames": {m: sum(1 for f in frames if f["mode"] == m) for m in ("day", "night")},
           "modes": {}}
    for mode in ("day", "night"):
        m = read_meta(out_root, cam, mode)
        if m:
            ex = os.path.join(mode_dir(out_root, cam, mode), "extra")
            ok, missing = covered(layout, m.get("tiles"))
            out["modes"][mode] = {"built": m.get("built"), "images": m.get("images"), "threshold": m.get("map_threshold"),
                                  "objects_found_pct": m.get("objects_found_pct"), "fits": bool(ok) and not missing,
                                  "covered": [layout["zones"][k]["name"] for k in ok], "missing": missing,
                                  "false_alarms_learnt": len(os.listdir(ex)) if os.path.isdir(ex) else 0,
                                  "first": m.get("first"), "last": m.get("last")}
    return out


# ── self-test ───────────────────────────────────────────────────────────────────────────────────────────────────
def _selftest():
    zone = {"line": [[0, 0.45], [1, 0.45]], "outside": [0.5, 0.1],
            "regions": [{"name": "Inside", "kind": "landing", "points": [[0.05, 0.55], [0.95, 0.55], [0.95, 0.95], [0.05, 0.95]]},
                        {"name": "Outside", "kind": "landing", "points": [[0.1, 0.05], [0.6, 0.05], [0.6, 0.35], [0.1, 0.35]]}]}
    lay = landing_layout(zone)
    assert [z["side"] for z in lay["zones"]] == ["inside", "outside"], lay["zones"]
    assert len(set(lay["tiles"])) == len(lay["tiles"]) and all(0 <= x and x + s <= 1920 and 0 <= y and y + s <= 1080 for x, y, s in lay["tiles"])
    assert lay["crop"][0] % MAP_SCALE == 0 and lay["crop"][1] % MAP_SCALE == 0
    assert landing_layout(zone)["sig"] == lay["sig"] and landing_layout({})["tiles"] == []
    moved = json.loads(json.dumps(zone)); moved["regions"][0]["points"][0][1] = 0.5
    assert landing_layout(moved)["sig"] != lay["sig"]                         # a moved zone: the bank no longer fits
    m = zone_masks(lay)
    assert set(int(v) for v in set(m.flatten().tolist())) == {0, 1, 2}
    ig = json.loads(json.dumps(zone)); ig["regions"].append({"name": "Tree", "kind": "ignore", "points": [[0.05, 0.55], [0.5, 0.55], [0.5, 0.95], [0.05, 0.95]]})
    li = landing_layout(ig)
    assert li["sig"] == lay["sig"] and (zone_masks(li) == 1).sum() < 0.6 * (m == 1).sum()   # no-detection cut out, same bank
    one = json.loads(json.dumps(zone)); one["regions"] = one["regions"][:1]                  # a zone removed
    l1 = landing_layout(one)
    assert covered(l1, lay["tiles"]) == ([0], []) and covered(lay, l1["tiles"]) == ([0], ["Outside"])
    sl = sub_layout(lay, [1])
    assert [z["name"] for z in sl["zones"]] == ["Outside"] and len(sl["tiles"]) == len(lay["zones"][1]["tiles"]), sl
    assert all(t in lay["tiles"] for t in sl["tiles"]) and sl["crop"][3] <= 1080
    fr = [{"path": f"/x/c_{i:04d}", "mode": "day", "ts": f"{i:04d}"} for i in range(100)]
    tr, ho = split_frames(fr, {**DEFAULTS, "max_images": 50})
    assert len(ho) == 15 and len(tr) == 50 and not set(f["ts"] for f in tr) & set(f["ts"] for f in ho)
    try:
        import torch
    except ImportError:
        print(json.dumps({"layout": "ok", "torch": "not here"}))
        return
    torch.manual_seed(0)
    pts = torch.cat([torch.randn(500, 8) + 10 * torch.eye(8)[i % 8] for i in range(8)])
    ch = coreset_idx(pts, 8)
    assert len(set((ch // 500).tolist())) == 8, ch                           # k-centre: one from each far cluster
    assert len(set(ch.tolist())) == 8
    print(json.dumps({"layout": "ok", "coreset": "ok", "tiles": len(lay["tiles"])}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("build", "status", "selftest"))
    ap.add_argument("--camera"); ap.add_argument("--zones"); ap.add_argument("--captures")
    ap.add_argument("--out", default=os.path.join(os.environ.get("TRINETRA_DATA", "/root/PERIMETER/data"), "ds", "banks"))
    ap.add_argument("--frames", action="append", default=[], help="another folder of <camera>_<time>_<day|night>.jpg")
    ap.add_argument("--max-images", type=int); ap.add_argument("--coreset", type=float)
    a = ap.parse_args()
    if a.cmd == "selftest":
        _selftest()
        print("bank self-test ok")
        sys.exit(0)
    zone = (json.load(open(a.zones)) if a.zones else {}).get(a.camera) or {}
    if a.cmd == "status":
        print(json.dumps(status(a.camera, zone, a.captures, a.out, a.frames)))
        sys.exit(0)
    cfg = {}
    if a.max_images:
        cfg["max_images"] = a.max_images
    if a.coreset:
        cfg["coreset_ratio"] = a.coreset
    try:
        frames = normal_frames(a.captures, a.camera, a.frames)
        _log(f"{a.camera}: {len(frames)} normal frame(s) found "
             f"({sum(1 for f in frames if f['mode'] == 'day')} day, {sum(1 for f in frames if f['mode'] == 'night')} night)")
        res = build(a.camera, zone, frames, a.out, cfg)
        print("RESULT " + json.dumps(res), flush=True)
        sys.exit(0 if any(m.get("built") for m in res["modes"].values()) else 2)
    except Exception as e:
        _log(f"FAILED: {e}")
        print("RESULT " + json.dumps({"camera": a.camera, "error": str(e)}), flush=True)
        sys.exit(1)
