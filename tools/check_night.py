"""Exercise how a night starts and how it ends.

    python tools/check_night.py

These are the parts nobody watches.  A sequence starts while the operator is
still in the room, so a slow start is merely annoying; it *ends* at four in the
morning, and everything that goes wrong there is found at breakfast.  The faults
this covers were all found that way:

  * Stop appeared to hang, because the warm-down ran on the sequencer thread and
    held the run open for ten minutes of sleeping.
  * Guiding was still running at dawn, because guiding was only ever stopped as
    a side effect of parking — and parking is off by default.
  * The mount was never parked, for the same reason plus the warm-down in front
    of it.
  * The night stood still waiting for the sensor to reach -10 C before it would
    slew, instead of cooling while it homed, slewed, centred and focused.
  * The mount was never homed, so a power cycle meant every slew of the night
    was wrong by the same amount.

No test framework, for the same reason as the other checks here.
"""
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol.config import Config                          # noqa: E402
from astrocontrol.sequencer import Sequencer                    # noqa: E402
from astrocontrol.devices.base import DeviceError               # noqa: E402

LOG = []


class Guider:
    """PHD2 that is doing something — not necessarily guiding."""

    def __init__(self, state="Guiding", fail=False):
        self.connected = True
        self.state = state
        self.settling = False
        self.stops = 0
        self.fail = fail

    @property
    def guiding(self):
        return self.state == "Guiding"

    def stop_guiding(self):
        if self.fail:
            raise DeviceError("PHD2 did not answer")
        self.stops += 1
        self.state = "Stopped"


class SlowParkMount:
    """A mount that clears Slewing at once and sets AtPark a beat later.

    Common enough to be worth a case of its own: watching Slewing alone reported
    a mount that had parked perfectly well as having failed, and an abort that
    cries wolf gets ignored.
    """

    def __init__(self, delay=2.0):
        self.connected = True
        self.can_park = True
        self.slewing = False
        self.tracking = True
        self._parked_at = None
        self.delay = delay

    @property
    def at_park(self):
        return (self._parked_at is not None
                and time.monotonic() - self._parked_at >= self.delay)

    def park(self):
        self._parked_at = time.monotonic()


class Mount:
    def __init__(self, can_find_home=True, home_fails=False, at_home=False):
        self.connected = True
        self.can_find_home = can_find_home
        self.can_park = True
        self.slewing = False
        self.tracking = True
        self.at_park = False
        self._at_home = at_home
        self.homes = 0
        self.parks = 0
        self.unparks = 0
        self.slews = []
        self.home_fails = home_fails

    @property
    def at_home(self):
        return self._at_home

    def find_home(self):
        self.homes += 1
        if self.home_fails:
            raise DeviceError("the mount refused to home")
        self._at_home = True

    def park(self):
        self.parks += 1
        self.at_park = True

    def unpark(self):
        self.unparks += 1
        self.at_park = False

    def set_tracking(self, on):
        self.tracking = bool(on)

    def slew_to(self, ra, dec):
        self.slews.append((ra, dec))


class Camera:
    """A cooled camera that comes down to temperature over a few reads."""

    def __init__(self, temperature=20.0, step=0.0):
        self.connected = True
        self.can_cool = True
        self.gain = 100
        self.offset = 30
        self._temperature = temperature
        self._step = step
        self.setpoints = []
        self.cooler = None

    @property
    def temperature(self):
        self._temperature -= self._step
        return self._temperature

    def set_setpoint(self, value):
        self.setpoints.append(value)

    def set_cooler(self, on):
        self.cooler = bool(on)

    def set_settings(self, gain=None, offset=None):
        pass


class Manager:
    def __init__(self, devices):
        self.devices = devices

    def get(self, kind):
        return self.devices.get(kind)

    def require(self, kind):
        device = self.devices.get(kind)
        if device is None:
            raise DeviceError(f"{kind} not connected")
        return device

    def log(self, message, level="info"):
        LOG.append((level, message))


class Capture:
    def abort(self):
        pass


class Rig:
    def __init__(self, config, devices):
        self.id = "rig1"
        self.name = "Telescope 1"
        self.role = "master"
        self.config = config
        self.manager = Manager(devices)
        self.capture = Capture()
        self.solver = None
        self.focuser = None


