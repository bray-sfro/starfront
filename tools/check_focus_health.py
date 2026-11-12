"""A focuser that says Connected but is not answering is reconnected (0.2.52d).

    python tools/check_focus_health.py

2026-11-03, FSQ106 and vsd100: ASCOM.EAF.Focuser connected=true, position 0,
temperature -273.0, and every Move threw (-2147352567, 'Exception occurred.',
(0, 'ASCOM.EAF', 'Move Failure', ...)). Starfront said "the focuser is at the
end of its travel" and autofocus failed all night. These drive a fake EAF that
does exactly that through the real DeviceManager, Rig, AutoFocuser, warnings
board and the /api/focuser/move endpoint.
"""
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
TMP = Path(tempfile.mkdtemp(prefix="focushealth-"))
os.environ["ASTRO_DATA_DIR"] = str(TMP / "data")
os.environ["HOME"] = str(TMP / "home")
os.environ["LOCALAPPDATA"] = str(TMP / "lad")

from astrocontrol import focushealth, warnings                     # noqa: E402
from astrocontrol.config import Config                             # noqa: E402
from astrocontrol.devices import manager as manager_mod            # noqa: E402
from astrocontrol.devices.base import Camera, DeviceError, Focuser  # noqa: E402
from astrocontrol.devices.manager import DeviceManager             # noqa: E402
from astrocontrol.focushealth import FocuserHealth, bogus_reading, is_dead_error  # noqa: E402
from astrocontrol.focusing import AutoFocuser                      # noqa: E402

results = []
MOVE_FAILURE = ("Move: (-2147352567, 'Exception occurred.', (0, 'ASCOM.EAF', "
                "'Move Failure', None, 0, -2146233088), None)")


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Hardware:
    """The EAF at the end of the cable: plugged in, or not."""

    def __init__(self, plugged=True):
        self.plugged = plugged
        self.opens = 0


class FakeEAF(Focuser):
    """ZWO's EAF ASCOM driver. A handle opened while nothing answered - or one
    that lost its focuser since - stays dead: 0 steps, -273 C, Move Failure."""

    max_step = 60000

    def __init__(self, driver_id, name, hardware):
        Focuser.__init__(self, driver_id, name)
        self.hw = hardware
        self.live = False
        self._pos = 0
        self.is_moving = False
        self.moves = []

    def connect(self):
        self.hw.opens += 1
        self.live = self.hw.plugged
        self._pos = 31000 if self.live else 0
        self._connected = True

    @property
    def position(self):
        return self._pos if self.live else 0

    @property
    def moving(self):
        return self.is_moving

    @property
    def temperature(self):
        return 9.5 if self.live else -273.0

    def move_to(self, position):
        self._require()
        if not self.live:
            raise DeviceError(MOVE_FAILURE)
        if not 0 <= int(position) <= self.max_step:
            raise DeviceError(f"position must be 0..{self.max_step}")
        self._pos = int(position)
        self.moves.append(int(position))

    def halt(self):
        pass


class FakeCam(Camera):
    def __init__(self):
        Camera.__init__(self, "cam", "Camera")
        self.disconnects = 0

    def disconnect(self):
        self.disconnects += 1
        self._connected = False


HARDWARE = {}


def fake_create(kind, driver_id, name, channel="default"):
    return FakeEAF(driver_id, name, HARDWARE[channel])


manager_mod.ascom.create = fake_create
SPEC = {"backend": "ascom", "driverId": "ASCOM.EAF.Focuser", "name": "ZWO EAF",
        "options": None}


def build(channel="rig1", plugged_now=False, plugged_later=True):
    """A manager whose focuser was connected while dead; the hardware is then
    `plugged_later` (a stale handle when True, unplugged when False)."""
    HARDWARE[channel] = Hardware(plugged=plugged_now)
    mgr = DeviceManager(label="", channel=channel)
    cam = FakeCam()
    cam.connect()
    mgr._devices["camera"] = cam
    mgr.connect("focuser", "ascom", "ASCOM.EAF.Focuser", "ZWO EAF")
    HARDWARE[channel].plugged = plugged_later
    sleeps = []
    health = FocuserHealth("Telescope 1", mgr, lambda: dict(SPEC), sleep=sleeps.append)
    return mgr, cam, health, sleeps


