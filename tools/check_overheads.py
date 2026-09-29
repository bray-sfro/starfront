"""Exercise the measured-overhead model and the budget it feeds.

    python tools/check_overheads.py

The thing being checked is an estimate, so "is it right" is the wrong question.
The right ones are: does timing one focus run predict a differently-shaped run,
does a single stalled download move the figure, and does the plan actually get
charged for the sweeps it is going to do — which it never used to be, and which
is how a night budgeted to the last minute ran out of dark an hour early.

No test framework, for the same reason as the other checks here.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol import schedule                                # noqa: E402
from astrocontrol.overheads import KINDS, WINDOW, OverheadStore  # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


def store():
    return OverheadStore(Path(tempfile.mkdtemp()) / "overheads.json")


# ------------------------------------------------------------- the basics
s = store()
case("an unmeasured figure falls back to the assumption",
     s.value("download") == KINDS["download"]["default"]
     and not s.measured("download"))

for value in (11.0, 12.0, 13.0):
    s.record("download", value)
case("a measured figure is the median of what was seen",
     s.value("download") == 12.0 and s.measured("download"),
     f"{s.value('download')}s from 11, 12, 13")

# One stalled download must not move the estimate. This is why it is a median.
s.record("download", 300.0)
case("one stalled download barely moves it",
     s.value("download") == 12.5, f"{s.value('download')}s after a 300s outlier")

s.record("download", -4.0)
s.record("download", 99999.0)
case("implausible figures are refused rather than stored",
     len([v for v in [s.value("download")] if v < 20]) == 1,
     f"{s.value('download')}s")

# The window rolls, so a rig that changes is followed rather than averaged
# with its own history for ever.
s = store()
for _ in range(WINDOW + 20):
    s.record("download", 30.0)
for _ in range(WINDOW):
    s.record("download", 5.0)
case("the window rolls, so a faster camera is believed",
     s.value("download") == 5.0, f"{s.value('download')}s after {WINDOW} fast frames")


# ------------------------------- one focus run predicts every focus run
s = store()
# A real nine-point sweep at 6s, one frame a point, that took four minutes.
s.record_focus_run(seconds=240.0, points=9, exposure=6.0, frames_per_point=1)
predicted_same = s.focus_seconds(9, 6.0, 1)
case("timing one run reproduces that run",
     abs(predicted_same - 240.0) < 1.0,
     f"predicted {predicted_same:.0f}s for the run that was timed (240s)")

# The same rig, a longer sweep with longer subs: the exposures are arithmetic
# and the per-point cost is what was measured.
predicted_other = s.focus_seconds(15, 20.0, 1)
shutter = 15 * 20.0
case("a differently-shaped sweep is predicted from the same numbers",
     predicted_other > shutter and predicted_other < shutter + 400,
     f"{predicted_other:.0f}s for 15 points at 20s ({shutter:.0f}s of it shutter)")
case("and the extra points cost the measured per-point figure",
     abs((s.focus_seconds(10, 6.0, 1) - s.focus_seconds(9, 6.0, 1))
         - (6.0 + s.value("focusPerPoint"))) < 0.01,
     f"one more point costs {s.focus_seconds(10, 6.0, 1) - s.focus_seconds(9, 6.0, 1):.1f}s")

case("frames per point are counted",
     s.focus_seconds(9, 6.0, 2) > s.focus_seconds(9, 6.0, 1) + 9 * 5.9)

# A run whose overhead is negative (a clock jump) is ignored rather than stored.
before = s.value("focusPerPoint")
s.record_focus_run(seconds=10.0, points=9, exposure=6.0, frames_per_point=1)
case("a run that cannot be right is ignored",
     s.value("focusPerPoint") == before)


# ------------------------------------------ the plan is charged for focus
ALLOC = [{"name": "L", "exposure": 300, "count": 12}]
bare = {"perFrame": 15.0, "filterChange": 20.0, "perPanel": 90.0}
withfocus = {**bare, "focusRun": 240.0, "focusEvery": 60.0, "focusOnFilter": True}

plain = schedule.plan_seconds(ALLOC, 1, bare)
costed = schedule.plan_seconds(ALLOC, 1, withfocus)
case("autofocus used to cost the plan nothing at all",
     plain == 12 * 315 + 20 + 90, f"{plain:.0f}s")
case("now it is charged for",
     costed > plain, f"{costed:.0f}s against {plain:.0f}s, "
                     f"{(costed - plain) / 60:.1f} min of focusing")
case("at least one sweep, even for a short target",
     schedule.plan_seconds([{"name": "L", "exposure": 60, "count": 2}], 1, withfocus)
     - schedule.plan_seconds([{"name": "L", "exposure": 60, "count": 2}], 1, bare)
     >= 240.0)

# The clock trigger and the filter trigger overlap — a run for one resets the
# other — so the greater is taken rather than the sum.
four = [{"name": n, "exposure": 300, "count": 3} for n in ("L", "R", "G", "B")]
by_filter = (schedule.plan_seconds(four, 1, {**withfocus, "focusEvery": 0})
             - schedule.plan_seconds(four, 1, {**bare, "focusEvery": 0}))
case("four filters pay for four sweeps",
     abs(by_filter - 4 * 240.0) < 1.0, f"{by_filter / 60:.1f} min")
both = (schedule.plan_seconds(four, 1, withfocus)
        - schedule.plan_seconds(four, 1, bare))
case("the two triggers are not double-counted",
     both <= 4 * 240.0 + 1.0, f"{both / 60:.1f} min, not {8 * 240.0 / 60:.1f}")

# And the ceiling a filter input offers has to leave room for them.
room_bare = schedule.max_count(300, 1, 4 * 3600, [], bare)
room_focus = schedule.max_count(300, 1, 4 * 3600, [], withfocus)
case("the frame ceiling leaves room for the sweeps",
     room_focus < room_bare,
     f"{room_focus} frames fit against {room_bare} when focusing was free")
fits = schedule.plan_seconds([{"name": "L", "exposure": 300, "count": room_focus}],
                             1, withfocus)
case("and what it offers really does fit the night",
     fits <= 4 * 3600, f"{fits / 3600:.2f}h of a 4h window")
one_more = schedule.plan_seconds(
    [{"name": "L", "exposure": 300, "count": room_focus + 1}], 1, withfocus)
case("and one more frame would not",
     one_more > 4 * 3600, f"{one_more / 3600:.2f}h")

# The ceiling and the budget have to agree with each other, which they did not
# while each worked the focus allowance out on its own.
kept = [{"name": "L", "exposure": 300, "count": 12}]
room = schedule.max_count(180, 1, 4 * 3600, kept, withfocus)
together = schedule.plan_seconds(
    kept + [{"name": "R", "exposure": 180, "count": room}], 1, withfocus)
case("a second filter's ceiling accounts for the first",
     together <= 4 * 3600,
     f"12 L plus {room} R is {together / 3600:.2f}h of 4h")
case("and is not left short by counting the focus twice",
     schedule.plan_seconds(kept + [{"name": "R", "exposure": 180,
                                    "count": room + 1}], 1, withfocus) > 4 * 3600,
     f"{room} R is the most that fits")

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
