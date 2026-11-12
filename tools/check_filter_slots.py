"""Hakos FSQ85 filter slots (0.2.53b): names L,R,G,B,S,H,O on a 7-slot ZWO EFW.

0.2.53b must never add a slot, reorder or shift names: slot 0 = L ... 6 = O,
the wheel's position reads back as that name, and selecting a filter by name
moves to that slot.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astrocontrol.devices.base import FilterWheel              # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))


class EFW(FilterWheel):
    """ZWO EFW over ASCOM: seven slots the driver calls 1..7 (or its own names)."""
    def __init__(self, driver_names):
        FilterWheel.__init__(self, "ASCOM.EFW2.FilterWheel", "ZWO FilterWheel (1)")
        self.driver_names = list(driver_names)
        self._pos = 0
        self._connected = True

    @property
    def names(self):
        return self._named(self.driver_names)

    @property
    def position(self):
        return self._pos

    def set_position(self, index):
        self._pos = int(index)


HAKOS = ["L", "R", "G", "B", "S", "H", "O"]
for label, driver in [("driver says 1..7", [str(i) for i in range(1, 8)]),
                      ("driver has its own names", ["Lum", "Red", "Green", "Blue", "SII", "Ha", "OIII"])]:
    wheel = EFW(driver)
    wheel.set_name_overrides(HAKOS)
    names = wheel.names
    case(f"{label}: 7 slots, 0=L ... 6=O, nothing added or shifted", names == HAKOS, str(names))
    mapped = {}
    for slot in range(7):
        wheel.set_position(slot)
        mapped[slot] = wheel.current_name()
    case(f"{label}: each position reads back its own name",
         mapped == dict(enumerate(HAKOS)), str(mapped))
    case(f"{label}: name -> slot", all(names.index(n) == i for i, n in enumerate(HAKOS)))

print()
print(f"{sum(results)} of {len(results)} passed")
sys.exit(0 if all(results) else 1)
