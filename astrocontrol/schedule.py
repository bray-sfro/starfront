"""What can actually be shot tonight, and in what order.

Three jobs, each of which the planner needs before it can offer a sensible
choice:

  * **the night** - sunset, sunrise, and the astronomically dark hours between
    them, for the observing site;
  * **the window** - when a given target is both in the dark and high enough to
    be worth shooting, which is what limits how many frames will fit;
  * **the route** - for a mosaic, which tile to start on and how to walk the
    grid so that neighbouring tiles are shot next to each other in time.

That last one matters more than it looks.  Sky brightness drifts through the
night, so tiles shot hours apart at opposite ends of a grid stack with a visible
seam.  Walking only between edge-adjacent tiles keeps the gradient smooth.
"""

from __future__ import annotations

import datetime as _dt
import math
import time as _time
from typing import Any, Iterable

from . import astro

# Sun altitudes that define the parts of a night.
SUNSET_ALTITUDE = -0.833          # allows for refraction and the solar radius
CIVIL = -6.0
NAUTICAL = -12.0
ASTRONOMICAL = -18.0

_SAMPLE_SECONDS = 60              # for sun events
_CURVE_SECONDS = 300              # for the altitude graph


def _site_zone(longitude: float | None) -> _dt.tzinfo:
    """The clock a site keeps by the sun: UTC shifted by its longitude.

    The PC's own zone is what it used to be, which is right until the
    telescope is on another continent - a rig in the east run from a PC in
    the west had its "noon" fall in the middle of its night, and dawn came
    out before dusk. Solar time at the site is right everywhere, and within
    an hour of the PC's clock whenever the two are in the same place.
    """
    if longitude is None:
        return _dt.datetime.now().astimezone().tzinfo or _dt.timezone.utc
    hours = max(-23.0, min(23.0, float(longitude) / 15.0))
    return _dt.timezone(_dt.timedelta(hours=hours))


def _local_noon(date: _dt.date | None = None,
                longitude: float | None = None) -> _dt.datetime:
    """Noon at the site on `date`, as an aware datetime.

    A night belongs to the day it starts on, so everything is anchored to the
    noon before it rather than to midnight, which would split it in two.
    """
    zone = _site_zone(longitude)
    day = date or _dt.datetime.now(zone).date()
    return _dt.datetime.combine(day, _dt.time(12, 0), tzinfo=zone)


def clock_window(from_text: str, to_text: str,
                 now: float | None = None) -> tuple[float | None, float | None]:
    """Two local clock times — "21:00", "01:00" — as moments in tonight.

    What somebody says is a *time of night*, and the small hours belong to the
    night that started the evening before: at nine in the evening, "01:00"
    means four hours away, not twenty hours ago. So anything before noon is
    placed on the following day, which is the same rule the rest of this module
    anchors to and the reason `_local_noon` exists.

    Either end may be blank, and a blank end is an open one. Nonsense comes back
    as None rather than as a guess: a window nobody can parse should behave as
    no window, not as midnight.
    """
    anchor = (_dt.datetime.now().astimezone() if now is None
              else _dt.datetime.fromtimestamp(now).astimezone())
    # The evening this night started on.
    day = anchor.date() if anchor.hour >= 12 else (
        anchor.date() - _dt.timedelta(days=1))

    def moment(text: str) -> float | None:
        parts = str(text or "").strip().split(":")
        if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
            return None
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None
        on = day if hour >= 12 else day + _dt.timedelta(days=1)
        return _dt.datetime.combine(on, _dt.time(hour, minute),
                                    tzinfo=anchor.tzinfo).timestamp()

    return moment(from_text), moment(to_text)


def night(latitude: float, longitude: float,
          date: _dt.date | None = None) -> dict[str, Any]:
    """Sun events for the night starting on `date`, or for the night that counts.

    With no date, "the night that counts" is the one happening now if there is
    one, and otherwise the one coming.  That distinction is the whole of this
    function's difficulty and it used to be got wrong: the anchor was *today's*
    noon whatever the time, so at one in the morning — in the middle of a night,
    with a sequence running — everything switched to the *following* night.
    Dusk moved twenty-five hours into the future, every target's rise time went
    with it, and a run that had been told to wait for its target to rise waited
    for a rise a day away while the thing sat overhead.

    The boundary between one night and the next is dawn, not midnight and not
    noon: before this morning's dawn you are still in last night.  Dawn needs the
    sun, which is why the choice is made here rather than in `_local_noon` —
    which has no site and cannot know.

    Times are Unix timestamps.  Anything the sun does not do that night — never
    setting in the summer at high latitude, never getting properly dark — comes
    back as None, and the planner says so rather than inventing a window.
    """
    if date is not None:
        return _night_from(_local_noon(date, longitude), latitude, longitude)

    # By the site's sun, not the PC's clock: see `_site_zone`.
    now = _dt.datetime.now(_site_zone(longitude))
    if now.hour >= 12:
        # Afternoon or evening: the night that starts today, whether or not it
        # has started yet. No need to ask the sun.
        return _night_from(_local_noon(now.date(), longitude), latitude, longitude)

    # Small hours or morning. Work out last night, and keep it while it lasts.
    previous = _night_from(_local_noon(now.date() - _dt.timedelta(days=1), longitude),
                           latitude, longitude)
    ends = (previous.get("dawnAstronomical") or previous.get("sunrise")
            or previous.get("windowEnd"))
    if ends and _time.time() < ends:
        return previous
    return _night_from(_local_noon(now.date(), longitude), latitude, longitude)


