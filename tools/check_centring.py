"""Centring by a pointing offset, never by SyncToCoordinates.

    python tools/check_centring.py

TheSky's ASCOM driver throws a null reference at SyncToCoordinates. Centring
does not call it. After each accepted solve the slew goes to
target + (target - solved), which is the target minus (solved - target).
A flipped or jumped solve is discarded before that slew. One blind solve
covers a near-hint miss, then the tight radius is used again. When the
attempts run out, the sequencer fails that target instead of focusing and
imaging where the mount happens to be.

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

from astrocontrol import astro                                      # noqa: E402
from astrocontrol.config import Config                              # noqa: E402
from astrocontrol.devices.base import DeviceError                    # noqa: E402
from astrocontrol.sequencer import Sequencer, _CentreFailed          # noqa: E402
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


case("...by one offset slew of exactly the measured error, from the target",
     len(mount.slews) == 2 and near(mount.slews[1], (TARGET[0] - 0.03, TARGET[1] + 0.02)),
     str(mount.slews))
case("...and SyncToCoordinates is never called",
     mount.syncs == [] and not any("would not sync" in m for _, m in solver.manager.logged),
     str(mount.syncs))
attempts_logged = [m for _, m in solver.manager.logged if m.startswith("Centre attempt")]
case("...every attempt says how far off it landed, split into RA and Dec",
     len(attempts_logged) == 2 and "RA " in attempts_logged[0] and "Dec " in attempts_logged[0]
     and "within tolerance" in attempts_logged[1], str(attempts_logged))
case("...and the offset slew says where it aimed",
     any("Pointing offset" in m and "slewing to" in m for _, m in solver.manager.logged))
case("...landing within tolerance",
     abs(mount.ra - TARGET[0]) < 1e-6 and abs(mount.dec - TARGET[1]) < 1e-6,
     f"{mount.ra:.5f}h {mount.dec:.4f}")

solver, mount = build(refuses_sync=False)
solver._center(*TARGET)
case("a mount that would accept a sync is still not synced",
     mount.syncs == [] and len(mount.slews) == 2
     and near(mount.slews[1], (TARGET[0] - 0.03, TARGET[1] + 0.02)),
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


def solved(ra, dec, rotation, scale=2.0, fov=3.5):
    return SolveResult(ra=ra, dec=dec, rotation=rotation, scale=scale,
                       fov_width=5.3, fov_height=fov, flipped=False,
                       image_id="frame", filename="frame.fits")


def scripted(frames):
    """A solver that returns the next scripted frame, ignoring the mount."""
    pending = list(frames)

    def solve(image_id):
        ra, dec, rotation = pending.pop(0)
        return solved(ra, dec, rotation)
    return solve


# The M31 night: a flipped false match ~24° away must not be slewed to,
# and the following good solve must aim at target + (target - solved).
M31 = (0.71167, 41.2667)
solver, mount = build(refuses_sync=True)
solver.config.update("optics", {"rotation": 95.65})
solver.config.update("solver", {"attempts": 4, "tolerance": 1.0})
solver._solve_image = scripted([
    (22.72276, 41.27, -84.49),   # false match, rotation flipped
    (0.68769, 41.2667, 95.65),   # real field, ~16' off
    (M31[0], M31[1], 95.65),     # on target after the offset slew
])
try:
    solver._center(*M31)
    m31_ended = "centred"
except Exception as exc:                                 # noqa: BLE001
    m31_ended = f"{type(exc).__name__}: {exc}"
case("a flipped solve is rejected and does not slew toward 2.7h",
     m31_ended == "centred" and mount.syncs == []
     and all(ra < 3.0 for ra, _ in mount.slews)
     and near(mount.slews[1], (0.73565, M31[1]), tol=1e-3),
     str(mount.slews))
case("...the rejection is logged and the mount was not moved for it",
     any("rejected" in m and "flipped" in m for _, m in solver.manager.logged))

# A large model error, the right way up, is the case the blind/wide radius
# exists for. It must be kept and corrected, not thrown out as a flip.
solver, mount = build(refuses_sync=True)
solver.config.update("optics", {"rotation": 95.65})
solver._solve_image = scripted([
    (M31[0] + 1.2, M31[1], 95.65),
    (M31[0], M31[1], 95.65),
])
solver._center(*M31)
case("a large same-angle model error is corrected, not rejected",
     mount.syncs == [] and len(mount.slews) == 2
     and near(mount.slews[1], (astro.normalise_ra_hours(M31[0] - 1.2), M31[1]), tol=1e-3)
     and not any("rejected" in m for _, m in solver.manager.logged),
     str(mount.slews))

# Near-hint miss: one blind solve, then the tight radius again.
solver, mount = build(refuses_sync=True)
solver.config.update("optics", {"rotation": 95.65})
tight_calls = {"n": 0}
wide_calls = {"n": 0}


def tight_then(image_id):
    tight_calls["n"] += 1
    if tight_calls["n"] == 1:
        raise DeviceError("ASTAP did not solve: no stars")
    return solved(M31[0], M31[1], 95.65)


def wide(record):
    wide_calls["n"] += 1
    return solved(M31[0] + 0.02, M31[1], 95.65)


solver._solve_image = tight_then
solver._solve_wide = wide
solver._center(*M31)
case("a near-hint miss falls back to one blind solve, then tight again",
     wide_calls["n"] == 1 and tight_calls["n"] == 2 and mount.syncs == [],
     f"tight {tight_calls['n']}, wide {wide_calls['n']}, slews {mount.slews}")
case("...and that one blind solve is not repeated",
     wide_calls["n"] == 1 and any("blind solve" in m for _, m in solver.manager.logged),
     f"wide {wide_calls['n']}")
# The blind answer was 0.02h off, so the second slew is the offset, and the
# tight solve after it is what centres.
case("...then the tight solve is what finishes the centring",
     len(mount.slews) == 2 and near(mount.slews[1], (M31[0] - 0.02, M31[1]), tol=1e-3),
     str(mount.slews))

# Every solve flipped: nothing is slewed except the opening goto, and the
# centring fails rather than walking off.
solver, mount = build(refuses_sync=True)
solver.config.update("optics", {"rotation": 95.65})
solver.config.update("solver", {"attempts": 4})
solver._solve_image = scripted([(22.72, 41.27, -84.5)] * 4)
try:
    solver._center(*M31)
    exhausted = "centred"
except DeviceError as exc:
    exhausted = str(exc)
case("flipped solves exhaust centring without an offset slew or a sync",
     exhausted.startswith("could not centre") and len(mount.slews) == 1
     and mount.syncs == [] and near(mount.slews[0], M31),
     f"{exhausted}; slews {mount.slews}")


# A rejected blind field must not spend the only whole-sky recovery.
# The next exposure may search again. Once the field is trusted (scale and
# image FOV match the optics), the slew is the pointing offset back to the
# target — not a slew to the measured field, and not a sync. The saved
# camera angle stays +95.65, not the flipped −84°.
FIELD = (22.7206, 41.366)
SCALE = 206.265 * 3.76 / 389.0
FOV = SCALE * 6388 / 3600.0
delta_ra = ((FIELD[0] - M31[0] + 12.0) % 24.0) - 12.0
delta_dec = FIELD[1] - M31[1]
AIM = (astro.normalise_ra_hours(M31[0] - delta_ra),
       max(-90.0, min(90.0, M31[1] - delta_dec)))

solver, mount = build(refuses_sync=True)
solver.config.update("optics", {"rotation": 95.65, "focalLength": 389})
solver.config.update("solver", {"attempts": 4, "tolerance": 1.0})
camera = types.SimpleNamespace(pixel_size_um=3.76, connected=False)
solver.manager.get = lambda kind: camera if kind == "camera" else (
    mount if kind == "mount" else None)
solver.capture.capture_blocking = lambda exposure, **kw: types.SimpleNamespace(
    id=f"frame-{len(mount.slews)}-{exposure}", filename="frame.fits",
    binning=1, width=9576, height=6388, exposure=exposure)
wide_calls = {"n": 0}


def tight_until_offset(image_id):
    if len(mount.slews) > 1:
        return solved(M31[0], M31[1], 95.65, scale=SCALE, fov=FOV)
    raise DeviceError("ASTAP did not solve: no stars")


def wide_then_trusted(record):
    wide_calls["n"] += 1
    if wide_calls["n"] == 1:
        return solved(FIELD[0], FIELD[1], -84.42, scale=0.0, fov=FOV)
    return solved(FIELD[0], FIELD[1], -84.42, scale=SCALE, fov=FOV)


def confirm(record, **kwargs):
    ra, dec = kwargs["hint"]
    scale = 0.0 if wide_calls["n"] == 1 else SCALE
    return solved(ra, dec, -84.42, scale=scale, fov=FOV)


solver._solve_image = tight_until_offset
solver._solve_wide = wide_then_trusted
solver._solve_record = confirm
try:
    solver._center(*M31)
    trusted_ended = "centred"
except Exception as exc:                                 # noqa: BLE001
    trusted_ended = f"{type(exc).__name__}: {exc}"
case("a rejected blind solve does not block a later whole-sky recovery",
     wide_calls["n"] >= 2 and trusted_ended == "centred",
     f"wide {wide_calls['n']}, {trusted_ended}")
case("a trusted far field offsets back to the target, not to that field or via sync",
     mount.syncs == [] and len(mount.slews) >= 2
     and near(mount.slews[1], AIM, tol=1e-3)
     and not near(mount.slews[1], FIELD, tol=0.1)
     and any("Trusted measured field" in m for _, m in solver.manager.logged)
     and any("not syncing" in m for _, m in solver.manager.logged),
     f"aim {mount.slews[1]}, wanted {AIM}")
case("...and the flipped angle is not saved as the camera angle",
     abs(float(solver.config.get('optics', 'rotation')) - 95.65) < 1e-6,
     str(solver.config.get("optics", "rotation")))


# Local revalidation: -r is the search radius, -fov is the image.
import astrocontrol.solving as solving_mod

solver, mount = build(refuses_sync=True)
args_camera = types.SimpleNamespace(pixel_size_um=3.76, connected=False)
solver.manager.get = lambda kind: args_camera if kind == "camera" else None
solver.config.update("optics", {"focalLength": 389})
command = []


class FakeProcess:
    returncode = 1

    def __init__(self, args):
        self.args = args

    def communicate(self, timeout=None):
        Path(self.args[self.args.index("-o") + 1]).with_suffix(".ini").write_text(
            "PLTSOLVD=F\nERROR=no solution\n")
        return "", None

    def kill(self):
        pass


def fake_popen(args, **kwargs):
    command.extend(args)
    return FakeProcess(args)


fov_record = types.SimpleNamespace(
    id="f", filename="f.fits", path=None, ra=M31[0], dec=M31[1],
    width=9576, height=6388, binning=1, exposure=5)
solver._fits_for = lambda record, workspace: workspace / "frame.fits"
old_popen = solving_mod.subprocess.Popen
solver.executable = lambda: Path("astap")
solving_mod.subprocess.Popen = fake_popen
try:
    solver._solve_record(fov_record, radius=1.5, hint_position=False,
                         hint=(22.7206, 41.366))
except DeviceError:
    pass
finally:
    solving_mod.subprocess.Popen = old_popen
case("a confirm solve keeps image FOV separate from the search radius",
     "-r" in command and command[command.index("-r") + 1] == "1.5"
     and "-fov" in command and float(command[command.index("-fov") + 1]) > 3.5
     and command[command.index("-ra") + 1] == "22.720600"
     and "-z" not in command,
     " ".join(command))


# Sequencer: a failed centre fails the target and still runs the next one.
class Rig:
    id = "fsq"
    name = "FSQ106"
    role = "master"

    def __init__(self, config):
        self.config = config
        self.manager = Manager(mount)
        self.capture = Capture()
        self.solver = types.SimpleNamespace(
            executable=lambda: Path("astap"),
            start=lambda *a, **k: None,
            abort=lambda: None,
            busy=False,
            status=lambda: {"error": "still 83.43' from the target after 10 attempts",
                            "state": "idle"},
        )


class Rigs:
    def __init__(self, rig):
        self.master = rig
        self.all = [rig]

    def imaging(self):
        return self.all


config = Config(Path(tempfile.mkdtemp()) / "settings.json")
mount.connected = True
mount.tracking = True
rig = Rig(config)
seq = Sequencer(Rigs(rig), config, None, None)
seq._home_mount = lambda: None
seq._slew = lambda m, ra, dec, label: m
seq._wait_for_mount = lambda m, timeout=0: None
seq._record_overhead = lambda *a, **k: None
focused = []
seq._maybe_focus = lambda **k: focused.append("focus")
try:
    seq._goto({"ra": M31[0], "dec": M31[1]}, "M31")
    centre_fail = ""
except _CentreFailed as exc:
    centre_fail = str(exc)
case("exhausted centring raises a target failure before focus",
     "83.43" in centre_fail and "not started" in centre_fail and focused == [],
     centre_fail)

seen = []


def run_entry(entry, target, reframed=False):
    seen.append(entry["name"])
    if entry["name"] == "M31":
        raise _CentreFailed("centring failed on M31")


seq._run_entry = run_entry
seq._why_not_now = lambda *a, **k: ""
seq._walk_plan(
    {"entries": [{"id": "a", "name": "M31", "targetId": "t1"},
                 {"id": "b", "name": "M33", "targetId": "t2"}]},
    {"t1": {"id": "t1", "type": "target"}, "t2": {"id": "t2", "type": "target"}},
)
case("the failed target does not finish, and the next target still runs",
     seen == ["M31", "M33"] and seq._error is None
     and "a" not in seq._finished_entries,
     str(seen))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
