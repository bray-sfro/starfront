"""What a target will actually get through before its window closes.

    python tools/check_tonight.py

An allocation is what somebody asked for.  On a mosaic bigger than one night the
two are very different numbers, and only one of them is a plan: forty panels of
narrowband in a five-hour window is forty-five hours of work, and a plan that
reports only the ask is how an operator finds out in the morning that thirty-six
panels were never started.

The forecast has to agree with what the run really does, or it is worse than no
forecast at all.  So the properties checked here are the ones that would let it
drift: that whole panels are costed through the same `plan_seconds` the budget
and the ceilings use, that the panel cut short is the one the run would cut
short, and that nothing is ever promised that does not fit.

No test framework, for the same reason as the other checks here.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astrocontrol import schedule                                 # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


OVERHEADS = {"perFrame": 15.0, "filterChange": 20.0, "perPanel": 90.0,
             "focusRun": 153.0, "focusEvery": 60.0, "focusOnFilter": True}

HA = [{"name": "Ha", "exposure": 300.0, "count": 12}]
FORTY = [{"index": i} for i in range(1, 41)]


def hours(value):
    return value * 3600.0


# ===========================================================================
print("\n-- the case this exists for --")

# A collaboration chunk of forty panels, twelve 300-second frames each, and a
# five-hour window. The plan used to say "5 hours" and mean it as a total.
whole = schedule.plan_seconds(HA, 40, OVERHEADS)
case("forty panels of this is a season, not a night",
     whole / 3600.0 > 40, f"{whole / 3600.0:.1f} hours of work")

night = schedule.tonight(HA, FORTY, hours(5), OVERHEADS)
case("five hours gets through four whole panels", night["complete"] == 4,
     str(night["complete"]))
case("...and starts a fifth", night["partial"] is not None
     and night["partial"]["panel"] == 5, str(night["partial"]))
case("...so five panels are named", night["panels"] == [1, 2, 3, 4, 5],
     str(night["panels"]))
case("...and it says it does not fit", night["fits"] is False)
case("...with the frames that really come home",
     night["frames"] == {"Ha": 4 * 12 + night["partial"]["frames"]},
     str(night["frames"]))

# The promise that matters: never more than the window holds.
case("what is promised fits in the window", night["seconds"] <= hours(5),
     f'{night["seconds"] / 3600.0:.2f}h of 5h')

# ...and the whole-panel part agrees exactly with the function the budget uses.
case("whole panels are costed the way the budget costs them",
     abs(schedule.plan_seconds(HA, night["complete"], OVERHEADS)
         + sum(schedule.frame_seconds(300.0, OVERHEADS)
               for _ in range(night["partial"]["frames"]))
         + OVERHEADS["perPanel"] + OVERHEADS["filterChange"]
         - night["seconds"]) < 1.0,
     f'{night["seconds"]:.1f}s')

# ===========================================================================
print("\n-- the edges --")

case("a window that holds nothing shoots nothing",
     schedule.tonight(HA, FORTY, 60.0, OVERHEADS)["nothing"] is True)
case("no allocation is nothing planned",
     schedule.tonight([], FORTY, hours(5), OVERHEADS)["nothing"] is True)
case("no panels is nothing planned",
     schedule.tonight(HA, [], hours(5), OVERHEADS)["nothing"] is True)
case("a closed window is nothing planned",
     schedule.tonight(HA, FORTY, 0.0, OVERHEADS)["nothing"] is True)

# A single-panel target either fits or is cut short; it is never "skipped".
single = schedule.tonight(HA, [{"index": 1}], hours(5), OVERHEADS)
case("a single panel that fits, fits", single["fits"] is True
     and single["complete"] == 1 and single["partial"] is None,
     str(single["panels"]))
case("...and its frames are the whole allocation",
     single["frames"] == {"Ha": 12}, str(single["frames"]))

roomy = schedule.tonight(HA, FORTY, hours(500), OVERHEADS)
case("a window long enough gets all forty", roomy["fits"] is True
     and roomy["complete"] == 40, str(roomy["complete"]))
case("...and asks for nothing more than the allocation",
     roomy["frames"] == {"Ha": 40 * 12}, str(roomy["frames"]))

# Rows with no frames must not count as a filter: an empty row used to cost a
# filter change it never made.
padded = schedule.tonight(
    HA + [{"name": "OIII", "exposure": 300.0, "count": 0}],
    FORTY, hours(5), OVERHEADS)
case("a filter with no frames changes nothing",
     padded["frames"] == night["frames"] and padded["panels"] == night["panels"])

# ===========================================================================
print("\n-- the panel that gets cut short --")

# The run checks the clock before *every* frame, so the last panel is cut short
# rather than skipped. A forecast that skipped it would under-report a whole
# panel's worth of frames every night.
tight = schedule.tonight(HA, FORTY, hours(1.4), OVERHEADS)
case("a window that holds one panel and a bit starts the second",
     tight["complete"] == 1 and tight["partial"]
     and tight["partial"]["panel"] == 2,
     f'complete {tight["complete"]}, partial {tight["partial"]}')
case("...and counts the partial panel's frames",
     tight["frames"]["Ha"] > 12, str(tight["frames"]))
case("...but not more than a whole panel's worth",
     tight["partial"]["frames"] < 12, str(tight["partial"]))

# ===========================================================================
print("\n-- which frames, in which order --")

MIX = [{"name": "Ha", "exposure": 300.0, "count": 3},
       {"name": "OIII", "exposure": 300.0, "count": 2}]

grouped = [f["name"] for f in schedule.shooting_order(MIX, "grouped")]
rotated = [f["name"] for f in schedule.shooting_order(MIX, "rotate")]
case("grouped shoots one filter out before moving on",
     grouped == ["Ha", "Ha", "Ha", "OIII", "OIII"], str(grouped))
case("rotate cycles them", rotated == ["Ha", "OIII", "Ha", "OIII", "Ha"],
     str(rotated))
case("both take every frame", len(grouped) == len(rotated) == 5)

# And it matters to the forecast: a session cut short comes home with different
# data depending on which is in force, which is the whole reason `rotate`
# exists. A forecast that ignored it would describe a night nobody had.
room = hours(1.0)
by_group = schedule.tonight(MIX, FORTY, room, OVERHEADS, "grouped")
by_rotate = schedule.tonight(MIX, FORTY, room, OVERHEADS, "rotate")
case("the two orders come home with different frames",
     by_group["frames"] != by_rotate["frames"],
     f'grouped {by_group["frames"]}, rotate {by_rotate["frames"]}')
case("...but the same amount of night",
     abs(by_group["seconds"] - by_rotate["seconds"]) < 1.0)

# ===========================================================================
print("\n-- panels are named in the order they are walked --")

# The forecast is handed the run's own capture order, not 1..n, because a mosaic
# is walked by what is setting first. Naming panels 1, 2, 3 while the run shoots
# 7, 4, 1 would be a confident lie.
walked = schedule.tonight(HA, [7, 4, 1, 2], hours(3), OVERHEADS)
case("the panels named are the ones walked first",
     walked["panels"][:2] == [7, 4], str(walked["panels"]))


# ===========================================================================
print("\n-- 'give it three hours', the project's way --")

# The operator chooses how much; the project chooses what. A collaboration
# whose contributors each picked their own sub length would be stacking frames
# that do not belong in the same stack, so the exposures below come from the
# task and the split keeps its proportions.
import os                                                          # noqa: E402
import tempfile                                                    # noqa: E402

# Somewhere temporary, and never inside the project: importing `main` builds a
# data directory, and the first version of this put one in the source tree,
# where it was promptly picked up and published.
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())
from astrocontrol.main import _hours_allocation                    # noqa: E402

TASK = {"filters": [
    {"filter": "Ha", "exposure": 600.0, "hours": 3.0},
    {"filter": "OIII", "exposure": 600.0, "hours": 1.0},
]}

alloc = _hours_allocation(TASK, 1, 4.0, OVERHEADS)
by_name = {row["name"]: row for row in alloc}
case("the sub lengths are the project's, not ours",
     all(row["exposure"] == 600.0 for row in alloc),
     str([row["exposure"] for row in alloc]))
# Whole frames cannot hold an exact 3:1 - 23 frames do not divide that way - so
# what is checked is that it is as close as integers allow, not that it is
# exact. Asserting exactness here would be asserting something false.
ratio = by_name["Ha"]["count"] / max(1, by_name["OIII"]["count"])
case("three-to-one stays about three-to-one", 2.6 < ratio < 3.4,
     f'Ha {by_name["Ha"]["count"]}, OIII {by_name["OIII"]["count"]} = {ratio:.2f}')
case("and four hours is about four hours",
     abs(schedule.plan_seconds(alloc, 1, OVERHEADS) - hours(4)) < hours(0.5),
     f'{schedule.plan_seconds(alloc, 1, OVERHEADS) / 3600:.2f}h')

# ...and the leftover is not thrown away. Rounding every filter down on its own
# wasted a frame's worth of night, which on a two-hour session is real.
FRAME = 600.0 + OVERHEADS["perFrame"]
used = sum(row["count"] for row in alloc) * FRAME
case("the night is used up to the last frame that fits",
     hours(4) - used < FRAME,
     f"{(hours(4) - used) / 60:.1f} minutes left, a frame is {FRAME / 60:.1f}")

# Per panel, not in total: each panel of a mosaic wants the hours, because
# depth is integration time at a point on the sky.
one = _hours_allocation(TASK, 1, 4.0, OVERHEADS)
four = _hours_allocation(TASK, 4, 4.0, OVERHEADS)
case("a four-panel chunk gets a quarter of the frames on each",
     four[0]["count"] * 4 <= one[0]["count"] + 4,
     f'{one[0]["count"]} on one panel, {four[0]["count"]} each on four')

case("no hours is nothing planned", _hours_allocation(TASK, 1, 0.0, OVERHEADS) == [])
case("a task with no filters plans nothing",
     _hours_allocation({"filters": []}, 1, 3.0, OVERHEADS) == [])

# A budget too small for a frame still has to come back with something, or
# "twenty minutes" reads as "nothing planned" and the operator cannot tell the
# difference between a small night and a broken one.
tiny = _hours_allocation(TASK, 1, 0.05, OVERHEADS)
case("a tiny budget still plans one frame",
     sum(row["count"] for row in tiny) == 1, str(tiny))

# Equal hours split equally, even when the task never said any hours at all.
flat = _hours_allocation(
    {"filters": [{"filter": "L", "exposure": 120.0, "hours": 0},
                 {"filter": "R", "exposure": 120.0, "hours": 0}]},
    1, 2.0, OVERHEADS)
# Within one, not exactly equal: an odd number of frames cannot split evenly,
# and the odd one out goes to whoever came closest to earning it rather than
# being dropped.
case("a task that named no proportions splits evenly",
     abs(flat[0]["count"] - flat[1]["count"]) <= 1,
     str([r["count"] for r in flat]))


# ---------------------------------------------------- laying a mosaic again
print("\n-- laying a mosaic out again --")
from astrocontrol.targets import TargetStore                        # noqa: E402

store_ = TargetStore(Path(tempfile.mkdtemp()) / "targets.json")
made = store_.create("Orion", 5.6, -2.0, 0.0, 5.3, 3.5, rows=2, columns=3,
                     align="aligned", collab={"project": "p", "task": "t"})
store_.add_integration(made["id"], "Ha", 600.0, "2026-10-01")
laid = store_.reframe(made["id"], 268.0, 3, 2, "fixed")
case("the grid, angle and alignment move",
     laid["rows"] == 3 and laid["columns"] == 2 and laid["align"] == "fixed"
     and abs(laid["rotation"] - 268.0) < 1e-6 and len(laid["panels"]) == 6)
case("...every panel is at the camera's angle, uncorrected",
     all(abs(p["rotation"] - 268.0) < 1e-6 for p in laid["panels"]))
case("...and the id, the stamp and what was shot stay",
     laid["id"] == made["id"] and laid["collab"]["task"] == "t"
     and laid["integration"]["seconds"] == 600.0)
one = store_.reframe(made["id"], 12.0, 1, 1, "aligned")
case("a single frame is a target with no panels",
     one["type"] == "single" and one["panels"] == [])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
