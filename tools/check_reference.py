"""Exercise opening a FITS frame as a framing reference.

    python tools/check_reference.py

The part worth checking is the header reading, because FITS headers are written
by a dozen programs that agree on very little.  RA turns up as sexagesimal text,
as decimal hours and as decimal degrees; the scale turns up as CDELT or as a CD
matrix; and a frame that has already been solved must not be sent to ASTAP to
rediscover what it is already carrying — that is what makes this work on a
laptop with no solver installed, which is where planning gets done.

No test framework, for the same reason as the other checks here.
"""
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol.config import Config                          # noqa: E402
from astrocontrol.devices.base import DeviceError                # noqa: E402
from astrocontrol.imaging import fits                           # noqa: E402
from astrocontrol.solving import Solver, _FileRecord            # noqa: E402

results = []
WORK = Path(tempfile.mkdtemp())


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


def solver_without_astap():
    """A solver that cannot fall back, so only the header path can answer."""
    s = Solver.__new__(Solver)
    s.config = Config()
    s.executable = lambda: None
    return s


def write(name, header, width=1200, height=800):
    frame = np.full((height, width), 500, dtype=np.uint16)
    return fits.write(WORK / name, frame, header)


WCS = {
    "CRVAL1": (10.6847, ""), "CRVAL2": (41.269, ""),
    "CDELT1": (-2.0 / 3600.0, ""), "CDELT2": (2.0 / 3600.0, ""),
    "CROTA2": (35.0, ""),
}

# ----------------------------------------------------- a solved frame
path = write("solved.fit", {**WCS, "OBJECT": ("M31", ""), "EXPTIME": (300.0, "")})
result = solver_without_astap().solve_file(path)
case("a frame that carries a WCS needs no solver at all",
     abs(result.ra - 0.712313) < 1e-4 and abs(result.dec - 41.269) < 1e-3,
     f"{result.ra:.5f}h {result.dec:+.3f}")
case("its scale comes out in arcseconds per pixel",
     abs(result.scale - 2.0) < 1e-3, f"{result.scale}")
case("its field size comes from the scale and the frame size",
     abs(result.fov_width - 1200 * 2.0 / 3600.0) < 1e-4
     and abs(result.fov_height - 800 * 2.0 / 3600.0) < 1e-4,
     f"{result.fov_width:.4f} x {result.fov_height:.4f} deg")
case("and its position angle is read out",
     abs(result.rotation - 35.0) < 1e-6, f"{result.rotation}")
case("a normal sky orientation is not reported as mirrored",
     result.flipped is False)

mirrored = write("mirrored.fit", {**WCS, "CDELT1": (2.0 / 3600.0, "")})
case("a positive CDELT1 is reported as mirrored",
     solver_without_astap().solve_file(mirrored).flipped is True)

# ------------------------------------------------------- a CD matrix
# The same geometry expressed the other way round, which is what a good many
# programs write instead of CDELT. The standard relations, with CDELT1 negative
# for a normal east-left sky:
#     CD1_1 =  CDELT1 cos(rot)   CD1_2 = -CDELT2 sin(rot)
#     CD2_1 =  CDELT1 sin(rot)   CD2_2 =  CDELT2 cos(rot)
scale = 2.0 / 3600.0
angle = np.radians(35.0)
cd = write("cdmatrix.fit", {
    "CRVAL1": (10.6847, ""), "CRVAL2": (41.269, ""),
    "CD1_1": (-scale * np.cos(angle), ""), "CD1_2": (-scale * np.sin(angle), ""),
    "CD2_1": (-scale * np.sin(angle), ""), "CD2_2": (scale * np.cos(angle), ""),
})
result = solver_without_astap().solve_file(cd)
case("a CD matrix is understood as well as CDELT",
     abs(result.scale - 2.0) < 0.01, f"{result.scale}\" per pixel")
case("and the angle comes out of it the right way round",
     abs(result.rotation - 35.0) < 0.5, f"{result.rotation}")

