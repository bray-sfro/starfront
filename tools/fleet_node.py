"""One fake Starfront PC for check_fleet.py: the real server, fake devices.

    python tools/fleet_node.py --port P --token T [--triple] [--mount-delay S]

Opens LAN access with the fleet token the way a user would (POST
/api/settings/server from this PC), binds 0.0.0.0 like Starfront does with
lanAccess on, and installs a fake mount, and per telescope a fake camera,
wheel and focuser. Prints READY when it is up.
"""
import argparse
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--token", required=True)
parser.add_argument("--triple", action="store_true")
parser.add_argument("--mount-delay", type=float, default=0.0)
args = parser.parse_args()

TMP = Path(tempfile.mkdtemp(prefix="fleetnode-"))
os.environ["ASTRO_DATA_DIR"] = str(TMP / "data")
os.environ["HOME"] = str(TMP / "home")
os.environ["LOCALAPPDATA"] = str(TMP / "lad")

import httpx                                                        # noqa: E402
import uvicorn                                                      # noqa: E402

from astrocontrol import main                                      # noqa: E402
from fakes import FakeCamera, FakeFocuser, FakeMount, FakeWheel, install  # noqa: E402

server = uvicorn.Server(uvicorn.Config(main.app, host="0.0.0.0", port=args.port,
                                       log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
http = httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=30)
for _ in range(200):
    try:
        http.get("/api/status")
        break
    except Exception:                                               # noqa: BLE001
        time.sleep(0.1)
if args.triple:
    for name in ("RASA2", "RASA3"):
        http.post("/api/equipment/telescopes", json={"name": name})
reply = http.post("/api/settings/server", json={"lanAccess": True, "fleetToken": args.token,
                                                 "nodeName": "fake"})
assert reply.status_code == 200, reply.text
mount = install(main.rigs.master, "mount", FakeMount("mount"))
mount.delay = args.mount_delay
for i, rig in enumerate(main.rigs.all):
    install(rig, "camera", FakeCamera(f"cam-{rig.name}", seed=i))
    install(rig, "filterwheel", FakeWheel(f"wheel-{rig.name}"))
    focuser = install(rig, "focuser", FakeFocuser(f"foc-{rig.name}"))
    focuser.move_to(30000 + 1000 * i)
    q = "" if rig is main.rigs.master else f"?rig={rig.id}"
    http.post(f"/api/camera/expose{q}", json={"exposure": 0.05, "frameType": "light"})
time.sleep(1.0)
print("READY", flush=True)
while True:
    time.sleep(3600)
