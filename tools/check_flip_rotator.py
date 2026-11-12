"""A meridian flip keeps the camera's sky angle on a rig with a rotator.

    python tools/check_flip_rotator.py

2026-11-04, EAGLE (NiteCrawler rotator, iOptron CEM GEM), twin panel 4: the
centring solve had turned the rotator from the panel's 270.09 to 223.05 (this
rig's mechanical-to-sky offset is about +47 deg). At the flip the sequencer
re-commanded 270.09 and re-centred with no angle, so every sub after it was at
PA 137 instead of 270. This replays that flip against a rotator whose sky angle
is its mechanical angle +47 deg on the west side and +47+180 on the east.

No test framework, like the other checks here.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astrocontrol.config import DEFAULTS          # noqa: E402
from astrocontrol.sequencer import Sequencer      # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(("PASS " if ok else "FAIL ") + what)


OFFSET = 47.04


class Mount:
    def __init__(self):
        self.side_of_pier = "West"
        self.connected = True


class Rotator:
    connected = True
    moving = False

    def __init__(self, position):
        self.position = position
        self.moves: list[float] = []

    def move_absolute(self, angle):
        self.position = float(angle) % 360.0
        self.moves.append(self.position)


def sky_pa(rotator, mount) -> float:
    flip = 180.0 if mount.side_of_pier == "East" else 0.0
    return (rotator.position + OFFSET + flip) % 360.0


def run(settings_over=None, solver_present=True, flip_happens=True):
    mount = Mount()
    rotator = Rotator(223.05)                     # where the centring solve left it
    solves: list[dict] = []

    def solve_start(mode, image, ra, dec, rotation=None):
        solves.append({"mode": mode, "rotation": rotation, "paBefore": sky_pa(rotator, mount)})
        if rotation is not None:                  # what solving._correct_rotation does
            error = (float(rotation) - sky_pa(rotator, mount) + 180.0) % 360.0 - 180.0
            if abs(error) > 1.0:
                rotator.move_absolute(rotator.position + error)

    def slew(m, ra, dec, label):
        if flip_happens:
            m.side_of_pier = "East"
        return m

    settings = {**DEFAULTS["sequencer"], "flipAfterMinutes": 0.0, **(settings_over or {})}
    said: list[str] = []
    fake = SimpleNamespace(
        _settings=lambda: settings, _mount_ready=lambda: mount,
        _flip_minutes=Sequencer._flip_minutes, _hours_to_meridian=lambda ra: 0.0,
        _set=lambda *a: None, _say=lambda text, *a: said.append(text), _sleep=lambda s: None,
        manager=SimpleNamespace(get=lambda kind: {"rotator": rotator}.get(kind)),
        _slew=slew, _wait_for_mount=lambda m: None, _warn=lambda *a, **k: None,
        _tell=lambda *a, **k: None, _lock=threading.Lock(), _flips=0, _check=lambda: None,
        solver=SimpleNamespace(executable=lambda: "astap" if solver_present else None, start=solve_start),
        _wait_for_solver=lambda: None, _maybe_focus=lambda: None, FLIP_RETRIES=Sequencer.FLIP_RETRIES)
    panel = {"ra": 5.6, "dec": -2.0, "rotation": 270.09}
    flipped = Sequencer._flip(fake, panel, "M42 P4")
    return flipped, mount, rotator, solves, said


def main() -> int:
    check(DEFAULTS["sequencer"].get("rotatorFlipDegrees") == 180.0, "rotatorFlipDegrees defaults to 180")

    flipped, mount, rotator, solves, said = run()
    check(flipped and mount.side_of_pier == "East", "the mount flipped")
    check(270.09 not in rotator.moves, f"the uncorrected panel angle is not re-commanded ({rotator.moves})")
    check(abs(rotator.moves[0] - 43.05) < 0.01, f"the rotator keeps its corrected angle + 180 ({rotator.moves[0]:.2f})")
    check(solves and solves[0]["rotation"] == 270.09, "the re-centre after the flip is given the panel's angle")
    pa = sky_pa(rotator, mount)
    check(abs(pa - 270.09) < 1.0, f"after the flip the frame is at PA {pa:.2f}, not 137")

    # A rotator whose driver compensates (0): the solve still fixes it.
    flipped, mount, rotator, solves, _ = run({"rotatorFlipDegrees": 0})
    pa = sky_pa(rotator, mount)
    check(rotator.moves[0] == 223.05 and solves[0]["rotation"] == 270.09 and abs(pa - 270.09) < 1.0,
          f"with rotatorFlipDegrees 0 the solve turns it onto the sky angle (PA {pa:.2f})")

    # No flip after all (the mount stayed put): the rotator is not turned.
    flipped, mount, rotator, solves, _ = run(flip_happens=False)
    check(not flipped and rotator.moves == [] and abs(sky_pa(rotator, mount) - 270.09) < 1.0,
          "a slew that did not change side leaves the rotator where it is")

    # The old behaviour, for the record: 270.09 and no angle -> PA 137.
    old = Rotator(270.09)
    old_pa = sky_pa(old, SimpleNamespace(side_of_pier="East"))
    check(abs(old_pa - 137.13) < 0.1, f"the old flip would have left PA {old_pa:.2f} (EAGLE measured 137.13-137.16)")

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
