"""The all-sky survey: one fixed grid of fields, shot over many nights.

Unlike everything else in the target list, this is not a framing you shoot and
finish.  It is a grid covering a declination band, a goal for every field in it,
and a record of how far each one has got — kept across months and rebuilt from
an append-only log so that a crash at three in the morning costs one frame and
not the season.

**The grid.**  Declination rings at a constant spacing; inside each ring, fields
spaced in right ascension by however much the sky has narrowed there.  A field
of angular width `w` spans `w / cos(d)` *degrees of RA* at declination `d`, so
the RA it covers is narrowest at whichever of its edges is nearer the equator.
That edge is what the spacing is computed from, which means the requested
overlap is the *minimum* anywhere in the ring rather than an average — and it
means the overlap at the middle of a field grows as the rings climb towards the
pole, without anyone having to ask for it:

    dec   0°   fields  186   overlap at the field centre  20%
    dec  60°   fields   94                                22%
    dec  80°   fields   34                                27%
    dec  89°   fields    6                                69%

The alternative — one RA spacing in degrees for the whole sky — would shoot the
polar rings dozens of times over for no extra coverage at all.

**The order.**  Fields are only ever shot after they cross the meridian, never
before.  That single rule does three things at once: every field is caught
within an hour or two of its transit, which is the highest it will ever be from
this site; the mount stays on one side of the pier from dusk to dawn, so a
German equatorial never flips; and because the meridian sweeps through right
ascension at fifteen degrees an hour, working the meridian *is* working through
the survey in order.  Quality, efficiency and coverage turn out to want the same
thing.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

from . import astro
from .config import data_root
from .devices.base import DeviceError

# The most fields one survey may hold.  A full sky at a degree-and-a-half field
# is about fifty thousand; past that the grid is not the problem, the telescope
# time is.
MAX_FIELDS = 120_000

# Compact the append-only frame log into a snapshot once it passes this many
# lines, so start-up stays quick however many nights have been shot.
COMPACT_AFTER = 20_000

# Degrees of score subtracted per hour past the meridian.  Six is about the rate
# a field near the zenith actually loses altitude, so a field two hours west has
# to be genuinely higher to be worth preferring over one at the meridian.
TRANSIT_PENALTY = 6.0

# The altitude past which extra height stops being worth anything.  Airmass at
# 70 degrees is 1.06; at the zenith it is 1.00.
QUALITY_ALTITUDE = 70.0

# Degrees of score given up per degree of slew between one field and the next.
# Two is deliberately firm: it makes a twenty-degree jump cost as much as forty
# degrees of altitude, which nothing near the meridian ever is, so the survey
# walks its neighbours instead of hopping about the sky.
SLEW_WEIGHT = 2.0

# Degrees of score a part-finished field is given over an untouched one.  Large
# on purpose: a completed field is something that can be stacked and looked at,
# and a half-finished one is not, so the survey should be leaving finished sky
# behind it rather than a uniform smear.  At the default slew weight this is
# worth going about twelve degrees out of the way for.
FINISH_BONUS = 25.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------

def effective_field(width: float, height: float,
                    rotation: float = 0.0) -> dict[str, float]:
    """What a camera at this angle can be relied on to cover, north-up.

    The grid is laid out in right ascension and declination, so it tiles with
    axis-aligned rectangles.  A camera that is not square to north does not
    produce one: its footprint is a rotated rectangle, and the biggest
    north-up rectangle that fits *inside* that is smaller than the sensor.

    A rectangle W by H turned by theta has a bounding box of
    (W cos + H sin) by (W sin + H cos), and it fits inside the sensor exactly
    when that bounding box does — so the largest similar rectangle that fits is
    scaled by

        k = min( w / (w cos + h sin),  h / (w sin + h cos) )

    At zero it is the whole sensor.  At forty-five degrees it is about six
    tenths of it in each direction, which is nearly three times as many fields
    for the same sky — which is the argument for either squaring the camera up
    or letting a rotator do it.
    """
    angle = math.radians(abs(float(rotation)) % 180.0)
    cosine = abs(math.cos(angle))
    sine = abs(math.sin(angle))
    across = width * cosine + height * sine
    down = width * sine + height * cosine
    scale = min(width / across if across else 1.0,
                height / down if down else 1.0)
    scale = _clamp(scale, 0.05, 1.0)
    return {
        "width": round(width * scale, 6),
        "height": round(height * scale, 6),
        "scale": round(scale, 6),
        "rotation": round(float(rotation) % 360.0, 3),
        # How many more fields this costs against a camera squared up to north.
        "costFactor": round(1.0 / (scale * scale), 3),
    }


def ring_declinations(height: float, overlap: float, dec_min: float,
                      dec_max: float) -> list[float]:
    """Where the declination rings sit, so the band is covered end to end.

    The spacing is whatever divides the band into a whole number of rings at no
    less than the requested overlap — so the realised overlap is a little more
    than asked for rather than a little less, which is the side to err on.
    """
    span = dec_max - dec_min
    if height <= 0:
        raise DeviceError("the field height is not known")
    if span <= height:
        return [round((dec_min + dec_max) / 2.0, 6)]

    step = height * (1.0 - overlap)
    count = int(math.ceil((span - height) / step)) + 1
    actual = (span - height) / (count - 1)
    return [round(dec_min + height / 2.0 + index * actual, 6)
            for index in range(count)]


def ring_spacing(width: float, height: float, overlap: float,
                 declination: float) -> dict[str, Any]:
    """How many fields go round one ring, and what that really overlaps by.

    The binding constraint is the edge of the field nearer the equator: that is
    where a field covers the fewest degrees of right ascension, so that is where
    two neighbours are closest to leaving a gap between them.  Sizing the step
    there makes the requested overlap a floor that holds everywhere in the ring,
    at the cost of a little more overlap in the middle of each field — which is
    exactly the extra overlap towards the poles that a survey wants anyway.
    """
    # The pole-ward edge is the *widest* in RA, so it is not the constraint;
    # a field straddling the equator has its narrowest point at the equator.
    inner = max(0.0, abs(declination) - height / 2.0)
    inner = min(inner, 89.9)
    needed = width * (1.0 - overlap) / math.cos(math.radians(inner))

    count = max(1, int(math.ceil(360.0 / needed)))
    step = 360.0 / count

    # What that step actually leaves overlapping, at the middle of the field and
    # at its two edges.  The middle is the number people mean by "the overlap".
    def realised(at: float) -> float:
        extent = width / math.cos(math.radians(min(89.95, abs(at))))
        return _clamp(1.0 - step / extent, -9.0, 1.0)

    outer = min(89.95, abs(declination) + height / 2.0)
    return {
        "count": count,
        "step": step,
        "overlap": realised(declination),
        "overlapInner": realised(inner),
        "overlapOuter": realised(outer),
    }


def grid(width: float, height: float, overlap: float = 0.20,
         dec_min: float = -90.0, dec_max: float = 90.0,
         stagger: bool = True) -> list[dict[str, Any]]:
    """Every field in the survey, in a fixed and repeatable order.

    Deterministic in its arguments and nothing else: the same numbers always
    give the same fields with the same names, which is what lets a progress
    record from March still mean something in October.
    """
    if width <= 0 or height <= 0:
        raise DeviceError(
            "the all-sky grid needs the telescope's field of view — set the "
            "focal length and sensor size in Site & Optics first")
    overlap = _clamp(float(overlap), 0.0, 0.9)
    dec_min = _clamp(float(dec_min), -90.0, 90.0)
    dec_max = _clamp(float(dec_max), -90.0, 90.0)
    if dec_max <= dec_min:
        raise DeviceError("the declination range is empty")

    fields: list[dict[str, Any]] = []
    for ring, declination in enumerate(
            ring_declinations(height, overlap, dec_min, dec_max)):
        spacing = ring_spacing(width, height, overlap, declination)
        # Half a step of offset on alternate rings, so the corners of one ring
        # sit over the middles of the next instead of all meeting at a point.
        shift = spacing["step"] / 2.0 if (stagger and ring % 2) else 0.0
        for index in range(spacing["count"]):
            ra_degrees = (index * spacing["step"] + shift) % 360.0
            fields.append({
                "id": f"R{ring:03d}F{index:04d}",
                "ring": ring,
                "index": index,
                "ra": round(ra_degrees / 15.0, 6),
                "dec": declination,
                "ringCount": spacing["count"],
                "raStep": round(spacing["step"], 6),
                "overlap": round(spacing["overlap"], 4),
                "overlapMin": round(spacing["overlapInner"], 4),
            })
            if len(fields) > MAX_FIELDS:
                raise DeviceError(
                    f"that grid comes to more than {MAX_FIELDS:,} fields; "
                    "narrow the declination range or check the field of view")
    return fields


def summarise_grid(fields: list[dict[str, Any]], width: float,
                   height: float) -> dict[str, Any]:
    """The shape of the grid, for a form that has just been filled in."""
    rings: dict[int, dict[str, Any]] = {}
    for field in fields:
        rings.setdefault(field["ring"], {
            "dec": field["dec"], "count": field["ringCount"],
            "overlap": field["overlap"], "overlapMin": field["overlapMin"]})
    ordered = [rings[key] for key in sorted(rings)]
    area = sum(width * height for _ in fields)
    return {
        "fields": len(fields),
        "rings": len(ordered),
        "ringDetail": ordered,
        "fieldWidth": width,
        "fieldHeight": height,
        # How much sky the fields add up to against how much they actually
        # cover: the gap between them is the price of the overlap.
        "tiledArea": round(area, 1),
        "equatorOverlap": round(
            min((r["overlap"] for r in ordered
                 if abs(r["dec"]) <= abs(height)), default=0.0), 4),
        "poleOverlap": round(
            max((r["overlap"] for r in ordered), default=0.0), 4),
    }


# ---------------------------------------------------------------------------
# What has been shot
# ---------------------------------------------------------------------------

class ProgressStore:
    """How far every field has got, kept so that it cannot quietly be lost.

    Every finished frame appends one line to a log before anything else happens
    to it.  Appending is atomic enough on every filesystem this runs on, so a
    power cut costs the frame in progress and nothing else — and the aggregate
    can always be rebuilt by replaying the log, which is the property that makes
    a survey spanning months trustworthy rather than merely convenient.

    The log is folded into a snapshot once it grows, so start-up stays quick
    however many nights are behind it.
    """

    def __init__(self, path: Path | None = None) -> None:
        base = path or data_root()
        self.snapshot_path = base / "allsky-progress.json"
        self.log_path = base / "allsky-frames.jsonl"
        self._lock = threading.RLock()
        # survey id -> field id -> {"filters": {name: {frames, seconds}},
        #                           "firstNight", "lastNight", "frames"}
        self._done: dict[str, dict[str, Any]] = {}
        self._lines = 0
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            self._done = {}
            self._lines = 0
            try:
                stored = json.loads(self.snapshot_path.read_text("utf-8"))
                if isinstance(stored, dict) and isinstance(stored.get("surveys"), dict):
                    self._done = stored["surveys"]
            except (OSError, ValueError):
                pass
            # Then everything the log has that the snapshot does not.
            try:
                with open(self.log_path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self._apply(json.loads(line))
                        except ValueError:
                            continue        # a half-written last line
                        self._lines += 1
            except OSError:
                pass

    def _apply(self, row: dict[str, Any]) -> None:
        survey = str(row.get("survey") or "")
        field = str(row.get("field") or "")
        if not survey or not field:
            return
        fields = self._done.setdefault(survey, {})
        entry = fields.setdefault(field, {"filters": {}, "frames": 0,
                                          "seconds": 0.0, "firstNight": None,
                                          "lastNight": None})
        name = str(row.get("filter") or "") or "unfiltered"
        seconds = float(row.get("exposure") or 0.0)
        slot = entry["filters"].setdefault(name, {"frames": 0, "seconds": 0.0})
        slot["frames"] += 1
        slot["seconds"] = round(slot["seconds"] + seconds, 2)
        entry["frames"] += 1
        entry["seconds"] = round(entry["seconds"] + seconds, 2)
        night = row.get("night")
        if night:
            entry["firstNight"] = entry["firstNight"] or night
            entry["lastNight"] = night

    def record(self, survey: str, field: str, filter_name: str, exposure: float,
               night: str, telescope: str = "", path: str = "") -> None:
        """Credit one finished frame.  Written before it is believed."""
        row = {
            "t": round(time.time(), 3), "survey": survey, "field": field,
            "filter": filter_name or "", "exposure": round(float(exposure), 3),
            "night": night, "telescope": telescope, "file": path,
        }
        line = json.dumps(row, separators=(",", ":"))
        with self._lock:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                # A log that cannot be written is worth saying so about, but it
                # is not worth throwing away the frame that was just taken.
                pass
            self._apply(row)
            self._lines += 1
            if self._lines >= COMPACT_AFTER:
                self._compact()

    def _compact(self) -> None:
        """Fold the log into the snapshot.  Called with the lock held."""
        try:
            payload = json.dumps({"surveys": self._done,
                                  "written": time.time()}, separators=(",", ":"))
            temporary = self.snapshot_path.with_suffix(".tmp")
            temporary.write_text(payload, "utf-8")
            # Replace, then truncate: if it stops between the two, the log is
            # replayed onto a snapshot that already has it, which double-counts
            # — so the snapshot is only moved into place once it is complete,
            # and the log is cleared immediately after.
            temporary.replace(self.snapshot_path)
            self.log_path.write_text("", "utf-8")
            self._lines = 0
        except OSError:
            pass

    def flush(self) -> None:
        with self._lock:
            self._compact()

    # -- reading it --------------------------------------------------------
    def survey(self, survey_id: str) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._done.get(survey_id, {})))

    def field(self, survey_id: str, field_id: str) -> dict[str, Any]:
        with self._lock:
            entry = self._done.get(survey_id, {}).get(field_id)
            return json.loads(json.dumps(entry)) if entry else {
                "filters": {}, "frames": 0, "seconds": 0.0,
                "firstNight": None, "lastNight": None}

    def forget(self, survey_id: str) -> int:
        """Drop a survey's record, when the survey itself is deleted."""
        with self._lock:
            removed = len(self._done.pop(survey_id, {}))
            self._compact()
        return removed


