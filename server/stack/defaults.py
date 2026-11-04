"""What a hub-backed stacking project starts with, taken from the Collab contract.

Rapid Stacker Collab's `openapi.yaml` is "the one place" the policy defaults are
written (its `records.md` says so), so the values below are copies, not
decisions: a project bound to a hub must judge frames by the same rules a
project on the cloud service would, or the same night is accepted on one and
quarantined on the other. `tools/check_server.py` compares them with that file
whenever it sits beside this repository, so a contract change that moves a
default fails a check here instead of silently splitting the two.

Stdlib only.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

#: `components/schemas/Policy`, every key at its `default:`.
POLICY: dict[str, Any] = {
    "calibration": "preferred",
    "osc_allowed": False,
    "osc_mode": "rgb_channels",
    "merge_osc_channels": False,
    "merge_bandwidths": False,
    "min_exposure_s": 5,
    "accept_scale_min": 0.4,
    "accept_scale_max": 2.5,
    "max_fwhm_arcsec": 6,
    "fwhm_median_factor": 2.5,
    "max_eccentricity": 0.9,
    "min_star_fraction": 0.3,
    "require_coordinate_strip": False,
    "live_clip_k": 3,
    "live_n_min": 5,
    "gold_nightly": True,
    "gold_nightly_hour_utc": 12,
    "gold_threshold_frames": 50,
    "gold_threshold_growth_pct": 20,
    "gold_exclude_classes": ["poor_fit"],
    "require_approval": False,
    "retain_raw": False,
}

#: `records.md`, `collab-projects.quotas`: 60 GB, 2000 frames, 2 TB.
QUOTAS: dict[str, int] = {
    "contributor_max_bytes": 60 * 1000 ** 3,
    "contributor_max_frames": 2000,
    "project_max_bytes": 2 * 1000 ** 4,
}

#: `components/schemas/CanonicalFilter`.
CANONICAL_FILTERS = ("L", "R", "G", "B", "HA", "OIII", "SII", "HB", "NII", "CLEAR",
                     "DUO", "OSC", "IR", "UV", "U", "V", "I", "Z", "Y")

#: A Starfront collaboration names its channels with the letters
#: `astrocontrol.filters` uses; the contract spells the lines out.
_CHANNEL = {"L": "L", "R": "R", "G": "G", "B": "B", "H": "HA", "O": "OIII", "S": "SII"}

#: Where a placeholder grid's pixel size is kept: the contract refuses a grid an
#: owner sets above 12 000 px a side and a scale outside 0.1 to 20 arcsec/px.
GRID_MAX_PX = 12000
GRID_MIN_PX = 64
GRID_MIN_SCALE = 2.0
#: The contract's finest scale for any grid (a placeholder keeps to `GRID_MIN_SCALE`).
GRID_MIN_SCALE_ANY = 0.1
GRID_MAX_SCALE = 20.0


def contract_filters(names: Iterable[str]) -> list[str]:
    """A collaboration's filter names as the contract's canonical filters.

    "H" -> "HA"; a one-shot colour camera's broadband band ("OSC-RGB") ->
    "OSC", which is what the contract resolves an unfiltered (or L/clear)
    Bayer frame to; its narrowband band ("OSC-Ha+OIII") -> "DUO", because a
    Bayer frame behind a dual-band filter resolves to the filter, "DUO", and
    an undeclared one is quarantined (`filter_not_in_project`; T706, until
    then "OSC", which declared nothing that frame resolves to). A filter
    passing two lines at once ("Ha+OIII", "Dual") -> "DUO"; a name the
    contract has no word for is left out rather than invented. Whether Bayer
    frames are taken at all is the policy's `osc_allowed`, not a filter.
    """
    found: list[str] = []
    for name in names:
        text = str(name or "").strip()
        key = text.upper()
        if text in _CHANNEL:
            mapped = _CHANNEL[text]
        elif key.startswith("OSC"):
            mapped = "DUO" if "+" in text else "OSC"
        elif "+" in text or key in ("DUAL", "DUO"):
            mapped = "DUO"
        elif key in CANONICAL_FILTERS:
            mapped = key
        else:
            continue
        if mapped not in found:
            found.append(mapped)
    return found


def palette_for(filters: Iterable[str]) -> str:
    """The contract's palette for a set of canonical filters."""
    have = set(filters)
    if {"HA", "OIII", "SII"} <= have:
        return "SHO"
    if {"HA", "OIII"} <= have:
        return "HOO"
    if {"R", "G", "B"} <= have:
        return "LRGB" if "L" in have else "RGB"
    return "none"