def _night_from(noon: _dt.datetime, latitude: float,
                longitude: float) -> dict[str, Any]:
    """Sun events for the twenty-four hours after a local noon."""
    start = noon.timestamp()
    end = start + 24 * 3600

    samples = [(t, astro.sun_altitude(t, latitude, longitude))
               for t in range(int(start), int(end) + 1, _SAMPLE_SECONDS)]

    def first(level: float, direction: str) -> float | None:
        for timestamp, way in astro.crossings(samples, level):
            if way == direction:
                return timestamp
        return None

    def last(level: float, direction: str) -> float | None:
        found = [t for t, way in astro.crossings(samples, level) if way == direction]
        return found[-1] if found else None

    sunset = first(SUNSET_ALTITUDE, "down")
    sunrise = last(SUNSET_ALTITUDE, "up")
    dusk = first(ASTRONOMICAL, "down")
    dawn = last(ASTRONOMICAL, "up")

    altitudes = [value for _, value in samples]
    return {
        "date": noon.date().isoformat(),
        "windowStart": start,
        "windowEnd": end,
        "sunset": sunset,
        "sunrise": sunrise,
        "duskAstronomical": dusk,
        "dawnAstronomical": dawn,
        "duskNautical": first(NAUTICAL, "down"),
        "dawnNautical": last(NAUTICAL, "up"),
        "duskCivil": first(CIVIL, "down"),
        "dawnCivil": last(CIVIL, "up"),
        "darkMinutes": round((dawn - dusk) / 60.0, 1) if (dusk and dawn and dawn > dusk) else 0.0,
        "sunAlwaysUp": min(altitudes) > SUNSET_ALTITUDE,
        "sunAlwaysDown": max(altitudes) < SUNSET_ALTITUDE,
        "neverAstronomicallyDark": max(altitudes) > ASTRONOMICAL and dusk is None,
    }


def altitude_curve(ra_hours: float, dec_deg: float, latitude: float, longitude: float,
                   start: float, end: float,
                   step: int = _CURVE_SECONDS) -> list[dict[str, float]]:
    """Altitude every few minutes across a span, for drawing."""
    points = []
    for timestamp in range(int(start), int(end) + 1, step):
        lst = astro.local_sidereal_hours(
            longitude, _dt.datetime.fromtimestamp(timestamp, _dt.timezone.utc))
        altitude, azimuth = astro.ra_dec_to_alt_az(ra_hours, dec_deg, lst, latitude)
        points.append({"t": timestamp, "alt": round(altitude, 2),
                       "az": round(azimuth, 1)})
    return points


def moon_track(latitude: float, longitude: float, night_info: dict[str, Any],
               step: int = _CURVE_SECONDS) -> dict[str, Any]:
    """Where the Moon is across the night, and how much of it is lit.

    The single most useful thing to draw on a night plan after the targets
    themselves.  A full Moon thirty degrees from a broadband target costs more
    than clouds would, and the only way to see that coming is to see when it is
    up and where — so this returns a curve to draw rather than a verdict.

    Its position is recomputed at every sample: the Moon moves about half a
    degree an hour against the stars, which over a nine-hour night is thirteen
    degrees, and treating it as fixed would put it in the wrong place by the
    end of exactly the hours that matter.
    """
    start = night_info.get("sunset") or night_info.get("windowStart")
    end = night_info.get("sunrise") or night_info.get("windowEnd")
    if not start or not end or end <= start:
        return {"curve": [], "rises": None, "sets": None, "illumination": 0.0,
                "maxAltitude": None, "upMinutes": 0.0, "ra": None, "dec": None}

    curve: list[dict[str, float]] = []
    samples: list[tuple[float, float]] = []
    for timestamp in range(int(start), int(end) + 1, step):
        jd = astro.julian_from_timestamp(timestamp)
        ra_hours, dec_deg = astro.moon_position(jd)
        altitude = astro.altitude_at(ra_hours, dec_deg, timestamp, latitude, longitude)
        curve.append({"t": timestamp, "alt": round(altitude, 2),
                      "ra": round(ra_hours, 4), "dec": round(dec_deg, 3)})
        samples.append((float(timestamp), altitude))

    crossings = astro.crossings(samples, 0.0)
    rises = next((t for t, way in crossings if way == "up"), None)
    sets = next((t for t, way in crossings if way == "down"), None)
    # Up at dusk and still up at dawn: no crossing to find, but it is the Moon
    # that ruins the most nights, so say so rather than reporting nothing.
    if rises is None and samples and samples[0][1] > 0:
        rises = samples[0][0]
    if sets is None and samples and samples[-1][1] > 0:
        sets = samples[-1][0]

    up = sum(step for _, altitude in samples if altitude > 0)
    middle = astro.julian_from_timestamp((start + end) / 2.0)
    peak = max(samples, key=lambda item: item[1])
    ra_mid, dec_mid = astro.moon_position(middle)
    return {
        "curve": curve,
        "rises": rises,
        "sets": sets,
        "illumination": round(astro.moon_illumination(middle), 4),
        "maxAltitude": round(peak[1], 2),
        "peakTime": peak[0],
        "upMinutes": round(up / 60.0, 1),
        "ra": round(ra_mid, 4),
        "dec": round(dec_mid, 3),
    }