class Rigs:
    def __init__(self, rig):
        self.master = rig
        self.all = [rig]

    def imaging(self):
        return self.all

    def get(self, rig_id):
        return self.master


def build(devices):
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    # Real observatories give a roll-off five minutes to close. These checks
    # would then take seven, so they are turned right down — the timeouts being
    # settings rather than constants is what makes that possible.
    config.update("sequencer", {"coverTimeoutSeconds": 1.0,
                                "roofTimeoutSeconds": 1.0,
                                "parkTimeoutSeconds": 1.0})
    rig = Rig(config, devices)
    sequencer = Sequencer(Rigs(rig), config, None, None)
    return sequencer, rig, config


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


results = []

# ------------------------------------------------------------ homing the mount
mount = Mount()
seq, rig, config = build({"mount": mount})
seq._home_mount()
case("the mount is homed before the first slew", mount.homes == 1)

seq._home_mount()
case("and not again for the second target", mount.homes == 1)

mount = Mount()
seq, rig, config = build({"mount": mount})
config.update("sequencer", {"homeAtStart": False})
seq._home_mount()
case("turning it off leaves the mount alone", mount.homes == 0)

mount = Mount(can_find_home=False)
seq, rig, config = build({"mount": mount})
seq._home_mount()
case("a mount that cannot home is stepped over, not failed",
     mount.homes == 0 and any("cannot home" in m for _, m in LOG))

mount = Mount(at_home=True)
seq, rig, config = build({"mount": mount})
seq._home_mount()
case("a mount already at home is not sent there again", mount.homes == 0)

mount = Mount(home_fails=True)
seq, rig, config = build({"mount": mount})
seq._home_mount()
case("a mount that refuses to home does not end the night",
     mount.homes == 1 and any("Could not home" in m for _, m in LOG))

mount = Mount()
mount.at_park = True
seq, rig, config = build({"mount": mount})
seq._home_mount()
case("a parked mount is released before homing",
     mount.unparks == 1 and mount.homes == 1)

seq, rig, config = build({})
seq._home_mount()
case("no mount at all is not an error", True)

# --------------------------------------------------------- guiding at the end
guider = Guider(state="Guiding")
seq, rig, config = build({"guider": guider})
seq._stop_guiding()
case("guiding is stopped at the end of the night", guider.stops == 1)

for state in ("Looping", "Calibrating", "LostLock", "Settling"):
    guider = Guider(state=state)
    seq, rig, config = build({"guider": guider})
    seq._stop_guiding()
    case(f"PHD2 is stopped even when it is {state}, not guiding",
         guider.stops == 1, state)

guider = Guider(fail=True)
seq, rig, config = build({"guider": guider})
seq._stop_guiding()
case("a guider that will not stop is reported, not thrown",
     any("Could not stop guiding" in m for _, m in LOG))

seq, rig, config = build({})
seq._stop_guiding()
case("no guider connected is not an error", True)

# `_make_safe` must stop guiding before it moves the mount, or PHD2 is left
# chasing a star through a park slew.
order = []
guider = Guider()
mount = Mount()
seq, rig, config = build({"guider": guider, "mount": mount})
guider.stop_guiding = lambda: order.append("guide")
mount.park = lambda: order.append("park")
seq._make_safe(park=True)
case("parking stops guiding first", order == ["guide", "park"], str(order))

# ------------------------------------------------- cooling does not block the slew
camera = Camera(temperature=20.0, step=0.0)
seq, rig, config = build({"camera": camera})
config.update("camera", {"setpoint": -10.0, "coolAtStart": True})
started = time.monotonic()
seq._prepare_cameras()
elapsed = time.monotonic() - started
case("starting the coolers does not wait for temperature",
     elapsed < 2.0 and camera.cooler is True, f"{elapsed:.2f}s")
case("the setpoint is applied straight away", camera.setpoints == [-10.0])
case("and the wait is left owed for the first frame",
     "rig1" in seq._cooling_wanted)

# The wait itself still happens — just later, and it clears once satisfied.
camera = Camera(temperature=-9.5, step=0.0)
seq, rig, config = build({"camera": camera})
config.update("camera", {"setpoint": -10.0, "coolToleranceC": 1.0})
seq._prepare_cameras()
seq._await_cooling(rig)
case("the wait is satisfied when the sensor is at temperature",
     seq._cooling_wanted == set())

