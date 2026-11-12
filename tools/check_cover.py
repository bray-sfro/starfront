"""Flat-panel cover capability (0.2.53c).

ASCOM CoverStatus: 0 NotPresent, 1 Closed, 2 Moving, 3 Open, 4 Unknown, 5 Error.
Unknown and a failed read are not NotPresent. A lamp-only panel (0) is not
commanded. Nothing here talks to hardware.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="cover-"))
os.environ.setdefault("ASTRO_DATA_DIR", str(TMP / "data"))
os.environ.setdefault("HOME", str(TMP / "home"))
os.environ.setdefault("LOCALAPPDATA", str(TMP / "lad"))

from astrocontrol.devices.ascom import AscomFlatPanel          # noqa: E402
from astrocontrol.devices.base import DeviceError, FlatPanel   # noqa: E402

results: list[bool] = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


def panel(value, name="Optec Alnitak"):
    device = AscomFlatPanel.__new__(AscomFlatPanel)
    device.name = name
    device._prog_id = "ASCOM.OptecAlnitak.CoverCalibrator"
    device._connected = True
    device.store = {} if value is None else {"CoverState": value}
    device.commands = []

    def _get(prop, default=None):
        if prop in device.store:
            return device.store[prop]
        return default

    device._get = _get
    device._require = lambda: None

    def _invoke(method, *args):
        device.commands.append(method)
        if method == "OpenCover" and device.store.get("CoverState") in (1, 4, 5):
            device.store["CoverState"] = 3

    device._invoke = _invoke
    return device


class Lamp(FlatPanel):
    def __init__(self):
        super().__init__("lamp", "Lamp only")
        self._connected = True
        self.commands = []
        self._state = "notpresent"

    @property
    def cover_state(self):
        return self._state

    def open_cover(self):
        self.commands.append("open")

    def turn_off(self):
        pass


class Unknown(FlatPanel):
    def __init__(self):
        super().__init__("ASCOM.OptecAlnitak.CoverCalibrator", "Optec Alnitak")
        self._connected = True
        self.commands = []
        self._state = "unknown"

    @property
    def cover_state(self):
        return self._state

    def open_cover(self):
        self.commands.append("open")
        self._state = "open"

    def turn_off(self):
        pass


class Unread(FlatPanel):
    def __init__(self):
        super().__init__("panel", "Mystery")
        self._connected = True
        self.commands = []

    @property
    def cover_state(self):
        return "unread"

    def open_cover(self):
        self.commands.append("open")

    def turn_off(self):
        pass


# Enum map. Integer 0 stays NotPresent. A missing value is unread, not 0.
for value, want in ((0, "notpresent"), (1, "closed"), (2, "moving"),
                    (3, "open"), (4, "unknown"), (5, "error")):
    case(f"CoverState {value} maps to {want}", panel(value).cover_state == want)

missing = panel(None)
case("a failed CoverState read is unread, not notpresent",
     missing.cover_state == "unread" and missing.has_cover is None)

present = panel(0, name="Optec Alnitak")
case("NotPresent is no cover even if the name says Alnitak",
     present.cover_state == "notpresent" and present.has_cover is False
     and present.cover_prepare() == "skip")

unknown = panel(4)
case("Unknown is not hasCover false",
     unknown.cover_state == "unknown" and unknown.has_cover is None
     and unknown.cover_prepare() == "open",
     f"{unknown.has_cover} {unknown.cover_prepare()}")

case("Closed prepares an open and does not claim a physical position",
     panel(1).has_cover is True and panel(1).cover_prepare() == "open")
case("Open does not send another OpenCover", panel(3).cover_prepare() == "skip")
case("cover error is a cover, not 'no cover'",
     panel(5).has_cover is True and panel(5).cover_prepare() == "open")


# Sequence prep uses the same decision and does not command a lamp.
import astrocontrol.sequencer as sequencer_mod                 # noqa: E402
from astrocontrol.config import Config                          # noqa: E402
from astrocontrol.sequencer import Sequencer                    # noqa: E402

class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def time(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.01, float(seconds))


sequencer_mod.time = Clock()


def run_open(flat):
    config = Config(Path(tempfile.mkdtemp(dir=TMP)) / "settings.json")

    class Manager:
        def __init__(self):
            self.devices = {"flatpanel": flat}

        def get(self, kind):
            return self.devices.get(kind)

        def log(self, *args, **kwargs):
            pass

    class Rig:
        manager = Manager()

    class Rigs:
        master = Rig()

    seq = Sequencer.__new__(Sequencer)
    seq.rigs = Rigs()
    seq.config = config
    seq._state = "idle"
    seq._message = ""
    seq._check = lambda: None
    seq._say = lambda *args, **kwargs: None
    seq._set = lambda *args, **kwargs: None
    try:
        seq._open_cover()
        return "ok"
    except DeviceError as exc:
        return str(exc)


lamp = Lamp()
case("lamp-only NotPresent: OpenCover is not sent",
     run_open(lamp) == "ok" and lamp.commands == [], str(lamp.commands))

unk = Unknown()
case("driver unknown: OpenCover is sent once",
     run_open(unk) == "ok" and unk.commands == ["open"] and unk.cover_state == "open",
     f"{unk.commands} {unk.cover_state}")

bad = Unread()
text = run_open(bad)
case("unreadable cover: no OpenCover, and the error is not swallowed",
     bad.commands == [] and "not being treated" in text and "not sent" in text,
     text)

print()
print(f"{sum(results)} of {len(results)} passed")
sys.exit(0 if all(results) else 1)