def dark_overlap(intervals: list[dict[str, Any]], moon: dict[str, Any],
                 ra_hours: float, dec_deg: float,
                 separation_limit: float) -> dict[str, Any]:
    """How much of a target's window is free of a Moon that matters.

    "Moon up" is not the question — a thin crescent a hundred degrees away is
    nothing, and that is most of the Moon's time in the sky.  What counts is the
    Moon being up *and* nearer than the target can afford.
    """
    if not intervals or not moon.get("curve"):
        return {"minutes": 0.0, "spoiledMinutes": 0.0, "closest": None}

    by_time = {int(point["t"]): point for point in moon["curve"]}
    times = sorted(by_time)
    if not times:
        return {"minutes": 0.0, "spoiledMinutes": 0.0, "closest": None}
    step = (times[1] - times[0]) if len(times) > 1 else _CURVE_SECONDS

    clear = 0.0
    spoiled = 0.0
    closest: float | None = None
    for interval in intervals:
        for timestamp in times:
            if not (interval["start"] <= timestamp <= interval["end"]):
                continue
            point = by_time[timestamp]
            if point["alt"] <= 0:
                clear += step
                continue
            separation = astro.separation_degrees(ra_hours, dec_deg,
                                                  point["ra"], point["dec"])
            closest = separation if closest is None else min(closest, separation)
            if separation >= separation_limit:
                clear += step
            else:
                spoiled += step
    return {"minutes": round(clear / 60.0, 1),
            "spoiledMinutes": round(spoiled / 60.0, 1),
            "closest": None if closest is None else round(closest, 1)}


def observable(ra_hours: float, dec_deg: float, latitude: float, longitude: float,
               night_info: dict[str, Any], minimum_altitude: float = 30.0
               ) -> dict[str, Any]:
    """When this target is both dark and high enough, tonight.

    Returns the intervals, the longest single run (which is what an unbroken
    imaging session gets) and the total.
    """
    dusk = night_info.get("duskAstronomical")
    dawn = night_info.get("dawnAstronomical")
    if not dusk or not dawn or dawn <= dusk:
        return {"intervals": [], "totalMinutes": 0.0, "longestMinutes": 0.0,
                "rises": None, "sets": None, "maxAltitude": None,
                "transitTime": None, "reason": "no astronomical darkness tonight"}

    samples = [(t, astro.altitude_at(ra_hours, dec_deg, t, latitude, longitude))
               for t in range(int(dusk), int(dawn) + 1, _SAMPLE_SECONDS)]

    intervals: list[tuple[float, float]] = []
    open_at: float | None = None
    for timestamp, altitude in samples:
        if altitude >= minimum_altitude and open_at is None:
            open_at = timestamp
        elif altitude < minimum_altitude and open_at is not None:
            intervals.append((open_at, timestamp))
            open_at = None
    if open_at is not None:
        intervals.append((open_at, samples[-1][0]))

    peak = max(samples, key=lambda item: item[1])
    longest = max((b - a for a, b in intervals), default=0.0)
    return {
        "intervals": [{"start": a, "end": b, "minutes": round((b - a) / 60.0, 1)}
                      for a, b in intervals],
        "totalMinutes": round(sum(b - a for a, b in intervals) / 60.0, 1),
        "longestMinutes": round(longest / 60.0, 1),
        "rises": intervals[0][0] if intervals else None,
        "sets": intervals[-1][1] if intervals else None,
        "maxAltitude": round(peak[1], 2),
        "transitTime": peak[0],
        "reason": "" if intervals else
                  f"never reaches {minimum_altitude:g}° during astronomical darkness",
    }


# ---------------------------------------------------------------------------
# Fitting exposures into the time there is
# ---------------------------------------------------------------------------

def frame_seconds(exposure: float, overheads: dict[str, float]) -> float:
    """Wall-clock cost of one frame: the exposure plus its download and dither."""
    return float(exposure) + float(overheads.get("perFrame", 15.0))


def focus_seconds(shooting_seconds: float, filters_used: int,
                  overheads: dict[str, float]) -> float:
    """What autofocus costs a stretch of imaging.

    Two triggers, and both are counted because both really happen: a sweep
    every so many minutes, and a sweep on each filter change.  Neither used to
    be costed at all, which is how a night planned to the last minute ran out
    of dark forty minutes early — nine points at six seconds with the moves is
    four minutes, and a normal night does half a dozen of them.

    The clock trigger and the filter trigger overlap: a run for one resets the
    other.  So the greater of the two is taken rather than their sum, which is
    the honest bound on how many sweeps a stretch of imaging actually pays for.
    """
    run = float(overheads.get("focusRun", 0.0) or 0.0)
    if run <= 0 or shooting_seconds <= 0:
        return 0.0
    every = float(overheads.get("focusEvery", 0.0) or 0.0)
    by_clock = (shooting_seconds / (every * 60.0)) if every > 0 else 0.0
    by_filter = max(0, int(filters_used)) if overheads.get("focusOnFilter", True) else 0
    return run * max(1.0, max(by_clock, float(by_filter)))


def plan_seconds(filters: Iterable[dict[str, Any]], panels: int,
                 overheads: dict[str, float]) -> float:
    """How long a target's whole allocation takes, mosaic included.

    Counts are *per panel*, so a four-panel mosaic really does cost four times
    what one panel costs — which is the arithmetic that stops a mosaic being
    quietly over-committed.
    """
    panels = max(1, int(panels))
    per_panel = 0.0
    used_filters = 0
    for entry in filters:
        count = max(0, int(entry.get("count", 0)))
        if count == 0:
            continue
        used_filters += 1
        per_panel += count * frame_seconds(entry.get("exposure", 0), overheads)
    if per_panel == 0.0:
        return 0.0
    per_panel += used_filters * float(overheads.get("filterChange", 20.0))
    total = panels * (per_panel + float(overheads.get("perPanel", 90.0)))
    return total + focus_seconds(total, used_filters, overheads)


