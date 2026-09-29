"""Exercise how the sequencer walks a plan, against stub devices.

    python tools/check_plan_order.py

The plan is walked in order, each target finished before the next — so what
needs checking is not the walking but the *skipping*: the filter order, the
altitude floor, the Moon limit, the switch and the integration goal each decide
whether frames are taken at all, and each of them failing open costs a night
while each failing closed costs a target.

No test framework, for the same reason as `check_recovery.py`: this runs from a
cold checkout with nothing installed but what Starfront already needs.
"""
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol import plans                                   # noqa: E402
from astrocontrol.config import Config                           # noqa: E402
from astrocontrol.plans import PlanStore                         # noqa: E402
from astrocontrol.sequencer import Sequencer, _flatten           # noqa: E402
from astrocontrol.targets import TargetStore                     # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


# ---------------------------------------------------------------- filter order
alloc = [{"name": "L", "exposure": 300, "count": 3},
         {"name": "R", "exposure": 300, "count": 2}]
grouped = [f["filter"] for f in _flatten(alloc, "grouped")]
rotated = [f["filter"] for f in _flatten(alloc, "rotate")]
case("grouped shoots all of one filter then the next",
     grouped == ["L", "L", "L", "R", "R"], f"{grouped}")
case("rotate deals the filters round",
     rotated == ["L", "R", "L", "R", "L"], f"{rotated}")
case("rotate keeps every frame that was asked for",
     sorted(rotated) == sorted(grouped))


# ------------------------------------------------------------------- the stubs
class Camera:
    def __init__(self):
        self.connected = True
        self.can_cool = False
        self.gain = 100
        self.offset = 10
        self.temperature = None

    def set_settings(self, **kw):
        pass


class Mount:
    def __init__(self):
        self.connected = True
        self.slewing = False
        self.side_of_pier = None
        self.slews = []

    def slew_to(self, ra, dec):
        self.slews.append((round(ra, 3), round(dec, 3)))

    def set_tracking(self, on):
        pass

    def park(self):
        pass

    @property
    def site(self):
        return {"latitude": 31.9, "longitude": -99.1, "elevation": 0}


class Manager:
    def __init__(self, devices):
        self.devices = devices
        self.lines = []

    def get(self, kind):
        return self.devices.get(kind)

    def require(self, kind):
        return self.devices[kind]

    def log(self, message, level="info"):
        self.lines.append(message)


class Capture:
    """Counts frames and makes each one take a fixed, fake amount of time."""

    def __init__(self, clock):
        self.clock = clock
        self.frames = []

    def capture_blocking(self, exposure, frame_type="light"):
        self.clock.advance(exposure)
        record = types.SimpleNamespace(
            id=f"img{len(self.frames)}", exposure=exposure, filename="x.fits",
            path=None, timestamp=self.clock.now())
        self.frames.append(record)
        return record

    def set_output(self, **kw):
        pass

    def set_context(self, **kw):
        pass

    def clear_context(self):
        pass

    def mark_dithered(self):
        pass

    def night_name(self):
        return "2026-09-15"

    def frame(self, image_id):
        raise RuntimeError("no pixels in the stub")

    def abort(self):
        pass

    calibration_context = "light"


class Clock:
    """A fake clock, so a twenty-minute visit takes no wall time to test."""

    def __init__(self):
        self.t = time.time()

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Rig:
    def __init__(self, config, devices, clock):
        self.id = "rig1"
        self.name = "Telescope 1"
        self.role = "master"
        self.config = config
        self.manager = Manager(devices)
        self.capture = Capture(clock)
        self.solver = types.SimpleNamespace(executable=lambda: None)
        self.focuser = types.SimpleNamespace(
            status=lambda: {"running": False}, due=lambda changed=False: "")


class Rigs:
    def __init__(self, rig):
        self.master = rig
        self.all = [rig]

    def imaging(self):
        return self.all

    def get(self, rig_id):
        return self.master

    def connect_remembered(self, rig, kinds=None):
        return {"connected": [], "failed": []}


