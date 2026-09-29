"""Which night the program thinks it is, at every hour of the day.

    python tools/check_night_rollover.py

The bug this exists for: the night model was anchored to *today's* noon whatever
the time.  At one in the morning — in the middle of a night, with a sequence
running — everything therefore switched to the *following* night.  Dusk moved
twenty-five hours into the future, every target's rise time went with it, and a
run waiting for its target to rise waited for a rise a day away while the thing
sat overhead.

The boundary between one night and the next is **dawn**, not midnight and not
noon.  Before this morning's dawn you are still in last night.  That is what is
pinned down here, hour by hour around the clock, because the failure only
appears in a few of those hours and the ones it appears in are the ones nobody
is awake to check.

No test framework, for the same reason as the other checks here.
"""

import datetime as dt
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astrocontrol import astro, schedule                           # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


# A real observatory: central Texas, and a target that rises late.
LAT, LON = 31.9, -99.1
HORSEHEAD = (5.0 + 41.0 / 60.0, -2.45)


class FrozenClock:
    """Pretend it is a given local moment, for both `time` and `datetime`."""

    def __init__(self, when: dt.datetime):
        self.when = when.astimezone()

    def __enter__(self):
        stamp = self.when.timestamp()
        self._patches = [
            mock.patch.object(schedule._time, "time", lambda: stamp),
            mock.patch.object(schedule._dt, "datetime", _FrozenDatetime(self.when)),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()
        return False


def _FrozenDatetime(when):
    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return when if tz is None else when.astimezone(tz)
    return Frozen


def night_at(hour, minute=0, day=18):
    with FrozenClock(dt.datetime(2026, 9, day, hour, minute)):
        return schedule.night(LAT, LON)


def rise_at(hour, minute=0, day=18):
    with FrozenClock(dt.datetime(2026, 9, day, hour, minute)):
        info = schedule.night(LAT, LON)
        window = schedule.observable(HORSEHEAD[0], HORSEHEAD[1], LAT, LON,
                                     info, 30.0)
        now = dt.datetime(2026, 9, day, hour, minute).astimezone().timestamp()
        return info, window, now


print("\n-- the night in progress, hour by hour --")

# Evening: the night that started this evening.
case("at nine in the evening it is tonight",
     night_at(21)["date"] == "2026-09-18",
     night_at(21)["date"])

# The small hours: still last night. This is the one that was wrong.
for hour in (0, 1, 2, 3):
    info = night_at(hour)
    case(f"at {hour:02d}:00 it is still the night that started yesterday",
         info["date"] == "2026-09-17", info["date"])

# ...and in those hours the sun really is down, which is the point of it.
for hour in (0, 2):
    info = night_at(hour)
    now = dt.datetime(2026, 9, 18, hour).astimezone().timestamp()
    case(f"at {hour:02d}:00 the window it reports contains now",
         info["duskAstronomical"] <= now <= info["dawnAstronomical"],
         f'dusk {dt.datetime.fromtimestamp(info["duskAstronomical"]):%a %H:%M}'
         f' .. dawn {dt.datetime.fromtimestamp(info["dawnAstronomical"]):%a %H:%M}')

print("\n-- and after dawn, the night to come --")

# Once the sun is up, "tonight" means the one coming, or a morning of planning
# would be planning a night that has already happened.
for hour in (8, 10, 11):
    info = night_at(hour)
    case(f"at {hour:02d}:00 it has moved on to tonight",
         info["date"] == "2026-09-18", info["date"])

morning = night_at(9)
now9 = dt.datetime(2026, 9, 18, 9).astimezone().timestamp()
case("...and that night has not started yet",
     morning["duskAstronomical"] > now9,
     f'dusk {dt.datetime.fromtimestamp(morning["duskAstronomical"]):%a %H:%M}')

print("\n-- what it does to a target's rise time --")

# The symptom: a sequence told to wait for the target to rise, waiting a day.
info, window, now = rise_at(0, 50)
away = (window["rises"] - now) / 3600.0
case("at ten to one the Horsehead rises in about an hour, not tomorrow",
     0 < away < 3, f"{away:+.1f} hours away")
case("...and there is real dark left to shoot it in",
     window["longestMinutes"] > 60, f'{window["longestMinutes"]:.0f} minutes')

# The same instant under the old anchor, to show what was being fixed. Not a
# behaviour to preserve - a record of the size of the mistake.
with FrozenClock(dt.datetime(2026, 9, 18, 0, 50)):
    stale = schedule._night_from(schedule._local_noon(dt.date(2026, 9, 18)),
                                 LAT, LON)
    stale_window = schedule.observable(HORSEHEAD[0], HORSEHEAD[1], LAT, LON,
                                       stale, 30.0)
stale_away = (stale_window["rises"] - now) / 3600.0
case("the old anchor put that rise a day away", stale_away > 20,
     f"{stale_away:+.1f} hours — the bug, for comparison")

print("\n-- a date asked for by name is left alone --")

# The Plan tab can ask for a particular night, and that must not be
# second-guessed by what time it happens to be.
with FrozenClock(dt.datetime(2026, 9, 18, 2, 0)):
    named = schedule.night(LAT, LON, dt.date(2026, 9, 25))
case("asking for a date gets that date", named["date"] == "2026-09-25",
     named["date"])

print("\n-- the altitude floor is a separate thing --")

# Worth stating: "up" and "high enough" are different questions, and the
# program answers the second. A target at 16 degrees really is above the
# horizon and really is below a 30 degree floor.
with FrozenClock(dt.datetime(2026, 9, 18, 0, 50)):
    altitude = astro.altitude_at(HORSEHEAD[0], HORSEHEAD[1], now, LAT, LON)
case("the Horsehead is above the horizon at ten to one", altitude > 0,
     f"{altitude:.1f} degrees")
case("...and below a thirty degree floor, so waiting is right", altitude < 30,
     f"{altitude:.1f} degrees")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
