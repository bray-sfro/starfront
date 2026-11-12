"""The FSQ106 changes ported into the multi-scope tree in 0.2.49, checked on
one telescope and on three.

    python tools/check_fsq_port.py

No hardware: fake devices from tools/fakes.py, the FastAPI app driven through
its test client, a temp data folder.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
TMP = Path(tempfile.mkdtemp(prefix="fsq-port-"))
os.environ["ASTRO_DATA_DIR"] = str(TMP / "data")
os.environ["HOME"] = str(TMP / "home")
os.environ["LOCALAPPDATA"] = str(TMP / "lad")
CAPTURES = TMP / "captures"

import numpy as np                                            # noqa: E402
from fastapi.testclient import TestClient                     # noqa: E402

from astrocontrol import __version__, desktop, main           # noqa: E402
from astrocontrol.imaging import fits                         # noqa: E402
from astrocontrol.sequencer import Sequencer                  # noqa: E402
from fakes import FakeCamera, install                         # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


client = TestClient(main.app)
main.config.update("capture", {"rootDirectory": str(CAPTURES)})

# -- single scope ------------------------------------------------------------
print("-- one telescope --")
status = client.get("/api/status")
case("single scope: /api/status answers", status.status_code == 200
     and len(status.json()["rigs"]) == 1, f"{status.status_code}")
case("version is 0.2.49 or later", tuple(int("".join(ch for ch in part if ch.isdigit()) or 0) for part in __version__.split(".")) >= (0, 2, 49),
     __version__)

# 0001: the most recent night's subs come back into the list.
for night, count in (("2026-10-30", 2), ("2026-10-31", 3)):
    folder = CAPTURES / "M31" / night
    folder.mkdir(parents=True)
    for n in range(count):
        fits.write(folder / f"M31_{night}_{n}.fits", np.full((40, 60), 1000 + n, np.uint16),
                   {"IMAGETYP": "Light Frame", "EXPTIME": 300.0, "OBJECT": "M31",
                    "FILTER": "L", "DATE-OBS": f"{night}T23:0{n}:00"})
        time.sleep(0.01)
    fits.write(folder / "dark.fits", np.zeros((40, 60), np.uint16), {"IMAGETYP": "Dark Frame"})
master = main.rigs.master
recalled = master.capture.recall_recent()
images = client.get("/api/images").json()["images"]
case("0001: only the newest night's lights come back", recalled == 3 and len(images) == 3,
     f"recalled={recalled} listed={len(images)}")
stats = client.get(f"/api/images/{images[0]['id']}/stats")
case("0001: a recalled frame's statistics are worked out when looked at",
     stats.status_code == 200 and stats.json()["stats"], str(stats.status_code))
case("0001: a second recall adds nothing twice", master.capture.recall_recent() == 0)

# 0002: a sticky warning can be dismissed; an unknown one is a 404.
main.warning_board.raise_("solver.failing", "warning", "Plate solving keeps failing",
                          "", "", sticky=True)
board = main.warning_board.snapshot()
before = [w["key"] for w in board.get("active", board.get("warnings", []))] if isinstance(board, dict) else []
gone = client.post("/api/warnings/solver.failing/dismiss")
board = main.warning_board.snapshot()
after = [w["key"] for w in board.get("active", board.get("warnings", []))] if isinstance(board, dict) else []
case("0002: Dismiss takes a sticky warning off the board",
     gone.status_code == 200 and "solver.failing" in before and "solver.failing" not in after,
     f"before={before} after={after}")
case("0002: dismissing an unknown warning is a 404",
     client.post("/api/warnings/nope/dismiss").status_code == 404)

# 0004: the flip room with and without a limit, and a pause of 0 is 0.
room = Sequencer._flip_room
fake = type("S", (), {"_flip_minutes": staticmethod(Sequencer._flip_minutes)})()
r0 = room(fake, 1.0, {"flipPauseMinutes": 5, "flipLimitMinutes": 0})
r20 = room(fake, 1.0, {"flipPauseMinutes": 5, "flipLimitMinutes": 20})
rz = room(fake, 1.0, {"flipPauseMinutes": 0, "flipLimitMinutes": 0})
case("0004: no limit stops imaging the pause before transit", abs(r0 - (1 - 5 / 60)) < 1e-9)
case("0004: a 20 min limit lets imaging run 15 min past transit", abs(r20 - (1 + 15 / 60)) < 1e-9)
case("0004: a pause of 0 is 0, not the default 5", abs(rz - 1.0) < 1e-9)
case("0004: flipLimitMinutes is a setting the API takes",
     client.post("/api/settings/sequencer", json={"flipLimitMinutes": 12}).status_code == 200
     and float(main.config.get("sequencer", "flipLimitMinutes", 0)) == 12.0)

# 0005: the desktop port is remembered.
port = desktop.remembered_port()
desktop.remember_port(port)
case("0005: the port written down is the port asked for next time",
     desktop.remembered_port() == port and (TMP / "data" / "server.port").exists(), str(port))

# 0009: a frame taken without saving stays viewable as a reduced copy.
cam = install(master, "camera", FakeCamera("cam-main"))
cam.sensor_width, cam.sensor_height = 4000, 3000
master.capture.save_enabled = False
client.post("/api/camera/expose", json={"exposure": 0.01, "frameType": "light"})
time.sleep(0.2)
deadline = time.time() + 10
while time.time() < deadline and master.capture.busy:
    time.sleep(0.05)
unsaved = [i for i in master.capture.images if not i.saved]
for _ in range(master.capture.MAX_CACHED_FRAMES + 1):
    master.capture._cache.clear()
ok = bool(unsaved) and master.capture.viewable(unsaved[0].id)
frame, scale = master.capture.frame_for_view(unsaved[0].id) if ok else (None, 0)
case("0009: an unsaved frame out of the cache is drawn from its reduced copy",
     ok and scale == 3 and max(frame.shape) <= 1600, f"scale={scale}")
render = client.get(f"/api/images/{unsaved[0].id}/render.png?maxDim=800") if unsaved else None
case("0009: ...and the viewer can render it", render is not None and render.status_code == 200
     and int(render.headers.get("X-Render-Factor", "0")) >= 3,
     f"{render.status_code if render is not None else None} {render.headers.get('X-Render-Factor') if render is not None else ''} {render.text[:120] if render is not None and render.status_code != 200 else ''}")
listing = client.get("/api/images").json()["images"]
case("0009: every row in the list says whether it can be shown",
     all("viewable" in row for row in listing))
master.capture.save_enabled = True

with client.websocket_connect("/ws") as ws:
    first = ws.receive_json()
    second = ws.receive_json()
case("0009: the status socket sends the slow sections once, then only on change",
     all(k in first for k in ("events", "warnings")) and "warnings" not in second,
     f"second has {sorted(k for k in ('collab', 'events', 'warnings') if k in second)}")

# -- three telescopes ---------------------------------------------------------
print("-- three telescopes --")
for name in ("RASA2", "RASA3"):
    client.post("/api/equipment/telescopes", json={"name": name})
status = client.get("/api/status").json()
case("triple: three telescopes in the status", len(status["rigs"]) == 3,
     str([r["name"] for r in status["rigs"]]))
for rig in main.rigs.all:
    if rig is not master:
        install(rig, "camera", FakeCamera(f"cam-{rig.id}"))
ids = {}
for rig in main.rigs.all:
    rig.capture.start(0.01, "light")
deadline = time.time() + 15
while time.time() < deadline and any(r.capture.busy for r in main.rigs.all):
    time.sleep(0.05)
rows = client.get("/api/images").json()["images"]
by_rig = {}
for row in rows:
    by_rig.setdefault(row.get("rig"), []).append(row)
case("triple: each telescope's frame is listed under its own telescope",
     all(by_rig.get(r.id) for r in main.rigs.all), str({k: len(v) for k, v in by_rig.items()}))
other = next(r for r in main.rigs.all if r is not master)
newest = other.capture.images[0]
case("triple: a frame is found by id whichever telescope took it",
     client.get(f"/api/images/{newest.id}/stats").status_code == 200)
case("triple: a follower recalls from its own subfolder only",
     other.capture.subfolder is not None and other.capture.recall_recent() == 0,
     f"subfolder={other.capture.subfolder}")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