def build(clock):
    root = Path(tempfile.mkdtemp())
    config = Config(root / "settings.json")
    config.update("site", {"latitude": 31.9, "longitude": -99.1, "useMount": False})
    config.update("schedule", {"minAltitude": 0.0})
    config.update("sequencer", {"autofocusOnStart": False, "settleSeconds": 0.0,
                                "measureFrames": False, "solveEveryFrames": 0,
                                "meridianFlipEnabled": False})
    config.update("guiding", {"startWithSequence": False, "ditherEnabled": False})
    targets = TargetStore(root / "targets.json")
    plan = PlanStore(root / "plan.json")
    rig = Rig(config, {"camera": Camera(), "mount": Mount()}, clock)
    sequencer = Sequencer(Rigs(rig), config, targets, plan)
    # Everything the sequencer times is patched onto the fake clock, so a
    # forty-five minute visit costs the test nothing.
    sequencer._sleep = lambda seconds: clock.advance(seconds)
    return sequencer, rig, config, targets, plan


def add_target(targets, plan, name, ra, dec, filters, options=None):
    target = targets.create(name, ra, dec)
    entry = plan.add(target["id"], target["name"])
    plan.set_filters(entry["id"], filters, 1, 10 ** 9,
                     {"perFrame": 0, "filterChange": 0, "perPanel": 0})
    if options:
        plan.set_options(entry["id"], options)
    return target, entry


# ------------------------------------------- one target at a time, in order
clock = Clock()
seq, rig, config, targets, plan = build(clock)
add_target(targets, plan, "Alpha", 0.5, 40.0, [{"name": "L", "exposure": 60, "count": 10}])
add_target(targets, plan, "Beta", 6.0, 40.0, [{"name": "L", "exposure": 60, "count": 10}])

# The sequencer reads the wall clock in a dozen places; point them all at the
# fake one so a night costs the test no wall time.
import astrocontrol.sequencer as seqmod                          # noqa: E402
real_time = time.time
seqmod.time.time = clock.now
try:
    seq._run()
finally:
    seqmod.time.time = real_time

order = [record.exposure for record in rig.capture.frames]
slews = rig.manager.get("mount").slews
case("every frame the plan asked for was shot exactly once",
     len(order) == 20, f"{len(order)} frames")
case("each target is finished before the next one starts, so one slew each",
     len(slews) == 2, f"{len(slews)} slews for two targets")
case("and the plan is walked in the order it is written",
     [e["name"] for e in plan.raw()["entries"]] == ["Alpha", "Beta"])


# ------------------------------------------------------------- the options
clock = Clock()
seq, rig, config, targets, plan = build(clock)
off_target, off_entry = add_target(
    targets, plan, "Switched off", 0.5, 40.0,
    [{"name": "L", "exposure": 60, "count": 3}], {"enabled": False})
goal_target, goal_entry = add_target(
    targets, plan, "Already done", 6.0, 40.0,
    [{"name": "L", "exposure": 60, "count": 3}], {"goalHours": 1.0})
# Two hours already in the bank against a one-hour goal.
for _ in range(24):
    targets.add_integration(goal_target["id"], "L", 300.0, "2026-09-01")
moon_target, moon_entry = add_target(
    targets, plan, "Behind the Moon", 12.0, 0.0,
    [{"name": "L", "exposure": 60, "count": 3}], {"moonAvoidance": 180.0})
ok_target, ok_entry = add_target(
    targets, plan, "Fine", 18.0, 40.0, [{"name": "L", "exposure": 60, "count": 3}])

seqmod.time.time = clock.now
try:
    seq._run()
finally:
    seqmod.time.time = real_time

case("only the target with nothing against it was shot",
     len(rig.capture.frames) == 3, f"{len(rig.capture.frames)} frames")