def shooting_order(filters: list[dict[str, Any]],
                   order: str = "grouped") -> list[dict[str, Any]]:
    """One panel's frames, in the order they will actually be taken.

    `grouped` shoots all the luminance and then all the red; `rotate` cycles
    through the filters a frame at a time. Which one is in force changes what a
    session cut short comes home with, so a forecast that ignored it would
    describe a night nobody had.
    """
    rows = [dict(row) for row in filters
            if int(row.get("count", 0) or 0) > 0]
    if not rows:
        return []
    if order != "rotate":
        return [{"name": row.get("name"), "exposure": row.get("exposure", 0)}
                for row in rows for _ in range(int(row["count"]))]

    frames: list[dict[str, Any]] = []
    left = {index: int(row["count"]) for index, row in enumerate(rows)}
    while any(left.values()):
        for index, row in enumerate(rows):
            if left[index] <= 0:
                continue
            frames.append({"name": row.get("name"),
                           "exposure": row.get("exposure", 0)})
            left[index] -= 1
    return frames


def tonight(filters: list[dict[str, Any]], panels: list[Any],
            available_seconds: float, overheads: dict[str, float],
            order: str = "grouped") -> dict[str, Any]:
    """What this allocation will actually get through before the window closes.

    An allocation is what somebody asked for. On a mosaic of forty panels in a
    six-hour window it is a wish, and a plan that only reports the wish is why
    an operator finds out in the morning that thirty-six panels were never
    started. This says which panels get shot and what comes home.

    Modelled on what the run really does: panels are walked in the order given,
    each takes its whole allocation, and the clock is checked before every
    frame — so the last panel started is cut short rather than skipped. Whole
    panels are counted through `plan_seconds`, the same function the budget and
    the ceilings use, so the forecast cannot drift away from them.

    The partial panel is walked frame by frame. Focus sweeps *inside* it are not
    modelled, so its frame count is the optimistic end of what is honest; whole
    panels, which are the bulk of any real night, carry their full share.
    """
    rows = [dict(row) for row in filters if int(row.get("count", 0) or 0) > 0]
    total = max(0, len(panels))
    result = {
        "panels": [], "complete": 0, "partial": None,
        "frames": {}, "seconds": 0.0, "totalPanels": total,
        "fits": False, "nothing": True,
    }
    if not rows or total == 0 or available_seconds <= 0:
        return result

    # How many whole panels fit, asked of the same function that sets the
    # budget everywhere else rather than of arithmetic of its own.
    complete = 0
    while (complete < total
           and plan_seconds(rows, complete + 1, overheads) <= available_seconds):
        complete += 1

    used = plan_seconds(rows, complete, overheads) if complete else 0.0
    frames: dict[str, int] = {}
    for row in rows:
        if complete:
            frames[str(row.get("name"))] = int(row["count"]) * complete

    shot = [_panel_index(panels[i]) for i in range(complete)]
    partial = None
    if complete < total:
        # What the next panel gets through before the clock stops it. Its fixed
        # cost lands first: the slew, the centring and the filter changes happen
        # whether or not a single frame follows them.
        fixed = (float(overheads.get("perPanel", 90.0))
                 + len(rows) * float(overheads.get("filterChange", 20.0)))
        left = available_seconds - used - fixed
        taken = 0
        spent = 0.0
        for frame in shooting_order(rows, order):
            cost = frame_seconds(frame["exposure"], overheads)
            if cost > left:
                break
            left -= cost
            spent += cost
            taken += 1
            name = str(frame["name"])
            frames[name] = frames.get(name, 0) + 1
        if taken:
            # The fixed cost counts too. The slew and the filter change really
            # happen, and a forecast that spent them out of the budget while
            # leaving them out of the total would report a night two minutes
            # shorter than the one it just described.
            used += fixed + spent
            partial = {"panel": _panel_index(panels[complete]), "frames": taken,
                       # Reported separately so "how long on each panel" can be
                       # worked out without the short one dragging the average
                       # down.
                       "seconds": round(fixed + spent, 1)}
            shot.append(partial["panel"])

    result.update({
        "panels": shot,
        "complete": complete,
        "partial": partial,
        "frames": {name: count for name, count in frames.items() if count},
        "seconds": round(used, 1),
        "fits": complete >= total,
        "nothing": not shot,
    })
    return result


def _panel_index(panel: Any) -> int:
    if isinstance(panel, dict):
        return int(panel.get("index") or 0)
    try:
        return int(panel)
    except (TypeError, ValueError):
        return 0


def max_count(exposure: float, panels: int, available_seconds: float,
              others: list[dict[str, Any]], overheads: dict[str, float]) -> int:
    """The largest per-panel count of one filter that still fits.

    `others` are the rest of this target's rows, so each filter's ceiling
    reflects what the others have taken.

    Answered by asking `plan_seconds` rather than by arithmetic of its own.
    That matters more than it looks: every part of the cost that is not simply
    per-frame — the filter change a new channel adds, and above all the focus
    sweeps, which are shared across the whole allocation rather than owed by
    each filter — was double-counted the moment the two were worked out
    separately, and the ceiling then disagreed with the budget it was meant to
    enforce.
    """
    panels = max(1, int(panels))
    per_frame = frame_seconds(exposure, overheads)
    if per_frame <= 0 or available_seconds <= 0:
        return 0
    rest = [dict(item) for item in others if int(item.get("count", 0) or 0) > 0]

    def fits(count: int) -> bool:
        # The name is not used by `plan_seconds` — it counts rows, not names —
        # so this stands in for "one more filter's worth".
        trial = rest + [{"name": "probe", "exposure": exposure, "count": count}]
        return plan_seconds(trial, panels, overheads) <= available_seconds

    if not fits(1):
        return 0
    # Binary search rather than a formula: `plan_seconds` is monotonic in the
    # count but not linear in it, and there is no arithmetic inverse that stays
    # right when what it charges for changes.
    low, high = 1, 1
    while fits(high * 2) and high < 100000:
        high *= 2
    low, high = high, high * 2
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return low


# ---------------------------------------------------------------------------
# Walking a mosaic
# ---------------------------------------------------------------------------

