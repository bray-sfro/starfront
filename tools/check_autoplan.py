"""Exercise what Auto-arrange chooses, and the Moon maths under it.

    python tools/check_autoplan.py

The arranger writes a whole night's allocation from three inputs — the Moon,
what a target already has, and its goal — and the failure mode is not a crash.
It is a plausible-looking plan that spends a full-Moon night on broadband, or
gives a target its fifth night of luminance and no blue. So each rule is checked
for doing the thing it exists to do, with the Moon and the history dialled to
the case that rule is about.

No test framework, for the same reason as the other checks here: this runs from
a cold checkout with nothing installed but what Starfront already needs.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol import autoplan, schedule                     # noqa: E402
from astrocontrol.config import DEFAULTS                        # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


SETTINGS = dict(DEFAULTS["autoplan"])
OVERHEADS = {"perFrame": 15.0, "filterChange": 20.0, "perPanel": 90.0}
LRGB = ["L", "R", "G", "B"]
FULL_SET = ["L", "R", "G", "B", "Ha", "OIII", "SII"]
HOUR = 3600.0


def target(byfilter=None, seconds=None):
    collected = byfilter or {}
    return {"name": "T", "ra": 1.0, "dec": 40.0,
            "integration": {"seconds": seconds if seconds is not None
                            else sum(collected.values()),
                            "frames": 0, "byFilter": dict(collected),
                            "nights": [], "log": []}}


def moon(illumination, up_minutes):
    return {"illumination": illumination, "upMinutes": up_minutes, "curve": []}


def names(result):
    return [f["name"] for f in result["filters"]]


def hours(result):
    return sum(schedule.frame_seconds(f["exposure"], OVERHEADS) * f["count"]
               for f in result["filters"]) / HOUR


# ------------------------------------------------------------------ the Moon
dark = autoplan.conditions(moon(0.05, 0.0), SETTINGS)
bright = autoplan.conditions(moon(0.95, 400.0), SETTINGS)
crescent_up = autoplan.conditions(moon(0.15, 200.0), SETTINGS)

case("a Moon that is down is not a bright night", not dark["bright"])
case("a full Moon that is up is a bright night", bright["bright"])
case("a thin crescent that is up is not", not crescent_up["bright"],
     f"{crescent_up['illumination'] * 100:.0f}% lit")

result = autoplan.choose(target(), 4 * HOUR, FULL_SET, moon(0.95, 400.0),
                         bright, OVERHEADS, SETTINGS)
case("a bright Moon moves a full wheel onto narrowband",
     set(names(result)) == {"Ha", "OIII", "SII"}, f"{names(result)}")

result = autoplan.choose(target(), 4 * HOUR, LRGB, moon(0.95, 400.0),
                         bright, OVERHEADS, SETTINGS)
shortened = {f["name"]: f["exposure"] for f in result["filters"]}
case("a broadband-only wheel shoots shorter subs instead of nothing",
     bool(result["filters"])
     and shortened.get("L", 0) < SETTINGS["luminanceExposure"]
     and shortened.get("R", 0) < SETTINGS["broadbandExposure"],
     f"{shortened}")
case("and says why", any("no narrowband" in note for note in result["notes"]))

result = autoplan.choose(target(), 4 * HOUR, LRGB, moon(0.95, 400.0), bright,
                         OVERHEADS, SETTINGS, moon_separation=120.0)
case("a bright Moon far across the sky does not shorten anything",
     {f["name"]: f["exposure"] for f in result["filters"]}.get("L")
     == SETTINGS["luminanceExposure"],
     f"{[(f['name'], f['exposure']) for f in result['filters']]}")

result = autoplan.choose(target(), 4 * HOUR, FULL_SET, moon(0.05, 0.0),
                         dark, OVERHEADS, SETTINGS)
case("a dark night goes back to broadband",
     set(names(result)) == {"L", "R", "G", "B"}, f"{names(result)}")

result = autoplan.choose(target(), 4 * HOUR, ["Dual"], moon(0.95, 400.0),
                         bright, OVERHEADS, SETTINGS)
case("a dual-band filter counts as narrowband under the Moon",
     names(result) == ["Dual"]
     and result["filters"][0]["exposure"] == SETTINGS["narrowbandExposure"],
     f"{names(result)}")


# -------------------------------------------------- catching the colours up
# Four hours of luminance and nothing else: the colour channels are what a
# night should go to, not more of what is already there.
behind = target({"L": 4 * HOUR})
result = autoplan.choose(behind, 3 * HOUR, LRGB, moon(0.05, 0.0), dark,
                         OVERHEADS, SETTINGS)
allocated = {f["name"]: f["count"] * schedule.frame_seconds(f["exposure"], OVERHEADS)
             for f in result["filters"]}
case("a target with only luminance gets colour",
     allocated.get("R", 0) > 0 and allocated.get("G", 0) > 0
     and allocated.get("B", 0) > 0,
     f"{ {k: round(v / 60) for k, v in allocated.items()} } minutes")
case("and gets far less luminance than colour, because it is ahead",
     allocated.get("L", 0) < min(allocated.get("R", 1), allocated.get("G", 1),
                                allocated.get("B", 1)),
     f"L={round(allocated.get('L', 0) / 60)}m vs "
     f"R={round(allocated.get('R', 0) / 60)}m")
case("and says so", any("behind" in note for note in result["notes"]))

# One channel starved: it should take most of a short night.
starved = target({"L": 4 * HOUR, "R": 2 * HOUR, "G": 2 * HOUR, "B": 0.2 * HOUR})
result = autoplan.choose(starved, 2 * HOUR, LRGB, moon(0.05, 0.0), dark,
                         OVERHEADS, SETTINGS)
allocated = {f["name"]: f["count"] * schedule.frame_seconds(f["exposure"], OVERHEADS)
             for f in result["filters"]}
case("the starved channel takes the largest share of a short night",
     max(allocated, key=allocated.get) == "B",
     f"{ {k: round(v / 60) for k, v in allocated.items()} } minutes")

# Nothing shot at all: fall back to the plain ratio, luminance-heavy.
result = autoplan.choose(target(), 4 * HOUR, LRGB, moon(0.05, 0.0), dark,
                         OVERHEADS, SETTINGS)
allocated = {f["name"]: f["count"] * schedule.frame_seconds(f["exposure"], OVERHEADS)
             for f in result["filters"]}
case("a first night is luminance-heavy, at about twice any one colour",
     1.6 < allocated["L"] / allocated["R"] < 2.4,
     f"L/R = {allocated['L'] / allocated['R']:.2f}")


# ------------------------------------------------------------------ the goal
done = target({"L": 9.5 * HOUR}, seconds=9.5 * HOUR)
result = autoplan.choose(done, 4 * HOUR, LRGB, moon(0.05, 0.0), dark, OVERHEADS,
                         SETTINGS, goal_hours=10.0)
case("a target near its goal is given only what is left of it",
     0.0 < hours(result) <= 0.55, f"{hours(result):.2f}h allocated")
case("and says it was trimmed", any("goal" in note for note in result["notes"]))

finished = target({"L": 12 * HOUR}, seconds=12 * HOUR)
result = autoplan.choose(finished, 4 * HOUR, LRGB, moon(0.05, 0.0), dark,
                         OVERHEADS, SETTINGS, goal_hours=10.0)
case("a target past its goal is given nothing",
     result["filters"] == [], f"{names(result)}")


# --------------------------------------------------------------- small slots
result = autoplan.choose(target(), 14 * 60.0, LRGB, moon(0.05, 0.0), dark,
                         OVERHEADS, SETTINGS)
case("a slot too short to share goes entirely to one filter",
     len(result["filters"]) == 1, f"{names(result)}")
case("and never returns more than the slot holds",
     hours(result) <= 14 / 60.0 + 0.01, f"{hours(result) * 60:.1f} minutes")

result = autoplan.choose(target(), 20.0, LRGB, moon(0.05, 0.0), dark,
                         OVERHEADS, SETTINGS)
case("a slot too short for even one frame returns nothing, with a reason",
     result["filters"] == [] and result["notes"])

result = autoplan.choose(target(), 4 * HOUR, [], moon(0.05, 0.0), dark,
                         OVERHEADS, SETTINGS)
case("a telescope with no filters configured says so rather than guessing",
     result["filters"] == [] and "no filters" in result["notes"][0])


# ------------------------------------------------------ the Moon across a night
site = (31.9, -99.1)
night_info = schedule.night(*site)
track = schedule.moon_track(site[0], site[1], night_info)
case("the Moon track has a curve over the whole night",
     len(track["curve"]) > 20, f"{len(track['curve'])} points")
case("its illumination is a fraction, not a percentage",
     0.0 <= track["illumination"] <= 1.0, f"{track['illumination']}")
case("the Moon moves against the stars across the night",
     track["curve"][0]["ra"] != track["curve"][-1]["ra"],
     f"{track['curve'][0]['ra']}h to {track['curve'][-1]['ra']}h")

# A target the Moon is nowhere near loses none of its window.
far = schedule.dark_overlap(
    [{"start": night_info["duskAstronomical"], "end": night_info["dawnAstronomical"]}],
    track, (track["ra"] + 12.0) % 24.0, -track["dec"], 40.0)
case("a target on the far side of the sky loses no time to the Moon",
     far["spoiledMinutes"] == 0.0 and far["minutes"] > 0,
     f"{far['spoiledMinutes']}m spoiled, closest {far['closest']}°")

# A target sitting on the Moon loses whatever the Moon is up for.
on_top = schedule.dark_overlap(
    [{"start": night_info["duskAstronomical"], "end": night_info["dawnAstronomical"]}],
    track, track["ra"], track["dec"], 40.0)
case("a target the Moon sits on loses time when the Moon is up",
     on_top["spoiledMinutes"] >= 0
     and (on_top["closest"] is None or on_top["closest"] < 40.0),
     f"{on_top['spoiledMinutes']}m spoiled, closest {on_top['closest']}°")


# ------------------------------------------------------ per-filter exposures
tuned = dict(SETTINGS)
tuned["filterExposures"] = {"L": 45.0, "R": 240.0, "Ha": 900.0}
result = autoplan.choose(target(), 4 * HOUR, ["L", "R", "G", "B"],
                         moon(0.05, 0.0), dark, OVERHEADS, tuned)
lengths = {f["name"]: f["exposure"] for f in result["filters"]}
case("the per-filter table decides the sub length",
     lengths.get("L") == 45.0 and lengths.get("R") == 240.0, f"{lengths}")
case("a filter with no entry falls back to its class default",
     lengths.get("G") == tuned["broadbandExposure"], f"G={lengths.get('G')}")

result = autoplan.choose(target(), 4 * HOUR, ["Ha"], moon(0.95, 400.0),
                         bright, OVERHEADS, tuned)
case("the table also decides narrowband",
     result["filters"][0]["exposure"] == 900.0)


# -------------------------------------------------------- filling the slot
# A short window used to lose most of itself to rounding: the colour channels
# could not reach a useful count and were dropped, and nobody spent their time.
short = autoplan.choose(target(), 23 * 60.0, LRGB, moon(0.05, 0.0), dark,
                        OVERHEADS, SETTINGS)
case("a short slot is filled rather than left half empty",
     hours(short) * 3600 > 23 * 60 * 0.8,
     f"{hours(short) * 60:.1f} of 23 minutes used")
case("and never overruns it",
     hours(short) * 3600 <= 23 * 60 + 1, f"{hours(short) * 60:.1f} minutes")

full = autoplan.choose(target(), 4 * HOUR, LRGB, moon(0.05, 0.0), dark,
                       OVERHEADS, SETTINGS)
case("a long slot is filled too",
     hours(full) > 3.9, f"{hours(full):.2f} of 4.00 hours used")


# ---------------------------------------------------- sharing out the night
# The bug this was written for: three targets, and the first one asking for the
# whole night leaves the third with nothing.
DARK = 9 * HOUR
shares = schedule.fair_shares([
    {"id": "a", "capacity": 8 * HOUR, "priority": 5},
    {"id": "b", "capacity": 8 * HOUR, "priority": 5},
    {"id": "c", "capacity": 8 * HOUR, "priority": 5},
], DARK)
case("three equal targets each get a third of the night",
     all(abs(v - DARK / 3) < 60 for v in shares.values()),
     f"{ {k: round(v / 3600, 2) for k, v in shares.items()} } hours")

shares = schedule.fair_shares([
    {"id": "short", "capacity": 0.5 * HOUR, "priority": 5},
    {"id": "long1", "capacity": 9 * HOUR, "priority": 5},
    {"id": "long2", "capacity": 9 * HOUR, "priority": 5},
], DARK)
case("a target only up briefly gets all of its short window",
     abs(shares["short"] - 0.5 * HOUR) < 1,
     f"{shares['short'] / 3600:.2f}h of a 0.5h window")
case("and the time it cannot use goes to the others",
     abs(shares["long1"] + shares["long2"] - 8.5 * HOUR) < 60,
     f"{(shares['long1'] + shares['long2']) / 3600:.2f}h between two")
case("every target gets something",
     all(v > 0 for v in shares.values()))

shares = schedule.fair_shares([
    {"id": "important", "capacity": 9 * HOUR, "priority": 9},
    {"id": "ordinary", "capacity": 9 * HOUR, "priority": 3},
], DARK)
case("priority decides who gets the bigger share",
     shares["important"] > shares["ordinary"] * 2,
     f"{shares['important'] / 3600:.2f}h vs {shares['ordinary'] / 3600:.2f}h")


# ------------------------------------------------------- working around pins
spans = schedule.free_spans(0.0, 100.0, [(30.0, 50.0)])
case("a pinned slot is cut out of the night",
     spans == [(0.0, 30.0), (50.0, 100.0)], f"{spans}")
spans = schedule.free_spans(0.0, 100.0, [(30.0, 50.0), (60.0, 70.0)])
case("two pins leave three gaps",
     spans == [(0.0, 30.0), (50.0, 60.0), (70.0, 100.0)], f"{spans}")
spans = schedule.free_spans(0.0, 100.0, [(0.0, 100.0)])
case("a pin over the whole night leaves nothing", spans == [], f"{spans}")

# Real timestamps rather than zero: a dusk of 0.0 is falsy and takes the
# "no dark tonight" path, which would make this check pass without testing it.
T = 1_789_500_000.0
fake_night = {"duskAstronomical": T, "dawnAstronomical": T + 4 * HOUR}
pin = (T + 1 * HOUR, T + 2 * HOUR)
placed = schedule.arrange(
    [{"id": "early", "seconds": 4 * HOUR,
      "intervals": [{"start": T, "end": T + 4 * HOUR}]},
     {"id": "late", "seconds": 1 * HOUR,
      "intervals": [{"start": T, "end": T + 4 * HOUR}]}],
    fake_night, reserved=[pin])
slots = [(s["id"], s["start"], s["end"]) for s in placed["order"]]
case("the arranger places targets around a pinned slot",
     len(slots) >= 1, f"{[(i, round((a - T) / HOUR, 2), round((b - T) / HOUR, 2)) for i, a, b in slots]}")
case("and never schedules over it",
     all(not (a < pin[1] and b > pin[0]) for _i, a, b in slots),
     f"{[(i, round((a - T) / HOUR, 2), round((b - T) / HOUR, 2)) for i, a, b in slots]}")


# ------------------------------------------------------- not wasting the dark
# The bug this was written for: a target whose window closes before it can spend
# its fair share leaves that time behind, and the night ends with hours of dark
# and something sitting at sixty degrees doing nothing.
T = 1_789_500_000.0
NIGHT = {"duskAstronomical": T, "dawnAstronomical": T + 9 * HOUR}

placed = schedule.arrange([
    # Sets three hours in, but was owed three of the nine hours.
    {"id": "early", "seconds": 3 * HOUR,
     "intervals": [{"start": T, "end": T + 2 * HOUR}]},
    # Up all night.
    {"id": "allnight", "seconds": 3 * HOUR,
     "intervals": [{"start": T, "end": T + 9 * HOUR}]},
], NIGHT)
slots = {s["id"]: (s["start"] - T, s["end"] - T) for s in placed["order"]}
case("a target that outlives the others is run on to fill the night",
     abs(slots["allnight"][1] - 9 * HOUR) < 60,
     f"allnight runs to {slots['allnight'][1] / HOUR:.2f}h of a 9h night")
case("and the idle figure says so",
     placed["idleSeconds"] < 60,
     f"{placed['idleSeconds'] / 60:.1f} minutes idle")
case("the gap-filling pass reports what it rescued",
     placed["filledSeconds"] > 3 * HOUR,
     f"{placed['filledSeconds'] / HOUR:.2f}h filled")

# It must not run a target on past its own window, though.
placed = schedule.arrange([
    {"id": "early", "seconds": 2 * HOUR,
     "intervals": [{"start": T, "end": T + 2 * HOUR}]},
    {"id": "alsoearly", "seconds": 2 * HOUR,
     "intervals": [{"start": T, "end": T + 4 * HOUR}]},
], NIGHT)
for slot in placed["order"]:
    limit = T + (2 if slot["id"] == "early" else 4) * HOUR
    case(f"{slot['id']} is not run on past its own window",
         slot["end"] <= limit + 1,
         f"ends {(slot['end'] - T) / HOUR:.2f}h, window closes "
         f"{(limit - T) / HOUR:.2f}h")
case("dark nothing can use is still reported as idle",
     placed["idleSeconds"] > 4 * HOUR,
     f"{placed['idleSeconds'] / HOUR:.2f}h with nothing up")

# A target that never got a slot at all should take a big enough gap.
placed = schedule.arrange([
    {"id": "hog", "seconds": 9 * HOUR,
     "intervals": [{"start": T, "end": T + 3 * HOUR}]},
    {"id": "late", "seconds": 1 * HOUR, "priority": 9,
     "intervals": [{"start": T + 4 * HOUR, "end": T + 9 * HOUR}]},
], NIGHT)
case("a target left out is given a gap it can actually use",
     "late" in {s["id"] for s in placed["order"]},
     f"{[s['id'] for s in placed['order']]}")

# A target that dips below the floor and comes back must not be run through
# the hole in the middle.
placed = schedule.arrange([
    {"id": "split", "seconds": 9 * HOUR,
     "intervals": [{"start": T, "end": T + 2 * HOUR},
                   {"start": T + 6 * HOUR, "end": T + 9 * HOUR}]},
], NIGHT)
case("a target with a hole in its window is not run through it",
     all(s["end"] <= T + 2 * HOUR + 1
         or s["start"] >= T + 6 * HOUR - 1 for s in placed["order"]),
     f"{[((s['start'] - T) / HOUR, (s['end'] - T) / HOUR) for s in placed['order']]}")

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