# Asking for the old behaviour back gets it.
camera = Camera(temperature=-9.5, step=0.0)
seq, rig, config = build({"camera": camera})
config.update("camera", {"setpoint": -10.0, "waitForCooling": True})
seq._prepare_cameras()
case("waitForCooling brings back the wait before the first slew",
     seq._cooling_wanted == set())

# A camera that cannot cool is never waited for at all.
camera = Camera()
camera.can_cool = False
seq, rig, config = build({"camera": camera})
seq._prepare_cameras()
seq._await_cooling(rig)
case("a camera with no cooler is never waited for",
     seq._cooling_wanted == set() and camera.cooler is None)

# ------------------------------------------------------- warming off the thread
camera = Camera(temperature=-10.0)
seq, rig, config = build({"camera": camera})
config.update("camera", {"warmAtEnd": True, "warmRateCPerMinute": 3.0})
started = time.monotonic()
seq._start_warming()
elapsed = time.monotonic() - started
case("starting the warm-down returns immediately", elapsed < 1.0, f"{elapsed:.2f}s")
case("and it is reported as its own state, not as a running sequence",
     seq.warming and not seq.running)

seq._stop_warming()
case("a new sequence can cut the warm-down short",
     not seq.warming)

camera = Camera()
seq, rig, config = build({"camera": camera})
config.update("camera", {"warmAtEnd": False})
seq._start_warming()
case("warmAtEnd off starts no warm-down at all", not seq.warming)

# --------------------------------------------- focus frames are not kept
#
# A focus frame is a measurement, not data. Left saved it lands in the target's
# folder named exactly like a real sub, and a nine-point sweep every hour puts a
# hundred of them a night among the frames that matter.

from astrocontrol.focusing import AutoFocuser                   # noqa: E402


class FocusCapture:
    def __init__(self):
        self.save_enabled = True
        self.saw = []

    def capture_blocking(self, exposure, frame_type="light"):
        self.saw.append(self.save_enabled)
        raise DeviceError("no camera")          # ends the run after one frame

    def frame(self, image_id):
        return None


class Focuser:
    connected = True
    is_absolute = True
    max_step = 100000
    position = 30000
    temperature = 5.0
    moving = False

    def move_to(self, position):
        self.position = int(position)


config = Config(Path(tempfile.mkdtemp()) / "settings.json")
capture = FocusCapture()
focuser = AutoFocuser(Manager({"focuser": Focuser(), "camera": object()}),
                      capture, config)
try:
    focuser.run()
except Exception:                               # noqa: BLE001 - it is meant to fail
    pass
case("saving is off for every focus frame",
     capture.saw and not any(capture.saw), f"{capture.saw}")
case("and switched back on when the run ends", capture.save_enabled is True)

# A run that fails must still hand saving back, or the night that follows it
# silently keeps nothing.
capture = FocusCapture()
capture.save_enabled = False                    # a run inside a centring frame
focuser = AutoFocuser(Manager({"focuser": Focuser(), "camera": object()}),
                      capture, config)
try:
    focuser.run()
except Exception:                               # noqa: BLE001
    pass
case("and restored to what it was, not forced on",
     capture.save_enabled is False)


# ------------------------------------------------- putting the observatory to bed
#
# Stop ends the run and leaves the rig where it is. This is the other one: park,
# close the cover, warm the cameras — without anybody else touching it.


class Panel:
    def __init__(self, has_cover=True, state="open", sticks=False):
        self.connected = True
        self.has_cover = has_cover
        self._state = state
        self.sticks = sticks
        self.closes = 0
        self.off = 0

    @property
    def cover_state(self):
        return self._state

    def close_cover(self):
        self.closes += 1
        if not self.sticks:
            self._state = "closed"

    def turn_off(self):
        self.off += 1


order = []
guider = Guider()
mount = Mount()
panel = Panel()
camera = Camera(temperature=-10.0)
seq, rig, config = build({"guider": guider, "mount": mount,
                          "flatpanel": panel, "camera": camera})