# ----------------------------------------------------------- telling dead from 0
print("-- what a dead focuser looks like --")
mgr, cam, health, sleeps = build()
dead = mgr.get("focuser")
case("0 steps at -273 C is read as nobody answering", bogus_reading(dead) is not None,
     bogus_reading(dead))
live = FakeEAF("x", "x", Hardware(True)); live.connect(); live._pos = 0
case("a live focuser genuinely at 0 (9.5 C) is not", bogus_reading(live) is None)
probe = types.SimpleNamespace(temperature=-273.0, position=31000, max_step=60000)
case("-273 C in mid-travel (a broken probe) is not either", bogus_reading(probe) is None)
case("no temperature at all is not", bogus_reading(
    types.SimpleNamespace(temperature=None, position=0, max_step=60000)) is None)
case("-273 C at the top end is", bogus_reading(
    types.SimpleNamespace(temperature=-273.0, position=60000, max_step=60000)) is not None)
case("the FSQ106's Move Failure is a dead-focuser error", is_dead_error(DeviceError(MOVE_FAILURE)))
case("a range check of ours is not", not is_dead_error(DeviceError("position must be 0..60000")))
case("nor a slow move", not is_dead_error(DeviceError("the focuser did not stop moving in time")))

# ------------------------------------------------------- a stale handle comes back
print("-- a stale handle: one reconnect brings it back --")
mgr, cam, health, sleeps = build(plugged_later=True)
before = mgr.get("focuser")
result = health.call(lambda f: f.move_to(30000), "moving the focuser")
after = mgr.get("focuser")
case("the move is made after reconnecting", after.moves == [30000], str(after.moves))
case("a new focuser connection, not the dead one", after is not before and after.live)
case("one reconnect was enough", HARDWARE["rig1"].opens == 2 and sleeps == [],
     f"opens={HARDWARE['rig1'].opens} sleeps={sleeps}")
case("the camera was not touched", mgr.get("camera") is cam and cam.disconnects == 0
     and cam.connected)
case("health says reconnected", health.state.get("state") == "reconnected", str(health.state))

# ---------------------------------------------------------- unplugged: give up
print("-- unplugged at the scope: three tries, spaced, then a clear message --")
mgr, cam, health, sleeps = build(plugged_later=False)
try:
    health.call(lambda f: f.move_to(30000), "moving the focuser")
    case("an unplugged focuser fails the move", False)
except DeviceError as exc:
    case("an unplugged focuser fails the move with power-cycle advice",
         "power-cycle or replug" in str(exc), str(exc)[:120])
case("reconnected exactly 3 times", HARDWARE["rig1"].opens == 1 + 3,
     str(HARDWARE["rig1"].opens))
case("spaced out: 10 s then 30 s", sleeps == [10.0, 30.0], str(sleeps))
case("health says dead, with a retry time", health.state.get("state") == "dead"
     and health.state.get("retryAfter"), str(health.state))
case("the camera still untouched", cam.disconnects == 0 and mgr.get("camera") is cam)
opens = HARDWARE["rig1"].opens
case("in its cool-off it is not reconnected again", health.heal("again") is False
     and HARDWARE["rig1"].opens == opens)
HARDWARE["rig1"].plugged = True
health._given_up_until = 0.0
case("after the cool-off, once plugged back in, it comes back", health.heal("later") is True
     and mgr.get("focuser").live)

# ------------------------------------------------------ never mid-move
print("-- never while it is moving --")
mgr, cam, health, sleeps = build(plugged_later=True)
mgr.get("focuser").is_moving = True
case("a focuser that says it is moving is not reconnected",
     health.heal("test") is False and HARDWARE["rig1"].opens == 1)
mgr.get("focuser").is_moving = False