def clip_window(window: dict[str, Any], start_at: float | None,
                end_at: float | None) -> dict[str, Any]:
    """Narrow an observable window to the hours the operator pinned it to.

    A start and end set by hand are a promise about when the telescope will be
    on this target, so they have to shrink the time budget as well as the
    picture.  Without this the planner would happily accept eight hours of
    frames for a target pinned to a two hour slot.
    """
    intervals = [(i["start"], i["end"]) for i in window.get("intervals") or []]
    if start_at is not None:
        intervals = [(max(a, start_at), b) for a, b in intervals]
    if end_at is not None:
        intervals = [(a, min(b, end_at)) for a, b in intervals]
    intervals = [(a, b) for a, b in intervals if b > a]

    longest = max((b - a for a, b in intervals), default=0.0)
    clipped = dict(window)
    clipped["intervals"] = [{"start": a, "end": b, "minutes": round((b - a) / 60.0, 1)}
                            for a, b in intervals]
    clipped["totalMinutes"] = round(sum(b - a for a, b in intervals) / 60.0, 1)
    clipped["longestMinutes"] = round(longest / 60.0, 1)
    clipped["rises"] = intervals[0][0] if intervals else None
    clipped["sets"] = intervals[-1][1] if intervals else None
    clipped["pinned"] = start_at is not None or end_at is not None
    if not intervals and clipped["pinned"]:
        clipped["reason"] = "the times pinned on the graph leave no observable window"
    return clipped


def free_spans(dusk: float, dawn: float,
               reserved: list[tuple[float, float]] | None) -> list[tuple[float, float]]:
    """The dark hours with the pinned slots cut out of them.

    A target the operator has pinned to a time is not a request, it is a fact:
    the arranger works around it rather than over it.
    """
    spans = [(dusk, dawn)]
    for start, end in sorted(reserved or []):
        remaining: list[tuple[float, float]] = []
        for span_start, span_end in spans:
            if end <= span_start or start >= span_end:
                remaining.append((span_start, span_end))
                continue
            if span_start < start:
                remaining.append((span_start, start))
            if end < span_end:
                remaining.append((end, span_end))
        spans = remaining
    return [(a, b) for a, b in spans if b > a]


def fair_shares(requests: list[dict[str, Any]], seconds: float) -> dict[str, float]:
    """How much of the night each target should get.

    Max-min fair, weighted by priority and capped by how long each target is
    actually up.  The point of it is the guarantee: *every* target that is
    observable at all gets time, before any target gets a second helping.

    Without this the arranger hands the first target everything it asks for —
    and what a target asks for, when the arranger is also choosing the
    exposures, is its whole window.  A night of three targets then goes
    entirely to the first two and the third is told the night ran out, which is
    the opposite of what "arrange my night" means.

    Time a target cannot use, because it is only up for two hours, is handed
    back and shared among the ones that can.
    """
    pool = {r["id"]: float(r.get("capacity") or 0.0) for r in requests
            if float(r.get("capacity") or 0.0) > 0}
    weights = {r["id"]: max(1, int(r.get("priority", 5))) for r in requests}
    shares: dict[str, float] = {}
    left = max(0.0, seconds)

    while pool and left > 0:
        total_weight = sum(weights[i] for i in pool) or 1
        # Whoever cannot use their slice this round takes what they can and
        # leaves the rest behind for the others.
        capped = {i: c for i, c in pool.items()
                  if c <= left * weights[i] / total_weight}
        if not capped:
            for target_id, capacity in pool.items():
                shares[target_id] = left * weights[target_id] / total_weight
            break
        for target_id, capacity in capped.items():
            shares[target_id] = capacity
            left -= capacity
            del pool[target_id]
    return shares