guider.stop_guiding = lambda: order.append("guide")
panel.close_cover = lambda: (order.append("cover"), setattr(panel, "_state", "closed"))
# Wrapped, not replaced: parking has to still set `at_park`, because shutting
# down verifies that rather than trusting the call.
real_park = mount.park
mount.park = lambda: (order.append("park"), real_park())
rig.capture.abort = lambda: order.append("cameras")

result = seq.shut_down()
case("shutting down stops guiding", "guide" in order)
case("stops the cameras", "cameras" in order)
case("closes the cover", "cover" in order)
case("parks the mount", "park" in order)
case("and warms them afterwards", seq.warming)
case("the cover is closed before the mount parks",
     order.index("cover") < order.index("park"), str(order))
case("guiding stops before anything moves",
     order.index("guide") < order.index("park"), str(order))
case("and it reports what it managed",
     set(result["done"]) >= {"stop guiding", "close the cover", "park the mount"}
     and not result["failed"], f"{result}")
seq._stop_warming()

# The panel's light goes out as well as its cover closing.
case("the flat panel's light is put out", panel.off == 1)

# Each step is attempted whatever the one before it did: a mount that will not
# park is not a reason to leave the cover open and the cameras cold.
guider = Guider()
mount = Mount()
panel = Panel()
camera = Camera(temperature=-10.0)
seq, rig, config = build({"guider": guider, "mount": mount,
                          "flatpanel": panel, "camera": camera})


def refuse():
    raise DeviceError("the mount will not park")


mount.park = refuse
result = seq.shut_down()
case("a mount that will not park does not stop the rest",
     "close the cover" in result["done"] and panel.cover_state == "closed")
case("and the failure is reported rather than swallowed",
     "park the mount" in result["failed"], f"{result['failed']}")
case("the cameras still warm", seq.warming)
seq._stop_warming()

# A cover that never finishes moving is reported, not waited on for ever.
panel = Panel(sticks=True)
panel._state = "moving"
seq, rig, config = build({"flatpanel": panel})
result = seq.shut_down()
case("a cover that sticks is reported", "close the cover" in result["failed"],
     f"{result['failed']}")

# Nothing connected at all must not throw.
seq, rig, config = build({})
result = seq.shut_down()
case("shutting down an empty rig is not an error", not result["failed"],
     f"{result}")

# A panel with no cover is left alone beyond putting its light out.
panel = Panel(has_cover=False, state="notpresent")
seq, rig, config = build({"flatpanel": panel})
seq.shut_down()
case("a panel with no cover is not asked to close one", panel.closes == 0)

# A driver that sets AtPark a moment after it stops moving must not be reported
# as having failed to park. This was a real false alarm on 2026-09-17.
mount = SlowParkMount(delay=2.0)
seq, rig, config = build({"mount": mount})
config.update("sequencer", {"parkTimeoutSeconds": 20.0})
result = seq.shut_down()
case("a mount that sets AtPark late is still counted as parked",
     "park the mount" in result["done"], f"{result['failed']}")
seq._stop_warming()

# One that never parks is still reported, which is the point of checking at all.
mount = SlowParkMount(delay=9999.0)
seq, rig, config = build({"mount": mount})
config.update("sequencer", {"parkTimeoutSeconds": 3.0})
result = seq.shut_down()
case("and one that never parks is still reported",
     "park the mount" in result["failed"], f"{result['failed']}")
seq._stop_warming()

# ------------------------------------------------------------------ the roof


class Roof:
    def __init__(self, shutter="open", can_shutter=True, can_park=True,
                 can_slave=True, sticks=False):
        self.connected = True
        self.can_shutter = can_shutter
        self.can_park = can_park
        self.can_slave = can_slave
        self._shutter = shutter if can_shutter else "notpresent"
        self.sticks = sticks
        self.slaved = True
        self.at_park = False
        self.closes = 0
        self.parks = 0

    @property
    def shutter_state(self):
        return self._shutter

    def close_shutter(self):
        self.closes += 1
        if not self.sticks:
            self._shutter = "closed"

    def park(self):
        self.parks += 1
        self.at_park = True

    def set_slaved(self, on):
        self.slaved = bool(on)


