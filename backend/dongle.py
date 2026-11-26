"""Trinetra Perimeter — the 4G dongle on the Recordings page (Trinetra team only): signal strength, the traffic through it
right now, data used, and a speed test on demand — to place the dongle well at a site (USB extension cable, window,
high up) and to see why sending is slow.

No load on the Jetson and nothing in the background: everything is read only when the page asks (every 5 s while it is
open): the kernel's own byte counters of the dongle's network interface (no traffic), and the dongle's status from its
own web page (one small request, cached 5 s). The speed test runs only from its button (~7 MB of SIM data, lowest
priority, one at a time, at most once a minute) and only through the dongle.

Dongles read: HiMiLink "4G UFI" (the Ausha M4 kind: POST /himiapi/json, login DONGLE_USER / DONGLE_PASSWORD of the
settings file, default admin / admin) and Huawei HiLink (/api/device/signal). Others: counters + speed test only.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()
DRIVERS = ("rndis_host", "cdc_ether", "cdc_ncm")          # "network card" dongles (as trinetra-net)
SYS = Path("/sys/class/net")
_lock = threading.Lock()
_samples: list = []                                       # (time, iface, rx, tx): the speed right now, from the counters
_sig = {"at": 0.0, "data": None, "err": None, "session": None, "kind": None}
_speed = {"running": False, "last": None, "at": 0.0}


def _iface() -> Optional[str]:
    try:
        for d in sorted(SYS.iterdir()):
            drv = (d / "device" / "driver")
            if drv.exists() and drv.resolve().name in DRIVERS and "/usb" in str((d / "device").resolve()):
                return d.name
    except OSError:
        pass
    return None


def _routes() -> list:
    """default routes from the kernel: (iface, gateway, metric)"""
    out = []
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            f = line.split()
            if len(f) >= 7 and f[1] == "00000000" and f[7] == "00000000":
                gw = ".".join(str(int(f[2][i:i + 2], 16)) for i in (6, 4, 2, 0))
                out.append((f[0], gw, int(f[6])))
    except (OSError, ValueError, IndexError):
        pass
    return sorted(out, key=lambda r: r[2])


def _counter(iface: str, name: str) -> int:
    try:
        return int((SYS / iface / "statistics" / name).read_text())
    except (OSError, ValueError):
        return 0


def _post(url: str, body: dict, sess: Optional[str] = None) -> dict:
    if sess:
        body = {**body, "sessionId": sess}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json;charset=UTF-8", **({"Authorization": sess} if sess else {})})
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read(20000) or b"{}")


def _bars(dbm: Optional[int]) -> int:
    """the dongle's own steps: -95 dBm or better 4 bars … under -115 none"""
    if dbm is None or dbm > -44 or dbm < -120:
        return 0
    return 4 if dbm >= -95 else 3 if dbm >= -102 else 2 if dbm >= -108 else 1 if dbm >= -115 else 0


def _himilink(gw: str) -> Optional[dict]:
    url = f"http://{gw}/himiapi/json"
    user, pw = os.environ.get("DONGLE_USER", "admin"), os.environ.get("DONGLE_PASSWORD", "admin")
    for attempt in (1, 2):
        if not _sig["session"]:
            r = _post(url, {"cmdid": "login", "username": user, "password": pw})
            if r.get("reply") != "ok" or not r.get("session"):
                raise RuntimeError("the dongle's page refused the login: set DONGLE_PASSWORD in the settings file")
            _sig["session"] = r["session"]
            _sig["default_pw"] = pw == "admin" and user == "admin"
        st = _post(url, {"cmdid": "getallstatus"}, _sig["session"])
        if st.get("reply") == "ok":
            p = st.get("params") or {}
            info = {}
            try:
                s2 = _post(url, {"cmdid": "getsysinfo"}, _sig["session"])
                info = s2.get("params") or {} if s2.get("reply") == "ok" else {}
            except Exception:
                pass
            dbm = p.get("signalStrength")
            dbm = int(dbm) if isinstance(dbm, (int, float)) or (isinstance(dbm, str) and re.fullmatch(r"-?\d+", dbm)) else None
            return {"kind": "HiMiLink 4G UFI", "dbm": dbm, "operator": p.get("carrier"), "internet": p.get("internetState"),
                    "sim": p.get("simCardState"), "hotspot": p.get("wifiapstatus") == "on",
                    "temp_c": int(info["cputemp"]) if str(info.get("cputemp", "")).isdigit() else None,
                    "cell": info.get("cellid"), "default_password": _sig.get("default_pw", False)}
        _sig["session"] = None                            # SessionOut: log in again once
    return None