# ---------------------------------------------- other errors are left alone
mgr, cam, health, sleeps = build(plugged_now=True, plugged_later=True)
try:
    health.call(lambda f: f.move_to(99999), "moving the focuser")
    case("an out-of-range move is refused", False)
except DeviceError as exc:
    case("an out-of-range move is refused as before, no reconnect",
         "must be 0.." in str(exc) and HARDWARE["rig1"].opens == 1, str(exc))
case("a healthy focuser's move is just made",
     health.call(lambda f: f.move_to(1000), "m") is None and mgr.get("focuser").moves == [1000])
case("check() on a healthy focuser does nothing", health.check("x") and HARDWARE["rig1"].opens == 1)

# ---------------------------------------------- the stop flag cuts it short
mgr, cam, health, sleeps = build(plugged_later=False)
case("a Stop during the reconnects abandons them", health.heal("x", stop=lambda: True) is False
     and HARDWARE["rig1"].opens == 1)

# ---------------------------------------------------- autofocus retries once
print("-- autofocus --")
mgr, cam, health, sleeps = build(plugged_later=True)
config = Config(TMP / "af.json")
af = AutoFocuser(mgr, types.SimpleNamespace(), config)
af.health = health
calls = []


def sweep(should_abort=None, report=None):
    focuser = mgr.require("focuser")
    calls.append(focuser)
    focuser.move_to(30500)                 # the first move of a sweep
    return "focused"


af._run_once = sweep
said = []
result = af.run(report=said.append)
case("a dead focuser is reconnected before the sweep starts",
     result == "focused" and len(calls) == 1 and calls[0].live, f"{len(calls)} {said}")
case("and says so in the run's messages", any("reconnecting" in s for s in said), str(said))

# Dead in the middle: the preflight saw it fine; a move fails.
mgr, cam, health, sleeps = build(plugged_now=True, plugged_later=True)
af = AutoFocuser(mgr, types.SimpleNamespace(), config)
af.health = health
calls = []


def sweep_dies(should_abort=None, report=None):
    focuser = mgr.require("focuser")
    calls.append(focuser)
    if len(calls) == 1:
        raise DeviceError(MOVE_FAILURE)
    focuser.move_to(30500)
    return "focused"


af._run_once = sweep_dies
result = af.run()
case("a sweep failing on Move Failure is retried once after a reconnect",
     result == "focused" and len(calls) == 2 and calls[1] is not calls[0])

mgr, cam, health, sleeps = build(plugged_later=False)
af = AutoFocuser(mgr, types.SimpleNamespace(), config)
af.health = health
af._run_once = sweep
try:
    af.run()
    case("an unplugged focuser fails autofocus", False)
except DeviceError as exc:
    case("an unplugged focuser fails autofocus saying to power-cycle it",
         "power-cycle or replug" in str(exc), str(exc)[:100])

mgr, cam, health, sleeps = build(plugged_now=True, plugged_later=True)
af = AutoFocuser(mgr, types.SimpleNamespace(), config)
af.health = health


def no_minimum(should_abort=None, report=None):
    raise DeviceError("autofocus did not find a minimum: clouds")


af._run_once = no_minimum
try:
    af.run()
except DeviceError:
    pass
case("an ordinary focus failure does not reconnect anything", HARDWARE["rig1"].opens == 1)

# ------------------------------------------------- two telescopes, one each
print("-- a RASA mount: each telescope's focuser on its own --")
mgr1, cam1, health1, _ = build("rasa1", plugged_now=True, plugged_later=True)
mgr2, cam2, health2, _ = build("rasa2", plugged_now=False, plugged_later=True)
f1 = mgr1.get("focuser")
health2.call(lambda f: f.move_to(2000), "m")
case("reconnecting rasa2's focuser leaves rasa1's alone",
     mgr1.get("focuser") is f1 and HARDWARE["rasa1"].opens == 1
     and HARDWARE["rasa2"].opens == 2 and mgr2.get("focuser").moves == [2000])

# ------------------------------------------------------------- the warnings
print("-- the warnings board --")