# ---------------------------------------------- a frame with no solution
plain = write("plain.fit", {"OBJECT": ("M31", ""), "EXPTIME": (60.0, "")})
try:
    solver_without_astap().solve_file(plain)
    said = ""
except Exception as exc:                        # noqa: BLE001 - that is the check
    said = str(exc)
case("a frame nothing can solve says what each route said",
     "ASTAP" in said and "astrometry.net" in said, said)

try:
    solver_without_astap().solve_file(WORK / "nothing-here.fit")
    missing = ""
except Exception as exc:                        # noqa: BLE001
    missing = str(exc)
case("a missing file says so rather than throwing something cryptic",
     "is not a file" in missing, missing)


# ------------------------------------------- how headers write an angle
def hinted(header):
    return _FileRecord.from_header(Path("x.fit"), {"NAXIS1": 100, "NAXIS2": 100,
                                                   **header})


case("sexagesimal RA in hours is read",
     abs(hinted({"OBJCTRA": "00 42 44.3"}).ra - 0.712306) < 1e-4,
     f"{hinted({'OBJCTRA': '00 42 44.3'}).ra}")
case("colon-separated works too",
     abs(hinted({"OBJCTRA": "00:42:44.3"}).ra - 0.712306) < 1e-4)
case("a negative declination keeps its sign",
     abs(hinted({"OBJCTDEC": "-05 23 28"}).dec + 5.391111) < 1e-4,
     f"{hinted({'OBJCTDEC': '-05 23 28'}).dec}")
case("a bare number is taken as hours, which is what OBJCTRA means",
     abs(hinted({"OBJCTRA": 0.7123}).ra - 0.7123) < 1e-6)
# Under 24 it is genuinely ambiguous and the convention wins; over 24 it cannot
# be hours, so it must be degrees.
case("an RA that cannot be hours is read as degrees",
     abs(hinted({"OBJCTRA": 187.7}).ra - 12.513) < 1e-3,
     f"{hinted({'OBJCTRA': 187.7}).ra}h from 187.7")
case("and one that could be hours is left as hours",
     abs(hinted({"OBJCTRA": 10.6847}).ra - 10.6847) < 1e-6)
case("nonsense is None rather than a wrong number",
     hinted({"OBJCTRA": "not an angle"}).ra is None)
case("a WCS in the header beats the mount's guess",
     abs(hinted({"OBJCTRA": "23 00 00", "CRVAL1": 10.6847,
                 "CRVAL2": 41.269}).ra - 0.712313) < 1e-4)

# The frame's own size and binning come out for the solver's hints.
record = hinted({"NAXIS1": 6248, "NAXIS2": 4176, "XBINNING": 2})
case("the frame size and binning are read for the solver's hints",
     record.width == 6248 and record.height == 4176 and record.binning == 2)


# ------------------------------------------- ASTAP is asked to solve blind
# The bug this was written for: a file from another rig was handed *this*
# telescope's pointing and field as hints, so ASTAP searched a fifteen-degree
# circle around the wrong place at the wrong scale and reported, correctly,
# that there was nothing there.
class FakeProcess:
    returncode = 0

    def communicate(self, timeout=None):
        return ("", "")


captured = {}


def fake_popen(args, **kwargs):
    captured["args"] = list(args)
    # ASTAP writes its answer beside the output path; fake a solved one.
    out = Path(args[args.index("-o") + 1]).with_suffix(".ini")
    out.write_text("PLTSOLVD=T\nCRVAL1=10.6847\nCRVAL2=41.269\n"
                   "CDELT1=-0.000555\nCDELT2=0.000555\nCROTA2=35.0\n", "utf-8")
    return FakeProcess()


import subprocess                                                # noqa: E402
import astrocontrol.solving as solving                            # noqa: E402