reasons = "\n".join(rig.manager.lines)
case("a target switched off says so", "switched off in the plan" in reasons)
case("a target at its goal says so", "goal" in reasons)
case("a target too near the Moon says so", "Moon" in reasons)
case("a plan of nothing but unshootable targets ends rather than hanging",
     True, "the run above returned")

# A target due later in the night is judged at *its* turn, not at dusk —
# otherwise every target that has not risen yet is thrown out at the first one.
clock = Clock()
seq, rig, config, targets, plan = build(clock)
config.update("schedule", {"minAltitude": 30.0})
later_target, later_entry = add_target(
    targets, plan, "Rises later", 6.0, 40.0,
    [{"name": "L", "exposure": 60, "count": 3}])
below_now = dict(later_entry)
case("a target with no start time is judged on where it is now",
     isinstance(seq._why_not_now(below_now, later_target), str))
# Pin it to a moment twelve hours away: a different piece of sky entirely.
later_entry["startAt"] = clock.now() + 12 * 3600
seqmod.time.time = clock.now
try:
    at_turn = seq._why_not_now(later_entry, later_target)
    at_dusk = seq._why_not_now({**later_entry, "startAt": None}, later_target)
finally:
    seqmod.time.time = real_time
case("and its altitude is measured at the time its slot begins",
     at_turn != at_dusk or at_turn == "",
     f"at its turn: {at_turn!r}; now: {at_dusk!r}")

# A per-target floor beats the observatory's. Measured on a circumpolar field,
# so the answer does not depend on what time the test happens to run at: at
# latitude 31.9 a declination of 85 never drops below about 27 degrees.
config.update("schedule", {"minAltitude": 10.0})
pole = {"ra": 2.0, "dec": 85.0}
case("a target's own altitude floor overrides the shared one",
     seq._too_low(pole, 60.0, "Fine", dict(ok_entry, options={"minAltitude": 89.0}))
     is True)
case("without one, the shared floor is what applies",
     seq._too_low(pole, 60.0, "Fine", ok_entry) is False)


# --------------------------------------------- the night log the report reads
clock = Clock()
seq, rig, config, targets, plan = build(clock)
target = targets.create("Reported", 1.0, 40.0)
targets.add_integration(target["id"], "L", 300.0, "2026-09-10", hfr=2.5,
                        guide_rms=0.5, telescope="Telescope 1")
targets.add_integration(target["id"], "L", 300.0, "2026-09-10", hfr=3.5,
                        guide_rms=0.7, guide_lost=True, telescope="Telescope 1")
targets.add_integration(target["id"], "Ha", 600.0, "2026-09-11", hfr=2.0,
                        telescope="Telescope 1")
targets.note_recovery(target["id"], "2026-09-10")

log = targets.get(target["id"])["integration"]["log"]
first, second = log[0], log[1]
case("the log has a row per night, oldest first",
     [r["night"] for r in log] == ["2026-09-10", "2026-09-11"])
case("a night totals its own frames and seconds",
     first["frames"] == 2 and first["seconds"] == 600.0,
     f"{first['frames']} frames, {first['seconds']}s")
case("star size is kept as a sum and a range, not an average",
     first["hfrCount"] == 2 and first["hfrSum"] == 6.0
     and first["hfrMin"] == 2.5 and first["hfrMax"] == 3.5)
case("unguided frames and rescues land on the right night",
     first["guideLost"] == 1 and first["recoveries"] == 1
     and second["guideLost"] == 0)
case("filters are broken down within the night",
     first["byFilter"]["L"] == {"seconds": 600.0, "frames": 2}
     and second["byFilter"]["Ha"]["frames"] == 1)
case("the lifetime total still adds up",
     targets.get(target["id"])["integration"]["seconds"] == 1200.0)

# -------------------------------------------- slot times carried to the new night
#
# "Start Cygnus at half nine" is a time of night, not an instant, and it is as
# true tonight as it was yesterday. These used to be deleted at every rollover,
# which meant setting the same times again every evening.

