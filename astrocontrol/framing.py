"""Where a camera actually points, and where the panels of a mosaic go.

Two jobs:

  * turn a sensor and a focal length into an angular field, and
  * lay a grid of panels out around a centre at a given position angle.

The panel maths is done properly rather than by adding degrees to RA and Dec.
Offsets live in the tangent plane (a gnomonic, TAN, projection — the same one
the camera itself forms) and are deprojected back onto the sphere.  The naive
version is fine near the equator and visibly wrong for a mosaic at high
declination, which is exactly where people build them.

Position angle follows the usual convention: degrees from north through east,
describing where the camera's "up" axis points.
"""

from __future__ import annotations

import math
from typing import Any

# Arcseconds subtended by one micron at one millimetre: 206264.8 / 1000.
ARCSEC_PER_MICRON_MM = 206.2648


def field_of_view(sensor_width_px: int, sensor_height_px: int, pixel_size_um: float,
                  focal_length_mm: float) -> dict[str, float]:
    """Angular field of a sensor, in degrees, plus the pixel scale."""
    if not all((sensor_width_px, sensor_height_px, pixel_size_um, focal_length_mm)):
        raise ValueError("sensor size, pixel size and focal length are all required")
    arcsec_per_pixel = ARCSEC_PER_MICRON_MM * pixel_size_um / focal_length_mm
    return {
        "scale": arcsec_per_pixel,
        "width": arcsec_per_pixel * sensor_width_px / 3600.0,
        "height": arcsec_per_pixel * sensor_height_px / 3600.0,
    }


def offset_to_sky(ra0_deg: float, dec0_deg: float,
                  east_deg: float, north_deg: float) -> tuple[float, float]:
    """Deproject a tangent-plane offset back onto the sphere.

    `east_deg` and `north_deg` are standard coordinates on the plane tangent at
    (ra0, dec0), positive towards east and north.
    """
    ra0 = math.radians(ra0_deg)
    dec0 = math.radians(dec0_deg)
    xi = math.radians(east_deg)
    eta = math.radians(north_deg)

    denominator = math.cos(dec0) - eta * math.sin(dec0)
    ra = ra0 + math.atan2(xi, denominator)
    dec = math.atan2(math.sin(dec0) + eta * math.cos(dec0),
                     math.hypot(xi, denominator))
    return (math.degrees(ra) % 360.0, math.degrees(dec))


def sky_to_offset(ra0_deg: float, dec0_deg: float,
                  ra_deg: float, dec_deg: float) -> tuple[float, float]:
    """The inverse of `offset_to_sky`: sky position to tangent-plane offset."""
    ra0, dec0 = math.radians(ra0_deg), math.radians(dec0_deg)
    ra, dec = math.radians(ra_deg), math.radians(dec_deg)
    d_ra = ra - ra0

    denominator = (math.sin(dec) * math.sin(dec0)
                   + math.cos(dec) * math.cos(dec0) * math.cos(d_ra))
    if denominator <= 0:
        raise ValueError("position is more than 90 degrees from the tangent point")
    xi = math.cos(dec) * math.sin(d_ra) / denominator
    eta = ((math.sin(dec) * math.cos(dec0)
            - math.cos(dec) * math.sin(dec0) * math.cos(d_ra)) / denominator)
    return math.degrees(xi), math.degrees(eta)


def north_angle(ra0_deg: float, dec0_deg: float, ra_deg: float, dec_deg: float,
                epsilon: float = 1e-4) -> float:
    """Which way north points at (ra, dec), within the tangent plane at (ra0, dec0).

    Zero at the tangent point, growing towards the poles: meridians converge, so
    a camera held at a fixed position angle is *not* parallel to itself across a
    wide field.  Measured in degrees from the plane's north axis towards east.

    Done numerically by projecting a short step north, which is exact enough
    (the step is 0.36 arcseconds) and cannot be got subtly wrong the way the
    closed form can.
    """
    step = epsilon if dec_deg + epsilon <= 90.0 else -epsilon
    xi0, eta0 = sky_to_offset(ra0_deg, dec0_deg, ra_deg, dec_deg)
    xi1, eta1 = sky_to_offset(ra0_deg, dec0_deg, ra_deg, dec_deg + step)
    if step < 0:
        xi1, eta1 = 2 * xi0 - xi1, 2 * eta0 - eta1
    return math.degrees(math.atan2(xi1 - xi0, eta1 - eta0))


def rotate(x: float, y: float, position_angle_deg: float) -> tuple[float, float]:
    """Camera-frame offset to sky offset (east, north) at a position angle.

    `x` is to the right in the image, `y` is up.  At PA 0 the camera's up axis
    points north, and — because a north-up image puts east on the *left* — its
    right axis points west.  So the two basis vectors, written (east, north),
    are:

        up    = ( sin PA,  cos PA)
        right = (-cos PA,  sin PA)

    which is what the terms below are.  Increasing PA sweeps from north towards
    east, which is anticlockwise on screen, and the whole grid must turn with
    it: at PA 90 the panel that was above the centre has to end up to its east,
    drawn on the left.  Getting the sign of the `x` term wrong mirrors the grid,
    and a mirrored grid appears to counter-rotate as the slider moves while each
    individual frame still turns the right way.
    """
    angle = math.radians(position_angle_deg)
    cos, sin = math.cos(angle), math.sin(angle)
    east = -x * cos + y * sin
    north = x * sin + y * cos
    return east, north