def placeholder_grid(ra_deg: float, dec_deg: float, width_deg: float,
                     height_deg: float) -> dict[str, Any]:
    """A grid for a region, to stand until the first light sets the real one.

    The scale is a placeholder (the first light's plate solution replaces it,
    with the centre and rotation: `rotation_from_first_light`), chosen only so
    that the region fits inside the contract's pixel limit.
    """
    longest = max(abs(width_deg), abs(height_deg), 1e-6) * 3600.0
    scale = min(GRID_MAX_SCALE, max(GRID_MIN_SCALE, round(longest / GRID_MAX_PX + 0.005, 2)))

    def pixels(degrees: float) -> int:
        return int(min(GRID_MAX_PX, max(GRID_MIN_PX, round(abs(degrees) * 3600.0 / scale))))

    return {"center_ra_deg": round(float(ra_deg), 6) % 360.0,
            "center_dec_deg": round(max(-90.0, min(90.0, float(dec_deg))), 6),
            "rotation_deg": 0.0, "scale_arcsec_px": scale,
            "width_px": pixels(width_deg), "height_px": pixels(height_deg)}


#: How much sky a mosaic's grid keeps beyond its outermost panels, per side:
#: the contract's own margin for the grid it derives from a mosaic layout.
MOSAIC_MARGIN = 0.02


def _to_plane(ra0: float, dec0: float, ra: float, dec: float) -> tuple[float, float]:
    """Standard coordinates (xi east, eta north, degrees) of a sky position on
    the plane tangent at (ra0, dec0): the gnomonic projection."""
    a0, d0, a, d = map(math.radians, (ra0, dec0, ra, dec))
    cos_c = math.sin(d0) * math.sin(d) + math.cos(d0) * math.cos(d) * math.cos(a - a0)
    xi = math.cos(d) * math.sin(a - a0) / cos_c
    eta = (math.cos(d0) * math.sin(d) - math.sin(d0) * math.cos(d) * math.cos(a - a0)) / cos_c
    return math.degrees(xi), math.degrees(eta)


def _from_plane(ra0: float, dec0: float, xi: float, eta: float) -> tuple[float, float]:
    """The sky position of standard coordinates (degrees) on the plane tangent
    at (ra0, dec0): the inverse gnomonic projection."""
    a0, d0 = math.radians(ra0), math.radians(dec0)
    x, y = math.radians(xi), math.radians(eta)
    denominator = math.cos(d0) - y * math.sin(d0)
    ra = a0 + math.atan2(x, denominator)
    dec = math.atan2(math.sin(d0) + y * math.cos(d0), math.hypot(x, denominator))
    return math.degrees(ra) % 360.0, math.degrees(dec)


def _offsets(xi: float, eta: float, rotation: float) -> tuple[float, float]:
    """Standard coordinates as offsets right and up in a frame turned to
    `rotation`: the inverse of the contract's `Grid` convention
    (`xi = -dx cos t + dy sin t`, `eta = dx sin t + dy cos t`), which is its
    own inverse."""
    c, s = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
    return -c * xi + s * eta, s * xi + c * eta


def fold_rotation(degrees: float) -> float:
    """An angle folded into (-90, 90], as the contract stores a grid's rotation:
    half a turn is the same rectangle on the sky."""
    folded = (float(degrees) + 90.0) % 180.0 - 90.0
    return 90.0 if folded == -90.0 else folded