def arrange(requests: list[dict[str, Any]], night_info: dict[str, Any],
            reserved: list[tuple[float, float]] | None = None) -> dict[str, Any]:
    """Order the night's targets and give each one a start and end time.

    Earliest deadline first: at every moment, of the targets that are up and
    still owed time, shoot the one whose window closes soonest.  That is the
    classic answer to this shape of problem and it is optimal in the sense that
    matters here — if any order can fit everything in, this one does.

    `requests` are dicts of {id, seconds, intervals}, where `intervals` are the
    target's observable spans.  `reserved` are spans already spoken for by
    targets the operator pinned to a time, which this works around.  Returns an
    ordered list of assignments, plus whatever could not be fitted and why.
    """
    dusk = night_info.get("duskAstronomical")
    dawn = night_info.get("dawnAstronomical")
    if not dusk or not dawn or dawn <= dusk:
        return {"order": [], "unplaced": [{"id": r["id"], "reason": "no dark tonight"}
                                          for r in requests],
                "idleSeconds": 0.0, "usedSeconds": 0.0}

    pending = []
    unplaced = []
    for request in requests:
        spans = [(i["start"], i["end"]) for i in request.get("intervals") or []]
        if not spans or request.get("seconds", 0) <= 0:
            unplaced.append({"id": request["id"],
                             "reason": "not observable tonight" if not spans
                                       else "nothing planned"})
            continue
        pending.append({"id": request["id"], "seconds": float(request["seconds"]),
                        "start": min(s for s, _ in spans),
                        "end": max(e for _, e in spans),
                        # The real intervals, not just their outer bounds: a
                        # target that dips below the floor and comes back has a
                        # hole in the middle, and the gap-filling pass below
                        # must not hand it time it cannot use.
                        "spans": sorted(spans)})

    # `pending` is emptied as targets are placed, so the full list is kept for
    # the gap-filling pass — which has to know how long an *already placed*
    # target stays up in order to run it on.
    everyone = list(pending)

    spans = free_spans(dusk, dawn, reserved)
    order: list[dict[str, Any]] = []
    idle = 0.0
    span_index = 0
    cursor = spans[0][0] if spans else dawn

    while pending and span_index < len(spans):
        span_start, span_end = spans[span_index]
        cursor = max(cursor, span_start)
        if cursor >= span_end:
            span_index += 1
            continue

        ready = [p for p in pending if p["end"] > cursor]
        if not ready:
            for leftover in pending:
                unplaced.append({"id": leftover["id"],
                                 "reason": "its window had closed by the time "
                                           "the telescope was free"})
            pending = []
            break

        available = [p for p in ready if p["start"] <= cursor]
        if not available:
            # Nothing is up yet; wait for the next one rather than pretending.
            next_up = min(ready, key=lambda p: p["start"])
            if next_up["start"] >= span_end:
                span_index += 1
                continue
            idle += next_up["start"] - cursor
            cursor = next_up["start"]
            available = [p for p in ready if p["start"] <= cursor]

        chosen = min(available, key=lambda p: (p["end"], -p["seconds"]))
        pending.remove(chosen)

        # The real intervals, not their outer bounds: a target that drops below
        # the floor and climbs back has a hole in the middle of its night, and
        # scheduling straight through it would spend those hours on a frame the
        # limit exists to refuse.
        window = _next_window(chosen["spans"], cursor, span_end)
        if window is None:
            unplaced.append({"id": chosen["id"], "reason": "no room left in its window"})
            continue
        start, latest = window
        end = min(start + chosen["seconds"], latest)
        if end <= start:
            unplaced.append({"id": chosen["id"], "reason": "no room left in its window"})
            continue

        shortfall = chosen["seconds"] - (end - start)
        order.append({
            "id": chosen["id"],
            "start": round(start, 1),
            "end": round(end, 1),
            "seconds": round(end - start, 1),
            "shortSeconds": round(max(0.0, shortfall), 1),
        })
        cursor = end

    # Nothing should sit idle while something is up.
    #
    # A fair share decides what each target is *owed*; it cannot decide what the
    # night can actually deliver. A target whose window closes before it can
    # spend its slice leaves that time behind, and the run of the night is then
    # short by exactly that much — which is how a plan ends with an hour and a
    # half of dark and Andromeda sitting at sixty degrees doing nothing.
    #
    # So the leftovers are given away afterwards, to whoever is still up.
    filled = _use_idle_time(order, everyone, spans)
    placed_ids = {item["id"] for item in order}
    for leftover in [p for p in everyone if p["id"] not in placed_ids]:
        unplaced.append({"id": leftover["id"], "reason": "the night ran out"})

    used = sum(item["seconds"] for item in order)
    covered = _covered_seconds(order, spans)
    return {
        "order": order,
        "unplaced": unplaced,
        "usedSeconds": round(used, 1),
        # What is left over after the gap-filling pass, which is the honest
        # figure: time nothing could have used, rather than time nothing did.
        "idleSeconds": round(max(0.0, sum(b - a for a, b in spans) - covered), 1),
        "filledSeconds": round(filled, 1),
        "darkSeconds": round(dawn - dusk, 1),
    }


def _covered_seconds(order: list[dict[str, Any]],
                     spans: list[tuple[float, float]]) -> float:
    """How much of the usable dark has a target on it."""
    total = 0.0
    for span_start, span_end in spans:
        for slot in order:
            start = max(span_start, slot["start"])
            end = min(span_end, slot["end"])
            if end > start:
                total += end - start
    return total


def _reach(spans: list[tuple[float, float]], at: float, limit: float,
           forward: bool) -> float:
    """How far a target can run on from `at` without dropping below the floor.

    Bounded by `limit` and by the end of whichever observable interval `at`
    falls in — a target that sets and rises again cannot be run straight
    through the hole in the middle.
    """
    for start, end in spans:
        if start - 1.0 <= at <= end + 1.0:
            return min(limit, end) if forward else max(limit, start)
    return at


#: A gap worth pointing the telescope at something new for.  Shorter than this
#: and the slew, the settle and the plate solve eat most of it; extending a
#: target that is *already* on the mount has no such cost and has no minimum.
MIN_NEW_SLOT = 600.0


def _overlap(spans: list[tuple[float, float]], start: float,
             end: float) -> tuple[float, float] | None:
    """The longest stretch of [start, end] this target is actually up for."""
    best: tuple[float, float] | None = None
    for span_start, span_end in spans:
        low, high = max(start, span_start), min(end, span_end)
        if high > low and (best is None or high - low > best[1] - best[0]):
            best = (low, high)
    return best


def _next_window(spans: list[tuple[float, float]], after: float,
                 limit: float) -> tuple[float, float] | None:
    """The *earliest* stretch of [after, limit] this target is up for.

    Earliest rather than longest, because the caller is walking the night in
    order: a target that is up now and up again later should be shot now, and
    the later stretch left for whatever else wants it.
    """
    best: tuple[float, float] | None = None
    for span_start, span_end in sorted(spans):
        low, high = max(after, span_start), min(limit, span_end)
        if high > low and (best is None or low < best[0]):
            best = (low, high)
    return best