def _hilink(gw: str) -> Optional[dict]:
    base = f"http://{gw}"
    with urllib.request.urlopen(f"{base}/api/webserver/SesTokInfo", timeout=3) as r:
        x = r.read(5000).decode(errors="replace")
    ses, tok = re.search(r"<SesInfo>(.*?)</SesInfo>", x), re.search(r"<TokInfo>(.*?)</TokInfo>", x)
    if not ses:
        return None
    req = urllib.request.Request(f"{base}/api/device/signal", headers={"Cookie": ses.group(1), "__RequestVerificationToken": tok.group(1) if tok else ""})
    with urllib.request.urlopen(req, timeout=3) as r:
        x = r.read(10000).decode(errors="replace")
    num = lambda tag: (lambda m: int(m.group(1)) if m else None)(re.search(rf"<{tag}>\s*(-?\d+)", x))
    dbm = num("rsrp") if num("rsrp") is not None else num("rssi")
    return {"kind": "Huawei HiLink", "dbm": dbm, "rsrp": num("rsrp"), "sinr": num("sinr"), "rssi": num("rssi"),
            "operator": None, "internet": None, "sim": None, "hotspot": None, "temp_c": None, "default_password": False}


def _signal(gw: Optional[str]) -> tuple:
    if not gw:
        return None, "no address of the dongle"
    if time.time() - _sig["at"] < 5:
        return _sig["data"], _sig["err"]
    data, err = None, None
    for fn in ((_himilink, _hilink) if _sig["kind"] != "hilink" else (_hilink, _himilink)):
        try:
            data = fn(gw)
            if data:
                _sig["kind"] = "hilink" if fn is _hilink else "himilink"
                err = None
                break
        except RuntimeError as e:
            err = str(e)
            break
        except Exception:
            continue
    if not data and not err:
        err = "this dongle does not report its signal (its own page shows it)"
    if data:
        data["bars"] = _bars(data.get("dbm"))
        data["quality"] = ["none", "weak", "fair", "good", "excellent"][data["bars"]]
    _sig.update(at=time.time(), data=data, err=err)
    return data, err


@router.get("/api/dongle")
def api_dongle():
    with _lock:
        iface = _iface()
        routes = _routes()
        if not iface:
            _samples.clear()
            return {"present": False, "internet_via": routes[0][0] if routes else None}
        now, rx, tx = time.time(), _counter(iface, "rx_bytes"), _counter(iface, "tx_bytes")
        _samples.append((now, iface, rx, tx))
        while _samples and (now - _samples[0][0] > 30 or _samples[0][1] != iface):
            _samples.pop(0)
        old = max((s for s in _samples if now - s[0] >= 2), key=lambda s: s[0], default=None)   # newest sample ≥ 2 s old
        rate = None
        if old and rx >= old[2] and tx >= old[3]:
            dt = now - old[0]
            rate = {"down_mbit": round((rx - old[2]) * 8 / dt / 1e6, 2), "up_mbit": round((tx - old[3]) * 8 / dt / 1e6, 2)}
        gw = next((g for i, g, _ in routes if i == iface), None)
        sig, err = _signal(gw)
        return {"present": True, "iface": iface, "gateway": gw, "internet_via": routes[0][0] if routes else None,
                "dongle_is_internet": bool(routes) and routes[0][0] == iface, "now": rate,
                "used": {"down": rx, "up": tx}, "signal": sig, "signal_error": err,
                "speedtest": {"running": _speed["running"], "last": _speed["last"]}}


def _run_speedtest(iface: str):
    res = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "iface": iface}
    try:
        base = ["nice", "-n", "19", "curl", "-s", "-o", "/dev/null", "--interface", iface, "-m", "30"]
        d = subprocess.run(base + ["-w", "%{speed_download} %{size_download} %{time_connect}",
                                   "https://speed.cloudflare.com/__down?bytes=5000000"], capture_output=True, text=True, timeout=40)
        sp, size, conn = (d.stdout.split() + ["0", "0", "0"])[:3]
        res["down_mbit"] = round(float(sp) * 8 / 1e6, 2) if float(size) > 0 else None
        res["ping_ms"] = round(float(conn) * 1000) if float(conn) > 0 else None
        u = subprocess.run(base + ["-X", "POST", "--data-binary", "@-", "-w", "%{speed_upload} %{size_upload}",
                                   "https://speed.cloudflare.com/__up"], input=os.urandom(2_000_000), capture_output=True, timeout=40)
        sp, size = (u.stdout.decode().split() + ["0", "0"])[:2]
        res["up_mbit"] = round(float(sp) * 8 / 1e6, 2) if float(size) > 0 else None
        if res["down_mbit"] is None and res["up_mbit"] is None:
            res["error"] = "no answer through the dongle (no signal, no data on the SIM?)"
    except Exception as e:
        res["error"] = f"speed test failed: {e}"[:200]
    _speed.update(last=res, running=False)
    print(f"[dongle] speed test: {res}", flush=True)


@router.post("/api/dongle/speedtest")
def api_speedtest(request: Request):
    with _lock:
        iface = _iface()
        if not iface:
            raise HTTPException(409, "No 4G dongle plugged in")
        if _speed["running"]:
            raise HTTPException(409, "A speed test is running")
        if time.time() - _speed["at"] < 60:
            raise HTTPException(429, "One speed test a minute (each uses ~7 MB of SIM data)")
        _speed.update(running=True, at=time.time())
    threading.Thread(target=_run_speedtest, args=(iface,), daemon=True, name="dongle-speedtest").start()
    request.state.audit_target = iface
    return {"running": True}
