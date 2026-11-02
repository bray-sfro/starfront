"""Planning a twilight sweep for comets and near-Earth asteroids.

The survey works the sky near the Sun during twilight - the region the big
professional surveys largely leave alone, and where objects emerge from
conjunction.

**The geometry is Sun-relative, and that is the whole design.**  A survey region
is *not* a patch of RA and Dec.  It is a shape in (solar elongation, ecliptic
latitude), which is the same shape every night; what changes is where that shape
lands on the sky, because the Sun moves about a degree a day along the ecliptic.
Store panels as RA and Dec and the sweep is stale within a week.  Store the
region in the Sun's frame and it is transformed afresh at plan time, for
whatever date is asked for, forever.

Internally the frame is (dlambda, beta): ecliptic longitude measured *from the
Sun*, and ecliptic latitude.  Elongation follows from the two:

    cos(elongation) = cos(beta) * cos(dlambda)

Sign of dlambda picks the side of the Sun, and the two sides are worth very
different amounts:

  * **dlambda < 0 - the morning sky.**  West of the Sun, rising ahead of it.
    This is sky coming *out* of conjunction: it has been hidden for months, so
    anything new is here first.  Both 2I/Borisov and Nishimura were found in
    morning twilight.  A discovery here also stays observable long enough to
    build the follow-up arc a confirmation needs.
  * **dlambda > 0 - the evening sky.**  East of the Sun, setting after it, and
    heading *into* conjunction.  Already picked over, and anything found is
    about to be lost - but the equipment is otherwise idle and it catches a
    different population, so it is worth running at a lower priority.

Nothing here reduces images or detects anything.  It produces a list of panels
and hands them to the ordinary target list and sequencer.
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any

from . import astro, framing

SIDES = ("morning", "evening", "both")

# Two surveys, not one.  They want opposite geometry at the same clock time, so
# the mode belongs to a sweep rather than to the program.
#
#   deep  - magnitude ~20 near-Earth asteroids in the dark hours.  No extinction
#           margin at all, so the altitude floor is strict and the sky near the
#           Sun is no use; long bursts for synthetic tracking.
#   comet - magnitude 11-13 comets in the twilight glow, 20-45 degrees from the
#           Sun and as low as five degrees up.  Nine magnitudes of margin means
#           2.5 magnitudes of extinction at 5 degrees is affordable, and that
#           sliver of sky is the one the professional surveys cannot reach.
#           Nishimura was found at 23 degrees elongation and 5-8 degrees
#           altitude, through a 66 mm lens.
MODES: dict[str, dict[str, Any]] = {
    "deep": {
        "label": "Deep NEA survey",
        "elongationMin": 60.0,
        "elongationMax": 180.0,
        "betaMin": -30.0,
        "betaMax": 30.0,
        "minAltitude": 20.0,        # 15 is the hard floor
        "floorAltitude": 15.0,
        "sunHigh": -15.0,
        "sunLow": -18.0,
        "exposure": 30.0,
        "exposureCount": 36,
        "binning": 2,
        "dither": True,
        "detail": "magnitude ~20, synthetic tracking, every clear night",
    },
    "comet": {
        "label": "Twilight comet sweep",
        "elongationMin": 20.0,
        "elongationMax": 45.0,
        "betaMin": -20.0,
        "betaMax": 20.0,
        "minAltitude": 5.0,
        "floorAltitude": 5.0,
        "sunHigh": -6.0,
        "sunLow": -12.0,
        "exposure": 10.0,
        "exposureCount": 15,
        "binning": 2,
        "dither": True,
        "detail": "magnitude 11-13, frame differencing, ~20 min when the "
                  "geometry allows",
    },
}

# The Sun altitude at which Mode B viability is judged: the deepest part of its
# window, when the target zone is as high as it is going to get.
VIABILITY_SUN_ALTITUDE = -12.0

# Morning sky is worth more than evening sky, for the reasons above. Applied as
# a multiplier on the panel score so the two can be planned together and the
# morning fields still come out on top.
MORNING_WEIGHT = 1.6

# How much nearer the Sun an evening sweep has to reach before it beats a
# morning one. Morning is the better half — but only by about this much, and a
# spring evening with the ecliptic standing up beats a spring morning with it
# lying flat by far more than this.
MORNING_ELONGATION_BONUS = 5.0

# Sampling step when looking for the twilight window, in seconds. The Sun moves
# about a quarter of a degree in altitude per minute at this latitude, so a
# minute is finer than the window edges are meaningful.
_SUN_STEP = 60.0


def elongation_of(delta_longitude: float, beta: float) -> float:
    """Solar elongation of a point at (dlambda, beta), in degrees."""
    cos_e = (math.cos(math.radians(beta))
             * math.cos(math.radians(delta_longitude)))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_e))))


def twilight_window(latitude: float, longitude: float, night: dict[str, Any],
                    sun_high: float = -8.0, sun_low: float = -18.0,
                    which: str = "morning") -> dict[str, Any] | None:
    """When the Sun sits between two altitudes, evening or morning.

    This is the binding constraint on the whole survey: it is about
    three-quarters of an hour, and everything else is fitted into it.
    `sun_high` is the shallower altitude (nearer the horizon), `sun_low` the
    deeper one.
    """
    high, low = max(sun_high, sun_low), min(sun_high, sun_low)
    start = float(night.get("windowStart") or 0)
    end = float(night.get("windowEnd") or 0)
    if end <= start:
        return None

    samples: list[tuple[float, float]] = []
    when = start
    while when <= end:
        samples.append((when, astro.sun_altitude(when, latitude, longitude)))
        when += _SUN_STEP

    crossing_high = astro.crossings(samples, high)
    crossing_low = astro.crossings(samples, low)
    if which == "evening":
        # Sun descending: through the shallow altitude first, then the deep one.
        first = next((t for t, way in crossing_high if way == "down"), None)
        second = next((t for t, way in crossing_low if way == "down"), None)
    else:
        # Morning: rising back up through the deep altitude, then the shallow.
        first = next((t for t, way in crossing_low if way == "up"), None)
        second = next((t for t, way in crossing_high if way == "up"), None)
    if first is None or second is None or second <= first:
        return None
    return {"start": first, "end": second,
            "minutes": round((second - first) / 60.0, 1),
            "which": which, "sunHigh": high, "sunLow": low}


def ecliptic_tilt(latitude: float, longitude: float, timestamp: float) -> float:
    """The angle the ecliptic makes with the horizon near the Sun, in degrees.

    The number that decides whether a twilight comet sweep is possible at all.
    Steep and twenty-odd degrees along the ecliptic lifts a target well clear of
    the horizon; flat and the same twenty degrees is nearly all sideways, and
    the zone sits below the horizon at any aperture.  It is the same geometry
    that makes Mercury a spring-evening and autumn-morning object.
    """
    jd = astro.julian_from_timestamp(timestamp)
    sun_longitude = astro.sun_ecliptic_longitude(jd)
    first = _altaz_of(sun_longitude - 5.0, 0.0, jd, timestamp, latitude, longitude)
    second = _altaz_of(sun_longitude + 5.0, 0.0, jd, timestamp, latitude, longitude)
    rise = second[0] - first[0]
    # Azimuth difference, wrapped, and shrunk by the cosine of the altitude
    # because a degree of azimuth is less than a degree of arc away from the
    # horizon.
    across = ((second[1] - first[1] + 180.0) % 360.0) - 180.0
    across *= math.cos(math.radians((first[0] + second[0]) / 2.0))
    if abs(across) < 1e-9 and abs(rise) < 1e-9:
        return 0.0
    # Both magnitudes: the answer wanted is how far the line is from
    # horizontal, between 0 and 90. Signed components give the supplement half
    # the year, which reads as a steep ecliptic exactly when it is flat.
    return math.degrees(math.atan2(abs(rise), abs(across)))


def ecliptic_position_angle(longitude_deg: float, beta: float, jd: float,
                            step: float = 0.05) -> float:
    """Which way is "up the ecliptic" here, as a sky position angle.

    Degrees from celestial north through east, pointing towards increasing
    ecliptic latitude.  This is the angle the camera has to be rotated to for a
    survey grid to tile cleanly: the panels are laid out along ecliptic
    longitude and latitude, and a camera left at north-up sits skewed across
    that grid by however far the ecliptic has turned from the meridian — which
    at these latitudes is tens of degrees, and opens gaps between panels that
    were supposed to overlap.
    """
    beta = max(-89.9, min(89.9, beta))
    ra0, dec0 = astro.ecliptic_to_equatorial(longitude_deg, beta, jd)
    ra1, dec1 = astro.ecliptic_to_equatorial(longitude_deg, beta + step, jd)
    d_ra = math.radians((ra1 - ra0) * 15.0)
    dec0_r, dec1_r = math.radians(dec0), math.radians(dec1)
    y = math.sin(d_ra) * math.cos(dec1_r)
    x = (math.cos(dec0_r) * math.sin(dec1_r)
         - math.sin(dec0_r) * math.cos(dec1_r) * math.cos(d_ra))
    return math.degrees(math.atan2(y, x)) % 360.0


def _altaz_of(longitude_deg: float, beta: float, jd: float, timestamp: float,
              latitude: float, longitude: float) -> tuple[float, float]:
    ra, dec = astro.ecliptic_to_equatorial(longitude_deg, beta, jd)
    lst = astro.local_sidereal_hours(
        longitude, _dt.datetime.fromtimestamp(timestamp, _dt.timezone.utc))
    return astro.ra_dec_to_alt_az(ra, dec, lst, latitude)


def viability(latitude: float, longitude: float, night: dict[str, Any],
              which: str = "morning", floor_altitude: float = 5.0,
              sun_altitude: float = VIABILITY_SUN_ALTITUDE,
              search_to: float = 90.0) -> dict[str, Any]:
    """How close to the Sun tonight's twilight can actually reach.

    Answers the one question that decides whether a comet sweep is worth
    running: *what is the smallest solar elongation reachable at or above the
    altitude floor, with the Sun at `sun_altitude`?*  Below about 30 degrees is
    interesting; below 25 is the gap the professional surveys leave.  When the
    ecliptic is lying flat there is no answer at all, and the honest response is
    to say so rather than generate targets that are under the ground.
    """
    moment = _sun_at(latitude, longitude, night, sun_altitude, which)
    if moment is None:
        return {"viable": False, "which": which,
                "detail": f"the Sun never reaches {sun_altitude:g}° tonight"}

    jd = astro.julian_from_timestamp(moment)
    sun_longitude = astro.sun_ecliptic_longitude(jd)
    sign = -1 if which == "morning" else 1

    best: dict[str, Any] | None = None
    # Walk outwards from the Sun *along the ecliptic* and take the first
    # elongation that clears the floor.
    #
    # On the ecliptic, deliberately. Searching a band either side of it finds a
    # point that is high because it is far north, not because it is far from the
    # Sun — which returns a cheerful answer in every month of the year and hides
    # the seasonal geometry that is the entire question. What is being asked is
    # how far up the ecliptic itself is standing.
    for elongation in [e / 2.0 for e in range(20, int(search_to * 2) + 1)]:
        delta = sign * elongation
        altitude, azimuth = _altaz_of(sun_longitude + delta, 0.0, jd, moment,
                                      latitude, longitude)
        if altitude >= floor_altitude:
            best = {"elongation": round(elongation, 1),
                    "beta": 0.0,
                    "altitude": round(altitude, 1),
                    "azimuth": round(azimuth, 1)}
            break

    tilt = ecliptic_tilt(latitude, longitude, moment)
    result = {
        "which": which,
        "at": moment,
        "sunAltitude": sun_altitude,
        "floorAltitude": floor_altitude,
        "eclipticTilt": round(tilt, 1),
        "viable": best is not None,
    }
    if best is None:
        result["detail"] = (
            f"nothing within {search_to:g}° of the Sun clears {floor_altitude:g}° "
            f"— the ecliptic is only {tilt:.0f}° from the horizon")
        return result
    result.update(best)
    result["detail"] = (
        f"reaches {best['elongation']:g}° elongation at {best['altitude']:g}° "
        f"altitude; ecliptic {tilt:.0f}° from the horizon")
    return result


def _sun_at(latitude: float, longitude: float, night: dict[str, Any],
            altitude: float, which: str) -> float | None:
    """When the Sun passes an altitude, going down in the evening or up in the
    morning."""
    start = float(night.get("windowStart") or 0)
    end = float(night.get("windowEnd") or 0)
    if end <= start:
        return None
    samples = []
    when = start
    while when <= end:
        samples.append((when, astro.sun_altitude(when, latitude, longitude)))
        when += _SUN_STEP
    wanted = "down" if which == "evening" else "up"
    for moment, way in astro.crossings(samples, altitude):
        if way == wanted:
            return moment
    return None


def season(latitude: float, longitude: float, year: int,
           floor_altitude: float = 5.0, step_days: int = 5,
           sun_altitude: float = VIABILITY_SUN_ALTITUDE) -> list[dict[str, Any]]:
    """Mode B viability across a whole year, morning and evening.

    Arguably the most useful thing in the tab: it says which twenty-odd mornings
    of the year not to miss.  A comet sweep is not a scheduling problem on the
    wrong dates — the zone is under the horizon.
    """
    from . import schedule

    out: list[dict[str, Any]] = []
    day = _dt.date(year, 1, 1)
    while day.year == year:
        night = schedule.night(latitude, longitude, day)
        entry: dict[str, Any] = {"date": day.isoformat()}
        for which in ("morning", "evening"):
            found = viability(latitude, longitude, night, which, floor_altitude,
                              sun_altitude)
            entry[which] = {
                "viable": found["viable"],
                "elongation": found.get("elongation"),
                "eclipticTilt": found.get("eclipticTilt"),
            }
        out.append(entry)
        day += _dt.timedelta(days=step_days)
    return out


def cells(region: dict[str, Any], field_width: float, field_height: float,
          overlap: float = 0.08) -> list[dict[str, Any]]:
    """Tile the survey region in the Sun's frame.

    Steps in ecliptic latitude by the panel height, and in longitude by the
    panel width divided by cos(beta) so the *on-sky* spacing stays constant as
    the bands converge towards the ecliptic poles.  Cells whose elongation falls
    outside the region are dropped, which is what gives the sweep its curved
    inner and outer edges.
    """
    if field_width <= 0 or field_height <= 0:
        raise ValueError("the instrument's field of view is not known")
    if not 0.0 <= overlap < 0.9:
        raise ValueError("overlap must be at least 0 and less than 0.9")

    e_min = float(region.get("elongationMin", 30.0))
    e_max = float(region.get("elongationMax", 60.0))
    b_min = float(region.get("betaMin", -30.0))
    b_max = float(region.get("betaMax", 30.0))
    side = region.get("side", "morning")
    if side not in SIDES:
        raise ValueError(f"side must be one of {', '.join(SIDES)}")
    if e_max <= e_min or b_max <= b_min:
        raise ValueError("the survey region is empty")

    step_beta = field_height * (1.0 - overlap)
    found: list[dict[str, Any]] = []

    signs = ((-1,) if side == "morning" else (1,) if side == "evening" else (-1, 1))

    rows = max(1, int(math.ceil((b_max - b_min) / step_beta)))
    for row in range(rows):
        beta = b_min + (row + 0.5) * (b_max - b_min) / rows
        cos_beta = math.cos(math.radians(beta))
        if cos_beta < 1e-6:
            continue
        step_lambda = field_width * (1.0 - overlap) / cos_beta

        # The longitude span that lands inside the region at this latitude.
        #
        # Along a band of constant ecliptic latitude, elongation runs from
        # |beta| straight up the meridian through the Sun, to 180 - |beta| at
        # the far side.  So the asked-for elongation range has to be clipped
        # into what this band can actually reach before it is inverted — a
        # limit outside that span is not "no band", it is "the whole band on
        # that side", and treating an unreachable limit as absent collapses the
        # inner edge to zero and unpicks the tiling completely.
        lowest = abs(beta)
        highest = 180.0 - abs(beta)
        low = max(e_min, lowest)
        high = min(e_max, highest)
        if high <= low:
            continue                      # this band never enters the region

        def longitude_for(elongation: float) -> float:
            ratio = max(-1.0, min(1.0, math.cos(math.radians(elongation)) / cos_beta))
            return math.degrees(math.acos(ratio))

        inner = longitude_for(low)        # nearer the Sun, so a smaller dlambda
        outer = longitude_for(high)
        if outer - inner <= 1e-9:
            continue

        columns = max(1, int(math.ceil((outer - inner) / step_lambda)))
        for sign in signs:
            for column in range(columns):
                magnitude = inner + (column + 0.5) * (outer - inner) / columns
                delta = sign * magnitude
                elongation = elongation_of(delta, beta)
                if not (e_min - 1e-9) <= elongation <= (e_max + 1e-9):
                    continue
                found.append({
                    "dLambda": round(delta, 4),
                    "beta": round(beta, 4),
                    "elongation": round(elongation, 3),
                    "side": "morning" if sign < 0 else "evening",
                })
    return found


def _panel_at(cell: dict[str, Any], sun_longitude: float, jd: float,
              timestamp: float, latitude: float, longitude: float
              ) -> dict[str, Any]:
    """Where a Sun-relative cell actually is, at a moment."""
    lam = (sun_longitude + cell["dLambda"]) % 360.0
    ra, dec = astro.ecliptic_to_equatorial(lam, cell["beta"], jd)
    lst = astro.local_sidereal_hours(
        longitude, _dt.datetime.fromtimestamp(timestamp, _dt.timezone.utc))
    altitude, azimuth = astro.ra_dec_to_alt_az(ra, dec, lst, latitude)
    return {
        **cell,
        "ra": round(ra, 6),
        "dec": round(dec, 5),
        "lambda": round(lam, 4),
        # The camera angle this panel has to be shot at for the grid to abut.
        "rotation": round(ecliptic_position_angle(lam, cell["beta"], jd), 3),
        "altitude": round(altitude, 3),
        "azimuth": round(azimuth, 2),
        "airmass": (round(value, 3)
                    if (value := astro.airmass(altitude)) is not None else None),
    }


def _setting_rate(cell: dict[str, Any], sun_longitude: float, jd: float,
                  timestamp: float, latitude: float, longitude: float) -> float:
    """Degrees of altitude lost per minute; positive means it is going down."""
    ahead = _panel_at(cell, sun_longitude, jd, timestamp + 300.0, latitude, longitude)
    now = _panel_at(cell, sun_longitude, jd, timestamp, latitude, longitude)
    return (now["altitude"] - ahead["altitude"]) / 5.0


def plan(region: dict[str, Any], field: dict[str, float], site: dict[str, float],
         night: dict[str, Any], settings: dict[str, Any],
         observed: dict[str, float] | None = None) -> dict[str, Any]:
    """Work out tonight's sweep: which panels, in what order, and whether they fit.

    Returns every candidate panel with the reason it was kept or dropped, rather
    than silently returning the survivors — a sweep that produces four panels
    when you expected forty is a question that needs answering, and the answer
    is usually the horizon or the Moon.
    """
    latitude = float(site["latitude"])
    longitude = float(site["longitude"])
    overlap = float(settings.get("overlap", 0.08))
    minimum_altitude = float(settings.get("minAltitude", 20.0))
    maximum_airmass = float(settings.get("maxAirmass", 0) or 0)
    moon_avoidance = float(settings.get("moonAvoidance", 40.0))
    scale_moon = bool(settings.get("moonScaleByPhase", True))
    avoid_galactic = float(settings.get("galacticAvoidance", 0) or 0)
    revisit_nights = float(settings.get("revisitNights", 5) or 0)
    exposure = float(settings.get("exposure", 30.0))
    count = int(settings.get("exposureCount", 36))
    per_panel_overhead = float(settings.get("panelOverheadSeconds", 25.0))
    dither_seconds = float(settings.get("ditherSeconds", 4.0))

    sides = ([region.get("side", "morning")]
             if region.get("side") in ("morning", "evening") else ["morning", "evening"])

    grid = cells(region, field["width"], field["height"], overlap)
    seconds_per_panel = (count * exposure + max(0, count - 1) * dither_seconds
                         + per_panel_overhead)

    windows: dict[str, Any] = {}
    for which in sides:
        window = twilight_window(
            latitude, longitude, night,
            float(settings.get("sunHigh", -8.0)),
            float(settings.get("sunLow", -18.0)), which)
        if window is not None:
            windows[which] = window

    results: list[dict[str, Any]] = []
    moon_by_window: dict[str, Any] = {}
    sun_by_window: dict[str, Any] = {}
    ecliptic_by_window: dict[str, Any] = {}
    for which, window in windows.items():
        # Judge both sides at the middle of their window.
        #
        # Not at the start, which is what this used to do and which is not the
        # same instant for the two of them: an evening window opens with the Sun
        # at its shallowest and a morning window opens with it at its deepest.
        # Evening fields are setting and morning fields are rising, so starting
        # at the open judged the evening at its best and the morning at its
        # worst — which quietly penalised exactly the side of the Sun that is
        # worth the most, and made the sky look different between dusk and dawn
        # for a reason that was an artefact rather than the real geometry.
        moment = (window["start"] + window["end"]) / 2.0
        jd = astro.julian_from_timestamp(moment)
        sun_longitude = astro.sun_ecliptic_longitude(jd)
        moon_ra, moon_dec = astro.moon_position(jd)
        moon_lst = astro.local_sidereal_hours(
            longitude, _dt.datetime.fromtimestamp(moment, _dt.timezone.utc))
        moon_altitude, moon_azimuth = astro.ra_dec_to_alt_az(
            moon_ra, moon_dec, moon_lst, latitude)
        moon_up = moon_altitude > -0.5
        moon_lit = astro.moon_illumination(jd)
        # The avoidance radius is for a full Moon. Holding a thin crescent to
        # the same distance is what quietly costs a survey its best mornings:
        # a waning crescent lies *in* the twilight zone on its way to
        # conjunction, so a flat rule throws away the three or four darkest
        # mornings of every lunation. Scaled, a new Moon still keeps a third of
        # the radius, which is enough to stay out of its glare.
        moon_radius = (moon_avoidance * (0.35 + 0.65 * moon_lit)
                       if scale_moon else moon_avoidance)
        sun_ra, sun_dec = astro.sun_position(jd)
        sun_altitude, sun_azimuth = astro.ra_dec_to_alt_az(
            sun_ra, sun_dec, moon_lst, latitude)
        sun_by_window[which] = {
            "altitude": round(sun_altitude, 2),
            "azimuth": round(sun_azimuth, 2),
            "ra": round(sun_ra, 5),
            "dec": round(sun_dec, 4),
            "eclipticLongitude": round(sun_longitude, 3),
        }
        # The ecliptic itself. Carried as RA and Dec as well as alt/az, because
        # the browser re-projects it as the time scrubber moves — the angle it
        # makes with the horizon changes right through the twilight window, and
        # that swing is the thing worth watching rather than a puzzle.
        trace = []
        for lam in range(0, 360, 3):
            ra, dec = astro.ecliptic_to_equatorial(lam, 0.0, jd)
            alt, az = astro.ra_dec_to_alt_az(ra, dec, moon_lst, latitude)
            trace.append({
                "ra": round(ra, 5), "dec": round(dec, 4),
                "altitude": round(alt, 2), "azimuth": round(az, 2),
                "dLambda": round(((lam - sun_longitude + 180) % 360) - 180, 1),
            })
        ecliptic_by_window[which] = trace
        moon_state = {
            "altitude": round(moon_altitude, 2),
            "azimuth": round(moon_azimuth, 2),
            "up": moon_up,
            "illumination": round(moon_lit, 3),
            "elongation": round(astro.separation_degrees(
                *astro.sun_position(jd), moon_ra, moon_dec), 1),
            "ra": round(moon_ra, 5),
            "dec": round(moon_dec, 4),
            "avoidanceRadius": round(moon_radius, 1),
        }
        moon_by_window[which] = moon_state

        for cell in grid:
            if cell["side"] != which:
                continue
            panel = _panel_at(cell, sun_longitude, jd, moment, latitude, longitude)
            panel["window"] = which
            reasons: list[str] = []

            if panel["altitude"] < minimum_altitude:
                reasons.append(f"only {panel['altitude']:.0f}° up")
            if (maximum_airmass and panel["airmass"]
                    and panel["airmass"] > maximum_airmass):
                reasons.append(f"airmass {panel['airmass']:.1f}")

            if moon_up and moon_avoidance:
                separation = astro.separation_degrees(
                    panel["ra"], panel["dec"], moon_ra, moon_dec)
                panel["moonDistance"] = round(separation, 2)
                if separation < moon_radius:
                    reasons.append(f"{separation:.0f}° from the Moon")

            galactic = astro.galactic_latitude(panel["ra"], panel["dec"])
            panel["galacticLatitude"] = round(galactic, 2)
            if avoid_galactic and abs(galactic) < avoid_galactic:
                reasons.append(f"{abs(galactic):.0f}° from the galactic plane")

            key = cell_key(cell)
            panel["cell"] = key
            last = (observed or {}).get(key)
            if last is not None:
                nights = (moment - last) / 86400.0
                panel["lastObserved"] = last
                panel["nightsSince"] = round(nights, 2)
                if revisit_nights and nights < revisit_nights:
                    reasons.append(f"shot {nights:.1f} nights ago")

            panel["rejected"] = reasons
            panel["usable"] = not reasons
            if panel["usable"]:
                rate = _setting_rate(cell, sun_longitude, jd, moment,
                                     latitude, longitude)
                panel["settingRate"] = round(rate, 4)
                # Fields about to be lost first, then the ones lowest in the
                # sky, then the morning weighting.
                score = (max(0.0, rate) * 40.0 + max(0.0, 40.0 - panel["altitude"]))
                if which == "morning":
                    score *= MORNING_WEIGHT
                panel["score"] = round(score, 3)
            results.append(panel)

    usable = sorted((p for p in results if p["usable"]),
                    key=lambda p: -p["score"])

    # Fit them into the windows, best first, and stop when the time runs out.
    scheduled: list[dict[str, Any]] = []
    clocks = {which: window["start"] for which, window in windows.items()}
    for panel in usable:
        which = panel["window"]
        window = windows[which]
        start = clocks[which]
        if start + seconds_per_panel > window["end"]:
            panel["overruns"] = True
            continue
        panel["startAt"] = round(start, 1)
        panel["endAt"] = round(start + seconds_per_panel, 1)
        clocks[which] = start + seconds_per_panel
        scheduled.append(panel)

    # Selection is by score; the *output* is in the order the panels will be
    # shot. With both sides of the Sun in one plan those are two runs hours
    # apart, so they are numbered within their own window rather than
    # interleaved — a list that jumps between dusk and dawn is unreadable, and
    # the panel numbers have to match what the sequencer will do.
    scheduled.sort(key=lambda p: (p["window"] != "evening", p["startAt"]))
    counters: dict[str, int] = {}
    for panel in scheduled:
        which = panel["window"]
        counters[which] = counters.get(which, 0) + 1
        panel["index"] = counters[which]

    capacity = {
        which: int((window["end"] - window["start"]) // seconds_per_panel)
        for which, window in windows.items()
    }

    # What the camera has to do to make the grid abut, and what it costs if it
    # cannot. The panels are laid out along ecliptic longitude and latitude, so
    # each wants its own sky position angle; a fixed camera is skewed across the
    # grid and the panels no longer meet where they were supposed to.
    angles = [p["rotation"] for p in scheduled if p.get("rotation") is not None]
    rotation: dict[str, Any] = {"required": bool(angles)}
    if angles:
        lowest, highest = min(angles), max(angles)
        # Angles wrap, so measure the spread the short way round.
        spread = (highest - lowest) if (highest - lowest) <= 180 else (
            360 - (highest - lowest))
        fixed = settings.get("cameraAngle")
        rotation.update({
            "min": round(lowest, 1), "max": round(highest, 1),
            "spread": round(spread, 1),
            "hasRotator": bool(settings.get("hasRotator")),
        })
        if not settings.get("hasRotator") and fixed is not None:
            worst = max(_angle_between(a, float(fixed)) for a in angles)
            rotation["fixedAngle"] = round(float(fixed), 1)
            rotation["worstMismatch"] = round(worst, 1)
            # A rectangle turned by `worst` still covers a smaller upright
            # rectangle; below this the promised overlap is gone.
            keeps = max(0.0, math.cos(math.radians(worst)))
            rotation["effectiveOverlap"] = round(
                1.0 - (1.0 - overlap) / max(keeps, 1e-6), 3) if keeps else None
            rotation["gaps"] = keeps <= (1.0 - overlap)
    return {
        "panels": scheduled,
        "candidates": results,
        "windows": windows,
        # Where the observer is, so the browser can work out alt/az for any
        # moment in the window rather than only the one this was judged at.
        "site": {"latitude": latitude, "longitude": longitude},
        "secondsPerPanel": round(seconds_per_panel, 1),
        "capacity": capacity,
        "counts": {
            "generated": len(grid),
            "considered": len(results),
            "usable": len(usable),
            "scheduled": len(scheduled),
            "dropped": len(results) - len(usable),
        },
        "field": dict(field),
        "rotation": rotation,
        "moon": moon_by_window,
        "sun": sun_by_window,
        "ecliptic": ecliptic_by_window,
    }


def optimise(latitude: float, longitude: float, night: dict[str, Any],
             field: dict[str, float], base: dict[str, Any],
             observed: dict[str, float] | None = None,
             only: str | None = None) -> dict[str, Any]:
    """Work out the best sweep for one night, and say why.

    There are a dozen knobs in this tab and only a couple of them are really
    free on any given night: the geometry decides most of it.  This settles
    them in the order the sky does.

      1. **Can the comet zone be reached at all?**  That is a seasonal
         question, and on the wrong dates the answer is no at any aperture.
         When it can be reached, it wins: the window is twenty minutes, it is
         the only route to a bright comet, and unlike the deep survey it cannot
         be made up on another night.
      2. **Which side of the Sun.**  Morning if it is viable — it is fresh sky
         coming out of conjunction, and a find there stays observable long
         enough to confirm.
      3. **How near the Sun to start.**  Not the textbook 20 degrees but what
         tonight actually reaches, because panels below the horizon are not a
         plan.
      4. **How long a burst.**  The mode's own figures, unless the window is
         too short to hold even two fields, in which case shorten the burst
         rather than shoot one field and go to bed.
    """
    reasons: list[str] = []
    options: list[dict[str, Any]] = []

    # Asking for the best *evening* sweep is a perfectly reasonable question —
    # the equipment is there, and evening catches a different population — so a
    # named side narrows the search rather than being overruled by the general
    # preference for morning.
    sides = ((only,) if only in ("morning", "evening") else ("morning", "evening"))
    for which in sides:
        window = twilight_window(latitude, longitude, night,
                                 MODES["comet"]["sunHigh"],
                                 MODES["comet"]["sunLow"], which)
        comet_reach = viability(latitude, longitude, night, which,
                                MODES["comet"]["floorAltitude"],
                                VIABILITY_SUN_ALTITUDE)
        deep_window = twilight_window(latitude, longitude, night,
                                      MODES["deep"]["sunHigh"],
                                      MODES["deep"]["sunLow"], which)

        # The comet zone is worth having whenever it reaches inside about 35
        # degrees; past that it is no longer the gap the big surveys leave.
        if window and comet_reach.get("viable") and comet_reach["elongation"] <= 35.0:
            mode = "comet"
            reach = comet_reach
            chosen_window = window
        elif deep_window:
            mode = "deep"
            reach = viability(latitude, longitude, night, which,
                              MODES["deep"]["floorAltitude"], -15.0)
            chosen_window = deep_window
        else:
            continue

        settings = {**base, **MODES[mode]}
        settings.pop("label", None)
        settings.pop("detail", None)
        settings.pop("floorAltitude", None)
        settings["side"] = which

        if mode == "comet":
            # Start where the sky actually is, not where the textbook says.
            start = max(MODES["comet"]["elongationMin"],
                        math.floor(reach["elongation"]))
            settings["elongationMin"] = float(start)
            settings["elongationMax"] = float(
                max(start + 15.0, MODES["comet"]["elongationMax"]))

        # Fit the burst to the window: one field is not a survey.
        seconds = float(chosen_window["end"] - chosen_window["start"])
        floor_frames = 11        # what the detection stage needs at all
        count = int(settings["exposureCount"])
        exposure = float(settings["exposure"])
        dither = float(settings.get("ditherSeconds", 4.0))
        overhead = float(settings.get("panelOverheadSeconds", 25.0))

        def per_panel(frames: int) -> float:
            return frames * exposure + max(0, frames - 1) * dither + overhead

        trimmed = False
        while count > floor_frames and seconds / per_panel(count) < 2.0:
            count -= 1
            trimmed = True
        settings["exposureCount"] = count
        fields = int(seconds // per_panel(count))

        # Morning is worth more — fresh sky out of conjunction, and a find
        # there lasts long enough to confirm — but it is a thumb on the scale,
        # not a veto. In spring the morning ecliptic lies flat and the evening
        # stands up; insisting on morning then would send the sweep to the
        # worse half of the sky for the sake of a rule of thumb.
        effective = (reach.get("elongation") or 999.0)
        if which == "morning":
            effective -= MORNING_ELONGATION_BONUS

        options.append({
            "which": which, "mode": mode, "settings": settings,
            "reach": reach, "window": chosen_window, "fields": fields,
            "trimmed": trimmed,
            "rank": ((0 if mode == "comet" else 1), effective, -fields),
        })

    if not options:
        return {"ok": False,
                "detail": (f"there is no usable {only} twilight window tonight"
                           if only else
                           "there is no usable twilight window tonight")}

    options.sort(key=lambda o: o["rank"])
    best = options[0]
    settings = best["settings"]
    mode_label = MODES[best["mode"]]["label"]

    reasons.append(
        f"{mode_label} on the {best['which']} side."
        if best["mode"] == "comet" else
        f"{mode_label}: the comet zone is not reachable tonight, so the "
        f"window is better spent on deep panels.")
    if best["reach"].get("viable"):
        reasons.append(
            f"Tonight's {best['which']} twilight reaches "
            f"{best['reach']['elongation']:g}° from the Sun at "
            f"{best['reach']['altitude']:g}° up, with the ecliptic "
            f"{best['reach']['eclipticTilt']:g}° from horizontal.")
    if best["mode"] == "comet":
        reasons.append(
            f"Sweeping {settings['elongationMin']:g}–{settings['elongationMax']:g}° "
            f"down to {settings['minAltitude']:g}°, which is where the "
            f"professional surveys stop looking.")
    if best["trimmed"]:
        reasons.append(
            f"Shortened the burst to {settings['exposureCount']} frames so more "
            f"than one field fits the {best['window']['minutes']:.0f}-minute window.")
    reasons.append(
        f"About {best['fields']} field(s) fit, at "
        f"{settings['exposureCount']}×{settings['exposure']:g}s each.")

    other = [o for o in options if o is not best]
    return {
        "ok": True,
        "mode": best["mode"],
        "side": best["which"],
        "settings": settings,
        "fields": best["fields"],
        "reasons": reasons,
        "alternatives": [
            {"which": o["which"], "mode": o["mode"], "fields": o["fields"],
             "elongation": o["reach"].get("elongation")} for o in other],
    }


def _angle_between(a: float, b: float) -> float:
    """The short way round between two position angles, in degrees.

    A camera at 350 degrees and a grid wanting 10 is twenty degrees out, not
    three hundred and forty.
    """
    return abs(((a - b + 180.0) % 360.0) - 180.0)


def cell_key(cell: dict[str, Any], resolution: float = 2.0) -> str:
    """A stable name for a patch of the Sun-relative sky.

    Coverage history has to be kept in this frame, not in RA and Dec: "have I
    shot this bit of the twilight zone recently" is a question about the Sun's
    neighbourhood, and the same patch of it is a different patch of sky every
    night.
    """
    lam = int(round(cell["dLambda"] / resolution))
    beta = int(round(cell["beta"] / resolution))
    return f"{lam:+d}_{beta:+d}"


def summarise(result: dict[str, Any]) -> str:
    """One line for the log."""
    counts = result["counts"]
    windows = ", ".join(
        f"{which} {window['minutes']:.0f} min" for which, window in
        result["windows"].items()) or "no twilight window"
    return (f"{counts['scheduled']} panels scheduled of {counts['usable']} usable "
            f"({counts['generated']} in the region); {windows}")