class WRig:
    def __init__(self, rid, name, mgr, health):
        self.id, self.name, self.manager, self.focus_health = rid, name, mgr, health


def board_for(rigs):
    cfg = Config(Path(tempfile.mkdtemp()) / "s.json")
    cfg.update("site", {"latitude": 31.9, "longitude": -99.1})
    board = warnings.WarningBoard(log=lambda m, level="info": None)
    now = time.time()
    ctx = warnings.Context(
        config=cfg, rigs=types.SimpleNamespace(master=rigs[0], all=rigs),
        sequencer=types.SimpleNamespace(status=lambda: {"running": True}),
        plan=types.SimpleNamespace(raw=lambda: {"entries": [{"id": "e", "name": "M42"}]}),
        night=lambda: (now - 600, now + 3600))
    dog = warnings.Watchdog(board, ctx, interval=999, log=lambda m, level="info": None)
    dog.pass_once()
    return {w["key"]: w for w in board.snapshot()["active"]}


mgr, cam, health, _ = build(plugged_later=False)
active = board_for([WRig("main", "FSQ106", mgr, health)])
case("a dead focuser is not called 'at the end of its travel'", "focuser.limit" not in active,
     str(list(active)))
case("it is called not responding", "focuser.dead" in active
     and "not responding" in active["focuser.dead"]["title"], str(list(active)))
health.heal("x")
active = board_for([WRig("main", "FSQ106", mgr, health)])
case("after giving up it says to power-cycle or replug",
     "Power-cycle or replug" in active.get("focuser.dead", {}).get("fix", ""))

mgr, cam, health, _ = build(plugged_now=True)
mgr.get("focuser")._pos = 0
active = board_for([WRig("main", "FSQ106", mgr, health)])
case("a live focuser really at 0 still gets the end-of-travel warning",
     "focuser.limit" in active and "focuser.dead" not in active, str(list(active)))

mgr, cam, health, _ = build(plugged_later=True)
health.call(lambda f: f.move_to(100), "m")
active = board_for([WRig("main", "FSQ106", mgr, health)])
case("after a reconnect a notice says it was reconnected",
     active.get("focuser.dead", {}).get("level") == "notice"
     and "reconnected" in active["focuser.dead"]["title"], str(active.get("focuser.dead")))

mgr1, _, h1, _ = build("r1", plugged_now=True)
mgr2, _, h2, _ = build("r2", plugged_now=False, plugged_later=False)
active = board_for([WRig("main", "rasa1", mgr1, h1), WRig("e027", "rasa2", mgr2, h2)])
case("on a multi-telescope mount the drawer that is dead is the one named",
     "focuser.dead.e027" in active and "focuser.dead" not in active
     and active["focuser.dead.e027"]["title"].startswith("rasa2"), str(list(active)))

# ------------------------------------------------ the real Rig and the endpoint
print("-- the real program --")
from fastapi.testclient import TestClient                          # noqa: E402
from astrocontrol import main                                      # noqa: E402

rig = main.rigs.master
HARDWARE[rig.id] = Hardware(plugged=False)
rig.manager.connect("focuser", "ascom", "ASCOM.EAF.Focuser", "ZWO EAF")
HARDWARE[rig.id].plugged = True
case("every telescope has its focuser health", all(
    getattr(r, "focus_health", None) is not None for r in main.rigs.all))
case("its autofocus is wired to it", rig.focuser.health is rig.focus_health)
case("with no remembered driver it reconnects the one connected now",
     rig._focuser_spec() == {"backend": "ascom", "driverId": "ASCOM.EAF.Focuser",
                             "name": "ZWO EAF", "options": None}, str(rig._focuser_spec()))
client = TestClient(main.app)
response = client.post("/api/focuser/move", json={"position": 25000})
for _ in range(50):
    if rig.manager.get("focuser").moves:
        break
    time.sleep(0.1)
case("POST /api/focuser/move on a dead focuser reconnects it and moves",
     response.status_code == 200 and rig.manager.get("focuser").moves == [25000],
     f"{response.status_code} {response.text[:120]}")

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
