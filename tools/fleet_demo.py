"""The fleet dashboard fed by made-up Starfront PCs: for a look, a screenshot,
or the page check.

    python tools/fleet_demo.py                      # open http://127.0.0.1:8790/
    python tools/fleet_demo.py --screenshot out.png # headless Chrome, then exit

Each made-up PC answers /api/status, /api/images, /api/fleet/node and the
render.png thumbnails with canned values shaped exactly like Starfront's own
(a triple RASA imaging with one telescope recovering, an FSQ106 imaging and
guiding, one PC parked and idle, one PC switched off), and the real fleet
dashboard polls them over HTTP like it would the real PCs.
"""
from __future__ import annotations

import argparse
import math
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np                                                  # noqa: E402
import uvicorn                                                      # noqa: E402
from fastapi import FastAPI, Request                                # noqa: E402
from fastapi.responses import JSONResponse, Response                # noqa: E402

from astrocontrol.fleet.config import normalise                     # noqa: E402
from astrocontrol.fleet.core import Fleet                           # noqa: E402
from astrocontrol.fleet.server import create_app                    # noqa: E402
from astrocontrol.piercam import _to_png                            # noqa: E402

TOKEN = "demo-fleet-token-0123456789"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def starfield(seed: int, w: int = 360, h: int = 240, nebula: bool = False) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = rng.normal(900, 25, (h, w))
    yy, xx = np.mgrid[0:h, 0:w]
    if nebula:
        cx, cy = w * 0.55, h * 0.5
        img += 900 * np.exp(-(((xx - cx) / 70) ** 2 + ((yy - cy) / 40) ** 2))
        img += 400 * np.exp(-(((xx - cx + 40) / 25) ** 2 + ((yy - cy - 20) / 50) ** 2))
    for _ in range(140):
        x, y = rng.uniform(0, w), rng.uniform(0, h)
        amp = rng.pareto(1.6) * 800 + 300
        img += amp * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / 2.2)
    # Already stretched, the way the real render.png would show a light frame.
    stretched = np.arcsinh(np.clip(img - 860.0, 0, None) / 30.0) / math.asinh(2500.0 / 30.0)
    return np.clip(stretched * 65535.0, 0, 65535).astype(np.uint16)


def mount(ra, dec, *, parked=False, tracking=True, slewing=False, pier="pierEast"):
    return {"connected": True, "ra": ra, "dec": dec, "atPark": parked, "tracking": tracking,
            "slewing": slewing, "sideOfPier": pier}


def camera(temp, setpoint=-10.0, power=38, exposing=True):
    return {"connected": True, "exposing": exposing, "temperature": temp, "setpoint": setpoint,
            "coolerOn": True, "coolerPower": power}


def guider(rms):
    return {"connected": True, "state": "Guiding", "guiding": True, "rmsTotal": rms,
            "rmsRa": rms * 0.7, "rmsDec": rms * 0.7, "rmsTotalPx": rms / 1.2}


def rig(rid, name, role, devices, *, target="", filt="L", busy=True, state="exposing"):
    return {"id": rid, "name": name, "role": role, "kinds": list(devices),
            "devices": devices, "capture": {"state": state, "busy": busy, "target": target,
                                            "filter": filt, "remaining": 112 if busy else None}}


