"""Spherical astronomy helpers.

Small, dependency-free versions of the handful of conversions the mount, the
planetarium and the plate solver all need.  Angles follow the conventions in
`devices.base`: RA in hours, everything else in degrees.
"""

from __future__ import annotations

import datetime as _dt
import math


def julian_date(when: _dt.datetime | None = None) -> float:
    """Julian date for a UTC datetime (now, if none is given)."""
    when = when or _dt.datetime.now(_dt.timezone.utc)
    if when.tzinfo is not None:
        when = when.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    a = (14 - when.month) // 12
    y = when.year + 4800 - a
    m = when.month + 12 * a - 3
    day_number = (when.day + (153 * m + 2) // 5 + 365 * y
                  + y // 4 - y // 100 + y // 400 - 32045)
    fraction = (when.hour - 12) / 24 + when.minute / 1440 + when.second / 86400
    return day_number + fraction


def gmst_degrees(jd: float) -> float:
    """Greenwich mean sidereal time in degrees."""
    t = (jd - 2451545.0) / 36525.0
    value = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
             + 0.000387933 * t * t - t * t * t / 38710000.0)
    return value % 360.0


def local_sidereal_hours(longitude_deg: float, when: _dt.datetime | None = None) -> float:
    """Local sidereal time in hours.  Longitude is degrees east of Greenwich."""
    return ((gmst_degrees(julian_date(when)) + longitude_deg) % 360.0) / 15.0


def ra_dec_to_alt_az(ra_hours: float, dec_deg: float, lst_hours: float,
                     latitude_deg: float) -> tuple[float, float]:
    """Altitude and azimuth in degrees; azimuth is measured east of north."""
    hour_angle = math.radians((lst_hours - ra_hours) * 15.0)
    dec = math.radians(dec_deg)
    latitude = math.radians(latitude_deg)

    sin_alt = (math.sin(dec) * math.sin(latitude)
               + math.cos(dec) * math.cos(latitude) * math.cos(hour_angle))
    altitude = math.asin(max(-1.0, min(1.0, sin_alt)))
    cos_alt = math.cos(altitude)
    if cos_alt < 1e-9:                       # at the zenith azimuth is undefined
        return math.degrees(altitude), 0.0

    sin_az = -math.cos(dec) * math.sin(hour_angle) / cos_alt
    cos_az = ((math.sin(dec) - math.sin(altitude) * math.sin(latitude))
              / (cos_alt * math.cos(latitude)))
    azimuth = math.degrees(math.atan2(sin_az, cos_az)) % 360.0
    return math.degrees(altitude), azimuth


def alt_az_to_ra_dec(altitude_deg: float, azimuth_deg: float, lst_hours: float,
                     latitude_deg: float) -> tuple[float, float]:
    """The inverse of `ra_dec_to_alt_az`: RA in hours, Dec in degrees.

    Sky flats are the reason this exists.  The place to point for them is
    described in the horizon frame — the zenith, or the anti-solar point at
    seventy-odd degrees — but a mount is told where to go in RA and Dec, and
    the answer changes minute by minute as the sky turns.
    """
    altitude = math.radians(altitude_deg)
    azimuth = math.radians(azimuth_deg)
    latitude = math.radians(latitude_deg)

    sin_dec = (math.sin(altitude) * math.sin(latitude)
               + math.cos(altitude) * math.cos(latitude) * math.cos(azimuth))
    dec = math.asin(max(-1.0, min(1.0, sin_dec)))
    cos_dec = math.cos(dec)
    if abs(cos_dec) < 1e-9:                  # at the pole RA is undefined
        return normalise_ra_hours(lst_hours), math.degrees(dec)

    sin_ha = -math.sin(azimuth) * math.cos(altitude) / cos_dec
    cos_ha = ((math.sin(altitude) - math.sin(latitude) * sin_dec)
              / (math.cos(latitude) * cos_dec))
    hour_angle = math.degrees(math.atan2(sin_ha, cos_ha)) / 15.0
    return normalise_ra_hours(lst_hours - hour_angle), math.degrees(dec)


