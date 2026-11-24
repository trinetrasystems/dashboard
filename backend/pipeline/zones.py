"""Perimeter zones of one camera, drawn on the DeepStream page (Perimeter zones tab), and what a detection means there.

All points are fractions of the frame (0..1, x right, y down), so they hold for any stream resolution.

    {"line": [[x, y], ...],          # the perimeter line (wall / fence), a polyline of 2+ points
     "outside": [x, y],              # a point on the OUTSIDE of the line (clicked on the page)
     "alert": [[[x, y], ...], ...],  # alert regions (wall top, concertina wire): anything there = breach, at once
     "ignore": [[[x, y], ...], ...]} # ignored regions (a road, a tree that moves, a reflection)

classify(): the foot point of a box (bottom centre — where the object stands) decides:
    ignore region             → None
    alert region              → BREACH class   (dashboard: instant alert)
    outside side of the line  → OUTSIDE_PERSON / OUTSIDE_OBJECT (dashboard: dwell warning)
    inside                    → None (inside the premises: not reported)
A model trained on the perimeter classes themselves (its label is already one of PERIMETER_CLASSES) keeps its own
class, except in an ignore region.
"""
BREACH = "Boundry-object"
OUTSIDE_PERSON = "outside-person"
OUTSIDE_OBJECT = "outside-object"
PERIMETER_CLASSES = {BREACH.lower(), OUTSIDE_PERSON, OUTSIDE_OBJECT}
PERSON_LABELS = {"person", "people", "pedestrian", "human", "outside-person"}


def _pt(p):
    return float(p[0]), float(p[1])


def in_polygon(p, poly) -> bool:
    """ray casting; poly = [[x, y], ...] (3+ points)"""
    if len(poly) < 3:
        return False
    x, y = p
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = _pt(poly[i])
        xj, yj = _pt(poly[j])
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _side_of_segment(p, a, b) -> float:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def _closest_segment(p, line):
    best, seg = None, None
    for i in range(len(line) - 1):
        a, b = _pt(line[i]), _pt(line[i + 1])
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
        q = (a[0] + t * dx, a[1] + t * dy)
        d = (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
        if best is None or d < best:
            best, seg = d, (a, b)
    return seg


def _extended_side(p, line) -> float:
    """side of p relative to the polyline, the end segments extended beyond the drawn ends (a wall continues)"""
    a, b = _closest_segment(p, line)
    return _side_of_segment(p, a, b)


def is_outside(p, zone) -> bool:
    line, out = zone.get("line") or [], zone.get("outside")
    if len(line) < 2 or not out:
        return False
    s_out = _extended_side(_pt(out), line)
    s_p = _extended_side(p, line)
    return s_out != 0 and s_p * s_out > 0


def classify(label: str, box, zone, frame_w: float, frame_h: float):
    """box = (left, top, width, height) in pixels of a frame_w x frame_h frame → a perimeter class or None"""
    if not zone:
        return None
    foot = ((box[0] + box[2] / 2) / frame_w, (box[1] + box[3]) / frame_h)
    if any(in_polygon(foot, poly) for poly in zone.get("ignore") or []):
        return None
    lab = (label or "").strip().lower()
    if lab in PERIMETER_CLASSES:                       # a perimeter model: its own class
        return BREACH if lab == BREACH.lower() else lab
    centre = ((box[0] + box[2] / 2) / frame_w, (box[1] + box[3] / 2) / frame_h)
    if any(in_polygon(foot, poly) or in_polygon(centre, poly) for poly in zone.get("alert") or []):
        return BREACH
    if is_outside(foot, zone):
        return OUTSIDE_PERSON if lab in PERSON_LABELS else OUTSIDE_OBJECT
    return None


def clean(zone) -> dict:
    """a zone from the page, checked: fractions 0..1, sane sizes; raises ValueError"""
    if not isinstance(zone, dict):
        raise ValueError("zones must be an object")

    def pt(p):
        if not (isinstance(p, (list, tuple)) and len(p) == 2):
            raise ValueError("a point is [x, y]")
        x, y = float(p[0]), float(p[1])
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("points are fractions of the frame (0..1)")
        return [round(x, 5), round(y, 5)]

    line = [pt(p) for p in zone.get("line") or []]
    if len(line) == 1 or len(line) > 50:
        raise ValueError("the perimeter line needs 2–50 points")
    out = pt(zone["outside"]) if zone.get("outside") else None
    if line and not out:
        raise ValueError("mark the outside side of the perimeter line")
    polys = {}
    for k in ("alert", "ignore"):
        ps = zone.get(k) or []
        if not isinstance(ps, list) or len(ps) > 20:
            raise ValueError(f"at most 20 {k} regions")
        polys[k] = []
        for poly in ps:
            pts = [pt(p) for p in poly or []]
            if not 3 <= len(pts) <= 50:
                raise ValueError(f"a {k} region needs 3–50 points")
            polys[k].append(pts)
    return {"line": line, "outside": out, "alert": polys["alert"], "ignore": polys["ignore"]}


if __name__ == "__main__":                            # python3 zones.py: self-test (used by the install check)
    z = clean({"line": [[0, 0.5], [1, 0.5]], "outside": [0.5, 0.2], "alert": [[[0.2, 0.45], [0.8, 0.45], [0.8, 0.55], [0.2, 0.55]]]})
    box = lambda cx, fy: (cx - 20, fy - 100, 40, 100)
    got = [classify("person", box(500, 300), z, 1000, 1000), classify("car", box(500, 300), z, 1000, 1000),
           classify("person", box(500, 800), z, 1000, 1000), classify("person", box(500, 520), z, 1000, 1000)]
    want = [OUTSIDE_PERSON, OUTSIDE_OBJECT, None, BREACH]
    assert got == want, got
    print("zones self-test ok")