def scenario(now: float) -> dict[str, dict]:
    triple_rigs = [
        rig("main", "RASA1", "master", {
            "mount": mount(5.5881, -5.391), "camera": camera(-10.0, power=41),
            "filterwheel": {"connected": True, "currentName": "Ha"},
            "focuser": {"connected": True, "position": 30412, "temperature": 6.4},
            "guider": guider(0.62)}, target="M42", filt="Ha"),
        rig("r2", "RASA2", "slave", {
            "camera": camera(-10.1, power=44), "filterwheel": {"connected": True, "currentName": "OIII"},
            "focuser": {"connected": True, "position": 31877, "temperature": 6.2}}, target="M42", filt="OIII"),
        rig("r3", "RASA3", "slave", {
            "camera": {"connected": False}, "filterwheel": {"connected": True, "currentName": "SII"},
            "focuser": {"connected": True, "position": 29950, "temperature": 6.3}}, target="M42",
            filt="SII", busy=False, state="idle"),
    ]
    triple = {
        "rigs": triple_rigs, "masterRig": "main", "serverTime": now, "tasks": [],
        "warnings": {"active": [{"key": "camera.hung", "level": "critical",
                                 "title": "RASA3: camera stopped answering"}]},
        "sequence": {
            "running": True, "paused": False, "state": "imaging", "message": "Shooting M42",
            "error": None, "watchdog": {"state": "ok"}, "panel": 1, "panels": 1, "slot": 2, "slots": 3,
            "framesDone": {"a": 57},
            "rigRecovery": {"r3": {"rigId": "r3", "name": "RASA3", "state": "recovering",
                                   "reason": "camera did not finish its download",
                                   "step": "reconnecting the camera", "attempt": 2,
                                   "attempts": 6, "since": now - 95, "downSince": now - 95}},
            "telescopes": {"main": {"frame": 19, "frames": 40, "filter": "Ha", "state": "exposing"},
                           "r2": {"frame": 21, "frames": 40, "filter": "OIII", "state": "exposing"},
                           "r3": {"frame": 17, "frames": 40, "filter": "SII", "state": "recovering"}},
        },
    }
    fsq = {
        "rigs": [rig("main", "FSQ106", "master", {
            "mount": mount(20.6886, 30.712, pier="pierWest"), "camera": camera(-15.0, -15.0, 52),
            "filterwheel": {"connected": True, "currentName": "R"},
            "focuser": {"connected": True, "position": 6120, "temperature": 5.8},
            "guider": guider(0.48)}, target="NGC 6992 Veil East", filt="R")],
        "masterRig": "main", "serverTime": now, "tasks": [], "warnings": {"active": []},
        "sequence": {"running": True, "paused": False, "state": "imaging",
                     "message": "Shooting NGC 6992 Veil East", "error": None,
                     "watchdog": {"state": "ok"}, "panel": 2, "panels": 4, "slot": 1, "slots": 4,
                     "framesDone": {"a": 88}, "rigRecovery": {},
                     "telescopes": {"main": {"frame": 8, "frames": 30, "filter": "R",
                                             "state": "exposing"}}},
    }
    eagle = {
        "rigs": [rig("main", "Eagle rig", "master", {
            "mount": mount(0.0, 90.0, parked=True, tracking=False),
            "camera": camera(12.3, -10.0, 0, exposing=False) | {"coolerOn": False},
            "filterwheel": {"connected": True, "currentName": "L"},
            "focuser": {"connected": True, "position": 14250}}, busy=False, state="idle")],
        "masterRig": "main", "serverTime": now, "tasks": [], "warnings": {"active": []},
        "sequence": {"running": False, "paused": False, "state": "idle", "message": "",
                     "error": None, "watchdog": {"state": "idle"}, "rigRecovery": {},
                     "telescopes": {}},
    }
    return {"DESKTOP-4PILSFA": triple, "WIN-Q9DV8QD504V": fsq, "EAGLE4PRO0258": eagle}


IMAGES = {
    "DESKTOP-4PILSFA": [("a1", "main", "Ha", 300, 140, True), ("a2", "r2", "OIII", 300, 95, True),
                        ("a3", "r3", "SII", 300, 610, True)],
    "WIN-Q9DV8QD504V": [("b1", "main", "R", 180, 60, False)],
    "EAGLE4PRO0258": [("c1", "main", "L", 120, 9 * 3600, False)],
}


def node_app(name: str) -> FastAPI:
    app = FastAPI()
    pngs: dict[str, bytes] = {}

    @app.middleware("http")
    async def token(request: Request, call_next):
        if request.headers.get("x-fleet-token") != TOKEN:
            return JSONResponse(status_code=403, content={"detail": "token"})
        return await call_next(request)

    @app.get("/api/status")
    def status():
        return scenario(time.time())[name]

    @app.get("/api/fleet/node")
    def info():
        return {"version": "0.2.52", "hostname": name, "name": name}

    @app.get("/api/images")
    def images(limit: int = 40):
        now = time.time()
        return {"images": [{"id": i, "rig": r, "filter": f, "exposure": e, "timestamp": now - age,
                            "filename": f"LIGHT_{f}_{e}s.fits"} for i, r, f, e, age, _ in IMAGES[name]]}

    @app.get("/api/images/{image_id}/render.png")
    def render(image_id: str, maxDim: int = 360):            # noqa: N803
        if image_id not in pngs:
            nebula = next((n for i, *_, n in IMAGES[name] if i == image_id), False)
            pngs[image_id] = _to_png(starfield(hash(image_id) % 1000, nebula=nebula), maxDim, 1.0)[0]
        return Response(pngs[image_id], media_type="image/png")

    for action in ("sequence/start", "sequence/pause", "sequence/resume", "sequence/stop",
                   "camera/abort", "mount/park", "mount/unpark", "mount/slew"):
        app.add_api_route(f"/api/{action}", lambda: {"ok": True}, methods=["POST"])
    return app


