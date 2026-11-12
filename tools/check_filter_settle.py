"""Lights are not taken on an unknown filter (0.2.53g).

The ZWO EFW reports position -1 ("moving") from a poll that overlaps its
own status, and on this rig that read stays -1 the whole night. A later
read that is a real slot is that slot. All -1 is not a filter: the run
holds, and FILTER is not guessed. A colour camera with no wheel is
OSC-RGB, including when the drawer says L.

    python tools/check_filter_settle.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp(prefix="filter-settle-"))

from astrocontrol.capture import CaptureService                 # noqa: E402
from astrocontrol.config import Config                            # noqa: E402
from astrocontrol.devices.base import DeviceError, FilterWheel, read_slot  # noqa: E402
from astrocontrol.sequencer import Sequencer                      # noqa: E402

results: list[bool] = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))


class StuckWheel:
    """Position -1 on every read. The driver never names a slot."""

    connected = True
    names = ["L", "R", "G", "B", "S", "H", "O"]

    def __init__(self):
        self.commands = []

    @property
    def position(self):
        return -1

    @property
    def moving(self):
        return True

    def set_position(self, index):
        self.commands.append(index)


class ScriptedWheel(FilterWheel):
    """Hands back a scripted series of position reads."""

    def __init__(self, reads):
        super().__init__("fake", "EFW")
        self._connected = True
        self._reads = list(reads)
        self._slot_names = ["L", "R", "G", "B", "S", "H", "O"]
        self.commands = []

    @property
    def names(self):
        return list(self._slot_names)

    @property
    def position(self):
        if self._reads:
            return self._reads.pop(0)
        return -1

    def set_position(self, index):
        self.commands.append(index)


class ColourCamera:
    connected = True
    bayer_pattern = "RGGB"
    name = "ZWO ASI6200MC Pro"


class MonoCamera:
    connected = True
    bayer_pattern = None
    name = "ZWO ASI6200MM Pro"


class Manager:
    def __init__(self, devices):
        self.devices = devices
        self.logs = []

    def get(self, kind):
        return self.devices.get(kind)

    def log(self, message, level="info"):
        self.logs.append((level, message))


def service(devices, **camera):
    config = Config(Path(tempfile.mkdtemp(prefix="filt-cfg-")) / "settings.json")
    if camera:
        config.update("camera", camera)
    capture = CaptureService(Manager(devices), config)
    return capture


print("\n-- a wheel that only says moving --")

always = ScriptedWheel([-1, -1, -1])
slot, moving = read_slot(always)
case("three -1 reads are not a slot", slot is None and moving)

stuck = StuckWheel()

cap = service({"filterwheel": stuck, "camera": MonoCamera()})
try:
    cap._light_filter()
    case("a light is refused when the wheel has not settled", False)
except DeviceError as exc:
    case("a light is refused when the wheel has not settled",
         "not guessed" in str(exc), str(exc))

print("\n-- moving, but a position read is valid --")

glitch = ScriptedWheel([-1, -1, 0])
slot, said = read_slot(glitch)
case("a later valid read is the slot", slot == 0 and said, f"slot={slot} moving={said}")

cap = service({"filterwheel": ScriptedWheel([-1, 0, 0]), "camera": MonoCamera()})
name = cap._light_filter()
case("the light is tagged with that slot, not a blank", name == "L", name)
case("the moving flag was noticed",
     any("reported moving" in message for _level, message in cap.manager.logs))

print("\n-- the sequencer holds instead of exposing --")


class Rig:
    def __init__(self, wheel):
        self.id = "main"
        self.name = "FSQ85"
        self.role = "master"
        self.manager = Manager({"filterwheel": wheel})
        self.config = Config(Path(tempfile.mkdtemp(prefix="seq-cfg-")) / "settings.json")


class Rigs:
    def __init__(self, rig):
        self.master = rig
        self.all = [rig]

    def imaging(self):
        return [self.master]


config = Config(Path(tempfile.mkdtemp(prefix="seq2-")) / "settings.json")
seq = Sequencer(Rigs(Rig(stuck)), config, None, None)
seq.FILTER_SETTLE_SECONDS = 0.3
seq.warnings = None
try:
    seq._select_filter(seq.rigs.master, "H")
    case("an unsettled wheel raises before the exposure", False)
except DeviceError as exc:
    case("an unsettled wheel raises before the exposure", "did not settle" in str(exc),
         str(exc)[:80])

print("\n-- a colour camera with no wheel is OSC-RGB --")

colour = service({"camera": ColourCamera()}, fixedFilter="L", colour=True)
case("OSC-RGB, not the drawer's L", colour._light_filter() == "OSC-RGB",
     colour._light_filter())

mono = service({"camera": MonoCamera()}, fixedFilter="L")
case("a mono camera with no wheel keeps the filter that is fitted",
     mono._light_filter() == "L")

kept = service({"camera": ColourCamera()}, fixedFilter="OSC-Ha+OIII", colour=True)
case("an OSC name already set is kept", kept._light_filter() == "OSC-Ha+OIII")

failed = len(results) - sum(results)
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(1 if failed else 0)