def separation_degrees(ra1_hours: float, dec1_deg: float,
                       ra2_hours: float, dec2_deg: float) -> float:
    """Angular distance between two positions, via the haversine formula.

    The usual `acos` form loses all its precision for the small separations
    plate-solve centring cares about, which is exactly where it is used.
    """
    ra1, ra2 = math.radians(ra1_hours * 15.0), math.radians(ra2_hours * 15.0)
    dec1, dec2 = math.radians(dec1_deg), math.radians(dec2_deg)
    d_ra, d_dec = ra2 - ra1, dec2 - dec1
    h = (math.sin(d_dec / 2) ** 2
         + math.cos(dec1) * math.cos(dec2) * math.sin(d_ra / 2) ** 2)
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(h))))


def normalise_ra_hours(ra_hours: float) -> float:
    return ((ra_hours % 24.0) + 24.0) % 24.0


def same_half_turn(angle: float, reference: float) -> float:
    """`angle` or `angle + 180`, whichever lies within a quarter turn of `reference`.

    A camera is a rectangle, and a rectangle turned half a circle covers the
    same sky, so a plate solver is free to report 95 degrees for a camera the
    settings call 275. Comparing those two as a 180-degree disagreement, or
    laying a mosaic out again at the "new" angle, moves every panel number
    to a different patch of sky for no reason. Anything that reasons about
    the camera's angle without a rotator brings it to the layout's half-turn
    first. The result is in [0, 360).
    """
    angle = float(angle) % 360.0
    reference = float(reference) % 360.0
    gap = ((angle - reference + 180.0) % 360.0) - 180.0
    if abs(gap) > 90.0:
        angle = (angle + 180.0) % 360.0
    return angle


def julian_from_timestamp(timestamp: float) -> float:
    """Julian date from a Unix timestamp."""
    return timestamp / 86400.0 + 2440587.5


def sun_position(jd: float) -> tuple[float, float]:
    """Low-precision solar RA (hours) and declination (degrees).

    Good to about an arcminute, which is far better than sunset timing needs.
    """
    n = jd - 2451545.0
    mean_longitude = (280.460 + 0.9856474 * n) % 360.0
    anomaly = math.radians((357.528 + 0.9856003 * n) % 360.0)
    ecliptic_longitude = math.radians(
        mean_longitude + 1.915 * math.sin(anomaly) + 0.020 * math.sin(2 * anomaly))
    obliquity = math.radians(23.4393 - 3.563e-7 * n)

    ra = math.atan2(math.cos(obliquity) * math.sin(ecliptic_longitude),
                    math.cos(ecliptic_longitude))
    dec = math.asin(math.sin(obliquity) * math.sin(ecliptic_longitude))
    return normalise_ra_hours(math.degrees(ra) / 15.0), math.degrees(dec)


def obliquity_degrees(jd: float) -> float:
    """Mean obliquity of the ecliptic."""
    return 23.4393 - 3.563e-7 * (jd - 2451545.0)


def sun_ecliptic_longitude(jd: float) -> float:
    """The Sun's apparent ecliptic longitude in degrees.

    The anchor for the whole solar system survey: its region is defined
    relative to this, not to any fixed point on the sky.
    """
    n = jd - 2451545.0
    mean_longitude = (280.460 + 0.9856474 * n) % 360.0
    anomaly = math.radians((357.528 + 0.9856003 * n) % 360.0)
    return (mean_longitude + 1.915 * math.sin(anomaly)
            + 0.020 * math.sin(2 * anomaly)) % 360.0


def ecliptic_to_equatorial(longitude_deg: float, latitude_deg: float,
                           jd: float) -> tuple[float, float]:
    """Ecliptic (lambda, beta) to equatorial RA in hours and Dec in degrees."""
    obliquity = math.radians(obliquity_degrees(jd))
    lam = math.radians(longitude_deg)
    beta = math.radians(latitude_deg)

    sin_dec = (math.sin(beta) * math.cos(obliquity)
               + math.cos(beta) * math.sin(obliquity) * math.sin(lam))
    dec = math.asin(max(-1.0, min(1.0, sin_dec)))
    y = (math.sin(lam) * math.cos(obliquity)
         - math.tan(beta) * math.sin(obliquity))
    ra = math.atan2(y, math.cos(lam))
    return normalise_ra_hours(math.degrees(ra) / 15.0), math.degrees(dec)