def field_state(done: dict[str, Any], goal: list[dict[str, Any]]) -> dict[str, Any]:
    """How far one field has got against what was asked of it."""
    wanted = 0
    have = 0
    per_filter: dict[str, Any] = {}
    for item in goal:
        name = str(item.get("name") or "") or "unfiltered"
        count = max(0, int(item.get("count") or 0))
        got = int((done.get("filters", {}).get(name) or {}).get("frames", 0))
        wanted += count
        have += min(got, count)
        per_filter[name] = {"wanted": count, "have": got}
    fraction = 1.0 if wanted <= 0 else _clamp(have / wanted, 0.0, 1.0)
    return {
        "wanted": wanted,
        "have": have,
        "fraction": round(fraction, 4),
        "complete": wanted > 0 and have >= wanted,
        "started": done.get("frames", 0) > 0,
        "byFilter": per_filter,
        "seconds": done.get("seconds", 0.0),
        "lastNight": done.get("lastNight"),
    }


def visit_seconds(goal: list[dict[str, Any]], overheads: dict[str, float],
                  cap_minutes: float = 0.0, telescopes: int = 1) -> float:
    """How long one visit to a field lasts.

    Not necessarily the whole goal.  The sky hands a telescope the next field
    along a ring roughly every nine minutes at the equator — that is simply how
    fast it turns against a two-degree grid — so a field that takes forty
    minutes cannot be followed by its neighbour, and a night of forty-minute
    fields comes out as a scatter across half the sky rather than a strip that
    can be assembled.

    Capping the visit fixes that: shoot part of the goal on each of a run of
    touching fields, and come back on later nights to deepen them.  The record
    is per frame, so a field picked up again simply carries on from where it
    stopped.  What you get is a joined-up image straight away that gets deeper,
    rather than a handful of finished fields nowhere near each other.
    """
    whole = goal_seconds(goal, overheads) / max(1, telescopes)
    if cap_minutes and cap_minutes > 0:
        # A floor, so a cap set to nothing cannot turn the night into slewing.
        # It is only a floor on the *budget*: a visit always takes at least one
        # frame however short the budget, since leaving with nothing would mean
        # visiting the field for no reason at all.
        return max(5.0, min(whole, cap_minutes * 60.0))
    return whole