DAY = 86400.0


def timed_plan(entries):
    store = PlanStore(Path(tempfile.mkdtemp()) / "plan.json")
    store._plan["entries"] = [dict(e) for e in entries]
    return store


# Tonight runs 20:00 to 06:00 in whatever the epoch is; last night is a day back.
tonight_start = 1_800_000_000.0
tonight_end = tonight_start + 10 * 3600.0
last_start = tonight_start - DAY + 5400.0            # 21:30 last night
last_end = tonight_start - DAY + 5 * 3600.0          # 01:00 last night

store = timed_plan([{"id": "a", "startAt": last_start, "endAt": last_end,
                     "timesPinned": True}])
moved = store.roll_times(tonight_start, tonight_end)
entry = store.raw()["entries"][0]
case("last night's times are carried over, not cleared", moved == 2)
case("and they land at the same time of night",
     entry["startAt"] == last_start + DAY and entry["endAt"] == last_end + DAY,
     f"+{(entry['startAt'] - last_start) / 3600:.0f}h")
case("a pinned slot stays pinned across the rollover",
     entry.get("timesPinned") is True)

# A plan left alone for a week comes back to the same time of night.
store = timed_plan([{"id": "a", "startAt": last_start - 6 * DAY,
                     "endAt": last_end - 6 * DAY}])
store.roll_times(tonight_start, tonight_end)
entry = store.raw()["entries"][0]
case("a week-old plan rolls all the way forward",
     entry["startAt"] == last_start + DAY and entry["endAt"] == last_end + DAY)

# Times already inside tonight are left exactly alone.
inside_start = tonight_start + 3600.0
store = timed_plan([{"id": "a", "startAt": inside_start, "endAt": inside_start + 7200.0}])
moved = store.roll_times(tonight_start, tonight_end)
entry = store.raw()["entries"][0]
case("tonight's own times are not touched",
     moved == 0 and entry["startAt"] == inside_start)

# A shorter night: 21:30 now falls before dark, so it is pulled to the edge
# rather than thrown away.
short_start = tonight_start + 3 * 3600.0
store = timed_plan([{"id": "a", "startAt": last_start, "endAt": last_end}])
store.roll_times(short_start, tonight_end)
entry = store.raw()["entries"][0]
case("a time that no longer fits is pulled into the night, not dropped",
     entry["startAt"] == short_start and entry["endAt"] == last_end + DAY,
     f"{entry['startAt'] - short_start:.0f}s past the start")

# Both ends clamped to the same edge would leave a slot of no length.
store = timed_plan([{"id": "a", "startAt": last_start, "endAt": last_start + 60.0}])
store.roll_times(short_start, tonight_end)
entry = store.raw()["entries"][0]
case("a slot never ends before it starts", entry["endAt"] > entry["startAt"])

# An entry with only one of the two set keeps working.
store = timed_plan([{"id": "a", "endAt": last_end}])
moved = store.roll_times(tonight_start, tonight_end)
entry = store.raw()["entries"][0]
case("an end time on its own is carried over too",
     moved == 1 and entry["endAt"] == last_end + DAY and "startAt" not in entry)

# No night worked out yet — nothing is touched rather than guessed at.
store = timed_plan([{"id": "a", "startAt": last_start, "endAt": last_end}])
case("no window means nothing is moved",
     store.roll_times(None, None) == 0
     and store.raw()["entries"][0]["startAt"] == last_start)

# ------------------------------------------------------ what a target does by default

fresh = PlanStore(Path(tempfile.mkdtemp()) / "plan.json")
entry = fresh.add("t-1", "Cygnus Wall")
options = plans.options_for(entry)
case("a new target autofocuses when it starts", options["focusOnStart"] is True)
case("and takes the shared dither interval", options["ditherEveryFrames"] == 0)

