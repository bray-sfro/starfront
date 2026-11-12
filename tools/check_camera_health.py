"""A camera its driver has lost is never sent StartExposure (0.2.52e).

    python tools/check_camera_health.py

2026-11-04, FSQ106: ZWO's ASCOM camera driver (ASCOM.ASICamera2) had lost the
ASI6200 but still said Connected. Its exposure limits read 0 to 0 s, and
StartExposure put up a modal box - "Please input Exposure Duration between 0
and 0s, and restart exposure!" - that does not return until somebody clicks
OK. The exposure never started, Abort queued behind the box, the run hung.

These drive the real AscomCamera wrapper - its COM apartment threads, its
timeouts, the driver-dialog net - against a fake ASICamera2 COM object that
does exactly that (limits 0/0, a blocking box from StartExposure and from a
Gain write), through the real program: DeviceManager, Rig and RigSet (the
reconnect and the settings restore), CaptureService, /api/camera/abort, the
idle camera watch and the warnings board. Three telescopes for the RASA case.
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
TMP = Path(tempfile.mkdtemp(prefix="camerahealth-"))
os.environ["ASTRO_DATA_DIR"] = str(TMP / "data")
os.environ["HOME"] = str(TMP / "home")
os.environ["LOCALAPPDATA"] = str(TMP / "lad")

from astrocontrol.devices import ascom, driverdialogs              # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


# ------------------------------------------------------------------ the desktop
class Desktop:
    """Top-level windows, per Windows thread: what MessageBox puts up."""

    def __init__(self):
        self.lock = threading.Lock()
        self.windows = {}          # hwnd -> [thread id, text, Event]
        self.next = 100
        self.shown = []
        self.closed = []

    def show(self, text, wait=60.0):
        """MessageBox: modal, on the calling thread, until it is closed."""
        with self.lock:
            self.next += 1
            hwnd = self.next
            done = threading.Event()
            self.windows[hwnd] = [threading.get_native_id(), text, done]
            self.shown.append((time.monotonic(), text))
        done.wait(wait)
        with self.lock:
            self.windows.pop(hwnd, None)

    # the driverdialogs backend
    def dialogs(self, thread_id):
        with self.lock:
            return [(h, "error | " + w[1]) for h, w in self.windows.items()
                    if w[0] == thread_id]

    def close(self, hwnd):
        with self.lock:
            window = self.windows.get(hwnd)
        if window is not None:
            self.closed.append((time.monotonic(), window[1]))
            window[2].set()

    def close_all(self):
        with self.lock:
            for window in self.windows.values():
                window[2].set()


DESKTOP = Desktop()
driverdialogs.BACKEND = DESKTOP
driverdialogs.POLL_SECONDS = 0.1

EXPOSURE_BOX = "Please input Exposure Duration between 0 and 0s, and restart exposure!"


class Hardware:
    """The camera at the end of the USB cable."""

    def __init__(self):
        self.plugged = True
        self.opens = 0
        self.die_on_start = False      # falls off the bus as StartExposure arrives
        self.other_box = False         # StartExposure puts up a box nobody knows
        self.unplug_after_open = False


HARDWARE = {}
DRIVERS = []


class FakeASICamera2:
    """ZWO's ASCOM camera driver, as far as Starfront can see it.

    While it has its camera: real limits and readings. Once it has lost it it
    still says Connected, its exposure limits read 0 to 0 s, and StartExposure
    or a Gain write puts up a modal box on the calling thread."""

    def __init__(self, prog_id):
        self.__dict__["hw"] = HARDWARE[prog_id]
        self.__dict__["v"] = {
            "Connected": False, "CameraXSize": 0, "CameraYSize": 0, "PixelSizeX": 3.76,
            "MaxBinX": 4, "CanSetCCDTemperature": True, "CanAbortExposure": True,
            "GainMin": 0, "GainMax": 570, "OffsetMin": 0, "OffsetMax": 80,
            "BinX": 1, "BinY": 1, "Gain": 0, "Offset": 0, "SensorType": 0,
            "CCDTemperature": 12.0, "SetCCDTemperature": 0.0, "CoolerOn": False,
            "CoolerPower": 0.0, "CameraState": 0, "PercentCompleted": 0,
            "LastExposureDuration": 0.0}
        self.__dict__["live"] = False
        self.__dict__["started"] = None
        self.__dict__["calls"] = []
        DRIVERS.append(self)

    def _lost(self):
        return not (self.live and self.hw.plugged)

    def __getattr__(self, name):
        if name == "ExposureMin":
            return 0.0 if self._lost() else 0.000032
        if name == "ExposureMax":
            return 0.0 if self._lost() else 2000.0
        if name == "CameraXSize":
            return 64 if self.v["Connected"] else 0
        if name == "CameraYSize":
            return 48 if self.v["Connected"] else 0
        if name == "ImageReady":
            s = self.started
            return s is not None and time.monotonic() - s[0] >= s[1]
        if name == "ImageArray":
            return [[1000 + x + y for y in range(48)] for x in range(64)]
        if name == "StartExposure":
            return self._start
        if name == "AbortExposure":
            return self._abort
        if name in self.v:
            return self.v[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        self.calls.append(("set", name, value))
        if name == "Connected":
            self.v["Connected"] = bool(value)
            if value:
                self.hw.opens += 1
                self.__dict__["live"] = self.hw.plugged
                if self.hw.unplug_after_open:
                    self.hw.plugged = False
            return
        if name in ("Gain", "Offset", "BinX", "BinY") and self._lost():
            DESKTOP.show(f"Please input {name} between 0 and 0, and restart exposure!")
            return
        self.v[name] = value

    def _start(self, duration, light):
        self.calls.append(("StartExposure", float(duration), bool(light)))
        if self.hw.other_box:
            DESKTOP.show("The camera firmware needs updating. Click OK to continue.")
            return
        if self.hw.die_on_start:
            self.hw.plugged = False
        if self._lost():
            DESKTOP.show(EXPOSURE_BOX)
            return
        self.__dict__["started"] = (time.monotonic(), float(duration))

    def _abort(self):
        self.calls.append(("AbortExposure",))
        self.__dict__["started"] = None


class FakeEAF:
    """A focuser on the same telescope, on the same driver family."""

    def __init__(self, prog_id):
        self.__dict__["v"] = {"Connected": False, "MaxStep": 60000, "Absolute": True,
                              "StepSize": 0, "Position": 31000, "IsMoving": False,
                              "Temperature": 9.5}

    def __getattr__(self, name):
        if name in self.v:
            return self.v[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        self.v[name] = value


def dispatch(prog_id):
    if "Focuser" in prog_id:
        return FakeEAF(prog_id)
    if prog_id in HARDWARE:
        return FakeASICamera2(prog_id)
    raise RuntimeError(f"Invalid class string {prog_id}")


# A Windows PC with pywin32, as far as the ASCOM backend can tell.
ascom._HAVE_PYWIN32 = True
ascom.pythoncom = types.SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None)
ascom.win32com = types.SimpleNamespace(client=types.SimpleNamespace(Dispatch=dispatch))
ascom.AscomCamera.ABORT_SECONDS = 1.0
ascom.AscomCamera.STUCK_SECONDS = 1.0

from fastapi.testclient import TestClient                          # noqa: E402

from astrocontrol import main, warnings                            # noqa: E402
from astrocontrol.camerahealth import CameraHealth, CameraWatch    # noqa: E402
from astrocontrol.devices.base import CameraLostError, DeviceError  # noqa: E402

http = TestClient(main.app)
for name in ("RASA2", "RASA3"):
    http.post("/api/equipment/telescopes", json={"name": name})
RIGS = list(main.rigs.all)
case("three telescopes", len(RIGS) == 3, [r.name for r in RIGS])
PROG = {}
for i, rig in enumerate(RIGS):
    prog = "ASCOM.ASICamera2.Camera" if i == 0 else f"ASCOM.ASICamera2_{i + 1}.Camera"
    PROG[rig.id] = prog
    HARDWARE[prog] = Hardware()
    r = http.post(f"/api/devices/camera/connect?rig={rig.id}",
                  json={"backend": "ascom", "driverId": prog, "name": f"ZWO ASI6200MM Pro {i + 1}"})
    case(f"{rig.name}: camera connects on the real ASCOM wrapper", r.status_code == 200, r.text[:200])
r = http.post(f"/api/devices/focuser/connect?rig={RIGS[0].id}",
              json={"backend": "ascom", "driverId": "ASCOM.EAF.Focuser", "name": "ZWO EAF"})
case("the master's focuser connects", r.status_code == 200, r.text[:200])

SLEEPS = {}
CLOCK = {"now": 1_000_000.0}
for rig in RIGS:
    SLEEPS[rig.id] = []
    rig.camera_health._sleep = SLEEPS[rig.id].append
    rig.camera_health._clock = lambda: CLOCK["now"]
    rig.capture.save_enabled = False

master = RIGS[0]
hw = HARDWARE[PROG[master.id]]


def cam(rig=master):
    return rig.manager.get("camera")


def driver(rig=master):
    return cam(rig)._driver


def starts(rig=master):
    return [c for c in driver(rig).calls if c[0] == "StartExposure"]


case("the camera has an apartment thread of its own, not the focuser's",
     cam()._channel != master.manager.get("focuser")._channel,
     (cam()._channel, master.manager.get("focuser")._channel))

# ---------------------------------------------------------------- healthy
print("-- a healthy camera --")
case("probe: a live camera answers", cam().probe() is None, cam().probe())
record = master.capture.capture_blocking(0.2, "light")
case("a frame is taken", record is not None and starts()[-1][1] == 0.2, starts()[-1:])
case("no box was put up", not DESKTOP.shown, DESKTOP.shown)
case("clamped into real limits: 3000 s asks for the driver's 2000 s maximum",
     cam().clamp_exposure(3000) == 2000.0)
case("...and a 0 s bias asks for its minimum", cam().clamp_exposure(0) == 0.000032)
case("gain outside its range is clamped, not refused",
     (cam().set_settings(gain=900), driver().v["Gain"])[1] == 570)

# ---------------------------------------------------------------- lost: unit
print("-- a camera its driver has lost (limits 0 to 0 s) --")
hw.plugged = False
reason = cam().probe()
case("probe says so, and why", reason is not None and "0 to 0 s" in reason, reason)
before = len(starts())
try:
    cam().start_exposure(300)
    raised = None
except CameraLostError as exc:
    raised = str(exc)
case("StartExposure is refused before the driver is called", raised is not None
     and len(starts()) == before, raised)
case("...so the driver's box never came up", not DESKTOP.shown, DESKTOP.shown)
gain_before = driver().v["Gain"]
try:
    cam().set_settings(gain=120)
    raised = None
except CameraLostError as exc:
    raised = str(exc)
case("a Gain write is refused too (the same driver box)", raised is not None
     and driver().v["Gain"] == gain_before and not DESKTOP.shown, raised)
case("the 0/0 limits are not used to clamp anything", cam().clamp_exposure(300) == 300)

# ---------------------------------------------------------------- lost: reconnect
print("-- reconnected before the exposure, settings put back --")
main.config.update("camera", {"gain": 100, "offset": 50})
master.note_cooler(True, -5.0)
cam().binning = 2
driver().v["BinX"] = 2
opens = hw.opens
hw.plugged = True        # it is back on the bus; this handle is still the dead one
old_driver = driver()
old_driver.__dict__["live"] = False
old_starts = len([c for c in old_driver.calls if c[0] == "StartExposure"])
record = master.capture.capture_blocking(0.2, "light")
fresh = driver()
case("the frame is taken after a reconnect", record is not None and fresh is not old_driver)
case("one reconnect, at once (no wait before the first)", hw.opens == opens + 1
     and SLEEPS[master.id] == [], (hw.opens - opens, SLEEPS[master.id]))
case("cooler, setpoint, gain, offset and binning are put back",
     fresh.v["CoolerOn"] is True and fresh.v["SetCCDTemperature"] == -5.0
     and fresh.v["Gain"] == 100 and fresh.v["Offset"] == 50 and fresh.v["BinX"] == 2,
     {k: fresh.v[k] for k in ("CoolerOn", "SetCCDTemperature", "Gain", "Offset", "BinX")})
case("StartExposure was never sent to the dead handle",
     len([c for c in old_driver.calls if c[0] == "StartExposure"]) == old_starts)
case("no box", not DESKTOP.shown, DESKTOP.shown)
case("the focuser was not touched", master.manager.get("focuser").connected
     and master.manager.get("focuser").position == 31000)
case("state: reconnected", master.camera_health.state.get("state") == "reconnected",
     master.camera_health.state)

# ---------------------------------------------------------------- lost for good
print("-- a camera that does not come back --")
hw.plugged = False
driver().__dict__["live"] = False
opens = hw.opens
SLEEPS[master.id].clear()
t0 = time.monotonic()
try:
    master.capture.capture_blocking(0.2, "light")
    raised = None
except CameraLostError as exc:
    raised = str(exc)
case("the frame stops with the plain warning",
     raised is not None and "not responding, power-cycle or replug it at the scope" in raised,
     raised)
case("three reconnects, spaced 0 / 10 / 30 s", hw.opens - opens == 3
     and SLEEPS[master.id] == [10.0, 30.0], (hw.opens - opens, SLEEPS[master.id]))
case("it did not hang", time.monotonic() - t0 < 20, f"{time.monotonic() - t0:.1f}s")
case("no box at any point", not DESKTOP.shown, DESKTOP.shown)
case("state: dead, with a retry time 30 min on",
     master.camera_health.state.get("state") == "dead"
     and master.camera_health.state.get("retryAfter") == CLOCK["now"] + 1800,
     master.camera_health.state)
opens = hw.opens
try:
    master.capture.capture_blocking(0.2, "light")
except CameraLostError:
    pass
case("inside the half hour it is not reconnected again", hw.opens == opens)
logs = [e["message"] for e in master.manager.recent_events(80)]
case("the cool-off is said once", sum("not reconnecting it again until" in m for m in logs) == 1,
     [m for m in logs if "until" in m][:3])

print("-- the warnings board --")


def board():
    b = warnings.WarningBoard(log=lambda m, level="info": None)
    now = time.time()
    ctx = warnings.Context(
        config=main.config, rigs=types.SimpleNamespace(master=master, all=RIGS),
        sequencer=types.SimpleNamespace(status=lambda: {"running": True}),
        plan=types.SimpleNamespace(raw=lambda: {"entries": []}),
        night=lambda: (now - 600, now + 3600))
    warnings.Watchdog(b, ctx, interval=999, log=lambda m, level="info": None).pass_once()
    return {w["key"]: w for w in b.snapshot()["active"]}


active = board()
w = active.get("camera.dead")
case("one plain warning: power-cycle or replug it at the scope",
     w is not None and "not responding, power-cycle or replug it at the scope" in w["title"]
     and w["level"] == "warning", w and w["title"])
case("only for that telescope", not any(k.startswith("camera.dead.") for k in active),
     sorted(active))

CLOCK["now"] += 31 * 60
hw.plugged = True
opens = hw.opens
record = master.capture.capture_blocking(0.2, "light")
case("after the half hour, replugged: reconnected and shooting again",
     record is not None and hw.opens == opens + 1)

# ---------------------------------------------------------------- stop / readout
print("-- Stop cuts it short; never during a readout --")
hw.plugged = False
driver().__dict__["live"] = False
opens = hw.opens
ok = master.camera_health.heal("test", stop=lambda: True)
case("a Stop abandons the reconnect before it starts", ok is False and hw.opens == opens)
master.capture._state = "downloading"
ok = master.camera_health.heal("test")
case("not while a frame is being read out", ok is False and hw.opens == opens)
master.capture._state = "idle"
hw.plugged = True
case("and then it heals", master.camera_health.heal("test") is True)

# ---------------------------------------------------------------- the box net
print("-- the box comes up anyway: closed by Starfront, camera reconnected --")
hw.die_on_start = True
DESKTOP.shown.clear()
DESKTOP.closed.clear()
opens = hw.opens
t0 = time.monotonic()
result = {}


def take():
    try:
        result["record"] = master.capture.capture_blocking(0.2, "light")
    except Exception as exc:                    # noqa: BLE001
        result["error"] = exc


t = threading.Thread(target=take)
t.start()
# replug as soon as the box has been seen, so the reconnect finds it
while not DESKTOP.shown and time.monotonic() - t0 < 5:
    time.sleep(0.02)
hw.die_on_start = False
hw.plugged = True
t.join(30)
case("the box came up (a camera lost between the check and the shutter)",
     len(DESKTOP.shown) == 1, DESKTOP.shown)
case("Starfront closed it itself, within ~1 s",
     DESKTOP.closed and DESKTOP.closed[0][0] - DESKTOP.shown[0][0] < 1.5,
     DESKTOP.closed and f"{DESKTOP.closed[0][0] - DESKTOP.shown[0][0]:.2f}s")
case("the camera was reconnected and the frame taken", "record" in result
     and hw.opens == opens + 1, result.get("error"))
logs = [e["message"] for e in master.manager.recent_events(60)]
case("the log says what was closed", any("Starfront closed it" in m for m in logs))

# ---------------------------------------------------------------- abort on a wedge
print("-- a driver wedged by a box nobody knows: Abort still answers --")
hw.other_box = True
DESKTOP.shown.clear()
r = http.post(f"/api/camera/expose?rig={master.id}", json={"exposure": 1, "frameType": "light"})
case("a frame is started by hand", r.status_code == 200, r.text[:200])
t0 = time.monotonic()
while not DESKTOP.shown and time.monotonic() - t0 < 5:
    time.sleep(0.02)
time.sleep(1.2)
focus_t0 = time.monotonic()
position = master.manager.get("focuser").position
case("the focuser still answers while the camera's thread is stuck",
     position == 31000 and time.monotonic() - focus_t0 < 1.0,
     f"{time.monotonic() - focus_t0:.2f}s")
t0 = time.monotonic()
r = http.post(f"/api/camera/abort?rig={master.id}")
took = time.monotonic() - t0
case("Abort returns in seconds, not minutes", r.status_code == 200 and took < 5,
     f"{took:.1f}s {r.status_code}")
case("the capture is no longer busy", not master.capture.busy)
case("an unknown box is left for a person, but said once",
     sum("has a window open" in e["message"] for e in master.manager.recent_events(80)) == 1
     and DESKTOP.windows)
case("probe calls the stuck driver not responding", "stuck" in (cam().probe() or ""),
     cam().probe())
hw.other_box = False
opens = hw.opens
ok = master.camera_health.heal("stuck")
case("reconnect on a fresh camera thread, past the stuck one", ok and hw.opens == opens + 1
     and cam().probe() is None)
DESKTOP.close_all()

# ---------------------------------------------------------------- RASA: per scope
print("-- RASA: one camera lost, the other two untouched --")
r2, r3 = RIGS[1], RIGS[2]
for rig in RIGS:
    rig.capture.save_enabled = False
hw2 = HARDWARE[PROG[r2.id]]
hw3 = HARDWARE[PROG[r3.id]]
hw2.plugged = False
d1, d3 = driver(master), driver(r3)
opens1, opens3 = hw.opens, hw3.opens
errors = {}


def shoot(rig):
    try:
        rig.capture.capture_blocking(0.2, "light")
    except Exception as exc:                    # noqa: BLE001
        errors[rig.id] = str(exc)


threads = [threading.Thread(target=shoot, args=(rig,)) for rig in RIGS]
[t.start() for t in threads]
[t.join(30) for t in threads]
case("RASA2 stops with the plain warning", "power-cycle or replug" in errors.get(r2.id, ""),
     errors)
case("RASA1 and RASA3 took their frames", master.id not in errors and r3.id not in errors)
case("their cameras were not reconnected", hw.opens == opens1 and hw3.opens == opens3
     and driver(master) is d1 and driver(r3) is d3)
active = board()
case("the warning names RASA2 only", f"camera.dead.{r2.id}" in active
     and "camera.dead" not in active and "RASA2" in active[f"camera.dead.{r2.id}"]["title"],
     sorted(k for k in active if k.startswith("camera")))

# ---------------------------------------------------------------- idle watch
print("-- the idle watch --")
CLOCK["now"] += 31 * 60
hw2.plugged = True
idle = {"on": True}
watch = CameraWatch(lambda: RIGS, lambda rig: idle["on"] and not rig.capture.busy)
idle["on"] = False
opens2 = hw2.opens
watch.tick()
case("not while a sequence is running", hw2.opens == opens2)
idle["on"] = True
found = watch.tick()
case("idle: the lost camera is found and reconnected", found and hw2.opens == opens2 + 1
     and cam(r2).probe() is None, found)
found = watch.tick()
case("and a healthy fleet costs only the reads", found == [] and hw2.opens == opens2 + 1)

# ---------------------------------------------------------------- recovery verify
print("-- the sequencer's camera recovery checks the camera answers --")
from astrocontrol import sequencer as seqmod                       # noqa: E402
src = Path(seqmod.__file__).read_text()
case("the background recovery uses the probe, not just Connected",
     "it reconnected but" in src)

DESKTOP.close_all()
passed = sum(results)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
