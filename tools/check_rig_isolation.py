"""Three telescopes driven at once over the real server: nothing crosses.

    python tools/check_rig_isolation.py

Starts Starfront under uvicorn with three telescopes of fake devices (the
master owns the mount), then from parallel threads, all at once: each
telescope changes to its own filter, moves its own focuser to its own
position and takes a saved light frame, while the mount slews. Then checks
every telescope's state, its frames, their FITS-side records (filter,
focuser position, telescope) and the folders they were written to, and that
no telescope's frame or device state shows up on another.
"""

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
TMP = Path(tempfile.mkdtemp(prefix="rigiso-"))
os.environ["ASTRO_DATA_DIR"] = str(TMP / "data")
os.environ["HOME"] = str(TMP / "home")
os.environ["LOCALAPPDATA"] = str(TMP / "lad")

import httpx                                                        # noqa: E402
import uvicorn                                                      # noqa: E402

from astrocontrol import main                                      # noqa: E402
from fakes import FakeCamera, FakeFocuser, FakeMount, FakeWheel, install  # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))


with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
http = httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=30)
for _ in range(100):
    try:
        http.get("/api/status")
        break
    except Exception:                                               # noqa: BLE001
        time.sleep(0.1)

for name in ("RASA2", "RASA3"):
    http.post("/api/equipment/telescopes", json={"name": name})
rigs = list(main.rigs.all)
master = main.rigs.master
case("three telescopes, one master", len(rigs) == 3 and rigs[0] is master, [r.name for r in rigs])

mount = install(master, "mount", FakeMount("mount"))
mount.slew_seconds = 1.0
fakes = {}
for i, r in enumerate(rigs):
    fakes[r.id] = {
        "camera": install(r, "camera", FakeCamera(f"cam-{r.name}", seed=i)),
        "focuser": install(r, "focuser", FakeFocuser(f"foc-{r.name}")),
        "filterwheel": install(r, "filterwheel", FakeWheel(f"wheel-{r.name}")),
    }
    fakes[r.id]["camera"].delay = 0.3            # every driver a little slow, so the calls overlap
    fakes[r.id]["filterwheel"].delay = 0.5
    fakes[r.id]["focuser"].delay = 0.3

OUT = TMP / "out"
q = lambda r: "" if r is master else f"?rig={r.id}"                # noqa: E731
for r in rigs:
    http.post(f"/api/capture/output{q(r)}", json={"save": True, "directory": str(OUT), "target": "M42"})

WANT = {r.id: {"filter": i + 1, "focus": 31000 + 1000 * i} for i, r in enumerate(rigs)}
errors = []


def drive(r):
    try:
        c = httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=30)
        for path, body in ((f"/api/filterwheel/position{q(r)}", {"position": WANT[r.id]["filter"]}),
                           (f"/api/focuser/move{q(r)}", {"position": WANT[r.id]["focus"]})):
            res = c.post(path, json=body)
            if res.status_code != 200:
                errors.append(f"{r.name} {path}: {res.status_code} {res.text}")
        time.sleep(0.5)                                             # let the moves land
        res = c.post(f"/api/camera/expose{q(r)}", json={"exposure": 0.05, "frameType": "light"})
        if res.status_code != 200:
            errors.append(f"{r.name} expose: {res.status_code} {res.text}")
    except Exception as exc:                                        # noqa: BLE001
        errors.append(f"{r.name}: {exc}")


threads = [threading.Thread(target=drive, args=(r,)) for r in rigs]


def slew():
    x = httpx.post(f"http://127.0.0.1:{PORT}/api/mount/slew", json={"ra": 7.5, "dec": -12.0}, timeout=30)
    if x.status_code != 200:
        errors.append(f"slew {x.status_code} {x.text}")


threads.append(threading.Thread(target=slew))
t0 = time.monotonic()
for t in threads:
    t.start()
for t in threads:
    t.join()
case("every command on every telescope was accepted", not errors, errors or f"{time.monotonic() - t0:.1f}s")

deadline = time.monotonic() + 15
while time.monotonic() < deadline:
    st = http.get("/api/status").json()
    if all((x["capture"] or {}).get("latestImageId") and x["capture"].get("state") == "idle" for x in st["rigs"]):
        break
    time.sleep(0.2)
st = http.get("/api/status").json()
by_id = {x["id"]: x for x in st["rigs"]}

for r in rigs:
    live = by_id[r.id]["devices"]
    case(f"{r.name}: its wheel is on its own filter", live["filterwheel"].get("position") == WANT[r.id]["filter"],
         live["filterwheel"].get("position"))
    case(f"{r.name}: its focuser is at its own position", live["focuser"].get("position") == WANT[r.id]["focus"],
         live["focuser"].get("position"))
case("the mount is the master's alone",
     (by_id[master.id]["devices"].get("mount") or {}).get("connected")
     and not any((by_id[r.id]["devices"].get("mount") or {}).get("connected") for r in rigs if r is not master),
     {r.name: bool((by_id[r.id]["devices"].get("mount") or {}).get("connected")) for r in rigs})
case("the slew went to the mount", abs(mount._ra - 7.5) < 1e-6 and abs(mount._dec + 12.0) < 1e-6,
     (mount._ra, mount._dec))
case("each camera took exactly one frame", [fakes[r.id]["camera"].exposures for r in rigs] == [1, 1, 1],
     [fakes[r.id]["camera"].exposures for r in rigs])

lists = {r.id: http.get(f"/api/images?rig={r.id}").json()["images"] for r in rigs}
ids = {r.id: {row["id"] for row in lists[r.id]} for r in rigs}
case("each telescope lists only its own frame", all(len(ids[r.id]) == 1 for r in rigs)
     and len(set().union(*ids.values())) == 3, {r.name: len(ids[r.id]) for r in rigs})
merged = http.get("/api/images").json()["images"]
case("the master's list interleaves all three, each tagged with its telescope",
     {row["rig"] for row in merged} == {r.id for r in rigs} and len(merged) == 3, [row.get("rig") for row in merged])

folders = {}
for i, r in enumerate(rigs):
    row = lists[r.id][0]
    rec = http.get(f"/api/images/{row['id']}/stats").json()["image"]
    names = fakes[r.id]["filterwheel"].names
    case(f"{r.name}: its frame records its own filter and focuser",
         rec["filter"] == names[WANT[r.id]["filter"]] and rec["focuser_position"] == WANT[r.id]["focus"],
         (rec["filter"], rec["focuser_position"]))
    path = Path(rec["path"] or "")
    folders[r.id] = path.parent
    case(f"{r.name}: its frame is on disk under the output folder", path.is_file() and OUT in path.parents,
         str(path))
    sub = r.capture.subfolder
    case(f"{r.name}: in its own telescope subfolder", bool(sub) and sub in path.parts, f"{sub} in {path}")
case("three telescopes, three different folders", len(set(folders.values())) == 3,
     sorted({str(p.relative_to(OUT)) for p in folders.values()}))
on_disk = sorted(p.relative_to(OUT).as_posix() for p in OUT.rglob("*.fits"))
case("exactly three FITS files written", len(on_disk) == 3, on_disk)

server.should_exit = True
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