# The shared interval is what 0 resolves to, and it is every third frame.
config = Config(Path(tempfile.mkdtemp()) / "settings.json")
case("the shared dither interval is every three frames",
     config.get("guiding", "ditherEveryFrames") == 3)

# Touching one option must not quietly pin all the others at today's defaults —
# otherwise a later change to what the program does by default reaches the
# targets nobody has opened and silently skips the ones they have.
fresh.set_options(entry["id"], {"priority": 8})
stored = fresh.raw()["entries"][0]["options"]
case("only the option that was changed is stored", stored == {"priority": 8},
     f"{stored}")
case("and the rest still read as the defaults",
     plans.options_for(fresh.raw()["entries"][0])["focusOnStart"] is True)

# Setting one back to its default drops it again rather than pinning it.
fresh.set_options(entry["id"], {"priority": 5})
case("setting an option back to the default un-pins it",
     fresh.raw()["entries"][0]["options"] == {})

# An option deliberately set away from the default is kept.
fresh.set_options(entry["id"], {"focusOnStart": False})
case("an option deliberately turned off is kept",
     plans.options_for(fresh.raw()["entries"][0])["focusOnStart"] is False)

# ------------------------------------------- a changed default reaching an old file
#
# A stored value beats a default, which is right — but it means a default
# changed after the first run reaches new installs only. These move the value,
# but only where it is still sitting on the old default.

path = Path(tempfile.mkdtemp()) / "settings.json"
path.write_text(json.dumps({"guiding": {"ditherEveryFrames": 1}}), "utf-8")
migrated = Config(path)
# Two moves in a row - 1 became 2, and 2 became 3 - so a file that was never
# touched lands on today's default however old it is.
case("an old file still on the old default is moved to the new one",
     migrated.get("guiding", "ditherEveryFrames") == 3)
case("and the move is recorded so it happens once",
     "dither-every-two-frames" in migrated.section("meta")["migrations"]
     and "dither-every-three-frames" in migrated.section("meta")["migrations"])

# Run again over the same file: nothing more happens.
again = Config(path)
case("re-opening the file does not migrate a second time",
     again.get("guiding", "ditherEveryFrames") == 3)

# And having been moved, it can be put back and stays back.
again.update("guiding", {"ditherEveryFrames": 1})
case("a value put back by hand stays put",
     Config(path).get("guiding", "ditherEveryFrames") == 1)

# A deliberate choice is never overwritten, because it is not the old default.
path = Path(tempfile.mkdtemp()) / "settings.json"
path.write_text(json.dumps({"guiding": {"ditherEveryFrames": 5}}), "utf-8")
case("a value somebody chose is left alone",
     Config(path).get("guiding", "ditherEveryFrames") == 5)

# ------------------------------------------- re-shooting some panels of a mosaic

fresh = PlanStore(Path(tempfile.mkdtemp()) / "plan.json")
entry = fresh.add("t-2", "Veil mosaic")
case("a mosaic shoots every panel by default",
     plans.options_for(entry)["panels"] == [])

fresh.set_options(entry["id"], {"panels": [3, 1, 3]})
picked = plans.options_for(fresh.raw()["entries"][0])["panels"]
case("picked panels are de-duplicated and sorted", picked == [1, 3], f"{picked}")

# Clearing is an empty list, not a missing key, and it goes back to all panels.
fresh.set_options(entry["id"], {"panels": []})
case("clearing the selection goes back to every panel",
     plans.options_for(fresh.raw()["entries"][0])["panels"] == []
     and fresh.raw()["entries"][0]["options"] == {},
     f"{fresh.raw()['entries'][0]['options']}")

# Nonsense in the list is dropped rather than stored and tripped over later.
fresh.set_options(entry["id"], {"panels": [2, "x", None, 4]})
case("nonsense in the panel list is dropped",
     plans.options_for(fresh.raw()["entries"][0])["panels"] == [2, 4],
     f"{plans.options_for(fresh.raw()['entries'][0])['panels']}")

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
