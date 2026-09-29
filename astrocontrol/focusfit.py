"""Turning a set of (focuser position, star size) points into a focus position.

Four fits, the same four NINA offers, because no single one is right on every
rig:

* **Trend lines** — a straight line through each arm of the V, and the position
  where they cross.  The arms of a real defocus curve are very nearly straight,
  and a straight line is the most robust thing there is to fit: it does not care
  that the bottom of the curve is noisy, which is exactly where the measurements
  are least reliable.
* **Hyperbolic** — the shape a defocus curve genuinely has, since star size goes
  as the distance from focus convolved with the seeing disc.  It uses every
  point at once, including the bottom.
* **Parabolic** — the classic, good near the minimum and poor in the wings.
* **Averages of a trend line fit with one of the curve fits**, which is what
  NINA uses by default and what behaves best on real data: the trend lines pin
  down where the arms are going, the curve smooths out the noise.

No SciPy, so the hyperbola is fitted by searching over its two shape parameters
and solving for the scale exactly at each trial - a coarse grid, then a refining
pass around the best cell.  There are never more than a few dozen points, so
this costs nothing worth measuring.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

METHODS = ("trendlines", "parabolic", "hyperbolic", "trendparabolic",
           "trendhyperbolic")


def _clean(points: Sequence[tuple[float, float]]) -> tuple[np.ndarray, np.ndarray]:
    usable = [(float(x), float(y)) for x, y in points
              if y is not None and math.isfinite(y) and y > 0]
    usable.sort()
    if not usable:
        return np.array([]), np.array([])
    xs = np.array([p[0] for p in usable], dtype=float)
    ys = np.array([p[1] for p in usable], dtype=float)
    return xs, ys


def _r_squared(ys: np.ndarray, fitted: np.ndarray) -> float:
    total = float(((ys - ys.mean()) ** 2).sum())
    if total <= 0:
        return 0.0
    residual = float(((ys - fitted) ** 2).sum())
    return max(0.0, 1.0 - residual / total)


# NINA drops points level with the minimum from both arms: the bottom of a real
# V is flat and noisy, and a point that is only a hair above the lowest one says
# nothing about which way the arm is going.
FLAT_BOTTOM = 0.1


def trendlines(points: Sequence[tuple[float, float]]) -> dict[str, Any] | None:
    """A line through each arm of the V, and where they cross.

    The lowest measured point belongs to neither arm, and neither does anything
    within `FLAT_BOTTOM` of it — exactly as NINA selects them. Fitting a line
    through the rounded bottom of the curve drags both arms towards the
    horizontal and moves the crossing point about.
    """
    xs, ys = _clean(points)
    if xs.size < 4:
        return None
    lowest = int(np.argmin(ys))
    floor = ys[lowest] + FLAT_BOTTOM

    left_mask = (xs < xs[lowest]) & (ys > floor)
    right_mask = (xs > xs[lowest]) & (ys > floor)
    if left_mask.sum() < 2 or right_mask.sum() < 2:
        return None

    left_fit = np.polyfit(xs[left_mask], ys[left_mask], 1)
    right_fit = np.polyfit(xs[right_mask], ys[right_mask], 1)
    slope_left, intercept_left = float(left_fit[0]), float(left_fit[1])
    slope_right, intercept_right = float(right_fit[0]), float(right_fit[1])

    # The arms have to actually point at each other: down on the left, up on
    # the right. Anything else is not a V and has no crossing worth having.
    if not (slope_left < 0 < slope_right):
        return None
    denominator = slope_left - slope_right
    if abs(denominator) < 1e-9:
        return None

    position = (intercept_right - intercept_left) / denominator
    fitted = np.where(xs <= position,
                      slope_left * xs + intercept_left,
                      slope_right * xs + intercept_right)
    return {
        "position": float(position),
        "value": float(slope_left * position + intercept_left),
        "rSquared": _r_squared(ys, fitted),
        "left": {"slope": slope_left, "intercept": intercept_left,
                 "count": int(left_mask.sum())},
        "right": {"slope": slope_right, "intercept": intercept_right,
                  "count": int(right_mask.sum())},
    }


def parabolic(points: Sequence[tuple[float, float]]) -> dict[str, Any] | None:
    """A parabola through every point; its vertex is the focus."""
    xs, ys = _clean(points)
    if xs.size < 3:
        return None
    a, b, c = np.polyfit(xs, ys, 2)
    if a <= 0:
        return None                             # opens downwards; not a minimum
    position = -b / (2.0 * a)
    fitted = a * xs ** 2 + b * xs + c
    return {"position": float(position),
            "value": float(a * position ** 2 + b * position + c),
            "rSquared": _r_squared(ys, fitted)}


def _hyperbola(xs: np.ndarray, centre: float, width: float) -> np.ndarray:
    return np.sqrt(1.0 + ((xs - centre) / width) ** 2)


def hyperbolic(points: Sequence[tuple[float, float]]) -> dict[str, Any] | None:
    """Fit `y = a * sqrt(1 + ((x - c) / b)^2)`, whose minimum is at `c`.

    This is the shape a defocus curve really has: away from focus the star grows
    linearly with distance, and near focus the seeing disc rounds the bottom
    off.  For a given centre and width the scale `a` that minimises the squared
    error has a closed form, so only the two shape parameters need searching.
    """
    xs, ys = _clean(points)
    if xs.size < 4:
        return None

    span = float(xs[-1] - xs[0])
    if span <= 0:
        return None

    def best_scale(centre: float, width: float) -> tuple[float, float]:
        shape = _hyperbola(xs, centre, width)
        denominator = float((shape * shape).sum())
        if denominator <= 0:
            return 0.0, float("inf")
        scale = float((ys * shape).sum() / denominator)
        residual = float(((ys - scale * shape) ** 2).sum())
        return scale, residual

    # Coarse sweep, then refine around whatever cell won.
    centres = np.linspace(xs[0] - span * 0.5, xs[-1] + span * 0.5, 80)
    widths = np.geomspace(max(span / 200.0, 1e-6), span * 2.0, 40)
    best = (float("inf"), 0.0, 0.0, 0.0)
    for _ in range(3):
        for centre in centres:
            for width in widths:
                scale, residual = best_scale(centre, width)
                if residual < best[0]:
                    best = (residual, centre, width, scale)
        _, centre, width, _ = best
        centre_step = (centres[1] - centres[0]) if centres.size > 1 else span * 0.1
        centres = np.linspace(centre - centre_step, centre + centre_step, 21)
        widths = np.geomspace(max(width * 0.5, 1e-6), width * 2.0, 21)

    residual, centre, width, scale = best
    if not math.isfinite(residual) or scale <= 0:
        return None
    fitted = scale * _hyperbola(xs, centre, width)
    return {"position": float(centre), "value": float(scale),
            "width": float(width), "rSquared": _r_squared(ys, fitted)}


def solve(points: Sequence[tuple[float, float]], method: str = "trendhyperbolic"
          ) -> dict[str, Any]:
    """Every fit that works, and the position the chosen method asks for.

    All of them are computed whatever was asked for, because they cost nothing
    and seeing them side by side is how you tell a good run from a lucky one.
    """
    method = (method or "trendhyperbolic").lower()
    if method not in METHODS:
        method = "trendhyperbolic"

    fits = {
        "trendlines": trendlines(points),
        "parabolic": parabolic(points),
        "hyperbolic": hyperbolic(points),
    }

    wanted: list[str]
    if method == "trendparabolic":
        wanted = ["trendlines", "parabolic"]
    elif method == "trendhyperbolic":
        wanted = ["trendlines", "hyperbolic"]
    else:
        wanted = [method]

    chosen = [fits[name]["position"] for name in wanted if fits.get(name)]
    if not chosen:
        # Fall back to anything that did fit rather than throwing the run away.
        chosen = [fit["position"] for fit in fits.values() if fit]
        if not chosen:
            return {"position": None, "fits": fits, "method": method,
                    "detail": "no fit described a minimum"}

    xs, _ = _clean(points)
    position = float(sum(chosen) / len(chosen))
    if xs.size and not (xs[0] <= position <= xs[-1]):
        return {"position": None, "fits": fits, "method": method,
                "detail": (f"the fitted minimum ({position:.0f}) is outside the "
                           f"positions measured ({xs[0]:.0f}-{xs[-1]:.0f})")}
    return {"position": position, "fits": fits, "method": method, "detail": ""}
