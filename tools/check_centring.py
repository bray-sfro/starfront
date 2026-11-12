"""Centring on a mount that will not take a sync.

    python tools/check_centring.py

Found on a real night: TheSky's ASCOM driver throws at SyncToCoordinates.
The first fallback nudged the slew by the measured error - but from the
last nudged aim rather than from the target, so each attempt added the
error again and the mount walked steadily away from the field, a tenth of
a degree a time, until the attempts ran out. The nudge has to be worked
out from the target every time.

No test framework, for the same reason as the other checks here.
"""
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from astrocontrol.config import Config                              # noqa: E402
from astrocontrol.solving import Solver, SolveResult                 # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Mount:
    """Lands a fixed distance short of wherever it is sent, and may refuse to sync."""

    def __init__(self, refuses_sync, offset=(0.03, -0.02)):
        self.can_sync = True
        self.slewing = False
        self.refuses_sync = refuses_sync
        self.offset = offset
        self.slews = []
        self.syncs = []
        self.ra, self.dec = 0.0, 0.0
        self.error = (0.0, 0.0)            # what the sync has corrected so far

    def slew_to(self, ra, dec):
        self.slews.append((round(ra, 4), round(dec, 4)))
        # Where it really ends up: short by the offset, less what a sync fixed.
        self.ra = ra + self.offset[0] - self.error[0]
        self.dec = dec + self.offset[1] - self.error[1]

    def sync_to(self, ra, dec):
        if self.refuses_sync:
            raise RuntimeError("SyncToCoordinates: Object reference not set to an instance")
        self.syncs.append((round(ra, 4), round(dec, 4)))
        # A sync tells the mount where it really is; from then on it lands true.
        self.error = self.offset


class Manager:
    def __init__(self, mount):
        self.mount = mount
        self.logged = []

    def require(self, kind):
        return self.mount

    def get(self, kind):
        return self.mount if kind == "mount" else None

    def log(self, message, level="info"):
        self.logged.append((level, message))


class Capture:
    save_enabled = True
    _suspended = 0

    def suspend_saving(self):
        if self._suspended == 0:
            self._save_before = self.save_enabled
            self.save_enabled = False
        self._suspended += 1

    def resume_saving(self):
        if self._suspended == 0:
            return
        self._suspended -= 1
        if self._suspended == 0:
            self.save_enabled = self._save_before

    def capture_blocking(self, exposure, **kw):
        return types.SimpleNamespace(id="frame", filename="frame.fits")


def build(refuses_sync):
    mount = Mount(refuses_sync)
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    config.update("solver", {"tolerance": 1.0, "exposure": 1.0, "attempts": 4})
    solver = Solver(Manager(mount), Capture(), config)
    solver._wait_for_slew = lambda m, timeout=0: None

    def solve(image_id):
        # The solver reports where the mount really is.
        return SolveResult(ra=mount.ra, dec=mount.dec, rotation=95.5, scale=2.0,
                           fov_width=5.3, fov_height=3.5, flipped=False,
                           image_id="frame", filename="frame.fits")
    solver._solve_image = solve
    return solver, mount


TARGET = (3.43945, 53.054)

solver, mount = build(refuses_sync=True)
try:
    solver._center(*TARGET)
    ended = "centred"
except Exception as exc:                                 # noqa: BLE001
    ended = f"{type(exc).__name__}: {exc}"
case("a mount that refuses to sync is still centred", ended == "centred", ended)
def near(a, b, tol=2e-4):
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


case("...by one nudge of exactly the measured error, from the target",
     len(mount.slews) == 2 and near(mount.slews[1], (TARGET[0] - 0.03, TARGET[1] + 0.02)),
     str(mount.slews))
case("...and the sync is not asked for again once refused",
     sum(1 for level, m in solver.manager.logged if "would not sync" in m) == 1)
attempts_logged = [m for _, m in solver.manager.logged if m.startswith("Centre attempt")]
case("...every attempt says how far off it landed, split into RA and Dec",
     len(attempts_logged) == 2 and "RA " in attempts_logged[0] and "Dec " in attempts_logged[0]
     and "within tolerance" in attempts_logged[1], str(attempts_logged))
case("...and the nudge says how far it moved the aim",
     any(m.startswith("Nudging the slew by") for _, m in solver.manager.logged))
case("...landing within tolerance",
     abs(mount.ra - TARGET[0]) < 1e-6 and abs(mount.dec - TARGET[1]) < 1e-6,
     f"{mount.ra:.5f}h {mount.dec:.4f}")

solver, mount = build(refuses_sync=False)
solver._center(*TARGET)
case("a mount that syncs is synced once and sent to the target again",
     len(mount.syncs) == 1 and len(mount.slews) == 2
     and all(near(s, TARGET) for s in mount.slews),
     f"syncs {mount.syncs}, slews {mount.slews}")

# The runaway. A mount whose error grows with how far off-target it is
# sent never lands exactly, so every attempt nudges; worked out from the
# target each time the aim stays within a few arcminutes, where the old
# compounding nudge walked off by a tenth of a degree an attempt.
solver, mount = build(refuses_sync=True)
plain_slew = mount.slew_to


def wobbly(ra, dec, _plain=plain_slew):
    mount.offset = (0.03 + 0.2 * (ra - TARGET[0]), -0.02 + 0.2 * (dec - TARGET[1]))
    _plain(ra, dec)


mount.slew_to = wobbly
solver.config.update("solver", {"tolerance": 0.001})   # never satisfied: every attempt nudges
try:
    solver._center(*TARGET)
except Exception:                                        # noqa: BLE001 - expected to give up
    pass
furthest = max(abs(ra - TARGET[0]) for ra, _ in mount.slews)
case("the nudge never compounds across attempts",
     furthest <= 0.05 and len(mount.slews) >= 4,
     f"furthest aim {furthest * 60:.1f}' from the target over {len(mount.slews)} slews")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
