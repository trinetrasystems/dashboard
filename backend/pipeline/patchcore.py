"""PatchCore's base for the landing / unattended-object detection (PERIMETER_THROW_DETECTION_SPEC §2.3–2.5, §7.5):
the fixed tile grid over a landing zone, the backbone features of a tile, the nearest-neighbour distance of every patch to
a tile's bank. The banks, the scoring map, hot spots and the worker come with step E (they build on this).

tile_grid       the landing zone's bounding box (1920x1080 streammux pixels) cut into tile_px squares with tile_overlap_px
                overlap, a FIXED grid (the last tile of a row / column is moved back to end at the edge): tile i is always the
                same piece of the picture, so it is compared only with normal pictures of that piece (spec §2.4)
Backbone        torchvision wide_resnet50_2 with ImageNet weights, eval, FP16 on the GPU; layer2 + layer3, each averaged
                over its 3x3 neighbourhood (stride 1), layer3 upsampled to layer2's size, concatenated: 512 + 1024 = 1536
                numbers per patch, one patch per 8x8 pixels (a 288 tile → 36x36 patches). layer4 and the classifier are
                dropped (not used, saves ~2/3 of the weights' GPU memory). The weights come from the image
                (~/.cache/torch/hub/checkpoints, the AI pack): never downloaded at run time.
nn_distance     each patch's distance to its nearest neighbour in a bank [N, D], chunked so the GPU memory stays bounded

python3 patchcore.py                self-test (tile grid; with a GPU: the backbone offline, shapes, distances)
python3 patchcore.py --bench [...]  time + GPU memory of one check at the spec's settings (see bench())
"""
import json
import math
import os
import sys
import time

TILE_PX, OVERLAP_PX = 288, 32
WEIGHTS = "wide_resnet50_2-95faca4d.pth"
MEAN, STD = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)


def _starts(lo: int, hi: int, tile: int, step: int) -> list:
    """tile starts covering [lo, hi): every step px, the last one moved back to end at hi (or one tile if shorter)"""
    if hi - lo <= tile:
        return [lo]
    out, x = [], lo
    while x + tile < hi:
        out.append(x)
        x += step
    out.append(hi - tile)
    return out