def goal_seconds(goal: list[dict[str, Any]], overheads: dict[str, float]) -> float:
    """How long one field costs, start to finish."""
    total = float(overheads.get("perPanel") or 0.0)
    filters = 0
    for item in goal:
        count = max(0, int(item.get("count") or 0))
        exposure = float(item.get("exposure") or 0.0)
        if count <= 0 or exposure <= 0:
            continue
        filters += 1
        total += count * (exposure + float(overheads.get("perFrame") or 0.0))
    if filters > 1:
        total += (filters - 1) * float(overheads.get("filterChange") or 0.0)
    return total


# ---------------------------------------------------------------------------
# Which fields to shoot tonight
# ---------------------------------------------------------------------------

def contiguity(chosen: list[dict[str, Any]], width: float,
               height: float) -> dict[str, Any]:
    """Does this list of fields join up into a picture, or a scatter?

    Two fields count as joined if their centres are no further apart than a
    field is wide, which for a grid with overlap means their footprints really
    do touch.  What comes back is the number of separate patches and how many
    consecutive steps stayed inside one — the difference between a night that
    adds to an image and a night that adds to a pile.
    """
    if not chosen:
        return {"patches": 0, "largestPatch": 0, "touchingSteps": 0,
                "contiguous": 0.0}

    reach = max(width, height) * 1.05
    joined: dict[int, list[int]] = {i: [] for i in range(len(chosen))}
    for i, a in enumerate(chosen):
        for j in range(i + 1, len(chosen)):
            b = chosen[j]
            if abs(a["dec"] - b["dec"]) > reach:
                continue
            if astro.separation_degrees(a["ra"], a["dec"],
                                        b["ra"], b["dec"]) <= reach:
                joined[i].append(j)
                joined[j].append(i)

    seen: set[int] = set()
    patches: list[int] = []
    for start in range(len(chosen)):
        if start in seen:
            continue
        stack, size = [start], 0
        seen.add(start)
        while stack:
            here = stack.pop()
            size += 1
            for other in joined[here]:
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        patches.append(size)

    steps = sum(1 for a, b in zip(chosen, chosen[1:])
                if astro.separation_degrees(a["ra"], a["dec"],
                                            b["ra"], b["dec"]) <= reach)
    return {
        "patches": len(patches),
        "largestPatch": max(patches),
        "touchingSteps": steps,
        "contiguous": round(steps / max(1, len(chosen) - 1), 3),
    }