def mosaic_grid(panels: list[dict[str, Any]], scale_arcsec_px: float) -> dict[str, Any] | None:
    """The grid a mosaic is stacked on: every panel inside it, framed by the
    panels themselves rather than by the first light.

    `panels` are the cells dealt to the collaboration's rigs, each with its
    sky centre (`ra`, `dec`, degrees), the camera's field along its own axes
    (`width`, `height`, degrees) and the camera's position angle
    (`rotation`). The grid is turned to the panels' common angle (their mean,
    a half-turn being the same rectangle), centred on the box the panels'
    corners fill in that frame, and as big as that box plus `MOSAIC_MARGIN` a
    side, at `scale_arcsec_px` (the finest rig's scale), coarsened only as far
    as the contract's 12 000 px a side needs.

    Why not the placeholder and `rotation_from_first_light`: that rule takes
    the centre of the grid from the first light, and the first light of a
    mosaic is one panel. On 2026-11-04 M81_M82's grid was moved onto panel 1,
    so panel 2's clear frames fell 90 % off it and were refused
    `pointing_off_target`. A mosaic's centre is known before any light: it is
    where its panels are.

    None when there is nothing to frame (no panels, or no usable scale).
    """
    usable = [p for p in panels
              if all(math.isfinite(float(p.get(k) or 0.0)) for k in ("ra", "dec", "width", "height"))
              and float(p.get("width") or 0.0) > 0 and float(p.get("height") or 0.0) > 0]
    if not usable or not (scale_arcsec_px and math.isfinite(scale_arcsec_px) and scale_arcsec_px > 0):
        return None
    # The panels' common angle: the mean of the doubled angles, so that 88 and
    # 268 (the same rectangle) agree instead of averaging to 178.
    doubled = [math.radians(2.0 * float(p.get("rotation") or 0.0)) for p in usable]
    rotation = fold_rotation(round(math.degrees(math.atan2(
        sum(math.sin(a) for a in doubled), sum(math.cos(a) for a in doubled))) / 2.0, 2))

    corners: list[tuple[float, float]] = []
    for p in usable:
        ra, dec = float(p["ra"]), float(p["dec"])
        half_w, half_h = float(p["width"]) / 2.0, float(p["height"]) / 2.0
        t = math.radians(float(p.get("rotation") or 0.0))
        c, s = math.cos(t), math.sin(t)
        for dx, dy in ((-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h)):
            corners.append(_from_plane(ra, dec, -dx * c + dy * s, dx * s + dy * c))

    # Tangent at the panels' mean direction, then once more at the centre of
    # the box they fill, so the box is measured where the grid will sit.
    vectors = [(math.cos(math.radians(d)) * math.cos(math.radians(a)),
                math.cos(math.radians(d)) * math.sin(math.radians(a)),
                math.sin(math.radians(d))) for a, d in ((float(p["ra"]), float(p["dec"])) for p in usable)]
    x, y, z = (sum(v[i] for v in vectors) for i in range(3))
    centre = (math.degrees(math.atan2(y, x)) % 360.0, math.degrees(math.atan2(z, math.hypot(x, y))))
    for _ in range(2):
        offsets = [_offsets(*_to_plane(*centre, ra, dec), rotation) for ra, dec in corners]
        left, right = min(o[0] for o in offsets), max(o[0] for o in offsets)
        low, high = min(o[1] for o in offsets), max(o[1] for o in offsets)
        mid_x, mid_y = (left + right) / 2.0, (low + high) / 2.0
        c, s = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
        centre = _from_plane(*centre, -mid_x * c + mid_y * s, mid_x * s + mid_y * c)
    width_deg = (right - left) * (1.0 + 2.0 * MOSAIC_MARGIN)
    height_deg = (high - low) * (1.0 + 2.0 * MOSAIC_MARGIN)

    scale = max(GRID_MIN_SCALE_ANY, float(scale_arcsec_px))
    longest = max(width_deg, height_deg) * 3600.0
    if longest / scale > GRID_MAX_PX:
        scale = math.ceil(longest / GRID_MAX_PX * 1e4) / 1e4
    scale = min(GRID_MAX_SCALE, round(scale, 4))

    def pixels(degrees: float) -> int:
        return int(min(GRID_MAX_PX, max(GRID_MIN_PX, round(degrees * 3600.0 / scale))))

    return {"center_ra_deg": round(centre[0], 6) % 360.0,
            "center_dec_deg": round(max(-90.0, min(90.0, centre[1])), 6),
            "rotation_deg": rotation, "scale_arcsec_px": scale,
            "width_px": pixels(width_deg), "height_px": pixels(height_deg)}