order = []
roof = Roof()
mount = Mount()
seq, rig, config = build({"dome": roof, "mount": mount})
real_park = mount.park
mount.park = lambda: (order.append("scope"), real_park())
roof.close_shutter = lambda: (order.append("roof"), setattr(roof, "_shutter", "closed"))
result = seq.shut_down()
case("shutting down closes the roof", "close the roof" in result["done"])
case("the telescope parks before the roof closes",
     order.index("scope") < order.index("roof"), str(order))
case("and slaving is turned off first", roof.slaved is False)
case("the dome parks too", roof.parks == 1)
seq._stop_warming()

roof = Roof(sticks=True)
roof._shutter = "closing"
seq, rig, config = build({"dome": roof})
result = seq.shut_down()
case("a roof that will not close is reported",
     "close the roof" in result["failed"], f"{result['failed']}")
seq._stop_warming()

roof = Roof(can_shutter=False)
seq, rig, config = build({"dome": roof})
seq.shut_down()
case("a dome with no shutter is parked rather than asked to close",
     roof.closes == 0 and roof.parks == 1)
seq._stop_warming()

roof = Roof(shutter="closed")
seq, rig, config = build({"dome": roof})
seq.shut_down()
case("a roof already closed is left alone", roof.closes == 0)
seq._stop_warming()

# --------------------------------------------------------- the weather stops it
seq, rig, config = build({})
config.update("safety", {"onUnsafe": "shutdown"})
seq.weather_unsafe("rain")
case("unsafe weather shuts the observatory down", seq.held_for_weather())
seq._stop_warming()

# Paused instead, for a building whose roof somebody else controls.
guider = Guider()
seq, rig, config = build({"guider": guider})
config.update("safety", {"onUnsafe": "pause"})
# `running` means "the thread is alive", so give it a live one rather than
# monkeypatching the property — patching it on the class breaks every later
# check in this file, which is exactly what it did.
holding = threading.Event()
seq._thread = threading.Thread(target=lambda: holding.wait(30.0), daemon=True)
seq._thread.start()
seq.weather_unsafe("cloud")
case("with onUnsafe=pause a running sequence is held, not shut down",
     seq.held_for_weather() and seq.paused and guider.stops == 0)
seq.weather_safe()
case("and carries on when the sky clears",
     not seq.held_for_weather() and not seq.paused)
holding.set()
seq._thread.join(timeout=5.0)
seq._thread = None

# ----------------------------------------- a warm-down yields to real work
#
# A warm-down runs for ten minutes after a sequence ends, raising the setpoint a
# few degrees a minute. A calibration run started during one shoots its darks up
# a temperature ramp, and a dark library is indexed by temperature — so those
# frames match nothing and are quietly worthless.

camera = Camera(temperature=-10.0)
seq, rig, config = build({"camera": camera})
config.update("camera", {"warmAtEnd": True, "coolAtStart": True, "setpoint": -15.0})
seq._start_warming()
case("a warm-down is running", seq.warming)

cancelled = seq.cancel_warming("a calibration run was started")
case("starting real work cancels it", cancelled and not seq.warming)
case("and puts the camera back on its setpoint",
     camera.setpoints and camera.setpoints[-1] == -15.0, f"{camera.setpoints[-1:]}")
case("with the cooler on", camera.cooler is True)
case("and the temperature wait owed again before the first frame",
     "rig1" in seq._cooling_wanted)

# Nothing to cancel is not an error, and says so by returning False.
case("cancelling when nothing is warming does nothing",
     seq.cancel_warming("x") is False)

# A camera that is not cooled at start is left alone rather than switched on.
camera = Camera(temperature=-10.0)
seq, rig, config = build({"camera": camera})
config.update("camera", {"warmAtEnd": True, "coolAtStart": False})
seq._start_warming()
seq.cancel_warming("x")
case("a camera set not to cool is not switched on by the cancel",
     camera.cooler is not True, f"cooler={camera.cooler}")

# ------------------------------------------------- ending the night on time
#
# The rig was found autofocusing at the ground at dawn. Every expensive thing —
# the slew, the centre, the focus sweep, settling the guider — used to happen
# before anything checked the clock or the horizon, so the end time was noticed
# only after the telescope had been driven to a field that had set and the
# focuser swept through it.

