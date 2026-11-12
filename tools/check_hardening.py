"""0.2.50 stability: the status keeps flowing, presses come back, frames follow.

    python tools/check_hardening.py

Each case is a reproduction of something Bray saw on the rig:
  H1  a slow or busy driver froze the whole status (every telescope) for as long
      as one property read took
  H2  one section of the status that raised killed /api/status and the socket
  H3  the page could not tell a live status from a stale one
  H4  a long command (park, a blocking slew, a filter change) held its button,
      and a server thread, for the whole move; a second press queued another
  H8  two quick presses of Capture could start two exposures on one camera
The page-side cases (H5 fetch timeout, H6/H7 the viewer following the newest
frame) are in tools/check_viewer.py, which runs app.js under node.

Fake devices (tools/fakes.py), the FastAPI app through its test client, one
telescope first and then three.
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
TMP = Path(tempfile.mkdtemp(prefix="hardening-"))
os.environ["ASTRO_DATA_DIR"] = str(TMP / "data")
os.environ["HOME"] = str(TMP / "home")
os.environ["LOCALAPPDATA"] = str(TMP / "lad")

from fastapi.testclient import TestClient                          # noqa: E402

from astrocontrol import main                                      # noqa: E402
from astrocontrol.devices.base import DeviceError                  # noqa: E402
from fakes import FakeCamera, FakeFocuser, FakeMount, FakeWheel, install  # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


def timed(fn, *a, **k):
    t = time.monotonic()
    out = fn(*a, **k)
    return out, time.monotonic() - t


def wait_for(pred, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


client = TestClient(main.app, raise_server_exceptions=False)
master = main.rigs.master
mount = install(master, "mount", FakeMount())
focuser = install(master, "focuser", FakeFocuser())
wheel = install(master, "filterwheel", FakeWheel())
camera = install(master, "camera", FakeCamera("cam-main"))

print("-- one telescope --")
# H1 ------------------------------------------------------------------------
client.get("/api/status")
mount.status_delay = 3.0
time.sleep(0.1)
reply, took = timed(client.get, "/api/status")
case("H1: a mount whose status read takes 3 s does not hold up /api/status",
     reply.status_code == 200 and took < 1.0, f"{took:.2f}s")
mstate = reply.json()["devices"]["mount"]
case("H1: ...and its last known state is shown, marked stale with its age",
     mstate.get("stale") is True and "statusAge" in mstate and mstate.get("connected"),
     str({k: mstate.get(k) for k in ("stale", "statusAge", "connected")}))
mount.status_delay = 0.0
wait_for(lambda: not client.get("/api/status").json()["devices"]["mount"].get("stale"), 6)
case("H1: once the driver answers again the state is fresh",
     not client.get("/api/status").json()["devices"]["mount"].get("stale"))

# H2 ------------------------------------------------------------------------
original = main.pier.status
main.pier.status = lambda: (_ for _ in ()).throw(RuntimeError("pier camera exploded"))
try:
    reply = client.get("/api/status")
    body = reply.json() if reply.status_code == 200 else {}
    case("H2: a section that raises leaves /api/status answering",
         reply.status_code == 200 and "devices" in body, str(reply.status_code))
    case("H2: ...and says which section failed",
         "piercam" in (body.get("sectionErrors") or {}), str(body.get("sectionErrors")))
    got = 0
    try:
        with client.websocket_connect("/ws") as ws:
            for _ in range(3):
                ws.receive_json()
                got += 1
    except Exception as exc:                     # noqa: BLE001
        got = -1 if got == 0 else got
    case("H2: the status socket keeps sending through it", got == 3, f"messages={got}")
finally:
    main.pier.status = original

# H3 ------------------------------------------------------------------------
with client.websocket_connect("/ws") as ws:
    a = ws.receive_json()
    b = ws.receive_json()
case("H3: every status carries the server's time and a sequence number",
     all(k in a for k in ("serverTime", "seq")) and b.get("seq", 0) > a.get("seq", 0)
     and abs(a.get("serverTime", 0) - time.time()) < 5,
     f"seq {a.get('seq')} -> {b.get('seq')}")

# H4 ------------------------------------------------------------------------
mount.delay = 4.0
reply, took = timed(client.post, "/api/mount/park")
body = reply.json()
case("H4: a park that takes 4 s answers within 2.5 s, as started",
     reply.status_code == 200 and took < 2.5 and body.get("pending") is True,
     f"{took:.2f}s {body}")
again, took2 = timed(client.post, "/api/mount/park")
case("H4: a second press while it runs is refused at once, not queued",
     again.status_code == 409 and took2 < 0.5, f"{again.status_code} {took2:.2f}s")
tasks = client.get("/api/status").json().get("tasks") or []
running = [t for t in tasks if t.get("key") == "mount:park"]
case("H4: the status says the park is running",
     running and running[0].get("state") == "running", str(running))
case("H4: and the page hears when it is done",
     wait_for(lambda: any(t.get("key") == "mount:park" and t.get("state") == "done"
                          for t in client.get("/api/status").json().get("tasks") or []), 8)
     and mount.at_park)
mount.delay = 0.0
reply, took = timed(client.post, "/api/mount/unpark")
case("H4: a quick command still answers ok straight away, not as pending",
     reply.status_code == 200 and not reply.json().get("pending") and took < 1.0,
     f"{reply.json()} {took:.2f}s")
focuser.fail = True
reply = client.post("/api/focuser/move", json={"position": 31000})
case("H4: a quick failure is still an error the button shows",
     reply.status_code == 400 and "simulated" in reply.text, f"{reply.status_code}")
focuser.fail = False
wheel.delay, wheel.fail = 3.0, True
reply = client.post("/api/filterwheel/position", json={"position": 2})
failed = wait_for(lambda: any(t.get("key", "").endswith("filterwheel:position") and t.get("state") == "error"
                              for t in client.get("/api/status").json().get("tasks") or []), 8)
case("H4: a slow failure ends as an error in the status, not silence",
     reply.status_code == 200 and reply.json().get("pending") and failed)
wheel.delay, wheel.fail = 0.0, False

# H8 ------------------------------------------------------------------------
real = camera.set_settings


def slow_settings(*a, **k):
    time.sleep(0.3)
    return real(*a, **k)


camera.set_settings = slow_settings
outcomes = []


def press():
    try:
        master.capture.start(0.2, "light")
        outcomes.append("started")
    except DeviceError as exc:
        outcomes.append(str(exc))


before = camera.exposures
threads = [threading.Thread(target=press) for _ in range(2)]
[t.start() for t in threads]
[t.join() for t in threads]
wait_for(lambda: not master.capture.busy, 10)
camera.set_settings = real
case("H8: two presses of Capture at once start one exposure",
     outcomes.count("started") == 1 and camera.exposures - before == 1,
     f"{outcomes} exposures={camera.exposures - before}")

# -- three telescopes -------------------------------------------------------
print("-- three telescopes --")
for name in ("RASA2", "RASA3"):
    client.post("/api/equipment/telescopes", json={"name": name})
others = [r for r in main.rigs.all if r is not master]
case("triple: three telescopes", len(main.rigs.all) == 3)
fz = {r.id: install(r, "focuser", FakeFocuser(f"foc-{r.id}")) for r in others}
for r in others:
    install(r, "camera", FakeCamera(f"cam-{r.id}"))
client.get("/api/status")
first = others[0]
fz[first.id].status_delay = 3.0
time.sleep(0.1)
reply, took = timed(client.get, "/api/status")
rigs_state = {r["id"]: r for r in reply.json()["rigs"]}
case("H1 triple: one telescope's stuck focuser holds up nobody",
     took < 1.0 and rigs_state[first.id]["devices"]["focuser"].get("stale")
     and not rigs_state[others[1].id]["devices"]["focuser"].get("stale"), f"{took:.2f}s")
fz[first.id].status_delay = 0.0
for r in others:
    fz[r.id].delay = 6.0  # a and b each wait 2 s before answering pending
a = client.post(f"/api/focuser/move?rig={others[0].id}", json={"position": 32000})
b = client.post(f"/api/focuser/move?rig={others[1].id}", json={"position": 32000})
c = client.post(f"/api/focuser/move?rig={others[0].id}", json={"position": 33000})
case("H4 triple: a slow move on one telescope does not block the same move on another",
     a.json().get("pending") and b.json().get("pending"), f"{a.json()} {b.json()}")
case("H4 triple: ...but a second move on the same telescope is refused while it runs",
     c.status_code == 409, str(c.status_code))
keys = {t["key"] for t in client.get("/api/status").json().get("tasks") or []}
case("H4 triple: tasks are named per telescope",
     {f"{others[0].id}:focuser:move", f"{others[1].id}:focuser:move"} <= keys, str(sorted(keys)))
wait_for(lambda: not any(f.moving for f in fz.values()), 10)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