def _use_idle_time(order: list[dict[str, Any]], everyone: list[dict[str, Any]],
                   spans: list[tuple[float, float]]) -> float:
    """Hand every leftover minute of dark to whoever can still use it.

    Extending the target already on the mount is always preferred: it costs no
    slew, no settle and no re-centre, and it is what an operator standing at the
    telescope would do. Only a gap nothing adjacent can reach is offered to a
    target that has not run at all.
    """
    # Every target that took part, placed or not, keyed by id — the placed ones
    # so an extension knows how far they stay up, the rest so a gap nothing
    # adjacent can reach can still be offered to one of them.
    by_id = {p["id"]: p for p in everyone}
    pending = everyone
    filled = 0.0
    changed = True
    while changed:
        changed = False
        for span_start, span_end in spans:
            inside = sorted(
                [o for o in order if o["end"] > span_start and o["start"] < span_end],
                key=lambda o: o["start"])
            cursor = span_start
            gaps: list[tuple[float, float]] = []
            for slot in inside:
                if slot["start"] > cursor + 1.0:
                    gaps.append((cursor, slot["start"]))
                cursor = max(cursor, slot["end"])
            if cursor < span_end - 1.0:
                gaps.append((cursor, span_end))

            for gap_start, gap_end in gaps:
                # Run on the target that was already pointing there.
                before = next((o for o in inside
                               if abs(o["end"] - gap_start) < 1.0), None)
                if before is not None and before["id"] in by_id:
                    reach = _reach(by_id[before["id"]]["spans"], before["end"],
                                   gap_end, forward=True)
                    if reach > before["end"] + 1.0:
                        filled += reach - before["end"]
                        before["end"] = round(reach, 1)
                        before["seconds"] = round(before["end"] - before["start"], 1)
                        before["extended"] = True
                        changed = True
                        continue

                # Or start the next one early.
                after = next((o for o in inside
                              if abs(o["start"] - gap_end) < 1.0), None)
                if after is not None and after["id"] in by_id:
                    reach = _reach(by_id[after["id"]]["spans"], after["start"],
                                   gap_start, forward=False)
                    if reach < after["start"] - 1.0:
                        filled += after["start"] - reach
                        after["start"] = round(reach, 1)
                        after["seconds"] = round(after["end"] - after["start"], 1)
                        after["extended"] = True
                        changed = True
                        continue

                # Or give it to a target that has not run at all, if one is up
                # for enough of the gap to be worth the slew.
                if gap_end - gap_start < MIN_NEW_SLOT:
                    continue
                placed_ids = {o["id"] for o in order}
                candidates = []
                for candidate in pending:
                    if candidate["id"] in placed_ids:
                        continue
                    window = _overlap(candidate["spans"], gap_start, gap_end)
                    if window and window[1] - window[0] >= MIN_NEW_SLOT:
                        candidates.append((candidate, window))
                if not candidates:
                    continue
                chosen, window = max(
                    candidates,
                    key=lambda item: (item[0].get("priority", 5),
                                      item[1][1] - item[1][0]))
                order.append({
                    "id": chosen["id"],
                    "start": round(window[0], 1),
                    "end": round(window[1], 1),
                    "seconds": round(window[1] - window[0], 1),
                    "shortSeconds": round(
                        max(0.0, chosen["seconds"] - (window[1] - window[0])), 1),
                    "extended": True,
                })
                order.sort(key=lambda item: item["start"])
                filled += window[1] - window[0]
                changed = True
    return filled


def rise_set_transit(ra_hours: float, dec_deg: float, latitude: float, longitude: float,
                     night_info: dict[str, Any], minimum_altitude: float = 30.0
                     ) -> dict[str, float | None]:
    """When a target crosses the altitude limit, ignoring daylight.

    Deliberately *not* clipped to the dark hours.  Tiles in a mosaic are often
    all well up by the time it gets dark, which makes their clipped rise times
    identical and useless for deciding what to shoot first.  The uncut crossing
    still distinguishes them, and transit is carried as a tie-break for tiles
    that never cross at all because they are circumpolar.
    """
    start = night_info["windowStart"]
    end = night_info["windowEnd"]
    samples = [(t, astro.altitude_at(ra_hours, dec_deg, t, latitude, longitude))
               for t in range(int(start), int(end) + 1, _SAMPLE_SECONDS)]

    ups = [t for t, way in astro.crossings(samples, minimum_altitude) if way == "up"]
    downs = [t for t, way in astro.crossings(samples, minimum_altitude) if way == "down"]
    peak = max(samples, key=lambda item: item[1])
    return {
        "rise": min(ups) if ups else None,
        "set": max(downs) if downs else None,
        "transit": peak[0],
        "maxAltitude": peak[1],
        "alwaysUp": not ups and not downs and peak[1] >= minimum_altitude,
    }


def _colour_counts(cells: set[tuple[int, int]]) -> tuple[int, int]:
    dark = sum(1 for row, column in cells if (row + column) % 2 == 0)
    return dark, len(cells) - dark


def _parity_allows(cells: set[tuple[int, int]], start: tuple[int, int],
                   goal: tuple[int, int]) -> bool:
    """The checkerboard test for a Hamiltonian path on a bipartite grid.

    Colours must alternate along the path, so an even number of tiles forces
    the two ends onto opposite colours, and an odd number forces both onto the
    colour there is one more of.  Necessary, not sufficient — but it rejects
    the impossible pairs instantly instead of searching for them.
    """
    dark, light = _colour_counts(cells)
    start_dark = (start[0] + start[1]) % 2 == 0
    goal_dark = (goal[0] + goal[1]) % 2 == 0
    if len(cells) % 2 == 0:
        return dark == light and start_dark != goal_dark
    majority_is_dark = dark > light
    return abs(dark - light) == 1 and start_dark == goal_dark == majority_is_dark


def _neighbours(cell: tuple[int, int], cells: set[tuple[int, int]]
                ) -> list[tuple[int, int]]:
    """Edge-sharing neighbours only.  Diagonal hops would jump a seam."""
    row, column = cell
    candidates = ((row - 1, column), (row + 1, column),
                  (row, column - 1), (row, column + 1))
    return [c for c in candidates if c in cells]