HOUR = 3600.0
seq, rig, config = build({})
seq._dawn_at = 0.0                               # no site: dawn cannot be known
entry = {"name": "Cygnus", "endAt": time.time() + 10 * 60}

case("well inside its window, there is no reason to stop",
     seq._past_deadline(entry, entry["endAt"], 60.0) is None)

# The point of the fix: a ten-minute setup does not begin with four minutes left.
case("a setup longer than the time left is refused",
     seq._past_deadline(entry, entry["endAt"], 20 * 60) is not None,
     str(seq._past_deadline(entry, entry["endAt"], 20 * 60)))

past = {"name": "Cygnus", "endAt": time.time() - 60}
case("an end time already gone stops it",
     "end time" in (seq._past_deadline(past, past["endAt"]) or ""))

case("a target with no end time is not stopped by the end time",
     seq._past_deadline({"name": "x"}, None) is None)

# ...which is exactly why dawn is a separate backstop. Without it, a plan whose
# targets have no end times has no end at all.
seq, rig, config = build({})
seq._dawn_at = time.time() + 30 * 60
case("dawn stops a target that has no end time of its own",
     "night is over" in (seq._past_deadline({"name": "x"}, None, 45 * 60) or ""),
     str(seq._past_deadline({"name": "x"}, None, 45 * 60)))
case("but not while there is still night left",
     seq._past_deadline({"name": "x"}, None, 60.0) is None)

# The margin keeps the last frame finishing in the dark rather than starting.
config.update("sequencer", {"dawnMarginMinutes": 45.0})
case("the margin stops it before dark actually ends",
     seq._past_deadline({"name": "x"}, None, 60.0) is not None)

config.update("sequencer", {"stopAtDawn": False, "dawnMarginMinutes": 0.0})
case("and turning the dawn stop off leaves only the plan's own times",
     seq._past_deadline({"name": "x"}, None, 10 * HOUR) is None)

# A site that cannot be worked out must not stop the night forever.
seq, rig, config = build({})
seq._dawn_at = 0.0
case("no dawn known means no dawn stop",
     seq._past_deadline({"name": "x"}, None, 10 * HOUR) is None)

# --------------------------------------------- focusing at a field that has set
#
# The thing actually found happening. A focus sweep is the longest operation the
# sequencer has, and on a starless frame it fails slowly.

focus_runs = []


class CountingRig(Rig):
    pass


seq, rig, config = build({})
config.update("schedule", {"minAltitude": 30.0})
config.update("site", {"latitude": 40.0, "longitude": -111.0, "useMount": False})
seq._focusable = lambda: []                      # nothing to focus, but the
seq._maybe_focus(panel={"ra": 0.0, "dec": -80.0}, label="set target",
                 entry={"name": "set target"})   # guard runs before that
case("focusing is refused for a field below the altitude limit",
     any("Not focusing" in m for _, m in LOG), LOG[-1][1] if LOG else "")

LOG.clear()
seq, rig, config = build({})
seq._dawn_at = time.time() + 60
seq._focusable = lambda: []
seq._maybe_focus(panel={"ra": 0.0, "dec": 89.0}, label="high target",
                 entry={"name": "high target"})
case("and refused when there is not enough night left for a sweep",
     any("not enough night" in m for _, m in LOG), LOG[-1][1] if LOG else "")

# ------------------------------------------------- abort has to actually stop it
#
# Abort parked the mount and reported success while the run was still going, so
# the sequence slewed away again and carried on. The flag is now latched before
# anything else happens.

seq, rig, config = build({})
config.update("sequencer", {"abortTimeoutSeconds": 1.0})
case("the abort flag is not set before it is asked for", not seq.stopping)
seq.shut_down()
case("shutting down latches the abort immediately", seq.stopping)
seq._stop_warming()

# And a run that will not stop is reported as a failure, not a success.
seq, rig, config = build({})
config.update("sequencer", {"abortTimeoutSeconds": 1.0})
stuck = threading.Event()


def never_stops():
    stuck.wait(30.0)


seq._thread = threading.Thread(target=never_stops, daemon=True)
seq._thread.start()
result = seq.shut_down()
case("a run that will not stop is reported as a failure",
     "stop the sequence" in result["failed"], f"{result['failed']}")
case("and it parks anyway rather than leaving the scope up",
     "park the mount" in result["done"] or "park the mount" in result["failed"])