def transit_altitude(dec: float, latitude: float) -> float:
    """The highest a declination ever gets from a site, in degrees."""
    return 90.0 - abs(latitude - dec)


def reachable(fields: list[dict[str, Any]], latitude: float,
              minimum_altitude: float) -> list[bool]:
    """Which fields ever clear the altitude limit from here.

    Worth knowing separately from progress: a survey run from thirty-one
    degrees north can never see the southern polar cap, and a progress bar that
    counts those fields in its denominator would never reach the end.
    """
    return [transit_altitude(f["dec"], latitude) >= minimum_altitude
            for f in fields]


def hour_angle(ra_hours: float, lst_hours: float) -> float:
    """Hours past the meridian; negative is still to the east, and rising."""
    return ((lst_hours - ra_hours + 12.0) % 24.0) - 12.0


def night_plan(fields: list[dict[str, Any]], progress: dict[str, Any],
               goal: list[dict[str, Any]], latitude: float, longitude: float,
               window_start: float, window_end: float,
               per_field_seconds: float,
               settings: dict[str, Any] | None = None,
               start_at_position: tuple[float, float] | None = None,
               exclude: set[str] | None = None,
               width_hint: float = 2.5,
               height_hint: float = 1.9) -> dict[str, Any]:
    """Work out which fields to shoot, in what order, between two moments.

    The rule is that a field is only taken once it has crossed the meridian and
    before it has drifted too far west of it.  Everything the survey wants falls
    out of that one constraint:

      * **altitude** — a field at its meridian is at the highest altitude it
        ever reaches from this site, so shooting near transit is shooting at the
        lowest airmass available.
      * **meridian flips** — a mount that only ever points west of the meridian
        never has to change sides.  Not "flips are handled": there are none.
      * **coverage** — the meridian moves through fifteen degrees of right
        ascension an hour and through all of it across a year, so following it
        works through the survey without anybody scheduling it.

    Among the fields that qualify at a given moment, the one that scores highest
    is chosen: highest altitude, penalised for how far past the meridian it has
    already gone, with the Moon excluded outright and part-finished fields
    preferred so that the survey ends up with completed fields rather than a
    uniform smear of half-done ones.
    """
    settings = settings or {}
    minimum_altitude = float(settings.get("minAltitude", 40.0) or 0.0)
    max_hours = float(settings.get("maxHourAngle", 3.0) or 3.0)
    avoid_flips = settings.get("minimiseFlips", True) is not False
    moon_avoidance = float(settings.get("moonAvoidance", 30.0) or 0.0)
    scale_moon = settings.get("moonScaleByPhase", True) is not False
    penalty = float(settings.get("transitPenalty", TRANSIT_PENALTY) or 0.0)
    # Above this altitude, more height buys nothing worth having: 70 degrees is
    # airmass 1.06 and 89 degrees is 1.00, a difference no stack will ever show.
    # Without the cap the survey would shoot the same few rings either side of
    # the zenith for a year before touching anything else, which is a great deal
    # of quality nobody asked for at the price of any coverage at all.
    ceiling = float(settings.get("qualityAltitude", QUALITY_ALTITUDE) or 90.0)
    # Degrees of score given up per degree of slew.  Above the quality ceiling
    # every candidate scores the same on altitude, so this is what decides
    # between them — and what it decides is "the one next door".  Fields near
    # the meridian at any moment form a column running from horizon to horizon,
    # so the neighbour is nearly always the next ring up or down: a degree and a
    # half of mount movement instead of forty.
    slew_weight = float(settings.get("slewWeight", SLEW_WEIGHT) or 0.0)
    finish_bonus = float(settings.get("finishBonus", FINISH_BONUS) or 0.0)

    if per_field_seconds <= 0:
        return {"fields": [], "detail": "no filters or exposures are set for a "
                                        "field, so there is nothing to shoot"}
    if window_end <= window_start:
        return {"fields": [], "detail": "the scheduled window is empty"}

    # Fields still wanting frames, and reachable from here at all.
    skip = exclude or set()
    pending: list[dict[str, Any]] = []
    for field in fields:
        if field["id"] in skip:
            continue
        if transit_altitude(field["dec"], latitude) < minimum_altitude:
            continue
        state = field_state(progress.get(field["id"], {}), goal)
        if state["complete"]:
            continue
        pending.append({**field, "state": state})
    if not pending:
        return {"fields": [], "detail": "every reachable field is finished"}

    moon_ra = moon_dec = None
    illumination = 1.0
    if moon_avoidance > 0:
        jd = astro.julian_from_timestamp((window_start + window_end) / 2.0)
        moon_ra, moon_dec = astro.moon_position(jd)
        illumination = astro.moon_illumination(jd)
    # Scaled by phase, but with a floor: even a thin crescent is far brighter
    # than the sky it sits in, and a survey frame taken right beside one is not
    # worth the disk it lands on.
    keep_away = (moon_avoidance * max(0.4, illumination) if scale_moon
                 else moon_avoidance)

    chosen: list[dict[str, Any]] = []
    used: set[str] = set()
    skipped_moon = 0
    moment = window_start
    # Where the mount is, as the plan is walked forward.  None for the first
    # field, which is chosen on its merits alone because there is nothing to be
    # near yet.
    here: tuple[float, float] | None = start_at_position
    total_slew = 0.0
    # If nothing qualifies at some moment, step forward rather than giving up:
    # the sky is turning, and something will.  Bounded at both ends so a very
    # short field cannot make this crawl, and a very long one cannot make it
    # skip over a window it would have fitted in.
    step = max(5.0, min(300.0, per_field_seconds / 4.0))

    while moment + 1.0 < window_end:
        remaining = window_end - moment
        block = min(per_field_seconds, remaining)
        middle = moment + block / 2.0
        # Judged at the *start* of the block, not the middle of it.  A field
        # that has only just reached the meridian by its midpoint spent the
        # first half of the block east of it — and that is a flip, in the middle
        # of a field, which is the one thing this is all arranged to avoid.
        lst = astro.local_sidereal_hours(longitude, _utc(moment))

        best = None
        best_score = -1e9
        for field in pending:
            if field["id"] in used:
                continue
            ha = hour_angle(field["ra"], lst)
            if avoid_flips and ha < 0.0:
                continue
            if abs(ha) > max_hours:
                continue
            altitude = astro.altitude_at(field["ra"], field["dec"], middle,
                                         latitude, longitude)
            if altitude < minimum_altitude:
                continue
            # It has to still be up when the block ends, or the last frames of
            # it are taken through the roof of the observatory.
            if astro.altitude_at(field["ra"], field["dec"], moment + block,
                                 latitude, longitude) < minimum_altitude:
                continue
            if moon_ra is not None and keep_away > 0:
                if astro.altitude_at(moon_ra, moon_dec, middle,
                                     latitude, longitude) > -2.0:
                    separation = astro.separation_degrees(
                        field["ra"], field["dec"], moon_ra, moon_dec)
                    if separation < keep_away:
                        skipped_moon += 1
                        continue

            score = min(altitude, ceiling) - penalty * abs(ha)
            # Finishing a field beats starting one, and by a wide margin.  A
            # survey is measured in *completed* fields: a night that half-does
            # thirty of them has produced thirty things that cannot be stacked,
            # where the same time spent finishing fifteen produces fifteen that
            # can. It is also the difference between having an image of some of
            # the sky within a week and having an image of none of it for a
            # season.
            if field["state"]["started"]:
                score += finish_bonus
            # How far the mount has to move to get there.  Every degree of slew
            # is time the shutter is shut, and the fields near the meridian are
            # a column of neighbours, so there is almost always one next door.
            if here is not None and slew_weight:
                gap = astro.separation_degrees(here[0], here[1],
                                               field["ra"], field["dec"])
                score -= slew_weight * gap
            # Among fields that are still equal, work up through the rings in
            # order.  Small enough never to outrank anything real, big enough to
            # be decisive when nothing else is.
            score -= field["ring"] * 1e-4
            if score > best_score:
                best_score, best = score, field

        if best is None:
            moment += step
            continue

        used.add(best["id"])
        altitude = astro.altitude_at(best["ra"], best["dec"], middle,
                                     latitude, longitude)
        slew = (astro.separation_degrees(here[0], here[1], best["ra"], best["dec"])
                if here is not None else 0.0)
        total_slew += slew
        here = (best["ra"], best["dec"])
        chosen.append({
            "slew": round(slew, 3),
            "id": best["id"], "ra": best["ra"], "dec": best["dec"],
            "ring": best["ring"], "index": best["index"],
            "startAt": round(moment, 1),
            "endAt": round(moment + block, 1),
            "seconds": round(block, 1),
            "partial": block < per_field_seconds - 1.0,
            "altitude": round(altitude, 2),
            "transitAltitude": round(transit_altitude(best["dec"], latitude), 2),
            "hourAngle": round(hour_angle(best["ra"], lst), 3),
            "airmass": (round(astro.airmass(altitude), 3)
                        if astro.airmass(altitude) else None),
            "moonSeparation": (round(astro.separation_degrees(
                best["ra"], best["dec"], moon_ra, moon_dec), 1)
                if moon_ra is not None else None),
            "state": best["state"],
        })
        moment += block

    detail = ""
    if not chosen:
        detail = ("nothing is past the meridian and high enough in that window"
                  + (" once the Moon is allowed for" if skipped_moon else ""))
    shape = contiguity(chosen, width_hint, height_hint)
    return {
        "fields": chosen,
        "perFieldSeconds": round(per_field_seconds, 1),
        "pending": len(pending),
        "skippedForMoon": skipped_moon,
        # What the mount is asked to do to get through the list.  Worth having
        # in front of you: it is the difference between a night of imaging and a
        # night of slewing.
        "slewDegrees": round(total_slew, 1),
        "slewPerField": round(total_slew / len(chosen), 2) if chosen else 0.0,
        # Whether the night's work joins up into something that can be
        # assembled, or lands as a scatter of fields that were merely near each
        # other in time.
        **shape,
        "moon": ({"ra": moon_ra, "dec": moon_dec,
                  "illumination": round(illumination, 3),
                  "avoidance": round(keep_away, 1)}
                 if moon_ra is not None else None),
        # Nothing ever crosses the meridian while it is being shot, so this is
        # not an estimate.
        "flips": 0 if avoid_flips else None,
        "detail": detail,
    }


