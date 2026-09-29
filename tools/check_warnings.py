"""The warnings board: the quiet failures, said out loud and cleared again.

    python tools/check_warnings.py

Each check is driven against a stub rig in the state that should trip it,
then in the state that should not, and the board is checked to raise, hold,
escalate, notify and clear the way the strip depends on.

No test framework, for the same reason as the other checks here.
"""
import os
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from astrocontrol import warnings                                  # noqa: E402
from astrocontrol.config import Config                             # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


LOG = []
SENT = []


class Notifier:
    def send(self, event, subject, body=""):
        SENT.append((event, subject))
        return True


class Manager:
    def __init__(self, devices):
        self.devices = devices

    def get(self, kind):
        return self.devices.get(kind)


class Capture:
    def __init__(self):
        self.root_dir = Path(tempfile.gettempdir())
        self.save_enabled = True
        self.images = []
        self.overheads = None
        self._exposure_started = None
        self._state = "idle"
        self._seconds = 0.0

    def status(self):
        return {"state": self._state, "exposureSeconds": self._seconds}


class Rig:
    def __init__(self, devices, config):
        self.id = "main"
        self.name = "Telescope 1"
        self.manager = Manager(devices)
        self.capture = Capture()
        self.config = config
        self.solver = types.SimpleNamespace(executable=lambda: "astap")


class Rigs:
    def __init__(self, rig):
        self.master = rig
        self.all = [rig]


class Sequencer:
    def __init__(self, **state):
        self.state = {"running": False, **state}

    def status(self):
        return dict(self.state)


class Plan:
    def __init__(self, entries):
        self.entries = entries

    def raw(self):
        return {"entries": self.entries}


def build(devices=None, entries=None, run=None, live=True, **overrides):
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    config.update("site", {"latitude": 31.9, "longitude": -99.1, "useMount": True})
    for section, values in overrides.items():
        config.update(section, values)
    rig = Rig(devices or {}, config)
    board = warnings.WarningBoard(log=lambda m, level="info": LOG.append((level, m)),
                                  notifier=Notifier())
    now = time.time()
    ctx = warnings.Context(
        config=config, rigs=Rigs(rig), sequencer=Sequencer(**(run or {})),
        plan=Plan(entries if entries is not None else [
            {"id": "e1", "name": "M31", "targetId": "t1",
             "filters": [{"name": "L", "exposure": 120, "count": 10}]}]),
        night=(lambda: (now - 600, now + 6 * 3600)) if live else (lambda: (now + 5 * 3600, now + 12 * 3600)),
        notifier=Notifier())
    dog = warnings.Watchdog(board, ctx, interval=999,
                            log=lambda m, level="info": LOG.append((level, m)))
    return board, ctx, dog, rig


def keys(board):
    return {w["key"]: w for w in board.snapshot()["active"]}


# ------------------------------------------------------------ the board itself
board, ctx, dog, rig = build()
board.begin()
board.raise_("x.one", "warning", "One")
board.raise_("x.two", "critical", "Two", "detail", "fix")
board.end()
snap = board.snapshot()
case("the board lists what was raised, worst first",
     [w["key"] for w in snap["active"]] == ["x.two", "x.one"] and snap["worst"] == "critical")
case("a critical warning goes out as a notification once",
     SENT[-1] == ("warning", "Two") and sum(1 for s in SENT if s[1] == "Two") == 1)
board.begin()
board.raise_("x.two", "critical", "Two")
board.end()
case("what a pass does not raise is cleared", "x.one" not in keys(board) and "x.two" in keys(board))
case("...and the clearing is logged", any("Cleared: One" in m for _, m in LOG))
board.begin()
board.raise_("x.two", "critical", "Two")
board.end()
case("a critical warning that persists is not sent again", sum(1 for s in SENT if s[1] == "Two") == 1)
board.acknowledge("x.two")
case("acknowledging keeps it on the list but marks it seen",
     keys(board)["x.two"]["acknowledged"] is True and board.snapshot()["unacknowledged"]["critical"] == 0)
board.begin()
board.raise_("x.two", "warning", "Two")
board.end()
case("a change of level un-acknowledges it", keys(board)["x.two"]["acknowledged"] is False)
board.raise_("ev.flip", "warning", "Sticky", sticky=True)
board.begin()
board.end()
case("a sticky warning survives a pass that did not raise it", "ev.flip" in keys(board))
board.clear_sticky()
case("...until the run starts afresh", "ev.flip" not in keys(board))