stuck.set()
seq._stop_warming()

# --------------------------------------------------- night after night
#
# Run on loop: the observatory opens itself at dusk and puts itself to bed at
# dawn, every night, with nobody in the room. The part that matters most is
# the mount homing and parking itself; the part that was missing was the cover
# ever being opened again.


class OpeningPanel(Panel):
    def __init__(self, state="closed", sticks=False):
        super().__init__(has_cover=True, state=state, sticks=sticks)
        self.opens = 0

    def open_cover(self):
        self.opens += 1
        if not self.sticks:
            self._state = "open"


panel = OpeningPanel(state="closed")
seq, rig, config = build({"flatpanel": panel})
seq._open_cover()
case("a closed cover is opened before the first slew",
     panel.opens == 1 and panel.cover_state == "open")
case("and its light is put out first", panel.off == 1)

panel = OpeningPanel(state="open")
seq, rig, config = build({"flatpanel": panel})
seq._open_cover()
case("a cover already open is left alone", panel.opens == 0)

panel = OpeningPanel(state="closed", sticks=True)
seq, rig, config = build({"flatpanel": panel})
try:
    seq._open_cover()
    case("a cover that will not open ends the night", False)
except DeviceError as exc:
    case("a cover that will not open ends the night", "not open" in str(exc), str(exc))

# The mount is released from park whether or not it is about to be homed:
# a night that parked itself yesterday has to be able to start today.
mount = Mount(can_find_home=False)
mount.at_park = True
seq, rig, config = build({"mount": mount})
seq._home_mount()
case("a parked mount that cannot home is still released",
     mount.unparks == 1 and mount.at_park is False)

mount = Mount()
mount.at_park = True
seq, rig, config = build({"mount": mount})
config.update("sequencer", {"homeAtStart": False})
seq._home_mount()
case("and so is one with homing turned off",
     mount.unparks == 1 and mount.homes == 0)

# The ASCOM simulator's own words: "SlewToCoordinatesAsync is not allowed
# when tracking is False" - which is how every mount comes out of park.
mount = Mount()
mount.at_park = True
mount.tracking = False
seq, rig, config = build({"mount": mount})
seq._release_mount(mount)
case("tracking is switched on when the mount is released",
     mount.unparks == 1 and mount.tracking is True)

# A mount still slewing to its park position is waited for, not unparked
# out from under the slew.
mount = Mount()
mount.slewing = True
mount.at_park = False
seq, rig, config = build({"mount": mount})
config.update("sequencer", {"parkTimeoutSeconds": 1.0})
threading.Timer(0.4, lambda: (setattr(mount, "slewing", False),
                              setattr(mount, "at_park", True))).start()
seq._release_mount(mount)
case("a mount still parking is waited for and then released",
     mount.unparks == 1 and mount.at_park is False)

# Bed: cover, then home, then park - and the park is verified.
order = []
guider = Guider()
mount = Mount()
panel = Panel()
camera = Camera(temperature=-10.0)
seq, rig, config = build({"guider": guider, "mount": mount,
                          "flatpanel": panel, "camera": camera})
guider.stop_guiding = lambda: order.append("guide")
panel.close_cover = lambda: (order.append("cover"), setattr(panel, "_state", "closed"))
real_home, real_park = mount.find_home, mount.park
mount.find_home = lambda: (order.append("home"), real_home())
mount.park = lambda: (order.append("park"), real_park())
told = []
seq.notifier = types.SimpleNamespace(send=lambda *a: told.append(a))
result = seq._put_to_bed()
case("bed stops guiding, closes the cover, homes, then parks",
     order == ["guide", "cover", "home", "park"], str(order))
case("and nothing failed", not result["failed"], str(result["failed"]))
case("and the cameras warm afterwards", seq.warming)
case("and somebody is told the night is over and the scope is parked",
     told and told[-1][0] == "sequenceEnd" and "parked" in told[-1][1], str(told))
seq._stop_warming()

# A park that cannot be verified is the one thing sent as a failure.
mount = Mount()
mount.park = refuse
seq, rig, config = build({"mount": mount})
told = []
seq.notifier = types.SimpleNamespace(send=lambda *a: told.append(a))
result = seq._put_to_bed()
case("a mount that will not park at bedtime is a failure, not a summary",
     "park the mount" in result["failed"]
     and told and told[-1][0] == "failure" and "did not park" in told[-1][1],
     f"{result['failed']} {told}")
