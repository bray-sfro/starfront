"""Exercise the filter offset calculator.

    python tools/check_offsets.py

Filters are not parfocal, and the differences are worth measuring once rather
than paying a focus sweep at every filter change all season.

The part that needs checking is not "does it sweep each filter" — it is the
arithmetic that makes the answer mean the *filters* rather than the night.
Focus drifts with temperature and a sweep takes minutes, so measuring L at 22:00
and Ha at 22:12 puts twelve minutes of cooling into the difference between them.
The filters are therefore swept round-robin and each pass is reduced against its
own reading of the reference before the passes are averaged, so drift cancels
instead of accumulating.

So the central case here is a fake focuser that drifts steadily while it is
measured, and the check is that the offsets come out clean anyway.

No test framework, for the same reason as the other checks here.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol.config import Config                          # noqa: E402
from astrocontrol.devices.base import DeviceError               # noqa: E402
from astrocontrol.filteroffsets import OffsetRun                # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Wheel:
    def __init__(self, names):
        self.connected = True
        self.names = list(names)
        self.position = 0
        self.moving = False

    def set_position(self, index):
        self.position = int(index)


class Focuser:
    connected = True
    position = 30000

    def move_to(self, position):
        self.position = int(position)


class Camera:
    connected = True


class FakeFocusRun:
    def __init__(self, position, detail=""):
        self.best_position = position
        self.best_hfd = 2.4 if position is not None else None
        self.detail = detail


class Focusing:
    """Stands in for the autofocus, returning where each filter 'focuses'.

    `truth` is the real offset per filter; `drift` is how far the whole train
    moves per sweep, which is what the round-robin is supposed to cancel.
    """

    def __init__(self, wheel, truth, drift=0.0, fails=(), base=30000, slow=0.0):
        self.wheel = wheel
        self.truth = truth
        self.drift = drift
        self.fails = set(fails)
        self.base = base
        self.slow = slow
        self.sweeps = 0

    def run(self, should_abort=None, report=None):
        name = self.wheel.names[self.wheel.position]
        self.sweeps += 1
        # A real sweep takes minutes. `slow` is for the cases that need one to
        # still be going when something else happens to it.
        if self.slow:
            deadline = time.monotonic() + self.slow
            while time.monotonic() < deadline:
                if should_abort and should_abort():
                    raise DeviceError("focus run aborted")
                time.sleep(0.01)
        if name in self.fails:
            raise DeviceError(f"{name} would not focus")
        return FakeFocusRun(
            int(round(self.base + self.truth.get(name, 0)
                      + self.drift * self.sweeps)))


class Manager:
    def __init__(self, devices):
        self.devices = devices

    def get(self, kind):
        return self.devices.get(kind)


class Rig:
    def __init__(self, config, wheel, focusing):
        self.id = "rig1"
        self.name = "Telescope 1"
        self.config = config
        self.manager = Manager({"filterwheel": wheel, "focuser": Focuser(),
                                "camera": Camera()})
        self.focuser = focusing


class Rigs:
    def __init__(self, rig):
        self.rig = rig
        self.lines = []

    def get(self, rig_id):
        return self.rig

    def log(self, message, level="info"):
        self.lines.append((level, message))


def build(truth, drift=0.0, fails=(), names=None, slow=0.0):
    names = names or list(truth)
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    wheel = Wheel(names)
    rig = Rig(config, wheel, Focusing(wheel, truth, drift, fails, slow=slow))
    return OffsetRun(Rigs(rig), config), rig, config


def finish(run, timeout=20.0):
    deadline = time.monotonic() + timeout
    while run.running and time.monotonic() < deadline:
        time.sleep(0.02)
    return run.status()


# One letter per filter, the program's one spelling.
TRUTH = {"L": 0, "R": -40, "G": -35, "B": 20, "H": 180, "O": 165, "S": 175}

# ------------------------------------------------------------- the plain case
run, rig, config = build(TRUTH)
run.start(rig, passes=1, reference="L")
status = finish(run)
case("it measures every filter", status["result"] is not None
     and len(status["result"]) == len(TRUTH), f"{status.get('error')}")
case("the reference is zero by definition", status["result"]["L"] == 0)
case("and the others are their true offsets",
     all(status["result"][n] == TRUTH[n] for n in TRUTH), f"{status['result']}")

# The answer is written into this telescope's settings, not just reported.
case("the answer is saved for the telescope",
     rig.config.get("sequencer", "filterOffsets", {}).get("H") == 180,
     f"{rig.config.get('sequencer', 'filterOffsets', {})}")

# --------------------------------------------------------------- drift cancels
#
# The whole reason for the round-robin. Five steps of drift per sweep, seven
# filters, two passes: measured end to end that is ~70 steps of contamination,
# and it must not appear in the answer.
run, rig, config = build(TRUTH, drift=5.0)
run.start(rig, passes=2, reference="L")
status = finish(run)
worst = max(abs(status["result"][n] - TRUTH[n]) for n in TRUTH)
case("a focuser drifting through the run still gives the true offsets",
     worst <= 5, f"worst error {worst} steps: {status['result']}")

# The cancellation is the reversal, not the averaging. One pass has nowhere to
# cancel against, and the error is then real — which is worth knowing, because
# it is the argument for the default being two rather than one.
run, rig, config = build(TRUTH, drift=5.0)
run.start(rig, passes=1, reference="L")
single = finish(run)
one_pass_error = max(abs(single["result"][n] - TRUTH[n]) for n in TRUTH)
case("a single pass cannot cancel drift, and does not pretend to",
     one_pass_error > worst, f"one pass {one_pass_error} vs two {worst}")

# The per-pass numbers are kept, so a run that disagrees with itself can be seen
# to have done so rather than presenting an average as though it were solid.
case("the raw per-pass offsets are reported",
     all(len(v) == 2 for v in status["offsets"].values()), f"{status['offsets']}")
case("and the spread between passes is worked out", bool(status["spread"]))

# With no drift the passes agree exactly, and that shows.
run, rig, config = build(TRUTH)
run.start(rig, passes=2, reference="L")
status = finish(run)
case("passes that agree report no spread",
     max(status["spread"].values()) == 0, f"{status['spread']}")

# ------------------------------------------------------- a filter that will not
run, rig, config = build(TRUTH, fails=("O",))
run.start(rig, passes=1, reference="L")
status = finish(run)
case("a filter that will not focus is skipped, not fatal",
     status["result"] is not None and "O" in status["skipped"],
     f"{status['skipped']}")
case("the rest are still measured", status["result"]["H"] == 180)
case("and the one that failed keeps whatever was already known about it",
     "O" not in status["result"])

# A previously stored value for the unmeasurable filter must survive - and
# one stored under the old spelling is found under the new.
run, rig, config = build(TRUTH, fails=("O",))
rig.config.update("sequencer", {"filterOffsets": {"OIII": 999}})
run.start(rig, passes=1, reference="L")
finish(run)
case("an existing offset is not wiped by a run that could not remeasure it",
     rig.config.get("sequencer", "filterOffsets", {})["O"] == 999,
     str(rig.config.get("sequencer", "filterOffsets", {})))

# The reference itself failing makes the pass unusable rather than wrong.
run, rig, config = build(TRUTH, fails=("L",))
run.start(rig, passes=1, reference="L")
status = finish(run)
case("a pass whose reference failed is discarded",
     status["result"] is None and status["error"], f"{status['error']}")

# ------------------------------------------------------------------ refusals
run, rig, config = build(TRUTH)
try:
    run.start(rig, filters=["L"], passes=1)
    refused = ""
except DeviceError as exc:
    refused = str(exc)
case("one filter is not enough to measure an offset", "two filters" in refused,
     refused)

run, rig, config = build(TRUTH)
rig.manager.devices["filterwheel"] = None
try:
    run.start(rig, passes=1)
    refused = ""
except DeviceError as exc:
    refused = str(exc)
case("no wheel is refused clearly", "filter wheel" in refused, refused)

run, rig, config = build(TRUTH)
rig.manager.devices["focuser"] = None
try:
    run.start(rig, passes=1)
    refused = ""
except DeviceError as exc:
    refused = str(exc)
case("no focuser is refused clearly", "focuser" in refused, refused)

# Only filters the wheel actually reports may be asked for.
run, rig, config = build(TRUTH)
run.start(rig, filters=["L", "Ha", "Nonsense"], passes=1, reference="L")
status = finish(run)
case("a filter the wheel does not have is dropped, and Ha is asked for as H",
     set(status["filters"]) == {"L", "H"}, f"{status['filters']}")

# The reference falls back to the autofocus filter when none is named.
run, rig, config = build(TRUTH)
rig.config.update("sequencer", {"autofocusFilter": "Ha"})
run.start(rig, passes=1)
status = finish(run)
case("the autofocus filter is the default reference",
     status["reference"] == "H" and status["result"]["H"] == 0,
     f"{status['reference']}")
case("and everything else is measured from it",
     status["result"]["L"] == -180, f"{status['result']}")

# ---------------------------------------------------------------- stopping it
run, rig, config = build(TRUTH, slow=0.3)
run.start(rig, passes=5, reference="L")
time.sleep(0.1)
run.abort()
status = finish(run)
case("it can be stopped part way", not run.running and status["error"],
     f"{status['error']}")
case("and nothing is saved from a stopped run", status["result"] is None)

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