def tile_grid(box, frame=(1920, 1080), tile_px: int = TILE_PX, overlap_px: int = OVERLAP_PX) -> list:
    """box = (left, top, right, bottom) in frame pixels → [(x, y, tile_px)] — a fixed grid, inside the frame"""
    fw, fh = frame
    l, t, r, b = (int(round(v)) for v in box)
    l, t, r, b = max(0, l), max(0, t), min(fw, r), min(fh, b)
    if r - l < tile_px:                                  # a narrow zone: one tile wide, kept inside the frame
        l = max(0, min(l - (tile_px - (r - l)) // 2, fw - tile_px)); r = l + tile_px
    if b - t < tile_px:
        t = max(0, min(t - (tile_px - (b - t)) // 2, fh - tile_px)); b = t + tile_px
    step = tile_px - overlap_px
    return [(x, y, tile_px) for y in _starts(t, b, tile_px, step) for x in _starts(l, r, tile_px, step)]


def zone_box(points, frame=(1920, 1080)):
    """a zone's points (fractions 0–1) → its bounding box in frame pixels"""
    xs, ys = [p[0] * frame[0] for p in points], [p[1] * frame[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def weights_path() -> str:
    import torch
    return os.path.join(torch.hub.get_dir(), "checkpoints", WEIGHTS)


class Backbone:
    def __init__(self, device: str = "cuda"):
        import torch
        import torchvision
        p = weights_path()
        if not os.path.isfile(p):
            raise FileNotFoundError(f"PatchCore backbone weights missing: {p} (the image's AI pack)")
        m = torchvision.models.wide_resnet50_2(weights=None)
        m.load_state_dict(torch.load(p, map_location="cpu", weights_only=True))
        m.layer4, m.fc, m.avgpool = torch.nn.Identity(), torch.nn.Identity(), torch.nn.Identity()
        self.torch, self.device = torch, device
        self.half = device.startswith("cuda")
        self.m = m.to(device).eval()
        if self.half:
            self.m = self.m.half()
        self.mean = torch.tensor(MEAN, device=device).view(1, 3, 1, 1)
        self.std = torch.tensor(STD, device=device).view(1, 3, 1, 1)
        self.pool = torch.nn.AvgPool2d(3, stride=1, padding=1)

    def features(self, tiles_bgr):
        """[H x W x 3 uint8 BGR tiles, all the same size] → tensor [n, 1536, H/8, W/8] (FP16 on the GPU)"""
        import numpy as np
        torch, F = self.torch, self.torch.nn.functional
        x = torch.from_numpy(np.ascontiguousarray(np.stack(tiles_bgr)[..., ::-1])).to(self.device)   # BGR → RGB
        x = x.permute(0, 3, 1, 2).float().div_(255).sub_(self.mean).div_(self.std)
        if self.half:
            x = x.half()
        m = self.m
        with torch.no_grad():
            x = m.maxpool(m.relu(m.bn1(m.conv1(x))))
            l2 = m.layer2(m.layer1(x))
            l3 = m.layer3(l2)
            l2, l3 = self.pool(l2), self.pool(l3)
            l3 = F.interpolate(l3, size=l2.shape[-2:], mode="bilinear", align_corners=False)
            return torch.cat([l2, l3], dim=1)


def nn_distance(feats, bank, chunk: int = 8192):
    """feats [D, h, w] (one tile), bank [N, D] → [h, w] distance of each patch to its nearest bank patch (one tile; the
    reference for nn_distance_tiles)"""
    torch = sys.modules["torch"]
    d, h, w = feats.shape
    q = feats.reshape(d, h * w).t().contiguous()                         # [P, D]
    best = torch.full((q.shape[0],), float("inf"), device=q.device, dtype=torch.float32)
    qq = (q.float() ** 2).sum(1, keepdim=True)                            # |q|² - 2 q·b + |b|², in chunks of the bank
    for i in range(0, bank.shape[0], chunk):
        b = bank[i:i + chunk]
        dist = qq - 2 * (q @ b.t()).float() + (b.float() ** 2).sum(1)[None, :]
        best = torch.minimum(best, dist.min(1).values)
    return best.clamp_(min=0).sqrt_().reshape(h, w)


def prepare_banks(banks):
    """banks [T, N, D] (every tile's normal patches, FP16 on the GPU) → (the same tensor, centred IN PLACE, their squared
    lengths [T, N] in FP32, the mean [T, D]). Centring on each tile's own mean changes no distance, but keeps the numbers small, so the FP16
    dot products stay exact enough (|q|² + |b|² − 2 q·b cancels badly when the lengths are large). Done once per bank."""
    torch = sys.modules["torch"]
    t, n, d = banks.shape
    mean = torch.empty((t, d), device=banks.device, dtype=torch.float32)
    sq = torch.empty((t, n), device=banks.device, dtype=torch.float32)
    for i in range(t):          # one tile at a time, IN PLACE: never a second copy of the banks (GBs at the spec's 10 %)
        b = banks[i].float()
        mean[i] = b.mean(0)
        b -= mean[i]
        banks[i] = b.half()
        sq[i] = (banks[i].float() ** 2).sum(-1)
        del b
    return banks, sq, mean


def nn_distance_tiles(feats, banks, sq, mean, chunk: int = 1024):
    """every tile at once: feats [T, D, h, w], (banks, sq, mean) from prepare_banks → [T, h, w] distance of each patch to
    the nearest patch of ITS tile's bank. The search is one batched FP16 matrix product per chunk of the banks (tensor
    cores); FP16 rounds each dot product, so the distance to the patch it found is then computed again exactly (FP32)."""
    torch = sys.modules["torch"]
    t, d, h, w = feats.shape
    q = (feats.flatten(2).transpose(1, 2).float() - mean[:, None, :]).half()     # [T, P, D] centred like the bank
    best = torch.full((t, h * w), float("inf"), device=q.device, dtype=torch.float32)
    arg = torch.zeros((t, h * w), device=q.device, dtype=torch.long)
    for i in range(0, banks.shape[1], chunk):
        v, j = (sq[:, None, i:i + chunk] - 2 * torch.bmm(q, banks[:, i:i + chunk].transpose(1, 2)).float()).min(-1)
        better = v < best
        best, arg = torch.where(better, v, best), torch.where(better, j + i, arg)
    out = torch.empty((t, h * w), device=q.device, dtype=torch.float32)
    for k in range(t):                       # the exact distance to the patch found, tile by tile (small FP32 temporaries)
        qf = feats[k].flatten(1).t().float() - mean[k]                            # [P, D]
        out[k] = (qf - banks[k][arg[k]].float()).pow_(2).sum(-1)
    return out.sqrt_().reshape(t, h, w)


def bench(zone=(0, 540, 1920, 1080), images: int = 300, coreset: float = 0.10, checks: int = 10, every_s: float = 1.0):
    """one check = the zone's tiles → features → nearest-neighbour distance to each tile's bank, as step E will run it.
    The banks are random (their size is what matters for time / memory): `images` normal pictures x 36x36 patches x
    `coreset`, FP16, one per tile, all on the GPU. → JSON: tiles, bank size, ms per check (median, max), GPU memory."""
    import numpy as np
    import torch
    grid = tile_grid(zone)
    t0 = time.time()
    bb = Backbone("cuda")
    load_s = time.time() - t0
    per_tile = int(images * (TILE_PX // 8) ** 2 * coreset)
    banks = prepare_banks(torch.randn(len(grid), per_tile, 1536, device="cuda", dtype=torch.float16))
    frame = np.random.randint(0, 255, (1080, 1920, 3), np.uint8)
    times, feat_ms = [], []
    torch.cuda.reset_peak_memory_stats()
    for i in range(checks + 1):
        t = time.time()
        tiles = [frame[y:y + s, x:x + s] for x, y, s in grid]
        f = bb.features(tiles)
        torch.cuda.synchronize()
        tf = time.time()
        nn_distance_tiles(f, *banks)
        torch.cuda.synchronize()
        if i:                                              # the first check warms the GPU up: not counted
            times.append((time.time() - t) * 1000)
            feat_ms.append((tf - t) * 1000)
        if every_s:
            time.sleep(max(0.0, every_s - (time.time() - t)))
    times.sort()
    return {"zone": list(zone), "tiles": len(grid), "bank_patches_per_tile": per_tile,
            "banks_mb": round(banks[0].numel() * 2 / 1e6), "load_s": round(load_s, 1),
            "check_ms_median": round(times[len(times) // 2]), "check_ms_max": round(times[-1]),
            "features_ms_median": round(sorted(feat_ms)[len(feat_ms) // 2]),
            "gpu_mem_peak_mb": round(torch.cuda.max_memory_allocated() / 1e6),
            "torch": torch.__version__, "device": torch.cuda.get_device_name(0)}


def cv2_free_picture(rng):
    """a 288x488 test picture with texture and edges (no camera, no OpenCV needed)"""
    import numpy as np
    y, x = np.mgrid[0:288, 0:488]
    base = (np.sin(x / 7.0) * 60 + np.cos(y / 11.0) * 50 + 128).astype(np.float32)
    base[100:180, 150:260] = 30                                                   # a dark object
    pic = np.stack([base, base * 0.8 + 20, base * 0.6 + 40], -1) + rng.normal(0, 6, (288, 488, 3))
    return np.clip(pic, 0, 255).astype(np.uint8)


def _selftest():
    g = tile_grid((0, 540, 1920, 1080))
    assert all(x + s <= 1920 and y + s <= 1080 and x >= 0 and y >= 0 for x, y, s in g), g
    xs, ys = sorted({x for x, _, _ in g}), sorted({y for _, y, _ in g})
    assert xs[0] == 0 and xs[-1] == 1920 - 288 and ys[-1] == 1080 - 288 and all(b - a <= 256 for a, b in zip(xs, xs[1:])), (xs, ys)
    assert len(g) == len(xs) * len(ys) == 8 * 2, len(g)                   # 1920 wide: 8 columns, 540 high: 2 rows
    assert tile_grid((900, 900, 950, 950)) == [(781, 781, 288)]           # small zone: one tile centred on it, inside
    assert tile_grid((0, 540, 1920, 1080)) == g                           # fixed: the same every time
    b = zone_box([[0.1, 0.5], [0.9, 0.5], [0.9, 1.0], [0.1, 1.0]])
    assert [round(v) for v in b] == [192, 540, 1728, 1080], b
    out = {"tile_grid": "ok"}
    try:
        import torch
    except ImportError:
        print(json.dumps({**out, "gpu": "no torch here"}))
        return
    if not torch.cuda.is_available():
        raise SystemExit("PyTorch does not see the GPU (torch %s)" % torch.__version__)
    import numpy as np
    bb = Backbone("cuda")
    tiles = [np.zeros((288, 288, 3), np.uint8), np.full((288, 288, 3), 128, np.uint8)]
    f = bb.features(tiles)
    assert tuple(f.shape) == (2, 1536, 36, 36) and f.dtype == torch.float16 and torch.isfinite(f).all(), (f.shape, f.dtype)
    d0 = nn_distance(f[0], f[0].reshape(1536, -1).t().contiguous())       # a tile against itself: ~0 everywhere
    d1 = nn_distance(f[1], f[0].reshape(1536, -1).t().contiguous())       # grey against black: clearly not
    assert float(d0.max()) < 0.5 and float(d1.min()) > float(d0.max()), (float(d0.max()), float(d1.min()))
    # the batched, centred FP16 search equals the exact FP32 distances (real features: 2 tiles of a real-looking picture)
    rng = np.random.default_rng(0)
    img = cv2_free_picture(rng)
    ft = bb.features([img[0:288, 0:288], img[0:288, 200:488]])                    # [2, D, 36, 36]
    bank = ft.flatten(2).transpose(1, 2)[:, ::3].contiguous()                     # every 3rd patch of each tile as its bank
    exact = torch.stack([torch.cdist(ft[i].float().flatten(1).t(), bank[i].float()).min(1).values for i in range(2)]).reshape(2, 36, 36)
    fast = nn_distance_tiles(ft, *prepare_banks(bank))                            # (bank is centred in place: after exact)
    # the FP16 search finds the nearest patch, the distance to it is exact: < 1 % off anywhere it matters
    err2 = float((fast ** 2 - exact ** 2).abs().max())
    assert err2 < 0.005 * float((exact ** 2).max()), (err2, float((exact ** 2).max()))
    far = exact > 0.5
    rel = float(((fast - exact).abs() / exact.clamp(min=1e-6))[far].max()) if bool(far.any()) else 0.0
    assert rel < 0.01, rel
    out["fast_vs_exact"] = {"sq_err_max": round(err2, 4), "rel_err_where_dist_gt_0.5": round(rel, 4)}
    print(json.dumps({**out, "backbone": "ok (offline weights)", "features": list(f.shape), "self_distance_max": round(float(d0.max()), 3),
                      "other_distance_min": round(float(d1.min()), 2), "torch": torch.__version__, "gpu": torch.cuda.get_device_name(0)}))


if __name__ == "__main__":
    if "--bench" in sys.argv:
        a = dict(x.split("=", 1) for x in sys.argv[1:] if "=" in x)
        zone = tuple(int(v) for v in a.get("zone", "0,540,1920,1080").split(","))
        print(json.dumps(bench(zone, int(a.get("images", 300)), float(a.get("coreset", 0.10)), int(a.get("checks", 10)),
                               float(a.get("every", 1.0)))))
    else:
        _selftest()
        print("patchcore self-test ok")