# -------------------------------------------------------------- the checks
def trip(name, devices=None, entries=None, run=None, live=True, expect=None, absent=None, **overrides):
    board, ctx, dog, rig = build(devices, entries, run, live, **overrides)
    dog.pass_once()
    found = keys(board)
    ok = (expect is None or expect in found) and (absent is None or absent not in found)
    case(name, ok, ", ".join(sorted(found)) or "nothing raised")
    return board, found, rig, dog


def camera(**kw):
    base = dict(connected=True, can_cool=True, cooler_on=True, setpoint=-10.0,
                temperature=-10.0, cooler_power=40.0, sensor_width=6000, sensor_height=4000)
    base.update(kw)
    return types.SimpleNamespace(**base)


def mount(**kw):
    base = dict(connected=True, tracking=True, slewing=False, site=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


# The site, and the mount disagreeing with it.
trip("no site at all is critical when there is a plan", entries=None, expect="site.unset",
     site={"latitude": None, "longitude": None})
trip("a mount whose site is elsewhere is critical",
     devices={"mount": mount(site={"latitude": 40.0, "longitude": -99.1})}, expect="mount.site")
trip("...but a mount that agrees is fine",
     devices={"mount": mount(site={"latitude": 31.9, "longitude": -99.1})}, absent="mount.site")

# Devices missing only when the night is on.
trip("no camera with the night on is critical", live=True, expect="camera.missing")
trip("...and in the afternoon it is nobody's business yet", live=False, absent="camera.missing")
trip("no mount with targets on the plan is critical", devices={"camera": camera()}, expect="mount.missing")
trip("a plan of only calibration needs no mount",
     devices={"camera": camera()},
     entries=[{"id": "c", "name": "darks", "kind": "calibration", "recipeId": "r"}],
     absent="mount.missing")

# The cooler.
b, found, r, dog = trip("a camera off its setpoint is a notice at first",
                        devices={"camera": camera(temperature=-2.0), "mount": mount()},
                        expect="camera.cooling")
case("...a notice, not yet a warning", found["camera.cooling"]["level"] == "notice")
b._active["camera.cooling"].since -= 20 * 60
r.manager.devices["camera"].cooler_power = 99.0
dog.pass_once()
case("...and after fifteen minutes at full power it is a warning that says so",
     keys(b)["camera.cooling"]["level"] == "warning" and "cannot" in keys(b)["camera.cooling"]["title"])

# Tracking off while imaging.
trip("tracking off during a light frame is critical",
     devices={"camera": camera(), "mount": mount(tracking=False)},
     run={"running": True, "state": "imaging"}, expect="mount.tracking")
trip("...not when merely slewing",
     devices={"camera": camera(), "mount": mount(tracking=False, slewing=True)},
     run={"running": True, "state": "imaging"}, absent="mount.tracking")

# The plan's filters against the wheel.
trip("a filter the wheel lacks is critical",
     devices={"camera": camera(), "mount": mount(),
              "filterwheel": types.SimpleNamespace(connected=True, names=["R", "G", "B"])},
     expect="plan.filters")
trip("...a spelling the wheel knows is fine",
     devices={"camera": camera(), "mount": mount(),
              "filterwheel": types.SimpleNamespace(connected=True, names=["Lum", "R"])},
     absent="plan.filters")

# Frames not saved, camera hung.
b, found, r, dog = trip("exposing with saving off is critical",
                        devices={"camera": camera(), "mount": mount()},
                        run={"running": True, "state": "imaging"}, absent="capture.unsaved")
r.capture.save_enabled = False
dog.pass_once()
case("...once saving is switched off", "capture.unsaved" in keys(b))
r.capture.save_enabled = True
r.capture._state = "downloading"
r.capture._exposure_started = time.time() - 700
dog.pass_once()
case("a download that has taken ten minutes is a hung camera", "camera.hung" in keys(b))

# The light path.
panel = types.SimpleNamespace(connected=True, has_cover=True, cover_state="closed", light_on=False)
trip("a shut cover during a light frame is critical",
     devices={"camera": camera(), "mount": mount(), "flatpanel": panel},
     run={"running": True, "state": "imaging"}, expect="panel.cover")
panel.cover_state = "open"
panel.light_on = True
trip("a lit panel during a light frame is critical",
     devices={"camera": camera(), "mount": mount(), "flatpanel": panel},
     run={"running": True, "state": "imaging"}, expect="panel.light")

# The guider.
guider = types.SimpleNamespace(connected=True, guiding=True,
                               status=lambda: {"state": "Guiding", "rmsTotal": 2.4, "samples": 40, "snr": 30})
trip("rough guiding is a warning",
     devices={"camera": camera(), "mount": mount(), "guider": guider},
     run={"running": True, "state": "imaging"}, expect="guider.rms")
lost = types.SimpleNamespace(connected=True, guiding=False,
                             status=lambda: {"state": "LostLock", "starLost": "star faded", "samples": 40})
trip("a lost guide star is a warning",
     devices={"camera": camera(), "mount": mount(), "guider": lost},
     run={"running": True, "state": "imaging"}, expect="guider.lost")
trip("no PHD2 with guiding wanted and the night on is a warning",
     devices={"camera": camera(), "mount": mount()}, expect="guider.missing")

# The frames themselves.
b, found, r, dog = trip("black frames are critical",
                        devices={"camera": camera(), "mount": mount()},
                        run={"running": True, "state": "imaging"}, absent="frames.black")
for i in range(3):
    r.capture.images.insert(0, types.SimpleNamespace(
        frame_type="light", timestamp=time.time(), offset=30, stats={"median": 45.0}))
dog.pass_once()
case("...once the last lights have nothing in them", "frames.black" in keys(b))
r.capture.images = [types.SimpleNamespace(frame_type="light", timestamp=time.time(), offset=30,
                                          stats={"median": 65000.0}) for _ in range(2)]
dog.pass_once()
case("saturated frames are critical too", "frames.white" in keys(b) and "frames.black" not in keys(b))
b, found, r, dog = trip("frames with no stars are critical",
                        devices={"camera": camera(), "mount": mount()},
                        run={"running": True, "state": "imaging",
                             "frames": [{"hfr": None, "stars": 0}] * 3},
                        expect="frames.nostars")
trip("focus drifting is a warning",
     devices={"camera": camera(), "mount": mount()},
     run={"running": True, "state": "imaging",
          "frames": [{"hfr": 3.0, "driftPercent": 5}, {"hfr": 4.0, "driftPercent": 40},
                     {"hfr": 4.1, "driftPercent": 42}]},
     expect="frames.focus")
trip("a run that keeps being rescued is a warning",
     devices={"camera": camera(), "mount": mount()},
     run={"running": True, "state": "imaging", "rescues": 4}, expect="sequence.rescues")

# Disk space, with the line drawn where this machine will trip it.
trip("a nearly full disk is critical", devices={"camera": camera(), "mount": mount()},
     expect="disk.space", warnings={"diskCriticalGb": 1e9, "diskWarnGb": 2e9})
trip("...and a roomy one is nothing", devices={"camera": camera(), "mount": mount()},
     absent="disk.space", warnings={"diskCriticalGb": 0.0, "diskWarnGb": 0.0})

# The solver and the collaboration.
b, found, r, dog = trip("ASTAP missing with targets on the plan is a warning",
                        devices={"camera": camera(), "mount": mount()}, absent="solver.missing")
r.solver = types.SimpleNamespace(executable=lambda: None)
dog.pass_once()
case("...once it cannot be found", "solver.missing" in keys(b))

board, ctx, dog, rig = build(devices={"camera": camera(), "mount": mount()})
ctx.collab = types.SimpleNamespace(status=lambda: {
    "configured": True, "error": "401: unknown token", "reachedAt": time.time() - 4000,
    "compatibility": None, "clockSkew": 400.0})
dog.pass_once()
found = keys(board)
case("a rejected token is critical", "collab.token" in found)
ctx.collab = types.SimpleNamespace(status=lambda: {
    "configured": True, "error": None, "reachedAt": time.time(),
    "compatibility": {"ok": True}, "clockSkew": 400.0})
dog.pass_once()
found = keys(board)
case("a clock four hundred seconds out is a warning",
     "clock.skew" in found and "collab.token" not in found)
ctx.collab = types.SimpleNamespace(status=lambda: {
    "configured": True, "error": "timed out", "reachedAt": time.time() - 4000,
    "compatibility": {"ok": False, "summary": "no Ha"}, "clockSkew": 0.0})
dog.pass_once()
found = keys(board)
case("an unreachable server and a rig that no longer fits are warnings",
     "collab.unreachable" in found and "collab.fit" in found and "collab.token" not in found)

# A broken check must not stop the rest.
board, ctx, dog, rig = build(devices={"camera": camera(), "mount": mount()})


def exploding(board_, ctx_):
    raise RuntimeError("boom")


warnings.CHECKS.insert(0, exploding)
dog.pass_once()
warnings.CHECKS.remove(exploding)
case("a check that throws is logged and the others still run",
     any("exploding" in m for _, m in LOG) and "guider.missing" in keys(board))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