def _hamiltonian(cells: set[tuple[int, int]], start: tuple[int, int],
                 goal: tuple[int, int], budget: int = 200000
                 ) -> list[tuple[int, int]] | None:
    """A path visiting every tile once, from `start` to `goal`.

    Backtracking, which is ample for the grids people actually build: even a
    5x5 resolves immediately.  Two prunings keep it that way - always try the
    most constrained neighbour first, and abandon any branch that strands part
    of the grid or cuts the goal off.
    """
    total = len(cells)
    if start not in cells or goal not in cells:
        return None
    if total == 1:
        return [start] if start == goal else None

    steps = 0
    path = [start]
    visited = {start}

    def reachable_from(cell: tuple[int, int], remaining: set[tuple[int, int]]) -> int:
        seen = {cell}
        stack = [cell]
        while stack:
            for neighbour in _neighbours(stack.pop(), remaining):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        return len(seen)

    def walk(cell: tuple[int, int]) -> bool:
        nonlocal steps
        steps += 1
        if steps > budget:
            return False
        if len(path) == total:
            return cell == goal

        remaining = cells - visited
        # Everything left has to still hang together, and include the goal.
        if goal not in remaining:
            return False
        probe = next(iter(remaining))
        if reachable_from(probe, remaining) != len(remaining):
            return False

        options = sorted(_neighbours(cell, remaining),
                         key=lambda c: len(_neighbours(c, remaining - {c})))
        for nxt in options:
            # Only the final step may land on the goal.
            if nxt == goal and len(path) + 1 != total:
                continue
            path.append(nxt)
            visited.add(nxt)
            if walk(nxt):
                return True
            path.pop()
            visited.remove(nxt)
        return False

    return list(path) if walk(start) else None


def tile_order(panels: list[dict[str, Any]], latitude: float, longitude: float,
               night_info: dict[str, Any], minimum_altitude: float = 30.0
               ) -> dict[str, Any]:
    """Decide the order to shoot a mosaic's tiles in.

    Start on the tile that becomes observable first and finish on the one that
    stays observable longest, stepping only between tiles that share an edge.
    If the grid's shape makes that pair of endpoints impossible — a chessboard
    parity problem, not a bug — the last tile is relaxed to the latest-setting
    tile that can actually end a path.
    """
    if not panels:
        return {"order": [], "cells": [], "note": "no panels"}

    # This walks a rectangular grid, so it needs the grid. Panels that are not
    # laid out as one — a survey sweep is a scattered selection of fields, not
    # a mosaic — keep whatever order they arrived in rather than bringing the
    # whole plan down with a KeyError.
    if any("row" not in p or "column" not in p for p in panels):
        return {"order": [p.get("index") for p in panels], "cells": [],
                "note": "not a grid; left in the order it was given"}

    cells = {(int(p["row"]), int(p["column"])) for p in panels}
    by_cell = {(int(p["row"]), int(p["column"])): p for p in panels}

    timing = {cell: rise_set_transit(panel["ra"], panel["dec"], latitude, longitude,
                                     night_info, minimum_altitude)
              for cell, panel in by_cell.items()}

    def rise_key(cell: tuple[int, int]) -> tuple:
        t = timing[cell]
        # Circumpolar tiles never rise; transit orders them the same way, and
        # row/column keeps the choice from depending on set iteration order.
        return (t["rise"] if t["rise"] is not None else t["transit"] - 86400,
                t["transit"], cell[0], cell[1])

    def set_key(cell: tuple[int, int]) -> tuple:
        t = timing[cell]
        return (-(t["set"] if t["set"] is not None else t["transit"] + 86400),
                -t["transit"], cell[0], cell[1])

    starts = sorted(cells, key=rise_key)
    ends = sorted(cells, key=set_key)

    ideal_start, ideal_end = starts[0], ends[0]
    path = None
    chosen_start = chosen_end = None

    # The first-rising tile is what the user asked to begin on, so it is tried
    # against every possible finish before any other tile is considered as a
    # start.  Only a grid whose shape forbids it entirely moves the start.
    for candidate_start in starts:
        for candidate_end in ends:
            if candidate_end == candidate_start and len(cells) > 1:
                continue
            if not _parity_allows(cells, candidate_start, candidate_end):
                continue
            path = _hamiltonian(cells, candidate_start, candidate_end)
            if path:
                chosen_start, chosen_end = candidate_start, candidate_end
                break
        if path:
            break

    notes = []
    if not path:
        # A serpentine walk is at least always edge-adjacent.
        rows = sorted({r for r, _ in cells})
        path = []
        for index, row in enumerate(rows):
            columns = sorted(c for r, c in cells if r == row)
            path += [(row, c) for c in (columns if index % 2 == 0 else columns[::-1])]
        chosen_start, chosen_end = path[0], path[-1]
        notes.append("no adjacent route fits this grid; using a serpentine walk")
    else:
        if chosen_start != ideal_start:
            notes.append(
                f"tile {by_cell[ideal_start].get('index', '?')} rises first, but the "
                "grid has no complete adjacent route from it, so the next earliest "
                "is used")
        if chosen_end != ideal_end:
            notes.append(
                f"tile {by_cell[ideal_end].get('index', '?')} sets last, but no "
                "adjacent route can end there")

    return {
        "order": [by_cell[cell].get("index", 0) for cell in path],
        "cells": [list(cell) for cell in path],
        "startsWith": by_cell[chosen_start].get("index", 0),
        "endsWith": by_cell[chosen_end].get("index", 0),
        "risesFirst": by_cell[ideal_start].get("index", 0),
        "setsLast": by_cell[ideal_end].get("index", 0),
        "adjacent": all(math.dist(a, b) == 1 for a, b in zip(path, path[1:])),
        "note": "; ".join(notes),
    }