real_popen = subprocess.Popen
solving.subprocess.Popen = fake_popen
try:
    s = Solver.__new__(Solver)
    s.config = Config()
    s.manager = None
    s.capture = None
    s._abort = __import__("threading").Event()
    s._process = None
    s.executable = lambda: Path("astap.exe")
    result = s.solve_file(plain)
finally:
    solving.subprocess.Popen = real_popen

args = captured.get("args", [])
case("a reference frame is solved blind, with no pointing hint",
     "-ra" not in args and "-spd" not in args, " ".join(args[1:]))
case("and with no field-size hint from this telescope's optics",
     "-fov" not in args, " ".join(args[1:]))
case("so the search covers the whole sky",
     "-r" in args and args[args.index("-r") + 1] == "180",
     " ".join(args[1:]))
case("and the answer is marked as ASTAP's", result.method == "astap")


# -------------------------------- astrometry.net's answer, in our own shape
record = _FileRecord(id="x", filename="x.fit", width=1200, height=800,
                     binning=1, ra=None, dec=None)
converted = Solver._from_calibration({
    "ra": 10.6847, "dec": 41.269, "pixscale": 2.0, "orientation": 35.0,
    "parity": -1.0, "jobId": 12345,
}, record)
case("astrometry.net degrees become hours",
     abs(converted.ra - 0.712313) < 1e-4, f"{converted.ra}h")
case("its pixel scale becomes a field size",
     abs(converted.fov_width - 1200 * 2.0 / 3600.0) < 1e-4,
     f"{converted.fov_width:.4f} deg")
case("its orientation is the position angle",
     abs(converted.rotation - 35.0) < 1e-6)
case("negative parity is a normal, unmirrored frame",
     converted.flipped is False)
case("positive parity is a mirrored one",
     Solver._from_calibration({"ra": 0, "dec": 0, "pixscale": 1,
                               "orientation": 0, "parity": 1.0}, record).flipped
     is True)
case("and the answer says where it came from",
     converted.method == "astrometry.net" and "12345" in converted.detail,
     f"{converted.method}, {converted.detail}")

# ------------------------------------- adopting the angle a reference measured


class _Manager:
    def __init__(self):
        self.lines = []
        self.devices = {}

    def log(self, message, level="info"):
        self.lines.append(message)

    def get(self, kind):
        return self.devices.get(kind)


def angle_solver(sensor=(1200, 800), rotation=268.0, enabled=True):
    s = Solver.__new__(Solver)
    s.config = Config()
    s.config.update("optics", {"sensorWidth": sensor[0], "sensorHeight": sensor[1],
                               "rotation": rotation, "angleFromSolve": enabled})
    s.manager = _Manager()
    s._angle_reasons = set()
    return s


def stored_angle(s):
    return s.config.get("optics", "rotation")


solved_at = write("angle.fit", {**WCS})
measured = solver_without_astap().solve_file(solved_at)

s = angle_solver()
s.adopt_angle(measured, 1200, 800)
case("a reference off this camera sets the stored camera angle",
     abs(stored_angle(s) - 35.0) < 1e-6, f"{stored_angle(s)}")
case("and it says so in the log",
     any("Camera angle set to" in line for line in s.manager.lines))

s = angle_solver()
s.adopt_angle(measured, 600, 400)
case("a bin-2 frame off the same sensor counts too",
     abs(stored_angle(s) - 35.0) < 1e-6, f"{stored_angle(s)}")

s = angle_solver()
s.adopt_angle(measured, 1200, 600)
case("a frame off another rig is declined",
     abs(stored_angle(s) - 268.0) < 1e-6, f"{stored_angle(s)}")

s = angle_solver()
s.adopt_angle(measured, 1800, 1200)
case("so is one larger than our sensor",
     abs(stored_angle(s) - 268.0) < 1e-6, f"{stored_angle(s)}")

s = angle_solver()
s.adopt_angle(measured, 1000, 667)
case("and so is a crop that is not a whole binning",
     abs(stored_angle(s) - 268.0) < 1e-6, f"{stored_angle(s)}")

