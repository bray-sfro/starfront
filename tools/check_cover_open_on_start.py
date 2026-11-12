"""The cover opens and is verified before any sky frame (0.2.53i).

    python tools/check_cover_open_on_start.py

Twin FSQ, 2026-11-11: the lid was left shut after an L-flat. The sequence
unparked, homed, slewed, centred and autofocused with no "Opening the cover"
line, because has_cover was latched false at connect and the only open ran
later, for a light frame. The frame after "Cover open" was still ~503 ADU.

Darks, bias, panel flats and autocal must not open the cover, and must not
open the roof.
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp(prefix="cover-open-"))

from astrocontrol import coverprep, roofguard                         # noqa: E402
from astrocontrol.capture import CaptureService                       # noqa: E402
from astrocontrol.config import Config                                # noqa: E402
from astrocontrol.devices.base import DeviceError                     # noqa: E402
from astrocontrol.sequencer import Sequencer                          # noqa: E402

results: list[bool] = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))


class Panel:
    """An Alnitak that can report Unknown and ignore a latched has_cover."""

    def __init__(self, state="closed", has_cover=False, sticks=False):
        self.connected = True
        self.has_cover = has_cover
        self._state = state
        self.sticks = sticks
        self.opens = 0
        self.closes = 0
        self.off = 0
        self.light_on = False
        self.brightness = 0
        self.max_brightness = 100
        self.name = "ASCOM.OptecAlnitak.CoverCalibrator"

    @property
    def cover_state(self):
        return self._state

    def open_cover(self):
        self.opens += 1
        if not self.sticks:
            self._state = "open"

    def close_cover(self):
        self.closes += 1
        if not self.sticks:
            self._state = "closed"

    def turn_off(self):
        self.off += 1
        self.light_on = False

    def turn_on(self, brightness):
        self.brightness = brightness
        self.light_on = self._state == "closed"


class Dome:
    def __init__(self):
        self.connected = True
        self.can_shutter = True
        self.commands = []
        self.shutter_state = "open"

    def open_shutter(self):
        self.commands.append("open")

    def close_shutter(self):
        self.commands.append("close")


class Mount:
    connected = True
    can_find_home = True
    can_park = True
    slewing = False
    tracking = True
    at_park = False
    at_home = True

    def __init__(self):
        self.homes = 0
        self.slews = []

    def find_home(self):
        self.homes += 1

    def slew_to(self, ra, dec):
        self.slews.append((ra, dec))

    def unpark(self):
        self.at_park = False

    def set_tracking(self, on):
        self.tracking = bool(on)


class Manager:
    def __init__(self, devices):
        self.devices = devices
        self.messages = []

    def get(self, kind):
        return self.devices.get(kind)

    def require(self, kind):
        device = self.devices.get(kind)
        if device is None:
            raise DeviceError(f"{kind} not connected")
        return device

    def log(self, message, level="info"):
        self.messages.append((level, str(message)))


class Rig:
    def __init__(self, config, devices):
        self.id = "fsq106"
        self.name = "FSQ106 twin"
        self.role = "master"
        self.config = config
        self.manager = Manager(devices)
        self.capture = devices.get("capture")
        self.solver = None
        self.focuser = None


class Rigs:
    def __init__(self, rig):
        self.master = rig
        self.all = [rig]
        self.messages = rig.manager.messages

    def imaging(self):
        return self.all

    def get(self, _rid):
        return self.master

    def log(self, message, level="info"):
        self.messages.append((level, str(message)))


def build(panel, extra=None):
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    config.update("sequencer", {"coverTimeoutSeconds": 0.4,
                                "roofTimeoutSeconds": 1.0,
                                "parkTimeoutSeconds": 1.0})
    devices = {"flatpanel": panel, "dome": Dome()}
    if extra:
        devices.update(extra)
    rig = Rig(config, devices)
    if rig.capture is None:
        rig.capture = CaptureService(rig.manager, config)
        devices["capture"] = rig.capture
    seq = Sequencer(Rigs(rig), config, None, None)
    return seq, rig, config


# ---------------------------------------------------- (b) unknown is opened
panel = Panel(state="unknown", has_cover=False)
seq, rig, config = build(panel)
seq._ensure_sky_cover("M42")
case("Unknown at sequence start opens the cover even when has_cover was latched false",
     panel.opens == 1 and panel.cover_state == "open",
     f"opens={panel.opens} state={panel.cover_state}")
case("the calibrator light is turned off before the open", panel.off == 1)
case("the roof was not commanded", rig.manager.get("dome").commands == [],
     str(rig.manager.get("dome").commands))

# Already open: re-read, do not command again.
panel = Panel(state="open", has_cover=True)
seq, rig, _config = build(panel)
seq._open_cover()
case("a cover that already reads Open is left alone", panel.opens == 0)

# NotPresent is the only "no cover" answer.
panel = Panel(state="notpresent", has_cover=False)
seq, rig, _config = build(panel)
seq._open_cover()
case("NotPresent is not commanded", panel.opens == 0)

# ---------------------------------------------------- (a) before any sky frame
panel = Panel(state="closed", has_cover=False)
mount = Mount()
seq, rig, _config = build(panel, {"mount": mount})
order = []
seq._home_mount = lambda: order.append("home")
try:
    seq._goto({"ra": 5.5, "dec": -5.0}, "M42")
except Exception as exc:                                  # noqa: BLE001 - we only need the start
    order.append(f"later:{exc}")
case("the cover is opened before the mount is homed or slewed",
     panel.opens == 1 and panel.cover_state == "open" and order[:1] == ["home"],
     str(order))
case("and that open is before any slew", mount.slews == [] or order[0] == "home",
     str(mount.slews))

goto_src = inspect.getsource(Sequencer._goto)
home_at = goto_src.find("_home_mount")
cover_at = goto_src.find("_ensure_sky_cover")
case("centring's slew path checks the cover before homing",
     0 <= cover_at < home_at, f"cover {cover_at} home {home_at}")
focus_src = inspect.getsource(Sequencer._maybe_focus)
case("autofocus checks the cover before the sweep",
     "_ensure_sky_cover" in focus_src)
entry_src = inspect.getsource(Sequencer._run_entry)
case("a light target checks the cover even when the roof is already open",
     entry_src.find("_ensure_sky_cover") < entry_src.find("_ensure_roof_open"))

# A cover that never reports Open holds the run. Nothing is slewed or centred.
panel = Panel(state="closed", has_cover=True, sticks=True)
mount = Mount()
seq, rig, _config = build(panel, {"mount": mount})
held = ""
try:
    seq._goto({"ra": 5.5, "dec": -5.0}, "M42")
except DeviceError as exc:
    held = str(exc)
case("a cover that does not open holds the run",
     "not open" in held and "Holding" in held, held)
case("and does not home or slew or centre",
     mount.homes == 0 and mount.slews == [], f"homes={mount.homes} slews={mount.slews}")
case("the hold does not open the roof", rig.manager.get("dome").commands == [])

# ---------------------------------------------------- (c) the frame itself
reason = coverprep.looks_capped({"median": 503, "stdDev": 6.0}, offset=50)
case("a 503 ADU flat frame is a closed cover", reason is not None, str(reason))
case("a real sky frame is not",
     coverprep.looks_capped({"median": 8400, "stdDev": 900}, offset=50) is None)
try:
    coverprep.judge_sky_frame({"median": 513, "stdDev": 4}, offset=50)
    case("a bias-like sky frame holds the run", False)
except DeviceError as exc:
    case("a bias-like sky frame holds the run",
         "Holding" in str(exc) and "503" not in str(exc) or "513" in str(exc),
         str(exc))

# The capture path used by centring and autofocus raises instead of shooting.
panel = Panel(state="closed", has_cover=False, sticks=True)
manager = Manager({"flatpanel": panel})
cap = CaptureService(manager, Config(Path(tempfile.mkdtemp()) / "c.json"))
cap.config.update("sequencer", {"coverTimeoutSeconds": 0.3})
raised = ""
try:
    cap.open_light_path()
except DeviceError as exc:
    raised = str(exc)
case("a light-path open that times out does not take the frame",
     "Holding" in raised and panel.opens == 1, raised)

panel = Panel(state="unknown", has_cover=False)
manager = Manager({"flatpanel": panel})
cap = CaptureService(manager, Config(Path(tempfile.mkdtemp()) / "c.json"))
done = cap.open_light_path()
case("centring and autofocus open an Unknown cover before the exposure",
     done.get("cover") is True and panel.cover_state == "open" and panel.opens == 1,
     str(done))

# ---------------------------------------------------- (d) reconnect
panel = Panel(state="open", has_cover=True)
seq, rig, _config = build(panel)
seq._open_cover()
panel._state = "closed"
panel.has_cover = False
seq._ensure_sky_cover("FSQ106 twin after the camera reconnect")
case("after a reconnect the cover is re-read and opened again",
     panel.opens == 1 and panel.cover_state == "open",
     f"opens={panel.opens} state={panel.cover_state} latched={panel.has_cover}")
case("reconnect re-check does not command the roof",
     rig.manager.get("dome").commands == [])

# ---------------------------------------------------- (e) darks stay shut
panel = Panel(state="closed", has_cover=True)
dome = Dome()
seq, rig, _config = build(panel)
rig.manager.devices["dome"] = dome
with roofguard.closed_for("bias"):
    seq._ensure_sky_cover("bias")
    seq._open_cover()
case("open-and-verify does not fire for a bias run", panel.opens == 0)
case("and the roof is not opened either", dome.commands == [])

with roofguard.closed_for("dark"):
    seq._ensure_sky_cover("dark")
case("open-and-verify does not fire for a dark run", panel.opens == 0)

with roofguard.closed_for("panel flats"):
    seq._ensure_sky_cover("panel flats")
case("open-and-verify does not fire for panel flats", panel.opens == 0)

for kind, source in (("bias", None), ("dark", None), ("darkflat", None),
                     ("flat", "panel")):
    case(f"{kind}/{source or 'panel'} is not a sky open",
         not coverprep.wants_open(kind, source))
case("a sky flat is a sky open", coverprep.wants_open("flat", "sky"))
case("autocal of darks and panel flats does not want the cover open",
     not any(coverprep.wants_open(kind, source)
             for kind, source in (("bias", None), ("dark", None), ("flat", "panel"))))

# Panel flats still close the cover and turn the light on. No OpenCover.
from astrocontrol.calibrating import CalibrationRunner               # noqa: E402


class Library:
    root = Path(tempfile.mkdtemp(prefix="cover-lib-"))

    def settings(self):
        return {"flatAutoBrightness": False}

    def sub_folder(self, *_a, **_k):
        folder = self.root / "subs"
        folder.mkdir(parents=True, exist_ok=True)
        return folder


class Cam:
    connected = True
    gain = 100
    offset = 50
    name = "ASI"
    temperature = -10
    sensor_width = 0
    sensor_height = 0

    def set_settings(self, **_kw):
        pass


panel = Panel(state="open", has_cover=True)
config = Config(Path(tempfile.mkdtemp()) / "settings.json")
config.update("sequencer", {"coverTimeoutSeconds": 0.4})
devices = {"flatpanel": panel, "camera": Cam(), "dome": Dome()}
rig = Rig(config, devices)
runner = CalibrationRunner(Rigs(rig), config, Library())
runner._prepare_light(rig, {"frameType": "flat", "source": "panel", "filter": "",
                            "brightness": 40, "binning": 1, "gain": 100,
                            "offset": 50}, lambda: None)
case("panel flats close the cover", panel.cover_state == "closed" and panel.closes >= 1,
     f"state={panel.cover_state} closes={panel.closes} opens={panel.opens}")
case("panel flats do not open the cover", panel.opens == 0)
case("panel flats turn the light on with the cover shut",
     panel.light_on and panel.brightness == 40, f"on={panel.light_on} b={panel.brightness}")
case("panel flats do not open the roof", devices["dome"].commands == [])

runner._darken(rig, lambda: None)
case("darks leave the cover closed", panel.cover_state == "closed" and panel.opens == 0)

sky = Panel(state="closed", has_cover=False)
devices["flatpanel"] = sky
rig.manager.devices = devices
runner._open_cover_for_sky(rig, "sky flats")
case("sky flats open a cover that was latched as absent",
     sky.opens == 1 and sky.cover_state == "open")
case("sky flats do not open the roof", devices["dome"].commands == [])

failed = len(results) - sum(results)
print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
