"""Darks, bias and panel flats never open the roof (0.2.53g).

A mock dome records every command. The runs are the real CalibrationRunner:
bias, dark, panel flats, and an autocal recipe of all three (what the
Calibrate tab offers as a full library). Sky flats are not in that set.

    python tools/check_calib_roof_closed.py

Enforced in:

* astrocontrol/roofguard.py
    needs_closed_roof, begin/end, blocked, request_open
* astrocontrol/calibrating.py
    CalibrationRunner._set_on_rig, _shoot_set, _require_roof_closed,
    _require_cover_closed, _one_filter
* astrocontrol/main.py
    dome_shutter — refuses an open while a closed-roof run is active
* astrocontrol/sequencer.py
    Sequencer._ensure_roof_open — goes through request_open
* astrocontrol/devices/ascom.py AscomDome.open_shutter
* astrocontrol/devices/alpaca.py AlpacaDome.open_shutter
    both raise before the driver is asked
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp(prefix="calib-roof-"))

from astrocontrol.calibrating import CalibrationRunner          # noqa: E402
from astrocontrol.config import Config                            # noqa: E402
from astrocontrol.devices.base import DeviceError                 # noqa: E402
from astrocontrol.main import dome_shutter                        # noqa: E402
from astrocontrol import roofguard                                # noqa: E402
from astrocontrol.sequencer import Sequencer                      # noqa: E402

results: list[bool] = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))


class Dome:
    def __init__(self, state="closed"):
        self.connected = True
        self.can_shutter = True
        self.name = "ASCOM.HakosRoof.Dome"
        self.state = state
        self.commands: list[str] = []
        self.reads = 0

    @property
    def shutter_state(self):
        self.reads += 1
        if self.state == "closing" and self.reads > 1:
            self.state = "closed"
        return self.state

    def open_shutter(self):
        self.commands.append("open")
        self.state = "opening"

    def close_shutter(self):
        self.commands.append("close")
        self.state = "closed"


class Panel:
    connected = True
    has_cover = True
    max_brightness = 100

    def __init__(self, cover="closed"):
        self.cover_state = cover
        self.light_on = False
        self.brightness = 0
        self.commands: list[str] = []

    def turn_on(self, brightness):
        self.commands.append(f"on({brightness})")
        self.light_on = self.cover_state == "closed"
        self.brightness = brightness

    def turn_off(self):
        self.commands.append("off")
        self.light_on = False

    def open_cover(self):
        self.commands.append("open-cover")
        self.cover_state = "open"

    def close_cover(self):
        self.commands.append("close-cover")
        self.cover_state = "closed"


class Camera:
    connected = True
    gain = 100
    offset = 50
    name = "ZWO ASI6200MM Pro"
    temperature = -10.0
    sensor_width = 0
    sensor_height = 0

    def set_settings(self, **_kw):
        pass


class Capture:
    def __init__(self, dome: Dome, refuse_open: bool):
        self.dome = dome
        self.refuse_open = refuse_open
        self.exposures: list[tuple] = []
        self.save_enabled = True
        self.calibration_context = ""
        self.open_requests: list[str] = []

    def night_name(self):
        return "2026-11-09"

    def set_output(self, **_kw):
        pass

    def snapshot_output(self):
        return {}

    def restore_output(self, _saved):
        pass

    def capture_blocking(self, exposure, frame_type="light"):
        if self.refuse_open:
            try:
                roofguard.request_open(self.dome)
                self.open_requests.append("sent")
            except DeviceError as exc:
                self.open_requests.append(f"refused: {exc}")
        self.exposures.append((frame_type, float(exposure)))

        class Record:
            path = "frame.fits"
            ccd_temperature = -10.0
            width = 4
            height = 4
            stats = {"median": 25000.0, "saturatedPercent": 0.0, "max": 25000}

        return Record()


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


class Rig:
    def __init__(self, devices):
        self.id = "main"
        self.name = "FSQ85"
        self.manager = Manager(devices)
        self.capture = devices["capture"]


class Rigs:
    def __init__(self, rig):
        self.master = rig
        self.all = [rig]
        self.messages = []

    def log(self, message, level="info"):
        self.messages.append((level, message))

    def imaging(self):
        return [self.master]

    def get(self, rid):
        return self.master if rid in (None, self.master.id) else None


class Library:
    root = Path(tempfile.mkdtemp(prefix="calib-lib-"))

    def settings(self):
        return {}

    def sub_folder(self, *_a, **_k):
        folder = self.root / "subs"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def match(self, *_a, **_k):
        return None, "none"

    def forget(self):
        pass

    def frame(self, *_a, **_k):
        return None


def runner(dome, panel, refuse_open=False):
    capture = Capture(dome, refuse_open)
    rig = Rig({"camera": Camera(), "dome": dome, "flatpanel": panel, "capture": capture})
    rig.capture = capture
    config = Config(Path(tempfile.mkdtemp(prefix="calib-cfg-")) / "settings.json")
    made = CalibrationRunner(Rigs(rig), config, Library())
    made._build = lambda job, stopped, note: {
        "telescope": rig.name, "rig": rig.id, "set": job["label"],
        "frameType": job["kind"], "master": {"path": "master.fits"},
        "info": {"frames": 1, "method": "test"},
        "exposure": job["exposure"], "measuredAdu": job.get("measured"),
    }
    return made, capture


def recipe(kinds):
    sets = []
    for kind in kinds:
        row = {"frameType": kind, "count": 1, "exposure": 1.0, "binning": 1}
        if kind == "flat":
            row["source"] = "panel"
            row["filter"] = ""
        sets.append(row)
    return {"name": "autocal" if len(kinds) > 1 else kinds[0], "sets": sets}


def run(kinds, state="closed", refuse_open=False, cover="closed"):
    dome = Dome(state)
    panel = Panel(cover)
    made, capture = runner(dome, panel, refuse_open=refuse_open)
    made.run(recipe(kinds))
    return dome, capture, made


print("\n-- closed roof, no open command --")

for kinds, label in (
    (["bias"], "bias"),
    (["dark"], "dark"),
    (["flat"], "panel flats"),
    (["bias", "dark", "flat"], "autocal (bias, dark, panel flats)"),
):
    dome, capture, _made = run(kinds, refuse_open=True)
    case(f"{label}: zero open commands",
         dome.commands.count("open") == 0, str(dome.commands))
    case(f"{label}: frames were taken under a closed roof",
         any(kind in ("bias", "dark", "flat") for kind, _e in capture.exposures),
         str(capture.exposures))
    case(f"{label}: an open request during the run was refused",
         capture.open_requests and all(item.startswith("refused") for item in capture.open_requests),
         str(capture.open_requests))
    case("the guard is down after the run", roofguard.blocked() is None)

print("\n-- a roof that reads open aborts the frames --")

dome, capture, made = run(["dark"], state="open")
case("no dark was taken while the roof read open", capture.exposures == [],
     str(capture.exposures))
case("still no open command", "open" not in dome.commands, str(dome.commands))
case("the run says why",
     any("roof" in message.lower() for _level, message in made.rigs.messages),
     str(made.rigs.messages[-3:]))

print("\n-- a roof that is closing is held, then the frames go --")

dome, capture, _made = run(["bias"], state="closing")
case("bias waited until the roof read closed", capture.exposures != [],
     str(capture.exposures))
case("holding did not open it", "open" not in dome.commands, str(dome.commands))

print("\n-- the cover, where there is one --")

dome, capture, made = run(["dark"], cover="open")
# The run closes the cover itself before the frames. That is the existing
# dark path. If it is closed by then, frames proceed and the roof stays shut.
case("the cover is closed for the darks",
     made.rigs.master.manager.get("flatpanel").cover_state == "closed")
case("the roof was not opened to close the cover", "open" not in dome.commands)

print("\n-- sky flats are not under the closed-roof rule --")

case("a sky flat may open",
     not roofguard.needs_closed_roof({"frameType": "flat", "source": "sky"}))
case("a panel flat may not",
     roofguard.needs_closed_roof({"frameType": "flat", "source": "panel"}))
case("a bias may not", roofguard.needs_closed_roof({"frameType": "bias"}))
case("a dark may not", roofguard.needs_closed_roof({"frameType": "dark"}))
case("a dark flat may not", roofguard.needs_closed_roof({"frameType": "darkflat"}))

print("\n-- where an open is refused --")

src_seq = inspect.getsource(Sequencer._ensure_roof_open)
src_api = inspect.getsource(dome_shutter)
src_set = inspect.getsource(CalibrationRunner._set_on_rig)
case("the sequencer opens through request_open", "roofguard.request_open" in src_seq)
case("the sequencer does not call open_shutter itself", "open_shutter" not in src_seq)
case("the roof button checks the guard before opening", "roofguard.blocked" in src_api)
case("a closed-roof set holds the guard", "roofguard.begin" in src_set)

# The guard refuses even with no runner, which is what the button and the
# sequencer both hit.
with roofguard.closed_for("bias"):
    lone = Dome()
    try:
        roofguard.request_open(lone)
        case("request_open during a closed-roof run raises", False)
    except DeviceError:
        case("request_open during a closed-roof run raises", True)
    case("and the dome recorded nothing", lone.commands == [], str(lone.commands))

failed = len(results) - sum(results)
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(1 if failed else 0)
