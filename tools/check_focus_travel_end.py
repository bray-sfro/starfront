"""Autofocus near the end of the focuser's travel.

    python tools/check_focus_travel_end.py

Written against a real failure: EAGLE4PRO0258 (twin FSQ106, NiteCrawler, travel
0-94580), 2026-11-04 00:02-00:15 and again 00:29 CST. Settings: 375-step sweep,
9 points, 3 attempts, starting at 1807. Every attempt measured

    307 3.35 | 682 4.31 | 1057 6.25 | 1432 8.22 | 1807 10.42 | ... | 3307 20.43

- a clean V whose bottom is at about 300 (hyperbola R^2 0.9996) - and was
thrown away with "the focuser reached the end of its travel before the curve
turned round", because the sweep insists on `focusPoints // 2` = 4 points on
each side of the lowest one and 307 - 375 is below zero. It then retried the
same sweep twice more and put the focuser back at 1807 (HFD ~10), so the
night's subs were soft.

Checked here, with that run replayed point for point:
  * the logged run now focuses, near 300, by measuring at 0 and accepting one
    point below the bottom when the travel allows no more;
  * focus genuinely past the end fails at once, says so, does not retry, and
    leaves the focuser at the sharpest point measured, not the soft start;
  * the top end works too (no overshoot past MaxStep);
  * tonight's good runs on the other rigs still focus the same.
"""
import math
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol import focusing                              # noqa: E402
from astrocontrol.config import Config                         # noqa: E402
from astrocontrol.devices.base import DeviceError              # noqa: E402
from astrocontrol.focusing import AutoFocuser                  # noqa: E402

# Moves settle instantly here.
_real_time = focusing.time
focusing.time = type("T", (), {"sleep": staticmethod(lambda s: None),
                               "monotonic": staticmethod(_real_time.monotonic),
                               "time": staticmethod(_real_time.time)})()

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Focuser:
    connected = True
    is_absolute = True
    temperature = 15.5
    moving = False

    def __init__(self, start, max_step):
        self.position = start
        self.max_step = max_step
        self.visited = []

    def move_to(self, position):
        # As the ASCOM / ZWO drivers do: refuse anything outside the travel.
        if self.max_step and not 0 <= position <= self.max_step:
            raise DeviceError(f"position must be 0..{self.max_step}")
        self.position = int(position)
        self.visited.append(self.position)


class Capture:
    def __init__(self, focuser):
        self.focuser = focuser
        self.frames = 0

    def suspend_saving(self):
        pass

    def resume_saving(self):
        pass

    def capture_blocking(self, exposure, frame_type="light"):
        self.frames += 1
        if self.frames > 400:
            raise RuntimeError("unbounded sweep")
        return type("R", (), {"id": self.frames})()

    def frame(self, image_id):
        return self.focuser.position


class Manager:
    def __init__(self, devices):
        self.devices = devices

    def get(self, kind):
        return self.devices.get(kind)

    def require(self, kind):
        if kind not in self.devices:
            raise DeviceError(kind)
        return self.devices[kind]

    def log(self, message, level="info"):
        pass


def hyperbola(centre, scale, width):
    return lambda x: scale * math.sqrt(1 + ((x - centre) / width) ** 2)


def run(shape, start, max_step, **settings):
    focuser = Focuser(start, max_step)
    capture = Capture(focuser)
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    config.update("sequencer", {"focusExposure": 1.0, **settings})
    focus = AutoFocuser(Manager({"focuser": focuser, "camera": object()}),
                        capture, config)
    focusing.stars = type("S", (), {"measure": staticmethod(
        lambda position: {"hfd": shape(position), "stars": 100})})()
    lines = []
    try:
        result = focus.run(report=lines.append)
        why = ""
    except DeviceError as exc:
        result, why = None, str(exc)
    return result, why, focuser, capture, lines


# ------------------------------------------------ EAGLE, 2026-11-04, replayed
EAGLE_LOGGED = {307: 3.345, 682: 4.314, 1057: 6.249, 1432: 8.217, 1807: 10.424,
                2182: 12.832, 2557: 15.311, 2932: 17.835, 3307: 20.425}
