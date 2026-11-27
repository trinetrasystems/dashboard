"""Perimeter zones of one camera, drawn on the DeepStream page (Perimeter zones) or the Alerts page (Regions), and what a
detection means there.

All points are fractions of the frame (0..1, x right, y down), so they hold for any stream resolution.

    {"line": [[x, y], ...],           # the perimeter line (wall / fence top), a polyline of 2+ points
     "outside": [x, y],               # a point on the OUTSIDE of the line (the inside is the other side)
     "regions": [{"name": "Wall top", "kind": "alert",  "points": [[x, y], ...]},   # anything here = breach
                 {"name": "Gate A",   "kind": "watch",  "points": [...]},           # the client's alert rules decide
                 {"name": "Near wall","kind": "near",   "points": [...]},           # a watch region the throw / loitering
                                                                                    # logic also uses (person near the wall)
                 {"name": "Road",     "kind": "ignore", "points": [...]},           # never reported
                 {"name": "Band 1",   "kind": "band",   "points": [...]},           # throw band: the strip along the wall
                                                                                    # top watched for thrown objects (motion,
                                                                                    # every frame); needs the perimeter line
                 {"name": "Landing 1","kind": "landing","points": [...]}]}          # landing zone: the ground inside where a
                                                                                    # thrown / left object is looked for
(PERIMETER_THROW_DETECTION_SPEC: wall_top_line / fence_crossing_line = "line", inside_side_point = the side opposite
"outside", wall_top_band = a band region, near_wall_zone = a near region, landing_zone = a landing region.)
Band and landing regions are drawn by the Trinetra team only (DeepStream page) and are not person / object regions:
describe() does not look at them.

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
KINDS = ("alert", "watch", "near", "ignore", "band", "landing")
DETECTION_KINDS = ("band", "landing")                 # for the throw / landing detection, not for person / object rules
TEAM_KINDS = ("near", "band", "landing")              # drawn on the DeepStream page only (the client's editor keeps them)
MAX_PER_KIND = {"band": 4, "landing": 4}
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
    names, alert, near = [], lab == BREACH.lower(), False
    for r in regions(zone):
        if r["kind"] == "ignore" or r["kind"] in DETECTION_KINDS:
            continue
        inside = in_polygon(foot, r["points"]) or (r["kind"] == "alert" and in_polygon(centre, r["points"]))
        if inside:
            names.append(r["name"])
            alert = alert or r["kind"] == "alert"
            near = near or r["kind"] == "near"
    outside = lab in (OUTSIDE_PERSON, OUTSIDE_OBJECT) or is_outside(foot, zone)
    if not (names or alert or outside):
        return None
    kind = "person" if lab == OUTSIDE_PERSON else ("object" if lab in (OUTSIDE_OBJECT, BREACH.lower()) else target_of(lab))
    return {"kind": kind, "regions": names, "alert": alert, "outside": outside, "near": near}


CROSSED_REGION = "Perimeter line"       # the region name a crossing is reported in (alert text: "… in Perimeter line")


class LineCrossing:
    """Person crossed the wall (spec §7.2 crossed_line / §7.6 person_crossed_fence): a tracked person whose foot point
    was OUTSIDE the perimeter line for `need` observations and then INSIDE for `need` observations has crossed — once per
    track. `need` > 1 so a box wobbling on the line (detector jitter) is not a crossing. Tracks not seen for `forget_s`
    are dropped (memory stays bounded)."""

    def __init__(self, need: int = 3, forget_s: float = 10.0):
        self.need, self.forget_s = need, forget_s
        self.t = {}                                   # (camera, track) → [outside_run, inside_run_after, crossed, last]

    def update(self, cam: str, track, foot, zone, now: float) -> bool:
        """foot = (x, y) as fractions of the frame → True exactly once, on the observation that completes a crossing"""
        if track is None or len(zone.get("line") or []) < 2 or not zone.get("outside"):
            return False
        k = (cam, track)
        st = self.t.get(k) or [0, 0, False, now]
        st[3] = now
        if is_outside(foot, zone):
            if st[1]:                                 # back outside before the crossing completed: start again
                st[0], st[1] = 0, 0
            st[0] += 1
        elif st[0] >= self.need and not st[2]:
            st[1] += 1
            if st[1] >= self.need:
                st[2] = True
                self.t[k] = st
                return True
        else:
            st[0] = 0                                 # inside without a full outside run first: not a crossing
        self.t[k] = st
        return False

    def prune(self, now: float):
        for k in [k for k, v in self.t.items() if now - v[3] > self.forget_s]:
            del self.t[k]


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


def _segments_cross(a, b, c, d) -> bool:
    s1, s2 = _side_of_segment(c, a, b), _side_of_segment(d, a, b)
    s3, s4 = _side_of_segment(a, c, d), _side_of_segment(b, c, d)
    return s1 * s2 < 0 and s3 * s4 < 0


def line_meets(poly, line) -> bool:
    """the polyline passes through the polygon (a point inside, or a segment crossing an edge)"""
    if any(in_polygon(_pt(p), poly) for p in line):
        return True
    edges = [(_pt(poly[i]), _pt(poly[(i + 1) % len(poly)])) for i in range(len(poly))]
    return any(_segments_cross(_pt(line[i]), _pt(line[i + 1]), a, b) for i in range(len(line) - 1) for a, b in edges)


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
            raise ValueError("a region is an alert, watch, near-wall, ignore region, a throw band or a landing zone")
        if not NAME_RE.match(name):
            raise ValueError(f"region name “{name[:40]}”: 1–40 letters, numbers, spaces, dots, dashes")
        if name.lower() in seen:
            raise ValueError(f"two regions are named {name}")
        seen.add(name.lower())
        pts = [pt(p) for p in r.get("points") or []]
        if not 3 <= len(pts) <= 50:
            raise ValueError(f"region {name} needs 3–50 corners")
        rs.append({"name": name, "kind": kind, "points": pts})
    for kind, n in MAX_PER_KIND.items():
        if sum(r["kind"] == kind for r in rs) > n:
            raise ValueError(f"at most {n} {'throw bands' if kind == 'band' else 'landing zones'} per camera")
    for r in rs:
        if r["kind"] == "band":
            if len(line) < 2:
                raise ValueError(f"throw band {r['name']}: draw the perimeter line (the wall top) and its outside side first "
                                 "— a throw is an object crossing it from outside to inside")
            if not line_meets(r["points"], line):
                raise ValueError(f"throw band {r['name']}: the perimeter line must run through the band (the band is the "
                                 "strip just above and below the wall top)")
    return {"line": line, "outside": out, "regions": rs}


def keep_team_regions(new: dict, old: dict) -> dict:
    """a zone saved from the client's editor (Alerts → Regions): the team's regions (near / band / landing) stay as they
    were — the client can neither add nor remove them"""
    mine = [r for r in (new.get("regions") or []) if isinstance(r, dict) and r.get("kind") not in TEAM_KINDS]
    team = [r for r in regions(old or {}) if r["kind"] in TEAM_KINDS]
    return {**new, "regions": mine + team}


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
    band = {"name": "Band 1", "kind": "band", "points": [[0.1, 0.4], [0.9, 0.4], [0.9, 0.6], [0.1, 0.6]]}
    land = {"name": "Landing 1", "kind": "landing", "points": [[0.1, 0.6], [0.9, 0.6], [0.9, 0.95], [0.1, 0.95]]}
    near = {"name": "Near wall", "kind": "near", "points": [[0, 0.5], [1, 0.5], [1, 0.75], [0, 0.75]]}
    z2 = clean({**z, "regions": z["regions"] + [band, land, near]})
    assert describe("person", box(500, 800), z2, 1000, 1000) is None            # landing / band: not person regions
    assert describe("person", box(500, 700), z2, 1000, 1000)["regions"] == ["Near wall"]
    for bad in ({"line": [], "outside": None, "regions": [band]},                # a band needs the line
                {**z, "regions": [{**band, "points": [[0.1, 0.7], [0.9, 0.7], [0.9, 0.8], [0.1, 0.8]]}]},   # line not in it
                {**z, "regions": [{**band, "name": f"B{i}"} for i in range(5)]}):
        try:
            clean(bad)
            raise AssertionError("not refused: %r" % bad)
        except ValueError:
            pass
    kept = keep_team_regions({**z, "regions": [z["regions"][0], {**band, "name": "Client band"}]}, z2)
    assert [r["name"] for r in kept["regions"]] == ["Wall top", "Band 1", "Landing 1", "Near wall"], kept
    d = describe("person", box(500, 700), z2, 1000, 1000)
    assert d["near"] and not describe("person", box(900, 800), z2, 1000, 1000)["near"]   # near-wall flag
    # crossing: outside 3x, then inside 3x → one crossing; jitter on the line is none; inside first is none
    lc = LineCrossing(need=3)
    o, i = (0.5, 0.3), (0.5, 0.7)
    seq = [lc.update("c", 7, p, z, t) for t, p in enumerate([o, o, o, i, i, i, i, i])]
    assert seq == [False] * 5 + [True, False, False], seq
    lc = LineCrossing(need=3)                                                   # wobbling on the line: never
    assert not any(lc.update("c", 8, p, z, t) for t, p in enumerate([o, i, o, i, o, i, i, o, i]))
    lc = LineCrossing(need=3)                                                   # inside → outside (leaving): never
    assert not any(lc.update("c", 9, p, z, t) for t, p in enumerate([i, i, i, o, o, o, o]))
    lc = LineCrossing(need=3)                                                   # outside, back out half-way, then in
    assert [lc.update("c", 1, p, z, t) for t, p in enumerate([o, o, o, i, o, o, o, i, i, i])][-1] is True
    assert not lc.update("c", None, i, z, 0) and not LineCrossing().update("c", 1, i, {"line": []}, 0)   # no track / line
    lc.prune(100.0); assert not lc.t
    print("zones self-test ok")