seq._stop_warming()

# The next night is the next one, not tonight again. A plan that ran out at
# one in the morning must not be run again at ten past.
from astrocontrol import schedule                                # noqa: E402

seq, rig, config = build({})
config.update("site", {"latitude": 31.9, "longitude": -99.1, "useMount": False})
tonight = schedule.night(31.9, -99.1)
midnight = (tonight["duskAstronomical"] + tonight["dawnAstronomical"]) / 2
found = seq._next_dusk(now=midnight)
case("in the dark, the loop starts straight away",
     found is not None and found[0] == midnight, str(found))
found = seq._next_dusk(now=midnight, after=tonight["dawnAstronomical"])
case("after a night is run, the next dusk is tomorrow's",
     found is not None and found[0] > tonight["dawnAstronomical"], str(found))
afternoon = tonight["duskAstronomical"] - 4 * HOUR
found = seq._next_dusk(now=afternoon)
case("in the afternoon it waits for dusk, less the lead",
     found is not None and abs(found[0] - (tonight["duskAstronomical"] - 20 * 60)) < 1,
     str(found))

# The loop itself: night, bed, wait, night... until Stop.
seq, rig, config = build({})
config.update("site", {"latitude": 31.9, "longitude": -99.1, "useMount": False})
ran = []
seq._night = lambda: ran.append("night")
seq._put_to_bed = lambda: ran.append("bed")
waits = []


def fake_wait(after=None):
    waits.append(after)
    if len(waits) >= 3:
        seq._abort.set()                        # somebody pressed Stop
        return False
    return True


seq._wait_for_dusk = fake_wait
seq._dawn = lambda: 12345.0
seq._loop = True
seq._run()
case("on loop the nights follow each other until stopped",
     ran == ["night", "bed", "night", "bed"], str(ran))
case("each wait is for the night after the one just run",
     waits == [None, 12345.0, 12345.0], str(waits))
case("and the loop is over when the thread is", seq._loop is False)

seq, rig, config = build({})
ran = []
seq._night = lambda: ran.append("night")
seq._put_to_bed = lambda: ran.append("bed")
seq._loop = False
seq._run()
case("not on loop, one night and no bed of its own", ran == ["night"], str(ran))

# A loop is refused where it could not put the mount to bed.
mount = Mount()
mount.can_park = False
seq, rig, config = build({"mount": mount, "camera": Camera()})
seq.plan = types.SimpleNamespace(raw=lambda: {"entries": [{"id": "e", "name": "x"}]})
try:
    seq.start(loop=True)
    case("a mount that cannot park is refused the loop", False)
except DeviceError as exc:
    case("a mount that cannot park is refused the loop", "cannot park" in str(exc), str(exc))

# ------------------------------------------------------ the meridian flip
#
# A mount that does not say which side of the pier it is on is still a mount
# that runs into its limit; and a panel slewed to after transit is already on
# the right side and must not be "flipped" a second time.
seq, rig, config = build({"mount": Mount()})
config.update("site", {"latitude": 31.9, "longitude": -99.1, "useMount": False})
from astrocontrol import astro                                   # noqa: E402

lst = astro.local_sidereal_hours(-99.1)
before = {"ra": (lst + 0.05) % 24.0, "dec": 10.0}               # 3 min to transit
case("a flip is due for a frame that would run into the meridian",
     seq._flip_due(before, 600.0) is True)
case("even on a mount that reports no side of pier",
     Mount().__dict__.get("side_of_pier") is None and seq._flip_due(before, 600.0) is True)
config.update("sequencer", {"meridianFlipEnabled": False})
case("and not at all with the flip turned off", seq._flip_due(before, 600.0) is False)
config.update("sequencer", {"meridianFlipEnabled": True})
later = {"ra": (lst - 0.5) % 24.0, "dec": 10.0}                 # half an hour past
ahead = seq._hours_to_meridian(later["ra"])
case("a panel slewed to after transit counts as already flipped",
     ahead is not None and ahead < 0.0, str(ahead))

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