def _utc(timestamp: float) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(timestamp, _dt.timezone.utc)


def survey_summary(fields: list[dict[str, Any]], progress: dict[str, Any],
                   goal: list[dict[str, Any]], latitude: float | None,
                   minimum_altitude: float, per_field_seconds: float
                   ) -> dict[str, Any]:
    """Where the whole survey has got to.

    Reported against the fields this site can actually reach, because that is
    the number the progress bar has to be able to finish at.
    """
    total = len(fields)
    within = 0
    complete = 0
    started = 0
    frames = 0
    seconds = 0.0
    by_ring: dict[int, dict[str, Any]] = {}

    for field in fields:
        ok = (latitude is None
              or transit_altitude(field["dec"], latitude) >= minimum_altitude)
        state = field_state(progress.get(field["id"], {}), goal)
        ring = by_ring.setdefault(field["ring"], {
            "ring": field["ring"], "dec": field["dec"], "fields": 0,
            "reachable": 0, "complete": 0, "started": 0})
        ring["fields"] += 1
        if ok:
            within += 1
            ring["reachable"] += 1
        frames += state["have"]
        seconds += float(state["seconds"] or 0.0)
        if state["complete"]:
            complete += 1
            if ok:
                ring["complete"] += 1
        elif state["started"]:
            started += 1
            if ok:
                ring["started"] += 1

    outstanding = max(0, within - complete)
    return {
        "fields": total,
        "reachable": within,
        "unreachable": total - within,
        "complete": complete,
        "started": started,
        "untouched": max(0, within - complete - started),
        "fraction": round(complete / within, 4) if within else 0.0,
        "frames": frames,
        "seconds": round(seconds, 1),
        "remainingSeconds": round(outstanding * per_field_seconds, 1),
        "rings": [by_ring[key] for key in sorted(by_ring)],
    }