def mosaic_panels(ra_deg: float, dec_deg: float, panel_width: float, panel_height: float,
                  rows: int = 1, columns: int = 1, overlap: float = 0.1,
                  position_angle: float = 0.0,
                  align: str = "aligned") -> list[dict[str, Any]]:
    """Panel centres and angles for a mosaic, ordered in a boustrophedon.

    `overlap` is the fraction of a panel shared with its neighbour, so 0.1 steps
    by 90% of the panel each time.  Rows alternate direction, which keeps the
    slew between consecutive panels short.

    `align` decides what happens to the camera angle across the grid, and the
    two answers are genuinely different pictures:

      * ``aligned`` (the default) - each panel's angle is corrected by the local
        convergence, so the frames stay parallel, the composite stays a
        rectangle and the overlap is exactly what was asked for at any position
        angle or declination.  This is what a mosaic is supposed to be, and it
        needs a rotator that moves between panels.
      * ``fixed`` - the rotator does not move.  Every panel is shot at the same
        position angle *measured from its own north*, and because meridians
        converge the frames are not parallel to each other: the overlap decays
        with declination and eventually goes negative, tearing holes in the
        mosaic.  Honest, but only worth choosing when the rotator cannot move.

    Each panel carries `rotation`, the sky position angle to shoot it at, and
    `convergence`, how far north has turned at that panel.
    """
    if rows < 1 or columns < 1:
        raise ValueError("a mosaic needs at least one row and one column")
    if not 0.0 <= overlap < 0.9:
        raise ValueError("overlap must be at least 0 and less than 0.9")
    if align not in ("fixed", "aligned"):
        raise ValueError("align must be 'fixed' or 'aligned'")

    step_x = panel_width * (1.0 - overlap)
    step_y = panel_height * (1.0 - overlap)

    panels: list[dict[str, Any]] = []
    for row in range(rows):
        order = range(columns) if row % 2 == 0 else reversed(range(columns))
        for column in order:
            # Centre the grid on the target: with three columns the offsets are
            # -step, 0, +step; with two they are -step/2, +step/2.
            x = (column - (columns - 1) / 2.0) * step_x
            y = ((rows - 1) / 2.0 - row) * step_y
            east, north = rotate(x, y, position_angle)
            panel_ra, panel_dec = offset_to_sky(ra_deg, dec_deg, east, north)

            convergence = north_angle(ra_deg, dec_deg, panel_ra, panel_dec)
            # Keeping the frames parallel in the plane means shooting each one
            # at an angle that undoes its own convergence.
            rotation = position_angle - convergence if align == "aligned" else position_angle

            panels.append({
                "row": row,
                "column": column,
                "ra": round(panel_ra / 15.0, 6),      # hours, as the API uses
                "dec": round(panel_dec, 5),
                "rotation": round(rotation % 360.0, 3),
                "convergence": round(convergence, 4),
                "offsetEast": round(east, 5),
                "offsetNorth": round(north, 5),
            })
    return panels


def mosaic_seams(panels: list[dict[str, Any]], panel_width: float,
                 panel_height: float, overlap: float) -> dict[str, Any]:
    """How well the panels actually meet.

    With `align="fixed"` the frames are rotated relative to one another, which
    pulls their corners away from where a flat grid would put them.  Once that
    displacement exceeds the overlap the mosaic has holes in it, and the planner
    needs to say so rather than draw a tidy lie.
    """
    if not panels:
        return {"spread": 0.0, "cornerError": 0.0, "margin": 0.0, "gaps": False}

    angles = [panel["convergence"] for panel in panels]
    spread = max(angles) - min(angles)
    half_diagonal = math.hypot(panel_width, panel_height) / 2.0
    corner_error = abs(half_diagonal * math.sin(math.radians(spread)))
    margin = min(panel_width, panel_height) * overlap
    return {
        "spread": round(spread, 3),
        "cornerError": round(corner_error, 5),
        "margin": round(margin, 5),
        "gaps": corner_error > margin,
    }


def mosaic_extent(panel_width: float, panel_height: float, rows: int, columns: int,
                  overlap: float) -> dict[str, float]:
    """Total angular size a mosaic covers, in degrees."""
    step_x = panel_width * (1.0 - overlap)
    step_y = panel_height * (1.0 - overlap)
    return {
        "width": round(step_x * (columns - 1) + panel_width, 6),
        "height": round(step_y * (rows - 1) + panel_height, 6),
    }
