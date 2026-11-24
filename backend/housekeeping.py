"""Trinetra Perimeter — disk housekeeping (the Workplace Analytics optimisations).

  · image retention  — camera images are kept TRINETRA_IMAGE_RETENTION_DAYS days (today counts as one; 0 = forever);
                       older days are deleted at night (01:00–05:59), the detections / alerts stay in the database
  · disk guard       — under TRINETRA_DISK_MIN_FREE_GB free, the OLDEST day of images goes first, one day at a time,
                       never today, at any hour
  · gentle deletion  — 1000 files, then a 0.2 s pause: a big delete never stalls the disk for the live dashboard
  · storage numbers  — images per day, the average day and the room left (Settings → Image retention)
  · database backup  — nightly SQLite online backup on this device (integrity checked, the last 7 kept)
"""
import os
import re
import shutil
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import config

_TS_RE = re.compile(r"_(\d{4})(\d{2})(\d{2})-\d{6}(?:-\d{3})?\.jpg$")


def image_day(p: Path) -> Optional[str]:
    """YYYY-MM-DD of a camera image: from its name (<camera>_<class>_<YYYYMMDD-HHMMSS>.jpg), else its file time"""
    m = _TS_RE.search(p.name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    try:
        return date.fromtimestamp(p.stat().st_mtime).isoformat()
    except OSError:
        return None


def image_days() -> dict:
    """{YYYY-MM-DD: [image paths]} of the archive"""
    out: dict = {}
    arch = Path(config.ARCHIVE_DIR)
    if not arch.is_dir():
        return out
    with os.scandir(arch) as it:
        for e in it:
            if e.is_file() and e.name.lower().endswith(".jpg"):
                p = Path(e.path)
                d = image_day(p)
                if d:
                    out.setdefault(d, []).append(p)
    return out


def trickle_delete(paths: list) -> int:
    """delete files 1000 at a time with a short pause between: the disk stays responsive"""
    n = 0
    for i, p in enumerate(paths, 1):
        try:
            p.unlink()
            n += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[retention] {p.name}: {e!r}")
        if i % 1000 == 0:
            time.sleep(0.2)
    return n


def free_gb() -> float:
    """free space of the disk the images are on (the nearest folder that exists: the archive is made on first use)"""
    p = Path(config.ARCHIVE_DIR)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 1e9
    except OSError:
        return 1e9


def disk_low() -> bool:
    return free_gb() < config.DISK_MIN_FREE_GB


def deletion_window(now: Optional[datetime] = None) -> bool:
    """old days are removed at night only (no deletes while people use the dashboard) — or at once when the disk is low"""
    return 1 <= (now or datetime.now()).hour < 6 or disk_low()


def retention(now: Optional[datetime] = None) -> int:
    """delete the images of days older than the days kept; returns the files removed"""
    keep = config.IMAGE_RETENTION_DAYS
    if keep <= 0 or not deletion_window(now):
        return 0
    cut = ((now or datetime.now()).date() - timedelta(days=keep - 1)).isoformat()
    old = [(d, ps) for d, ps in image_days().items() if d < cut]
    n = sum(trickle_delete(ps) for _, ps in sorted(old))
    if n:
        print(f"[retention] removed {n} image(s) of {len(old)} day(s) before {cut}: images kept {keep} days")
    return n


def disk_guard(now: Optional[datetime] = None) -> int:
    """the disk nearly full: delete the OLDEST day of images first, one day at a time, until there is room again —
    never today. Only images: the database stays."""
    n, today = 0, (now or datetime.now()).date().isoformat()
    while disk_low():
        days = image_days()
        older = sorted(d for d in days if d < today)
        if not older:
            break
        n += trickle_delete(days[older[0]])
        print(f"[disk-guard] {free_gb():.1f} GB free (< {config.DISK_MIN_FREE_GB:g} GB): removed the images of {older[0]}")
    return n


_STORAGE = [-1e9, {}]


def storage_stats() -> dict:
    """what the images take, the average day, and the room left before the disk guard at that rate"""
    if time.monotonic() - _STORAGE[0] > 600:            # measured at most every 10 minutes
        per = {}
        for d, ps in image_days().items():
            s = 0
            for p in ps:
                try:
                    s += p.stat().st_size
                except OSError:
                    pass
            per[d] = s
        _STORAGE[0], _STORAGE[1] = time.monotonic(), per
    per, today = _STORAGE[1], date.today().isoformat()
    past = [v for d, v in per.items() if d < today and v > 0] or [v for v in per.values() if v > 0]
    avg = sum(past) / len(past) / 1e9 if past else 0.0
    free = free_gb()
    return {"images_gb": round(sum(per.values()) / 1e9, 3), "days": len(per),
            "oldest_day": min(per) if per else None, "avg_gb_day": round(avg, 3) if avg else None,
            "free_gb": round(free, 1), "min_free_gb": config.DISK_MIN_FREE_GB,
            "room_days": int(max(0.0, free - config.DISK_MIN_FREE_GB) / avg) if avg else None,
            "keep_days": config.IMAGE_RETENTION_DAYS}


def refresh_storage():
    _STORAGE[0] = -1e9


# ─── nightly database backup (stays on this device) ───────────────────────────
def backup_dir() -> Path:
    return Path(config.DATA_DIR) / "backups"


def db_backup(db: Callable[[], sqlite3.Connection], now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    bdir = backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    dst = bdir / f"perimeter_{now:%Y%m%d}.db"
    tmp = dst.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    src, out = db(), sqlite3.connect(tmp)
    try:
        src.backup(out)                                  # consistent snapshot; writers are not blocked (WAL)
    finally:
        out.close()
        src.close()
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    chk = sqlite3.connect(tmp)
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        chk.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed its integrity check: {ok}")
    os.replace(tmp, dst)
    os.chmod(dst, 0o600)
    keep = max(1, config.BACKUP_KEEP)
    for old in sorted(bdir.glob("perimeter_*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    print(f"[backup] {dst.name}: {dst.stat().st_size // 1024} KB, integrity ok (keeping the last {keep})")
    return str(dst)


def backup_due(now: datetime, last: Optional[date]) -> bool:
    at = config.BACKUP_AT.strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        return False                                     # BACKUP_AT empty / invalid = off
    if now.strftime("%H:%M") < at or last == now.date():
        return False
    return not (backup_dir() / f"perimeter_{now:%Y%m%d}.db").exists()