# Between and below the logged points: the hyperbola those points fit.
eagle_curve = hyperbola(303.96, 3.418, 513.6)


def eagle(position):
    return EAGLE_LOGGED.get(position) or round(eagle_curve(position), 3)


EAGLE = dict(focusStepSize=375, focusPoints=9, focusBacklash=0, focusAttempts=3,
             focusMethod="trendhyperbolic")

result, why, focuser, capture, lines = run(eagle, 1807, 94580, **EAGLE)
measured = {p.position for p in (result.points if result else [])}
case("EAGLE's logged run now focuses", result is not None, why)
case("measuring the same points the rig logged",
     set(EAGLE_LOGGED) <= measured, f"{sorted(measured)}")
case("and the end of travel itself", 0 in measured)
case("landing near the bottom of the curve (~300)",
     result is not None and abs(result.best_position - 304) < 120,
     f"{result and result.best_position}")
case("with sharp stars, not the HFD 10 it started at",
     result is not None and result.best_hfd < 3.6, f"{result and result.best_hfd}")
case("on the first attempt", result is not None and result.attempt == 1)
case("in a normal number of frames", capture.frames <= 20, f"{capture.frames}")
case("never asking the focuser for a position outside its travel",
     all(0 <= p <= 94580 for p in focuser.visited))


# ---------------------------------------- focus really past the end of travel
past = hyperbola(-900, 3.4, 513.6)
result, why, focuser, capture, lines = run(past, 1807, 94580, **EAGLE)
case("focus truly below 0 still fails", result is None, why)
case("saying focus is past the low end of travel", "past the low end" in why, why)
case("without two more identical attempts",
     capture.frames <= 20 and not any("attempt 1 was no good" in l for l in lines),
     f"{capture.frames} frames")
case("and leaves the focuser at the sharpest point measured (0), not 1807",
     focuser.position == 0, f"{focuser.position}")

# ----------------------------------------------------- the top of the travel
top = hyperbola(59880, 3.0, 300)
result, why, focuser, capture, lines = run(top, 59500, 60000, focusStepSize=100,
                                           focusPoints=9, focusBacklash=25,
                                           focusAttempts=1, focusMethod="hyperbolic")
case("focus near the top end works", result is not None, why)
case("and lands near it", result is not None and abs(result.best_position - 59880) < 80,
     f"{result and result.best_position}")
case("with no move past MaxStep", all(0 <= p <= 60000 for p in focuser.visited))

# ---------------------------------- tonight's good runs, unchanged behaviour
GOOD = {
    "FSQ106 (WIN-Q9DV8QD504V) 00:20 CST": (hyperbola(33039, 1.0, 13), 32968, 60000,
        dict(focusStepSize=50, focusPoints=9, focusBacklash=25, focusAttempts=1,
             focusMethod="hyperbolic")),
    "vsd100 00:25 CST": (hyperbola(25190, 6.3, 130), 25187, 60000,
        dict(focusStepSize=100, focusPoints=9, focusBacklash=0, focusAttempts=3,
             focusMethod="trendhyperbolic")),
    "RASA rasa2 23:59 CST": (hyperbola(46561, 3.0, 220), 48178, 600000,
        dict(focusStepSize=250, focusPoints=9, focusBacklash=0, focusAttempts=1,
             focusMethod="hyperbolic")),
}
for name, (shape, start, top_, settings) in GOOD.items():
    centre = {"FSQ": 33039, "vsd": 25190, "RAS": 46561}[name[:3]]
    result, why, focuser, capture, lines = run(shape, start, top_, **settings)
    case(f"{name} still focuses", result is not None
         and abs(result.best_position - centre) < settings["focusStepSize"],
         why or f"{result.best_position}")

# ------------------------------------------- a failure that is not the travel
# Clouds: the old behaviour (go back to the start) is kept when nothing was
# measured sharper than the start.
result, why, focuser, capture, lines = run(lambda p: None, 30000, 60000,
                                           focusStepSize=100, focusPoints=9,
                                           focusAttempts=1)
case("a starless run still goes back to where it started",
     result is None and focuser.position == 30000, f"{focuser.position}: {why}")

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
