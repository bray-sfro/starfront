"""0.2.50: the real server over a real socket keeps talking while devices are slow.

    python tools/check_live_link.py

Runs Starfront under uvicorn on a free port with fake devices, opens /ws the
way the page does, and checks that while a 5 s park runs and a mount status
read takes 3 s the socket still delivers a status at least every second, with
`seq` increasing and the park reported in `tasks`; then the same with three
telescopes, each taking frames, and each telescope's newest frame first in
its own image list.
"""

import asyncio
import json
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
TMP = Path(tempfile.mkdtemp(prefix="livelink-"))
os.environ["ASTRO_DATA_DIR"] = str(TMP / "data")
os.environ["HOME"] = str(TMP / "home")
os.environ["LOCALAPPDATA"] = str(TMP / "lad")

import httpx                                                        # noqa: E402
import uvicorn                                                      # noqa: E402
import websockets                                                   # noqa: E402

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
BASE = f"http://127.0.0.1:{PORT}"
for _ in range(100):
    try:
        httpx.get(BASE + "/api/status", timeout=5)
        break
    except Exception:                                               # noqa: BLE001
        time.sleep(0.1)
http = httpx.Client(base_url=BASE, timeout=30)


async def listen(seconds):
    """Every status the socket sends in `seconds`: (arrival, seq, tasks)."""
    got = []
    async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws", max_size=None) as ws:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.monotonic()))
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            got.append((time.monotonic(), msg.get("seq"), msg.get("tasks") or [], msg))
    return got


def gaps(got):
    times = [g[0] for g in got]
    return max((b - a for a, b in zip(times, times[1:])), default=99)


master = main.rigs.master
mount = install(master, "mount", FakeMount("mount"))
install(master, "camera", FakeCamera("cam"))
install(master, "focuser", FakeFocuser("foc"))
install(master, "filterwheel", FakeWheel("wheel"))

# --- one telescope ---------------------------------------------------------
mount.delay = 5.0
mount.status_delay = 3.0
answers = {}
threading.Thread(target=lambda: answers.update(park=http.post("/api/mount/park", json={}))).start()
got = asyncio.run(listen(6.0))
mount.status_delay = 0.0
seqs = [g[1] for g in got]
case("single: the socket keeps sending while the mount is slow and parking",
     len(got) >= 8 and gaps(got) < 1.0, f"{len(got)} messages, longest gap {gaps(got):.2f}s")
case("single: seq only goes up", all(b > a for a, b in zip(seqs, seqs[1:])), f"{seqs[:3]}…{seqs[-2:]}")
states = {t["state"] for g in got for t in g[2] if t["key"] == "mount:park"}
case("single: the park shows as running then done", {"running", "done"} <= states, str(states))
park = answers.get("park")
case("single: the park press answered as pending", park is not None and park.json().get("pending"),
     park.json() if park is not None else None)
stale = [g[3]["devices"]["mount"].get("stale") for g in got if g[3].get("devices", {}).get("mount")]
case("single: the slow mount was shown stale, not frozen", any(stale), f"{sum(map(bool, stale))}/{len(stale)} stale")

http.post("/api/camera/expose", json={"exposure": 0.01, "frameType": "light"})
time.sleep(1.5)
http.post("/api/camera/expose", json={"exposure": 0.01, "frameType": "light"})
time.sleep(1.5)
imgs = http.get("/api/images").json()["images"]
latest = http.get("/api/status").json()["capture"]["latestImageId"]
case("single: the newest frame is first in the list and is the latest",
     len(imgs) >= 2 and imgs[0]["id"] == latest, f"{len(imgs)} frames")

# --- three telescopes --------------------------------------------------------
for name in ("RASA2", "RASA3"):
    http.post("/api/equipment/telescopes", json={"name": name})
others = [r for r in main.rigs.all if r is not master]
for r in others:
    install(r, "camera", FakeCamera(f"cam-{r.id}"))
    f = install(r, "focuser", FakeFocuser(f"foc-{r.id}"))
    f.status_delay = 2.5 if r is others[0] else 0.0
case("triple: three telescopes", len(main.rigs.all) == 3, str(len(main.rigs.all)))
for r in main.rigs.all:
    q = "" if r is master else f"?rig={r.id}"
    http.post(f"/api/camera/expose{q}", json={"exposure": 0.01, "frameType": "light"})
got = asyncio.run(listen(4.0))
case("triple: one telescope's slow focuser does not slow the socket",
     len(got) >= 6 and gaps(got) < 1.0, f"{len(got)} messages, longest gap {gaps(got):.2f}s")
time.sleep(1.0)
status = http.get("/api/status").json()
ok = []
for r in main.rigs.all:
    q = "" if r is master else f"?rig={r.id}"
    rows = [x for x in http.get(f"/api/images{q}").json()["images"]
            if x.get("rig", r.id) == r.id]          # the master's list interleaves every telescope
    mine = next(x for x in status["rigs"] if x["id"] == r.id)["capture"]["latestImageId"]
    ok.append(bool(rows) and rows[0]["id"] == mine)
    if not ok[-1]:
        print("   ", r.name, "latest", mine, [(x["id"], x["timestamp"], x["filename"]) for x in rows[:4]])
case("triple: each telescope's newest frame heads its own list", all(ok), str(ok))

server.should_exit = True
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