def equatorial_to_ecliptic(ra_hours: float, dec_deg: float,
                           jd: float) -> tuple[float, float]:
    """Equatorial to ecliptic (lambda, beta), both in degrees."""
    obliquity = math.radians(obliquity_degrees(jd))
    ra = math.radians(ra_hours * 15.0)
    dec = math.radians(dec_deg)

    sin_beta = (math.sin(dec) * math.cos(obliquity)
                - math.cos(dec) * math.sin(obliquity) * math.sin(ra))
    beta = math.asin(max(-1.0, min(1.0, sin_beta)))
    y = (math.sin(ra) * math.cos(obliquity)
         + math.tan(dec) * math.sin(obliquity))
    lam = math.atan2(y, math.cos(ra))
    return math.degrees(lam) % 360.0, math.degrees(beta)


def moon_position(jd: float) -> tuple[float, float]:
    """Low-precision lunar RA (hours) and declination (degrees).

    The main terms only, good to a fraction of a degree.  That is far inside
    the tolerance of a forty-degree avoidance radius, which is the only thing
    it is used for.
    """
    t = (jd - 2451545.0) / 36525.0
    longitude = (218.316 + 481267.8813 * t) % 360.0
    anomaly = math.radians((134.963 + 477198.8676 * t) % 360.0)
    node = math.radians((93.272 + 483202.0175 * t) % 360.0)
    # Evection and the other large periodic terms.
    lam = math.radians(longitude + 6.289 * math.sin(anomaly))
    beta = math.radians(5.128 * math.sin(node))
    return ecliptic_to_equatorial(math.degrees(lam), math.degrees(beta), jd)


def moon_illumination(jd: float) -> float:
    """Fraction of the Moon's disc that is lit, 0 to 1."""
    sun_ra, sun_dec = sun_position(jd)
    moon_ra, moon_dec = moon_position(jd)
    elongation = math.radians(separation_degrees(sun_ra, sun_dec, moon_ra, moon_dec))
    return (1.0 - math.cos(elongation)) / 2.0


# The galactic pole and node, J2000, for the Milky Way exclusion.
_GALACTIC_POLE_RA = math.radians(192.85948)
_GALACTIC_POLE_DEC = math.radians(27.12825)


def galactic_latitude(ra_hours: float, dec_deg: float) -> float:
    """Degrees from the galactic plane.

    Crowded fields are the enemy of moving-object detection, so the survey
    needs to know how close to the Milky Way a panel sits.
    """
    ra = math.radians(ra_hours * 15.0)
    dec = math.radians(dec_deg)
    sin_b = (math.sin(dec) * math.sin(_GALACTIC_POLE_DEC)
             + math.cos(dec) * math.cos(_GALACTIC_POLE_DEC)
             * math.cos(ra - _GALACTIC_POLE_RA))
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_b))))


def airmass(altitude_deg: float) -> float | None:
    """Airmass at an altitude, by Pickering's formula.

    The plane-parallel `sec z` is wrong by 10% at 20 degrees and hopeless
    below that, which is precisely the band a twilight survey works in.
    None below the horizon.
    """
    if altitude_deg <= 0:
        return None
    h = math.radians(altitude_deg + 244.0 / (165.0 + 47.0 * altitude_deg ** 1.1))
    return 1.0 / math.sin(h)


def altitude_at(ra_hours: float, dec_deg: float, timestamp: float,
                latitude: float, longitude: float) -> float:
    """Altitude of a fixed position at a moment, in degrees."""
    lst = local_sidereal_hours(
        longitude, _dt.datetime.fromtimestamp(timestamp, _dt.timezone.utc))
    return ra_dec_to_alt_az(ra_hours, dec_deg, lst, latitude)[0]


def sun_altitude(timestamp: float, latitude: float, longitude: float) -> float:
    ra, dec = sun_position(julian_from_timestamp(timestamp))
    return altitude_at(ra, dec, timestamp, latitude, longitude)


def crossings(samples: list[tuple[float, float]], level: float) -> list[tuple[float, str]]:
    """Where a sampled curve crosses `level`, by linear interpolation.

    Returns (timestamp, "down"|"up") pairs. Sampling and interpolating is used
    in preference to solving the transcendental equation: it cannot converge to
    the wrong root, and at a one-minute step it is accurate to a few seconds.
    """
    found = []
    for (t0, v0), (t1, v1) in zip(samples, samples[1:]):
        if (v0 - level) == 0.0:
            continue
        if (v0 - level) * (v1 - level) < 0:
            fraction = (level - v0) / (v1 - v0)
            found.append((t0 + fraction * (t1 - t0), "down" if v1 < v0 else "up"))
    return found