s = angle_solver(enabled=False)
s.adopt_angle(measured, 1200, 800)
case("turning the setting off stops it entirely",
     abs(stored_angle(s) - 268.0) < 1e-6, f"{stored_angle(s)}")


class _Rotator:
    connected = True


s = angle_solver()
s.manager.devices["rotator"] = _Rotator()
s.adopt_angle(measured, 1200, 800)
case("a connected rotator keeps the angle out of the settings",
     abs(stored_angle(s) - 268.0) < 1e-6, f"{stored_angle(s)}")

s = angle_solver(sensor=(0, 0))
s.adopt_angle(measured, 1200, 800)
case("an unknown sensor size declines rather than guesses",
     abs(stored_angle(s) - 268.0) < 1e-6, f"{stored_angle(s)}")

# ------------------------------------------------------------ centre and rotate
#
# A rotator is commanded in its own mechanical coordinates, and those agree with
# sky position angle only as far as its calibration does. The solve that centres
# the target measures the real angle, so the error is known exactly.


class Rotator:
    def __init__(self, position=0.0, refuses=False):
        self.connected = True
        self.position = float(position)
        self.moving = False
        self.refuses = refuses
        self.moves = []

    def move_absolute(self, angle):
        if self.refuses:
            raise DeviceError("the rotator will not move")
        self.moves.append(float(angle))
        self.position = float(angle) % 360.0


def rotating_solver(rotator, tolerance=1.0):
    s = Solver.__new__(Solver)
    s.config = Config()
    s.config.update("solver", {"rotationTolerance": tolerance})
    s.manager = _Manager()
    s.manager.devices["rotator"] = rotator
    s._angle_reasons = set()
    s._abort = __import__("threading").Event()
    s._lock = __import__("threading").RLock()
    s._state = "idle"
    s._message = ""
    return s


# Asked for 90°, the sky says 87°: three degrees out, so turn by three.
rot = Rotator(position=100.0)
s = rotating_solver(rot)
moved = s._correct_rotation(90.0, 87.0)
case("an angle error turns the rotator", moved and len(rot.moves) == 1)
case("by exactly the error the solve measured",
     abs(rot.moves[0] - 103.0) < 1e-6, f"{rot.moves[0]}")

# Inside tolerance, it is left alone — a rotator that hunts half a degree all
# night is worse than one that is half a degree out.
rot = Rotator(position=100.0)
s = rotating_solver(rot, tolerance=1.0)
case("an error inside tolerance moves nothing",
     not s._correct_rotation(90.0, 89.5) and not rot.moves)

# The short way round: 359° and 1° are two degrees apart, not 358.
rot = Rotator(position=50.0)
s = rotating_solver(rot)
s._correct_rotation(1.0, 359.0)
case("the correction goes the short way round the circle",
     abs(rot.moves[0] - 52.0) < 1e-6, f"{rot.moves[0]}")

rot = Rotator(position=50.0)
s = rotating_solver(rot)
s._correct_rotation(359.0, 1.0)
case("and the other way as well",
     abs(rot.moves[0] - 48.0) < 1e-6, f"{rot.moves[0]}")

# Wrapping past zero produces a position on the circle, not a negative one.
rot = Rotator(position=1.0)
s = rotating_solver(rot)
s._correct_rotation(0.0, 5.0)
case("a correction past zero wraps rather than going negative",
     0.0 <= rot.position < 360.0, f"{rot.position}")

# No rotator, or one that will not move, is reported rather than fatal.
s = rotating_solver(Rotator(refuses=True))
case("a rotator that refuses does not end the night",
     s._correct_rotation(90.0, 80.0) is False)

s = Solver.__new__(Solver)
s.config = Config()
s.manager = _Manager()
s._angle_reasons = set()
case("no rotator at all is not an error",
     s._correct_rotation(90.0, 80.0) is False)

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
