"""Which frames the perimeter pipeline saves as images (for the dashboard's alert rules) — nothing may be missed.

Per camera:
  · every class at most every `every` seconds while it is seen (the steady record that proves a presence goes on);
  · AT ONCE when
        – a new object appears: a tracking id that is not yet in a SAVED image (it stays "new" until a frame with it is
          saved), or
        – more objects of a kind are in a region (or outside the line) than in the last saved image (a crowd peak);
  · at most one image burst per `urgent_gap` seconds per camera — a frame with news that comes sooner is HELD (the
    pipeline keeps a copy of that very frame) and written as soon as the gap allows, with its own time: an object seen
    in a single frame right after a save is never lost. At most `max_held` frames wait per camera (the oldest goes
    when more come — only in a storm of new ids, e.g. a detector flickering).
An id unseen for `forget` seconds is forgotten (memory stays small; the same id much later counts as new).
File names carry milliseconds, so two images of the same second never collide.
"""
import collections


class SavePolicy:
    def __init__(self, every: float = 5.0, urgent_gap: float = 1.0, forget: float = 60.0, max_held: int = 5):
        self.every, self.urgent_gap, self.forget, self.max_held = every, urgent_gap, forget, max_held
        self.last_save = {}            # (camera, class) → time
        self.last_any = {}             # camera → time of its last save
        self.last_load = {}            # camera → {(kind, region): count} in its last saved image
        self.seen = {}                 # camera → {track: last time} of objects in a saved (or held) image
        self.gc = {}
        self.held = {}                 # camera → deque of held frames (the caller's payloads)

    @staticmethod
    def load(objs) -> collections.Counter:
        return collections.Counter((o["kind"], r) for o in objs
                                   for r in (o.get("regions") or ["*"]) + (["*outside"] if o.get("outside") else []))

    def _news(self, cam, objs, now) -> bool:
        seen = self.seen.setdefault(cam, {})
        for o in objs:
            if o.get("track") in seen:
                seen[o["track"]] = now
        if now - self.gc.get(cam, -1e9) > 30:
            self.gc[cam] = now
            for k in [k for k, t in seen.items() if now - t > self.forget]:
                seen.pop(k, None)
        new_obj = any(o.get("track") is not None and o["track"] not in seen for o in objs)
        peak = any(n > self.last_load.get(cam, {}).get(k, 0) for k, n in self.load(objs).items())
        return new_obj or peak

    def decide(self, cam: str, objs: list, now: float) -> tuple:
        """→ (classes to save now, hold): hold = this frame has news but must wait (the caller keeps a copy, see hold())"""
        if not objs:
            self.last_load[cam] = {}
            return [], False
        news = self._news(cam, objs, now)
        classes = list(dict.fromkeys(o["cls"] for o in objs))
        if news:
            if now - self.last_any.get(cam, -1e9) >= self.urgent_gap and not self.held.get(cam):
                return classes, False
            return [], True
        return [c for c in classes if now - self.last_save.get((cam, c), -1e9) >= self.every], False

    def hold(self, cam: str, objs: list, payload, now: float):
        """keep a frame with news until the gap allows it; its objects count as recorded (no second hold for them)"""
        q = self.held.setdefault(cam, collections.deque(maxlen=self.max_held))
        q.append((objs, payload))
        seen = self.seen.setdefault(cam, {})
        for o in objs:
            if o.get("track") is not None:
                seen[o["track"]] = now
        self.last_load[cam] = dict(self.load(objs))

    def release(self, cam: str, now: float):
        """the oldest held frame when the gap allows → (objs, payload, classes), else None"""
        q = self.held.get(cam)
        if q and now - self.last_any.get(cam, -1e9) >= self.urgent_gap:
            objs, payload = q.popleft()
            return objs, payload, list(dict.fromkeys(o["cls"] for o in objs))
        return None

    def saved(self, cam: str, objs: list, classes: list, now: float):
        """a frame was written as images of `classes`"""
        self.last_any[cam], self.last_load[cam] = now, dict(self.load(objs))
        for c in classes:
            self.last_save[(cam, c)] = now
        seen = self.seen.setdefault(cam, {})
        for o in objs:
            if o.get("track") is not None:
                seen[o["track"]] = now