def start_demo(port: int | None = None):
    nodes = []
    for name in ("DESKTOP-4PILSFA", "WIN-Q9DV8QD504V", "DESKTOP-F7DOP99", "EAGLE4PRO0258"):
        p = free_port()
        nodes.append({"name": name, "host": "127.0.0.1", "port": p})
        if name == "DESKTOP-F7DOP99":
            continue                       # switched off: nothing listens there
        server = uvicorn.Server(uvicorn.Config(node_app(name), host="127.0.0.1", port=p,
                                               log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
    nodes[0]["note"] = "triple RASA"
    nodes[1]["note"] = "FSQ106"
    settings = normalise({"token": TOKEN, "nodes": nodes, "pollSeconds": 1.0,
                          "timeoutSeconds": 1.5, "offlineAlertMinutes": 0.0,
                          "recoveringAlertMinutes": 1.0})
    fleet = Fleet(settings)
    fleet.start()
    # The switched-off PC has "been off" for a while in the picture.
    off = fleet.nodes["DESKTOP-F7DOP99"]
    off.down_since = time.time() - 14 * 60
    port = port or free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(fleet), host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(3.0)
    return fleet, f"http://127.0.0.1:{port}/"


def shoot(chrome: str, url: str, out: str, width: int = 1500, settle: float = 5.0) -> None:
    """Headless Chrome over the DevTools protocol (its --screenshot switch
    hangs in some containers): load the page, let it poll, save a full-page PNG."""
    import base64
    import json
    import os
    import signal

    import httpx
    from websockets.sync.client import connect

    port = free_port()
    profile = tempfile.mkdtemp(prefix="fleetshot-")
    proc = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                             "--hide-scrollbars", f"--user-data-dir={profile}",
                             f"--remote-debugging-port={port}", f"--window-size={width},1000",
                             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        target = None
        for _ in range(100):
            try:
                pages = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=2).json()
                target = next(p for p in pages if p.get("type") == "page")
                break
            except Exception:                                       # noqa: BLE001
                time.sleep(0.2)
        if target is None:
            raise RuntimeError("Chrome did not start")
        with connect(target["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            ids = iter(range(1, 10**6))

            def call(method, **params):
                mid = next(ids)
                ws.send(json.dumps({"id": mid, "method": method, "params": params}))
                while True:
                    msg = json.loads(ws.recv(timeout=60))
                    if msg.get("id") == mid:
                        return msg.get("result", {})

            call("Emulation.setDeviceMetricsOverride", width=width, height=1000,
                 deviceScaleFactor=1, mobile=False)
            call("Page.enable")
            call("Page.navigate", url=url)
            time.sleep(settle)
            metrics = call("Page.getLayoutMetrics")
            height = int(math.ceil(metrics.get("cssContentSize", {}).get("height", 1000)))
            call("Emulation.setDeviceMetricsOverride", width=width, height=max(600, height),
                 deviceScaleFactor=1, mobile=False)
            time.sleep(0.5)
            shot = call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        Path(out).write_bytes(base64.b64decode(shot["data"]))
    finally:
        with __import__("contextlib").suppress(Exception):
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    fleet, url = start_demo(None if args.screenshot else args.port)
    if not args.screenshot:
        print(f"demo fleet on {url}  (Ctrl+C to stop)")
        while True:
            time.sleep(3600)
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        print("SKIP  no headless Chrome")
        return
    shoot(chrome, url, args.screenshot)
    print(f"wrote {args.screenshot}")


if __name__ == "__main__":
    main()
