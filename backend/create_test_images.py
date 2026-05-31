"""
create_test_images.py
Run once to populate backend/test_images/ with realistic-looking
dummy security-camera frames for use by the admin test-data generator.

Usage:
    python backend/create_test_images.py
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import random, math

OUT = Path(__file__).parent / "test_images"
OUT.mkdir(exist_ok=True)

W, H = 640, 480   # standard SD security camera resolution

SCENES = [
    {"name": "cam-entrance_person1",  "bg": (30, 28, 40),  "badge": "GOLI",      "cam": "CAM-01 ENTRANCE",   "accent": (255, 80,  80)},
    {"name": "cam-parking_person2",   "bg": (20, 35, 25),  "badge": "TEST-02",   "cam": "CAM-02 PARKING",    "accent": (80,  200, 100)},
    {"name": "cam-perimeter_person3", "bg": (28, 28, 55),  "badge": "VISITOR-A", "cam": "CAM-03 PERIMETER",  "accent": (100, 140, 255)},
    {"name": "cam-lobby_person4",     "bg": (50, 35, 20),  "badge": "TEST-04",   "cam": "CAM-04 LOBBY",      "accent": (255, 180,  60)},
    {"name": "cam-entrance_person5",  "bg": (35, 20, 35),  "badge": "DEMO-01",   "cam": "CAM-01 ENTRANCE",   "accent": (200,  80, 200)},
    {"name": "cam-gate_person6",      "bg": (20, 40, 40),  "badge": "WORKER-7",  "cam": "CAM-05 GATE",       "accent": ( 60, 210, 210)},
    {"name": "cam-parking_person7",   "bg": (40, 30, 20),  "badge": "TEST-07",   "cam": "CAM-02 PARKING",    "accent": (255, 130,  50)},
    {"name": "cam-lobby_person8",     "bg": (25, 25, 25),  "badge": "STAFF-03",  "cam": "CAM-04 LOBBY",      "accent": (180, 180, 180)},
]

def draw_noise(draw, img):
    """Subtle scanline / film-grain effect."""
    px = img.load()
    for y in range(0, H, 4):
        for x in range(W):
            r, g, b = px[x, y]
            v = random.randint(-12, 12)
            px[x, y] = (max(0, min(255, r+v)),
                        max(0, min(255, g+v)),
                        max(0, min(255, b+v)))

def draw_grid(draw, bg):
    """Faint perspective grid — simulates floor tiles."""
    vp_x, vp_y = W // 2, H // 3
    line_color = tuple(min(255, c + 25) for c in bg)
    for i in range(-6, 7):
        x_end = W // 2 + i * 60
        draw.line([(vp_x, vp_y), (x_end, H)], fill=line_color, width=1)
    for j in range(4):
        y = H // 3 + j * (H * 2 // 3) // 4
        draw.line([(0, y), (W, y)], fill=line_color, width=1)

def draw_silhouette(draw, accent, x_center=None):
    """Draw a simple person silhouette."""
    if x_center is None:
        x_center = random.randint(W // 4, 3 * W // 4)
    scale = random.uniform(0.7, 1.1)
    body_color = tuple(c // 3 for c in accent)
    shadow = (10, 10, 10)

    # Head
    hx, hy = x_center, int(H * 0.38 * scale)
    hr = int(28 * scale)
    draw.ellipse([hx-hr, hy-hr, hx+hr, hy+hr], fill=shadow)
    draw.ellipse([hx-hr+2, hy-hr+2, hx+hr-2, hy+hr-2], fill=body_color)

    # Body
    bw = int(40 * scale)
    bh = int(80 * scale)
    by = hy + hr
    draw.rectangle([hx-bw, by, hx+bw, by+bh], fill=shadow)
    draw.rectangle([hx-bw+2, by+2, hx+bw-2, by+bh-2], fill=body_color)

    # Legs
    lw = int(18 * scale)
    lh = int(70 * scale)
    ly = by + bh
    draw.rectangle([hx-bw,    ly, hx-bw+lw*2, ly+lh], fill=shadow)
    draw.rectangle([hx+bw-lw*2, ly, hx+bw,   ly+lh], fill=shadow)
    draw.rectangle([hx-bw+2,     ly+2, hx-bw+lw*2-2, ly+lh-2], fill=body_color)
    draw.rectangle([hx+bw-lw*2+2, ly+2, hx+bw-2,     ly+lh-2], fill=body_color)

def draw_hud(draw, scene, img_w, img_h):
    """Overlay — timestamp, camera name, badge detected."""
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")

    # Top bar
    draw.rectangle([0, 0, img_w, 32], fill=(0, 0, 0, 180))
    draw.text((8, 6),  scene["cam"], fill=(200, 200, 200))
    draw.text((img_w - 180, 6), now, fill=(160, 160, 160))

    # Bottom bar
    draw.rectangle([0, img_h - 38, img_w, img_h], fill=(0, 0, 0, 180))
    badge_text = f"DETECTED:  {scene['badge']}"
    draw.rectangle([6, img_h - 32, 16 + len(badge_text) * 7, img_h - 8],
                   fill=scene["accent"])
    draw.text((10, img_h - 30), badge_text, fill=(0, 0, 0))

    # REC dot
    draw.ellipse([img_w - 30, 8, img_w - 14, 24], fill=(220, 30, 30))
    draw.text((img_w - 44, 6), "●", fill=(220, 30, 30))

for scene in SCENES:
    img = Image.new("RGB", (W, H), scene["bg"])
    draw = ImageDraw.Draw(img, "RGBA")

    draw_grid(draw, scene["bg"])

    # Extra background figures
    for _ in range(random.randint(1, 3)):
        draw_silhouette(draw, (60, 60, 60))

    # Main subject (brighter, centred-ish)
    draw_silhouette(draw, scene["accent"], x_center=random.randint(W//3, 2*W//3))

    draw_noise(draw, img)

    # Re-create draw without alpha for HUD (simpler)
    draw2 = ImageDraw.Draw(img)
    draw_hud(draw2, scene, W, H)

    out_path = OUT / f"{scene['name']}.jpg"
    img.save(out_path, "JPEG", quality=85)
    print(f"  ✓ {out_path.name}  ({out_path.stat().st_size // 1024} KB)")

print(f"\nGenerated {len(SCENES)} test images in {OUT}/")
