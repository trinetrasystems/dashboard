"""Perimeter zones of one camera, drawn on the DeepStream page (Perimeter zones) or the Alerts page (Regions), and what a
detection means there.

All points are fractions of the frame (0..1, x right, y down), so they hold for any stream resolution.

    {"line": [[x, y], ...],           # the perimeter line (wall / fence), a polyline of 2+ points
     "outside": [x, y],               # a point on the OUTSIDE of the line
     "regions": [{"name": "Wall top", "kind": "alert",  "points": [[x, y], ...]},   # anything here = breach
                 {"name": "Gate A",   "kind": "watch",  "points": [...]},           # the client's alert rules decide
                 {"name": "Road",     "kind": "ignore", "points": [...]}]}          # never reported

(Zones saved before 25 Nov 2026 had "alert": [...] and "ignore": [...] lists: read as regions "Alert 1", "Ignore 1", …)

describe(): the foot point of a box (bottom centre — where the object stands) decides:
    in an ignore region                → None (not reported)
    otherwise                          → {"kind": person|object, "regions": [names of alert / watch regions it is in],
                                          "alert": in an alert region, "outside": outside the perimeter line}
    nothing of the above (inside, no region)  → None
cls(): the class written into the image file name (what the Logs / Visits pages show):
    Boundry-object (alert region) > outside-person / outside-object > zone-person / zone-object (a watch region)
A model trained on the perimeter classes themselves (label Boundry-object / outside-person / outside-object) keeps its
meaning (alert / outside), and the drawn regions are still added.
"""
import re

BREACH = "Boundry-object"
OUTSIDE_PERSON = "outside-person"
OUTSIDE_OBJECT = "outside-object"
ZONE_PERSON = "zone-person"
ZONE_OBJECT = "zone-object"
PERIMETER_CLASSES = {BREACH.lower(), OUTSIDE_PERSON, OUTSIDE_OBJECT}
PERSON_LABELS = {"person", "people", "pedestrian", "human", "outside-person"}
KINDS = ("alert", "watch", "ignore")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")


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


def regions(zone, kind=None) -> list:
    """the zone's regions (old zones: their alert / ignore lists named Alert 1, Ignore 1, …)"""
    rs = list(zone.get("regions") or [])
    for k in ("alert", "ignore"):
        for i, pts in enumerate(zone.get(k) or [], 1):
            rs.append({"name": f"{k.title()} {i}", "kind": k, "points": pts})
    return [r for r in rs if kind is None or r.get("kind") == kind]


def target_of(label: str) -> str:
    return "person" if (label or "").strip().lower() in PERSON_LABELS else "object"


def describe(label: str, box, zone, frame_w: float, frame_h: float):
    """box = (left, top, width, height) in pixels of a frame_w x frame_h frame → what the object means here, or None"""
    zone = zone or {}
    foot = ((box[0] + box[2] / 2) / frame_w, (box[1] + box[3]) / frame_h)
    centre = ((box[0] + box[2] / 2) / frame_w, (box[1] + box[3] / 2) / frame_h)
    if any(in_polygon(foot, r["points"]) for r in regions(zone, "ignore")):
        return None
    lab = (label or "").strip().lower()
    names, alert = [], lab == BREACH.lower()
    for r in regions(zone):
        if r["kind"] == "ignore":
            continue
        inside = in_polygon(foot, r["points"]) or (r["kind"] == "alert" and in_polygon(centre, r["points"]))
        if inside:
            names.append(r["name"])
            alert = alert or r["kind"] == "alert"
    outside = lab in (OUTSIDE_PERSON, OUTSIDE_OBJECT) or is_outside(foot, zone)
    if not (names or alert or outside):
        return None
    kind = "person" if lab == OUTSIDE_PERSON else ("object" if lab in (OUTSIDE_OBJECT, BREACH.lower()) else target_of(lab))
    return {"kind": kind, "regions": names, "alert": alert, "outside": outside}


def cls(d) -> str:
    """the class in the image file name for one described object"""
    if d["alert"]:
        return BREACH
    if d["outside"]:
        return OUTSIDE_PERSON if d["kind"] == "person" else OUTSIDE_OBJECT
    return ZONE_PERSON if d["kind"] == "person" else ZONE_OBJECT


def classify(label: str, box, zone, frame_w: float, frame_h: float):
    """the file-name class of one detection, or None (not reported)"""
    d = describe(label, box, zone, frame_w, frame_h)
    return cls(d) if d else None


def clean(zone) -> dict:
    """a zone from the page, checked: fractions 0..1, sane sizes, unique region names; raises ValueError"""
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
    raw = regions(zone)
    if len(raw) > 40:
        raise ValueError("at most 40 regions per camera")
    rs, seen = [], set()
    for r in raw:
        if not isinstance(r, dict):
            raise ValueError("a region is {name, kind, points}")
        name, kind = str(r.get("name", "")).strip(), r.get("kind")
        if kind not in KINDS:
            raise ValueError("a region is an alert, watch or ignore region")
        if not NAME_RE.match(name):
            raise ValueError(f"region name “{name[:40]}”: 1–40 letters, numbers, spaces, dots, dashes")
        if name.lower() in seen:
            raise ValueError(f"two regions are named {name}")
        seen.add(name.lower())
        pts = [pt(p) for p in r.get("points") or []]
        if not 3 <= len(pts) <= 50:
            raise ValueError(f"region {name} needs 3–50 corners")
        rs.append({"name": name, "kind": kind, "points": pts})
    return {"line": line, "outside": out, "regions": rs}


if __name__ == "__main__":                            # python3 zones.py: self-test (used by the install check)
    z = clean({"line": [[0, 0.5], [1, 0.5]], "outside": [0.5, 0.2],
               "regions": [{"name": "Wall top", "kind": "alert", "points": [[0.2, 0.45], [0.8, 0.45], [0.8, 0.55], [0.2, 0.55]]},
                           {"name": "Gate", "kind": "watch", "points": [[0.85, 0.6], [1, 0.6], [1, 1], [0.85, 1]]}]})
    box = lambda cx, fy: (cx - 20, fy - 100, 40, 100)
    got = [classify("person", box(500, 300), z, 1000, 1000), classify("car", box(500, 300), z, 1000, 1000),
           classify("person", box(500, 800), z, 1000, 1000), classify("person", box(500, 520), z, 1000, 1000),
           classify("person", box(900, 800), z, 1000, 1000)]
    want = [OUTSIDE_PERSON, OUTSIDE_OBJECT, None, BREACH, ZONE_PERSON]
    assert got == want, got
    assert describe("person", box(900, 800), z, 1000, 1000)["regions"] == ["Gate"]
    print("zones self-test ok")
