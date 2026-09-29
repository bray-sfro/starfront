"""Exercise what the filters in the wheel are called.

    python tools/check_filters.py

The names are not cosmetic.  Almost everything downstream matches on them
rather than on slot numbers: the sequencer looks up "Ha" to decide where to move
the wheel, the FITS header records the name, the planner allocates against it
and the calibration library files flats by it.

So a wheel whose driver calls its slots "1".."7" does not merely look wrong.
The plan asks for Ha, `_select_filter` finds nothing called that, says so once
in the log and carries on — and the whole night goes through whichever slot
happened to be loaded.  ASCOM's `Names` is read-only, so the driver cannot be
told any better; the names typed into Equipment have to be laid over it.

No test framework, for the same reason as the other checks here.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol.config import Config                          # noqa: E402
from astrocontrol.devices.base import DeviceError, FilterWheel  # noqa: E402
from astrocontrol.devices.manager import DeviceManager          # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Wheel(FilterWheel):
    """A wheel whose driver reports whatever it was built with."""

    def __init__(self, driver_names, position=0):
        super().__init__("test.wheel", "Test wheel")
        self._names = list(driver_names)
        self._position = position
        self._connected = True

    @property
    def names(self):
        return self._named(self._names)

    @property
    def position(self):
        return self._position

    def set_position(self, index):
        self._position = int(index)


# ------------------------------------------------- laying names over the slots
numbered = ["1", "2", "3", "4", "5", "6", "7"]

wheel = Wheel(numbered)
case("a driver that only counts its slots reports numbers",
     wheel.names == numbered, f"{wheel.names}")

wheel.set_name_overrides(["L", "R", "G", "B", "Ha", "OIII", "SII"])
case("the names from Equipment are what it reports, folded to one letter each",
     wheel.names == ["L", "R", "G", "B", "H", "O", "S"], f"{wheel.names}")

# Naming some of them leaves the rest as the driver has them, rather than
# dropping slots that would then be unreachable.
wheel = Wheel(numbered)
wheel.set_name_overrides(["L", "R", "G"])
case("naming the first three leaves the other four alone",
     wheel.names == ["L", "R", "G", "4", "5", "6", "7"], f"{wheel.names}")

# A blank entry means "nothing said about this slot", not "no filter".
wheel = Wheel(numbered)
wheel.set_name_overrides(["L", "", "G", "  ", "Ha"])
case("a blank entry leaves that slot as the driver named it",
     wheel.names == ["L", "2", "G", "4", "H", "6", "7"], f"{wheel.names}")

# More names than slots: the wheel is the authority on how many there are.
wheel = Wheel(["1", "2", "3"])
wheel.set_name_overrides(["L", "R", "G", "B", "Ha"])
case("more names than slots does not invent slots",
     wheel.names == ["L", "R", "G"], f"{wheel.names}")

# A driver with real names of its own keeps them when nothing is configured -
# in the program's one spelling, so "Lum", "Red" and "Green" read L, R, G.
wheel = Wheel(["Lum", "Red", "Green"])
case("a driver with real names keeps them when nothing is set, folded",
     wheel.names == ["L", "R", "G"], f"{wheel.names}")

# ...and is still overridden when something is, because ASCOM's Names cannot be
# edited, so what the operator typed is the only way to correct a wrong one.
wheel.set_name_overrides(["B", "G", "R"])
case("what the operator typed wins over the driver",
     wheel.names == ["B", "G", "R"], f"{wheel.names}")

# Clearing them gives the driver's back.
wheel.set_name_overrides([])
case("clearing the names gives the driver's back",
     wheel.names == ["L", "R", "G"], f"{wheel.names}")

wheel.set_name_overrides(None)
case("and so does clearing them with nothing at all",
     wheel.names == ["L", "R", "G"])

# A name that is none of the seven is left as the driver had it.
wheel = Wheel(["Dual", "L-eXtreme", "Ha 3nm"])
case("an unknown name is kept as it is; a known one with a bandpass is folded",
     wheel.names == ["Dual", "L-eXtreme", "H"], f"{wheel.names}")

# A driver reporting no slots at all: the settings are all there is.
wheel = Wheel([])
wheel.set_name_overrides(["L", "R", "G"])
case("a driver reporting no slots falls back to the names entirely",
     wheel.names == ["L", "R", "G"], f"{wheel.names}")

# ------------------------------------------------------- what is in the path now
wheel = Wheel(numbered, position=4)
wheel.set_name_overrides(["L", "R", "G", "B", "Ha", "OIII", "SII"])
case("the filter in the light path is named, not numbered",
     wheel.current_name() == "H", wheel.current_name())

wheel._position = -1                       # between slots
case("a wheel between slots says so rather than guessing",
     wheel.current_name() == "-")

wheel._position = 99                       # a driver talking nonsense
case("a position outside the wheel does not throw",
     wheel.current_name() == "-")

# ------------------------------------------------ what the sequencer matches on
#
# The actual failure this prevents: the plan asks for Ha and the wheel is only
# counting, so nothing matches and the night is shot through slot 0.
wheel = Wheel(numbered)
case("without the names, the plan's filter is not found",
     "H" not in wheel.names)
wheel.set_name_overrides(["L", "R", "G", "B", "Ha", "OIII", "SII"])
case("with them, it is found at the right slot",
     wheel.names.index("H") == 4, f"slot {wheel.names.index('H')}")

# ------------------------------------------------------- applied on connecting
config = Config(Path(tempfile.mkdtemp()) / "settings.json")
config.update("camera", {"filterNames": ["L", "R", "G", "B", "Ha", "OIII", "SII"]})

manager = DeviceManager(config=config)
manager._devices["filterwheel"] = Wheel(numbered)
manager.apply_filter_names()
case("connecting a wheel applies the names from Equipment",
     manager._devices["filterwheel"].names[4] == "H")
case("...and the settings file itself now says H, O, S",
     config.get("camera", "filterNames") == ["L", "R", "G", "B", "H", "O", "S"],
     str(config.get("camera", "filterNames")))

# Changing them reaches the live wheel without a reconnect.
config.update("camera", {"filterNames": ["Lum", "Red"]})
manager.apply_filter_names()
case("changing them reaches the wheel without reconnecting",
     manager._devices["filterwheel"].names == ["L", "R", "3", "4", "5", "6", "7"],
     f"{manager._devices['filterwheel'].names}")

# The status the UI reads carries the same list.
status = manager.status()["filterwheel"]
case("and the status the interface reads says the same",
     status["names"] == ["L", "R", "3", "4", "5", "6", "7"])
case("including which one is in the path", status["currentName"] == "L")

# ------------------------------------------------------- the one spelling
from astrocontrol.filters import canonical, canonical_keys        # noqa: E402

for typed, letter in (("Ha", "H"), ("H-alpha", "H"), ("h alpha", "H"), ("Ha 3nm", "H"),
                      ("OIII", "O"), ("O3", "O"), ("O III", "O"), ("SII 6.5nm", "S"),
                      ("S2", "S"), ("Lum", "L"), ("luminance", "L"), ("Clear", "L"),
                      ("UV/IR", "L"), ("Red", "R"), ("green", "G"), ("Blue", "B"),
                      ("H", "H"), ("l", "L")):
    if not case(f"{typed!r} is {letter}", canonical(typed) == letter, canonical(typed)):
        break
case("a name that is none of the seven is kept", canonical("L-eXtreme") == "L-eXtreme")
case("blank stays blank", canonical("") == "" and canonical(None) == "")
case("a table keyed two ways folds to one key",
     canonical_keys({"Ha": 100, "H": 50, "OIII": 20}) == {"H": 100, "O": 20})

# A settings file from before the fold is folded on load, offsets included.
old = Config(Path(tempfile.mkdtemp()) / "settings.json")
old.update("camera", {"filterNames": ["L", "Ha", "OIII"],
                      "filterBandpass": {"Ha": 3.0, "OIII": 3.0}})
old.update("sequencer", {"filterOffsets": {"Ha": -140, "OIII": -90},
                         "autofocusFilter": "Ha"})
case("names, bandpasses, offsets and the focus filter are all folded",
     old.get("camera", "filterNames") == ["L", "H", "O"]
     and old.get("camera", "filterBandpass") == {"H": 3.0, "O": 3.0}
     and old.get("sequencer", "filterOffsets") == {"H": -140, "O": -90}
     and old.get("sequencer", "autofocusFilter") == "H",
     str(old.section("camera")))

# No wheel, or no config, must not throw.
empty = DeviceManager(config=config)
empty.apply_filter_names()
case("no wheel connected is not an error", True)
nowhere = DeviceManager(config=None)
nowhere._devices["filterwheel"] = Wheel(numbered)
nowhere.apply_filter_names()
case("no config is not an error either",
     nowhere._devices["filterwheel"].names == numbered)

# ------------------------------------------------------------------ slot order
#
# The order is not cosmetic: these land on the wheel's slots by position. LRGBSHO
# on a wheel loaded in that order has SII in slot 5 — put Ha there instead and
# the sequencer asks for Ha all night and moves to the slot holding SII.

lrgbsho = ["L", "R", "G", "B", "SII", "Ha", "OIII"]
folded = ["L", "R", "G", "B", "S", "H", "O"]
config = Config(Path(tempfile.mkdtemp()) / "settings.json")
config.update("camera", {"filterNames": lrgbsho})
case("the order is stored exactly as given, each name in its one spelling",
     config.get("camera", "filterNames") == folded,
     f"{config.get('camera', 'filterNames')}")

manager = DeviceManager(config=config)
manager._devices["filterwheel"] = Wheel(numbered)
manager.apply_filter_names()
wheel = manager._devices["filterwheel"]
case("and reaches the wheel in that order", wheel.names == folded, f"{wheel.names}")
case("so S is the slot it is fitted in",
     wheel.names.index("S") == 4, f"slot {wheel.names.index('S')}")
case("and H is the one after it, not before",
     wheel.names.index("H") == 5 and wheel.names.index("H") > wheel.names.index("S"))

# Reading it back out must not re-sort it, alphabetically or otherwise.
case("reading it back does not reorder it",
     Config(config.path).get("camera", "filterNames") == folded)

# The reverse order is equally valid and must survive equally.
reversed_order = list(reversed(lrgbsho))
config.update("camera", {"filterNames": reversed_order})
manager.apply_filter_names()
case("any order the operator chooses is kept",
     manager._devices["filterwheel"].names == list(reversed(folded)),
     f"{manager._devices['filterwheel'].names}")

# ------------------------------------------------------------- renaming a driver
#
# The names are still offered to the driver first, for the few that take one.
# Nothing depends on it working: ASCOM's `Names` is read-only, so refusing is
# the normal outcome and the overlay is what actually carries the names.
try:
    Wheel(numbered).set_names(["L"])
    refused = False
except DeviceError:
    refused = True
case("a driver that cannot be renamed raises rather than pretending", refused)

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
