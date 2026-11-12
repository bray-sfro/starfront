"""Every light is taken at the telescope's gain/offset, proven by read-back (0.2.53).

    python tools/check_gain_offset.py

2026-11-04, EAGLE (twin FSQ, ASI6200MM on ZWO's ASCOM driver ASICamera2): the
telescope's settings had no gain or offset, so Starfront never sent any; the
driver happened to be at 100 / 50 at dusk and P6_G_0001 was right. At 02:54
a StartExposure failed during autofocus, the camera was reconnected, the
reopened driver came up at 0 / 0, "Camera settings restored: cooler off" put
nothing back, and the next 36 m42 subs were gain 0 / offset 0 against a
calibration library at 100 / 50. Nothing said so until processing.

These drive the real AscomCamera wrapper, DeviceManager, Rig / RigSet (the
reconnect and the restore), CaptureService and the warnings board against a
fake ASICamera2 that, like the real one, comes up at gain 0 / offset 0 every
time it is opened, and that puts up its modal box for any write while it has
lost the camera.
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
TMP = Path(tempfile.mkdtemp(prefix="gainoffset-"))
os.environ["ASTRO_DATA_DIR"] = str(TMP / "data")
os.environ["HOME"] = str(TMP / "home")
os.environ["LOCALAPPDATA"] = str(TMP / "lad")

from astrocontrol.devices import ascom, driverdialogs              # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Desktop:
    """MessageBox, per thread: a box that comes up is a FAIL here."""

    def __init__(self):
        self.lock = threading.Lock()
        self.windows = {}
        self.next = 100
        self.shown = []

    def show(self, text, wait=5.0):
        with self.lock:
            self.next += 1
            hwnd = self.next
            done = threading.Event()
            self.windows[hwnd] = [threading.get_native_id(), text, done]
            self.shown.append(text)
        done.wait(wait)
        with self.lock:
            self.windows.pop(hwnd, None)

    def dialogs(self, thread_id):
        with self.lock:
            return [(h, "error | " + w[1]) for h, w in self.windows.items() if w[0] == thread_id]

    def close(self, hwnd):
        with self.lock:
            window = self.windows.get(hwnd)
        if window is not None:
            window[2].set()


DESKTOP = Desktop()
driverdialogs.BACKEND = DESKTOP
driverdialogs.POLL_SECONDS = 0.1


class Hardware:
    def __init__(self):
        self.plugged = True
        self.opens = 0
        self.fail_start = 0          # StartExposure raises (ASICamera2 "unspecified")
        self.refuse_gain = False     # Gain/Offset writes are accepted and ignored
        self.gain_unreadable_once = False
        self.dusk = None             # what the driver is at when first opened


HARDWARE = {}
DRIVERS = []


class FakeASICamera2:
    def __init__(self, prog_id):
        hw = HARDWARE[prog_id]
        self.__dict__["hw"] = hw
        gain, offset = (hw.dusk or (0, 0)) if hw.opens == 0 else (0, 0)
        self.__dict__["v"] = {
            "Connected": False, "PixelSizeX": 3.76, "MaxBinX": 4,
            "CanSetCCDTemperature": True, "CanAbortExposure": True,
            "GainMin": 0, "GainMax": 570, "OffsetMin": 0, "OffsetMax": 80,
            "BinX": 1, "BinY": 1, "Gain": gain, "Offset": offset, "SensorType": 0,
            "CCDTemperature": -5.0, "SetCCDTemperature": -5.0, "CoolerOn": True,
            "CoolerPower": 30.0, "CameraState": 0, "PercentCompleted": 0,
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
        if name in ("Gain", "Offset"):
            self.calls.append(("get", name))
            if name == "Gain" and self.hw.gain_unreadable_once:
                self.hw.gain_unreadable_once = False
                raise RuntimeError("Gain: camera not ready")
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
            return
        if name in ("Gain", "Offset", "BinX", "BinY") and self._lost():
            DESKTOP.show(f"Please input {name} between 0 and 0, and restart exposure!")
            return
        if name in ("Gain", "Offset") and self.hw.refuse_gain:
            return
        self.v[name] = value

    def _start(self, duration, light):
        self.calls.append(("StartExposure", float(duration), bool(light)))
        if self.hw.fail_start:
            self.hw.fail_start -= 1
            raise RuntimeError("(-2147352567, 'Exception occurred.', (0, 'ASCOM.ASICamera2', "
                               "'unspecified', None, 0, -2147220479), None)")
        if self._lost():
            DESKTOP.show("Please input Exposure Duration between 0 and 0s, and restart exposure!")
            return
        self.__dict__["started"] = (time.monotonic(), float(duration))

    def _abort(self):
        self.calls.append(("AbortExposure",))
        self.__dict__["started"] = None


def dispatch(prog_id):
    if prog_id in HARDWARE:
        return FakeASICamera2(prog_id)
    raise RuntimeError(f"Invalid class string {prog_id}")


ascom._HAVE_PYWIN32 = True
ascom.pythoncom = types.SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None)
ascom.win32com = types.SimpleNamespace(client=types.SimpleNamespace(Dispatch=dispatch))
ascom.AscomCamera.ABORT_SECONDS = 1.0
ascom.AscomCamera.STUCK_SECONDS = 1.0

from fastapi.testclient import TestClient                          # noqa: E402

from astrocontrol import main, warnings                            # noqa: E402

http = TestClient(main.app)
http.post("/api/equipment/telescopes", json={"name": "Twin B"})
RIGS = list(main.rigs.all)
master, second = RIGS[0], RIGS[1]
PROG = {master.id: "ASCOM.ASICamera2.Camera", second.id: "ASCOM.ASICamera2_2.Camera"}
for rig in RIGS:
    HARDWARE[PROG[rig.id]] = Hardware()
    rig.capture.save_enabled = False
    rig.camera_health._sleep = lambda s: None
HARDWARE[PROG[master.id]].dusk = (100, 50)      # ASICamera2's own remembered setting
hw = HARDWARE[PROG[master.id]]

# EAGLE's settings.json: camera.gain and camera.offset blank.
main.config.update("camera", {"gain": None, "offset": None})
for rig in RIGS:
    r = http.post(f"/api/devices/camera/connect?rig={rig.id}",
                  json={"backend": "ascom", "driverId": PROG[rig.id], "name": "ZWO ASI6200MM Pro"})
    case(f"{rig.name}: camera connects", r.status_code == 200, r.text[:200])


def cam(rig=master):
    return rig.manager.get("camera")


def driver(rig=master):
    return cam(rig)._driver


def logs(rig=master, n=60):
    return [e["message"] for e in rig.manager.recent_events(n)]


def light(rig=master):
    return rig.capture.capture_blocking(0.1, "light")


def board():
    b = warnings.WarningBoard(log=lambda m, level="info": None)
    now = time.time()
    ctx = warnings.Context(
        config=main.config, rigs=types.SimpleNamespace(master=master, all=RIGS),
        sequencer=types.SimpleNamespace(status=lambda: {"running": True, "state": "imaging"}),
        plan=types.SimpleNamespace(raw=lambda: {"entries": []}),
        night=lambda: (now - 600, now + 3600))
    warnings.Watchdog(b, ctx, interval=999, log=lambda m, level="info": None).pass_once()
    return {w["key"]: w for w in b.snapshot()["active"]}


def set_before_start(d):
    """Index of the last Gain write and the StartExposure after it."""
    calls = d.calls
    starts = [i for i, c in enumerate(calls) if c[0] == "StartExposure"]
    return calls, starts


# ------------------------------------------------------------ the EAGLE night
print("-- the EAGLE night: no gain/offset in the profile, a reconnect mid-run --")
rec = light()
case("first light: the driver's dusk 100 / 50, as P6_G_0001", (rec.gain, rec.offset) == (100, 50),
     (rec.gain, rec.offset))
case("...and the session now holds 100 / 50 for this telescope",
     getattr(master, "camera_settings_seen", None) == {"gain": 100, "offset": 50},
     getattr(master, "camera_settings_seen", None))
case("the log says no gain/offset is set and what is being held",
     any("No gain/offset is set" in m and "gain 100 / offset 50" in m for m in logs()))
active = board()
# 0.2.53a: no setting and no past lights to learn from is a warning, not a notice.
case("the board warns: no gain/offset of its own and no history",
     active.get("camera.settings", {}).get("level") == "warning", sorted(active))

old = driver()
opens = hw.opens
hw.fail_start = 1                       # 02:54:48 StartExposure: ASCOM.ASICamera2 unspecified
rec = light()
fresh = driver()
case("the failed StartExposure reconnected the camera", fresh is not old and hw.opens == opens + 1)
case("the reopened driver (0 / 0, as ASICamera2) was sent gain 100",
     ("set", "Gain", 100) in fresh.calls)
case("the restore put 100 / 50 back on it", fresh.v["Gain"] == 100 and fresh.v["Offset"] == 50,
     (fresh.v["Gain"], fresh.v["Offset"]))
case("the retried light is 100 / 50, not 0 / 0 (P6_G_0002..P10_G_0009)",
     (rec.gain, rec.offset) == (100, 50), (rec.gain, rec.offset))
case("the log says what was restored",
     any("Camera settings restored: gain 100, offset 50" in m for m in logs()))

# ------------------------------------------------------------ the profile wins
print("-- with the profile set: set and read back before every light --")
main.config.update("camera", {"gain": 100, "offset": 50})
fresh.v["Gain"], fresh.v["Offset"] = 0, 0          # reset behind the program's back
n0 = len(fresh.calls)
rec = light()
calls = fresh.calls[n0:]
first_start = next(i for i, c in enumerate(calls) if c[0] == "StartExposure")
before = calls[:first_start]
case("read back before the light", ("get", "Gain") in before and ("get", "Offset") in before)
case("set before StartExposure", ("set", "Gain", 100) in before and ("set", "Offset", 50) in before,
     [c for c in before if c[0] == "set"])
case("read back again after setting", [c for c in before if c == ("get", "Gain")].__len__() >= 2)
case("header GAIN/OFFSET 100 / 50", (rec.gain, rec.offset) == (100, 50))
case("the log names what was wrong and the source",
     any("gain was 0" in m and "gain 100 (profile)" in m for m in logs()))
n0 = len(fresh.calls)
light()
calls = fresh.calls[n0:]
case("a camera already right is only read, not written",
     not any(c[0] == "set" and c[1] in ("Gain", "Offset") for c in calls)
     and ("get", "Gain") in calls)
active = board()
case("a matching profile clears the board", "camera.settings" not in active, sorted(active))

main.config.update("camera", {"gain": 120, "offset": 30})
rec = light()
case("a profile change is applied at the next light", (rec.gain, rec.offset) == (120, 30)
     and fresh.v["Gain"] == 120)
main.config.update("camera", {"gain": 100, "offset": 50})

# ------------------------------------------------------------ the reconnect paths
print("-- re-applied after every reconnect path --")
opens = hw.opens
case("camera check reconnect (heal)", master.camera_health.heal("test") and hw.opens == opens + 1
     and driver().v["Gain"] == 100 and driver().v["Offset"] == 50,
     (driver().v["Gain"], driver().v["Offset"]))
hw.plugged = False
driver().__dict__["live"] = False
DESKTOP.shown.clear()
hw.plugged = True
rec = light()
case("lost camera: reconnected before the light, at 100 / 50", (rec.gain, rec.offset) == (100, 50)
     and driver().v["Gain"] == 100)
case("no 'between 0 and 0' box came up (nothing written to a lost driver)", not DESKTOP.shown,
     DESKTOP.shown)
opens = hw.opens
master.manager.disconnect("camera")
outcome = main.rigs.reconnect_camera(master)
case("RigSet.reconnect_camera restores from the profile", "camera" in outcome["connected"]
     and driver().v["Gain"] == 100 and driver().v["Offset"] == 50)

print("-- a Gain that could not be read at connect is not given up on --")
hw.gain_unreadable_once = True
main.config.update("camera", {"gain": None, "offset": None})   # restore has nothing to put back
getattr(master, "camera_settings_seen", {}).clear()
master.manager.disconnect("camera")
main.rigs.reconnect_camera(master)
main.config.update("camera", {"gain": 100, "offset": 50})
c = cam()
case("the Gain read failed at connect (has_gain False, gain 0)",
     getattr(c, "has_gain", True) is False and c.gain == 0, (getattr(c, "has_gain", None), c.gain))
driver().v["Gain"] = 0
rec = light()
case("has_gain is believed again and gain set", getattr(c, "has_gain", False)
     and driver().v["Gain"] == 100 and rec.gain == 100, (getattr(c, "has_gain", None), driver().v["Gain"]))

# ------------------------------------------------------------ refused: warn
print("-- a driver that refuses the gain: honest header and a warning --")
hw.refuse_gain = True
driver().v["Gain"], driver().v["Offset"] = 0, 0
rec = light()
case("the frame is still taken", rec is not None)
case("its header says what the camera read back: GAIN 0 / OFFSET 0", (rec.gain, rec.offset) == (0, 0))
case("an error says the camera did not take it",
     any("did not take the gain/offset" in m for m in logs()))
case("a warning log names the file and both values",
     any(rec.filename in m and "GAIN 0" in m and "gain 100" in m for m in logs()))
active = board()
w = active.get("camera.settings")
case("the warnings board: a warning for this telescope", w is not None and w["level"] == "warning"
     and "GAIN 0" in w["detail"] and "gain 100" in w["detail"], w)
case("only for this telescope", f"camera.settings.{second.id}" not in active)
hw.refuse_gain = False
light()
case("cleared once a light matches again", "camera.settings" not in board())

# ------------------------------------------------------------ not for calibration
print("-- calibration frames and manual captures keep their own --")
cam().set_settings(gain=0, offset=0)
n0 = len(driver().calls)
rec = master.capture.capture_blocking(0.1, "dark")
case("a dark asked for at gain 0 is taken at gain 0 (not forced to the profile)",
     rec.gain == 0 and not any(c[:2] == ("set", "Gain") for c in driver().calls[n0:]))
r = http.post(f"/api/camera/expose?rig={master.id}",
              json={"exposure": 0.1, "frameType": "light", "gain": 200, "offset": 20})
t0 = time.monotonic()
while master.capture.busy and time.monotonic() - t0 < 10:
    time.sleep(0.05)
rec = master.capture.latest() if hasattr(master.capture, "latest") else None
case("a manual light at gain 200 is taken at 200", r.status_code == 200
     and driver().v["Gain"] == 200 and driver().v["Offset"] == 20, (r.status_code, driver().v["Gain"]))
case("...and no mismatch is claimed for it", "camera.settings" not in board())
rec = light()
case("the next sequence light is back on the profile's 100 / 50", (rec.gain, rec.offset) == (100, 50))

# ------------------------------------------------------------ the panel, no profile
print("-- no profile: the Camera panel's gain is what a reconnect puts back --")
getattr(second, "camera_settings_seen", {}).clear()
r = http.post(f"/api/camera/settings?rig={second.id}", json={"gain": 100, "offset": 50})
case("panel set", r.status_code == 200)
HARDWARE[PROG[second.id]].fail_start = 1
second_rec = light(second)
case("second telescope after a reconnect: 100 / 50 from the panel",
     (second_rec.gain, second_rec.offset) == (100, 50), (second_rec.gain, second_rec.offset))

# ------------------------------------------------------------ history (0.2.53a)
print("-- blank profile: the camera's own historical gain/offset (Bray, 12:56) --")
from astrocontrol.imaging import fits as _fits                     # noqa: E402
import numpy as _np                                                # noqa: E402


def past_lights(rig, n, gain, offset, camera="ZWO ASI6200MM Pro", night="2026-10-20",
                kind="Light", folder=None):
    base = Path(rig.capture.root_dir) / "M42" / night
    sub = folder if folder is not None else (rig.capture.subfolder or "")
    target = base / sub if sub else base
    target.mkdir(parents=True, exist_ok=True)
    start = len(list(target.glob("*.fits")))
    for i in range(n):
        _fits.write(target / f"past_{gain}_{offset}_{kind}_{start + i:04d}.fits",
                    _np.zeros((4, 4), dtype=_np.uint16),
                    {"IMAGETYP": (kind, ""), "GAIN": (gain, ""), "OFFSET": (offset, ""),
                     "INSTRUME": (camera, ""), "TELESCOP": ("Telescope 1", "")})


def fresh_session(rig=master):
    main.config.update("camera", {"gain": None, "offset": None})
    getattr(rig, "camera_settings_seen", {}).clear()
    rig.camera_settings_seen_from = ""
    rig.capture._history_key = None
    rig.capture._said_unset = False


fresh_session()
past_lights(master, 5, 100, 50)
past_lights(master, 2, 120, 30)
past_lights(master, 20, 200, 10, camera="ZWO ASI2600MC Pro")       # another camera
past_lights(master, 20, 300, 5, folder=second.capture.subfolder)  # another telescope's folder
past_lights(master, 20, 250, 25, kind="Dark")                     # not lights
driver().v["Gain"], driver().v["Offset"] = 7, 7                    # whatever the driver is at
rec = light()
case("blank + history: the light is at the camera's usual 100 / 50 (mode), not the driver's 7 / 7",
     (rec.gain, rec.offset) == (100, 50), (rec.gain, rec.offset))
hist = master.capture._history or {}
case("only this camera's own lights in this telescope's folder counted",
     hist.get("lights") == 7 and hist.get("count") == 5, hist)
case("the log says it, with the count",
     any("using historical gain 100 / offset 50 (from 5 of 7 lights" in m for m in logs()))
w = board().get("camera.settings", {})
case("warnings strip: 'using historical gain 100 / offset 50 (from 5 lights)'",
     "using historical gain 100 / offset 50 (from 5 lights)" in w.get("title", ""), w)
hw.fail_start = 1
rec = light()
case("a reconnect restores the historical 100 / 50", (rec.gain, rec.offset) == (100, 50)
     and driver().v["Gain"] == 100, (rec.gain, rec.offset))

print("-- history polluted with 0/0 bug frames --")
fresh_session()
past_lights(master, 36, 0, 0, night="2026-11-04")                 # the EAGLE m42 G subs
rec = light()
hist = master.capture._history or {}
case("36 0/0 frames outnumber 5 good ones and are still ignored: 100 / 50",
     (rec.gain, rec.offset) == (100, 50) and hist.get("zeroes") == 36, hist)
case("the log says how many 0/0 frames were ignored",
     any("36 0/0 frames ignored" in m for m in logs()))

print("-- an explicit setting always wins over history --")
main.config.update("camera", {"gain": 139, "offset": 21})
rec = light()
case("profile 139 / 21 beats the history", (rec.gain, rec.offset) == (139, 21))
fresh_session()
main.config.update("camera", {"gain": None, "offset": 77})
rec = light()
case("key by key: blank gain from history, offset 77 from the profile",
     (rec.gain, rec.offset) == (100, 77), (rec.gain, rec.offset))

print("-- blank + no history: keep the camera's value and warn loudly --")
import shutil as _shutil                                           # noqa: E402
_shutil.rmtree(Path(master.capture.root_dir) / "M42", ignore_errors=True)
fresh_session()
driver().v["Gain"], driver().v["Offset"] = 90, 40
rec = light()
case("no history: the camera's own 90 / 40 is kept", (rec.gain, rec.offset) == (90, 40),
     (rec.gain, rec.offset))
case("the log warns that nothing is set and there is no history",
     any("No gain/offset is set" in m and "no past lights" in m for m in logs()))
w = board().get("camera.settings", {})
case("the warnings strip has a warning (not a notice)", w.get("level") == "warning", w)
main.config.update("camera", {"gain": 100, "offset": 50})

case("no driver box at any point", not DESKTOP.shown, DESKTOP.shown)
passed = sum(results)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
