"""Exercise the bounds on an autofocus sweep.

    python tools/check_focus_bounds.py

Written against a real failure. On the morning of 2026-09-17 a rig was found
autofocusing at a sunlit sky: one focus run took **915 frames over four and a
half hours** and ended only because somebody pressed Abort. A normal run on that
rig is eleven frames.

The mechanism, which is worth stating because it is not obvious:

  * The sweep extends itself towards whichever arm of the V is short, and the
    target is worked out from the points that *measured* — `min(positions) -
    step`, where `positions` holds only points with a usable HFD.
  * At dawn the new points have no stars, so they never join `positions`.
    `min(positions)` therefore never moves and the same focuser position is
    chosen again, and again.
  * The ceiling on the run was `len(measured)`, and `measured` is keyed by
    position — so re-sampling one place did not grow it. The guard could never
    fire.

So what is checked here is that a sweep which cannot converge *stops*, by every
route out: a run that makes no progress, a run with no stars in it, and a run
that simply goes on too long.

No test framework, for the same reason as the other checks here.
"""
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

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Focuser:
    connected = True
    is_absolute = True
    max_step = 0                    # a driver that does not report its travel
    temperature = 5.0
    moving = False

    def __init__(self):
        self.position = 30000

    def move_to(self, position):
        self.position = int(position)


class Sky:
    """A sky that can be given any shape of HFD curve, including none at all.

    `hfd_at` returns None where there is nothing measurable, which is what a
    sunlit or clouded frame gives.
    """

    def __init__(self, shape):
        self.shape = shape
        self.frames = 0


class Capture:
    def __init__(self, sky, focuser, limit=5000):
        self.sky = sky
        self.focuser = focuser
        self.save_enabled = True
        self.limit = limit

    def capture_blocking(self, exposure, frame_type="light"):
        self.sky.frames += 1
        if self.sky.frames > self.limit:
            # The test's own backstop. If this fires the sweep is unbounded,
            # which is the bug — it would otherwise run until the suite is killed.
            raise RuntimeError(f"unbounded sweep: {self.sky.frames} frames")
        return type("R", (), {"id": f"f{self.sky.frames}"})()

    def frame(self, image_id):
        return self.focuser.position


class Manager:
    def __init__(self, devices):
        self.devices = devices
        self.lines = []

    def get(self, kind):
        return self.devices.get(kind)

    def require(self, kind):
        if kind not in self.devices:
            raise DeviceError(kind)
        return self.devices[kind]

    def log(self, message, level="info"):
        self.lines.append(message)


def build(shape, **settings):
    """A focuser looking at a sky of the given shape."""
    focuser = Focuser()
    sky = Sky(shape)
    capture = Capture(sky, focuser)
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    config.update("sequencer", {"focusPoints": 9, "focusStepSize": 100,
                                "focusAttempts": 1, "focusExposure": 1.0,
                                **settings})
    focus = AutoFocuser(Manager({"focuser": focuser, "camera": object()}),
                        capture, config)
    # `stars.measure` is given the focuser position by the fake capture, so the
    # sky's shape decides what each point measures.
    focusing.stars = type("S", (), {
        "measure": staticmethod(lambda position: {
            "hfd": shape(position), "stars": 40 if shape(position) else 0})})()
    return focus, sky, focuser


# --------------------------------------------------- the failure, reproduced
#
# A real V around 30000, but nothing measurable below 29600 — the sweep walks
# left looking for the other arm, finds no stars, and the edge never moves.

def dawn(position):
    """Stars above 29600, nothing below it, and the curve still falling.

    The sweep therefore walks *downwards* looking for the other arm and walks
    straight into the blank region — which is the geometry the real failure had.
    A shape whose minimum sits at the top instead sends the sweep the other way
    and converges perfectly well, which is why the first version of this check
    passed against the broken code.
    """
    if position < 29600:
        return None                 # sunlit: nothing to measure
    return (position - 29600) / 100.0 + 2.0


def attempt(focus):
    """Run it, and hand back why it gave up. '' means it focused."""
    try:
        focus.run()
        return ""
    except DeviceError as exc:
        return str(exc)
    # RuntimeError is the fake capture's own backstop and means the sweep is
    # unbounded, which is the bug — let it through so the case fails loudly.


focus, sky, focuser = build(dawn)
why = attempt(focus)
case("a sweep that cannot find the other arm stops", bool(why), why)
case("and does so in a sane number of frames", sky.frames < 200,
     f"{sky.frames} frames")
case("saying that it stopped making progress",
     "progress" in why or "no measurable stars" in why, why)

# ------------------------------------------------------- nothing at all to see
#
# A completely blank sky: the cover is on, or it is broad daylight.

focus, sky, focuser = build(lambda position: None)
why = attempt(focus)
case("a completely blank sky stops almost at once",
     bool(why) and sky.frames <= 20, f"{sky.frames} frames: {why}")

# ----------------------------------------------------- a curve that never turns
#
# Monotonic: HFD falls forever in one direction and the minimum is always at the
# edge. Every point measures, so this is the case the sample ceiling has to catch
# rather than the no-progress check.

focus, sky, focuser = build(lambda position: max(0.5, (60000 - position) / 1000.0))
why = attempt(focus)
case("a curve that never turns round is given up on",
     bool(why) and sky.frames < 200, f"{sky.frames} frames")
case("and the reason names the number of points tried",
     "gave up after" in why, why)

# ------------------------------------------------------------- a good sweep
#
# The bounds must not break the ordinary case.

focus, sky, focuser = build(lambda position: abs(position - 30150) / 100.0 + 2.0)
why = attempt(focus)
case("an ordinary V still focuses", not why, why)
case("in about the number of frames it should take", sky.frames < 40,
     f"{sky.frames} frames")
case("and lands near the bottom of the curve",
     abs(focuser.position - 30150) < 400, f"{focuser.position}")

# Frames per point multiplies the exposures but must not multiply how far the
# sweep may wander — multiplying the ceiling by it was the arithmetic that let
# a run reach 915 frames.
focus, sky, focuser = build(dawn, focusFramesPerPoint=3)
why = attempt(focus)
case("more frames per point does not loosen the bound",
     bool(why) and sky.frames < 120, f"{sky.frames} frames")

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
