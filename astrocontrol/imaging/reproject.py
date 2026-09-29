"""Putting a frame onto somebody else's grid.

This is registration. Not "shift the second frame by three pixels and add it"
— that works for a night of subs off one camera and fails completely for the
thing this program is for, which is several telescopes at different focal
lengths, different rotations and different handedness contributing to one
picture. A 389 mm refractor and a 300 mm astrograph see the same nebula at
two arcseconds and two and a half arcseconds a pixel, one of them upside down
with respect to the other, and the only thing they agree on is where the sky
is. So the sky is what they are registered through.

The method is **inverse mapping**, which is the only one that produces an
output with no holes in it: walk the *output* pixels, ask the shared grid
where each one is on the sky, ask the frame's own solution which of its pixels
that is, and take the value from there. Walking the input instead — projecting
each input pixel forward and depositing it — leaves gaps wherever the output
is finer than the input and double-counts wherever it is coarser, and no
amount of smoothing afterwards puts back what was lost.

Two things make it correct rather than merely plausible:

  * **Block-averaging before sampling.** A frame at two arcseconds a pixel
    landing on a grid at four throws away three quarters of its pixels if it
    is simply sampled, and what it throws away is not noise — it is signal,
    and the stars that survive are the ones that happened to land near a grid
    point. Averaging the frame down to roughly the grid's own resolution first
    keeps the photons and removes the aliasing, and it is the cheaper of the
    two operations besides.

  * **An honest coverage mask.** Every output pixel either has data behind it
    or does not, and the parts of the grid a frame does not reach are not
    zeroes — a zero is a measurement of darkness, and the edge of a frame is
    not a measurement of anything. The mask travels with the values so the
    accumulator can tell the difference.

Interpolation is bilinear. Not because higher orders are hard but because
this runs on the machine driving the mount while a camera is downloading, and
a live stack that arrives after the next sub is not live. Bilinear on a frame
already averaged to the grid's resolution is visually indistinguishable from
anything fancier on data that is about to be averaged with thirty other
frames.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .wcs import Wcs, WcsError, bounds_on

#: Resample in bands of about this many output pixels at a time. The
#: trigonometry is vectorised, and vectorised over a whole mosaic canvas at
#: once it would allocate a dozen arrays the size of the canvas. Bands keep the
#: working set in cache and the peak memory flat.
BAND_PIXELS = 4_000_000

#: How much coarser the output has to be before the frame is averaged down.
#: Below this the sampling loss is small and the extra pass is not worth it;
#: above it, aliasing starts to matter.
DOWNSAMPLE_THRESHOLD = 1.35

#: Output pixels trimmed from the frame's own edge. A bilinear sample one
#: pixel inside the border is drawing on pixels outside it, and on a frame
#: with an overscan strip or a dark column at the edge that smears a defect
#: several pixels into the picture. Two is enough at any realistic scale ratio.
EDGE_TRIM = 2.0


def block_average(frame: np.ndarray, factor: int) -> np.ndarray:
    """Average a frame down by an integer factor, keeping the flux per pixel.

    The mean rather than the sum, so the result is in the same units as the
    original and a master flat still divides it. Odd rows and columns at the
    far edge are dropped rather than averaged over a short block, which would
    make the last row of a frame systematically different from the rest.
    """
    if factor <= 1:
        return frame
    rows = frame.shape[0] // factor
    columns = frame.shape[1] // factor
    if rows == 0 or columns == 0:
        return frame
    trimmed = frame[:rows * factor, :columns * factor]
    return trimmed.reshape(rows, factor, columns, factor).mean(axis=(1, 3))


def shrink_wcs(solution: Wcs, factor: int) -> Wcs:
    """The same solution, describing a frame that has been block-averaged.

    A new pixel `j` covers old pixels `factor*(j-1)+1` through `factor*j`, so
    its centre sits at old pixel `factor*j - (factor-1)/2`. Inverting that
    gives the reference pixel, and the scale simply multiplies. Getting this
    wrong is a registration half a pixel out per level of averaging, which
    shows up as stars that are subtly doubled in the stack and nowhere else.
    """
    if factor <= 1:
        return solution
    cd11, cd12, cd21, cd22 = solution.cd
    return Wcs(
        crval1=solution.crval1, crval2=solution.crval2,
        crpix1=(solution.crpix1 + (factor - 1) / 2.0) / factor,
        crpix2=(solution.crpix2 + (factor - 1) / 2.0) / factor,
        cd=(cd11 * factor, cd12 * factor, cd21 * factor, cd22 * factor),
        width=solution.width // factor, height=solution.height // factor,
        source=solution.source, assumptions=solution.assumptions)


def _sample_bilinear(frame: np.ndarray, x: np.ndarray, y: np.ndarray,
                     trim: float) -> tuple[np.ndarray, np.ndarray]:
    """Bilinear samples at one-based pixel coordinates, and where they are real.

    `trim` keeps the sample away from the frame's own border, where a bilinear
    weight would reach for pixels that do not exist.
    """
    height, width = frame.shape
    # One-based FITS pixel (1, 1) is the centre of element [0, 0].
    fx = x - 1.0
    fy = y - 1.0
    inside = ((fx >= trim) & (fx <= width - 1 - trim)
              & (fy >= trim) & (fy <= height - 1 - trim))
    # Clip before indexing rather than after: an out-of-range index raises,
    # and the values it would have produced are masked out anyway.
    cx = np.clip(fx, 0.0, width - 1.0001)
    cy = np.clip(fy, 0.0, height - 1.0001)
    x0 = np.floor(cx).astype(np.intp)
    y0 = np.floor(cy).astype(np.intp)
    wx = (cx - x0).astype(np.float32)
    wy = (cy - y0).astype(np.float32)
    x1 = x0 + 1
    y1 = y0 + 1

    top = frame[y0, x0] * (1.0 - wx) + frame[y0, x1] * wx
    bottom = frame[y1, x0] * (1.0 - wx) + frame[y1, x1] * wx
    return top * (1.0 - wy) + bottom * wy, inside


def resample(frame: np.ndarray, solution: Wcs, canvas: Wcs,
             box: tuple[int, int, int, int] | None = None,
             trim: float = EDGE_TRIM
             ) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Put `frame` onto `canvas`, over the part of it the frame reaches.

    Returns the resampled values, a boolean mask of where they are real, and a
    note saying what was done — the scale ratio, how much averaging was
    applied, and what fraction of the box came out covered. The note is not
    decoration: a contribution that lands on four per cent of the box it was
    given is a contribution whose plate solution is wrong, and that is worth
    seeing in a log rather than in a stack three hours later.

    `box` is a zero-based half-open `(x0, y0, x1, y1)` on the canvas; when it
    is not given, the frame's own bounding box is worked out and used. Passing
    one is how a caller resamples onto a slice of a canvas it is holding.

    Values come back as float32 in whatever units the frame was in. Nothing is
    scaled, offset or clipped here: photometric matching belongs to the
    accumulator, which can see what is already on the canvas, and doing it in
    two places is how two telescopes end up on two different scales.
    """
    if frame.ndim != 2:
        raise WcsError("only 2-D frames can be registered")
    if solution.width and solution.width != frame.shape[1]:
        raise WcsError(f"the solution describes a frame {solution.width} pixels "
                       f"across and this one is {frame.shape[1]}")

    if box is None:
        box = bounds_on(canvas, solution)
        if box is None:
            raise WcsError("the frame does not land on this canvas at all — "
                           "check that its plate solution and the canvas are "
                           "the same patch of sky")
    x0, y0, x1, y1 = box
    out_width, out_height = x1 - x0, y1 - y0
    if out_width <= 0 or out_height <= 0:
        raise WcsError("the region to resample onto is empty")

    # Average the frame down to roughly the canvas's resolution first.
    ratio = canvas.scale / max(solution.scale, 1e-9)
    factor = 1
    if ratio >= DOWNSAMPLE_THRESHOLD:
        factor = max(1, int(math.floor(ratio)))
    working = block_average(frame.astype(np.float32), factor)
    working_wcs = shrink_wcs(solution, factor)

    values = np.zeros((out_height, out_width), dtype=np.float32)
    covered = np.zeros((out_height, out_width), dtype=bool)

    # Bands of rows, so a mosaic-sized canvas never allocates six arrays of
    # its own size at once.
    band = max(1, min(out_height, BAND_PIXELS // max(1, out_width)))
    columns = np.arange(x0, x1, dtype=np.float64) + 1.0   # one-based
    for start in range(0, out_height, band):
        stop = min(out_height, start + band)
        rows = np.arange(y0 + start, y0 + stop, dtype=np.float64) + 1.0
        gx, gy = np.meshgrid(columns, rows)
        ra, dec = canvas.to_world(gx, gy)
        fx, fy, ahead = working_wcs.to_pixel(ra, dec)
        sampled, inside = _sample_bilinear(working, fx, fy, trim)
        good = inside & ahead
        values[start:stop] = np.where(good, sampled, 0.0)
        covered[start:stop] = good

    filled = float(covered.mean()) if covered.size else 0.0
    return values, covered, {
        "box": [x0, y0, x1, y1],
        "scaleRatio": round(ratio, 4),
        "averagedBy": factor,
        "coverage": round(filled, 4),
        "pixels": int(covered.sum()),
        "frameScale": round(solution.scale, 4),
        "canvasScale": round(canvas.scale, 4),
    }


# ---------------------------------------------------------------------------
# What a frame contributes, before anybody stacks it
# ---------------------------------------------------------------------------

def background(values: np.ndarray, mask: np.ndarray | None = None
               ) -> tuple[float, float]:
    """The sky level and the noise in it, robustly: median and scaled MAD.

    Both are needed by everything downstream. The level is what has to come
    off before two telescopes can be compared at all — one at a dark site and
    one under a town have sky backgrounds that differ by more than the nebula
    does — and the noise is what decides how much a frame's opinion is worth
    when it is averaged with everybody else's.

    Median and median absolute deviation rather than mean and standard
    deviation, because a frame full of stars and nebula has a mean well above
    its sky and a standard deviation dominated by the brightest object in it.
    """
    data = values[mask] if mask is not None else values.reshape(-1)
    if data.size == 0:
        return 0.0, 0.0
    # A few hundred thousand pixels settle a median to well under an ADU, and
    # a full pass over a sixty-megapixel frame to find the same number is a
    # second nobody needed to spend.
    if data.size > 400_000:
        data = data[:: max(1, data.size // 400_000)]
    level = float(np.median(data))
    spread = float(np.median(np.abs(data - level))) * 1.4826
    return level, max(spread, 1e-6)


#: How well the two must correlate to be believed to be the same sky through
#: the same filter — after the noise in each has been discounted, so this is a
#: correlation between the *signals* rather than between the pictures. Real
#: overlapping data scores well over 0.9; a misregistered frame or the wrong
#: filter scores nothing. Half is a wide margin either way.
MIN_CORRELATION = 0.5

#: Pixels whose two frames *disagree* by more than this many robust
#: deviations are left out of the moments. A satellite trail, a cosmic ray
#: and a star saturated in one frame but not the other are all real and none
#: of them describes the relation between two telescopes.
#:
#: Note what is clipped: the disagreement, never the brightness. Clipping
#: bright pixels was tried and is catastrophic on the one field where this
#: matters most — in a star field the robust spread of the pixels *is* the
#: noise, so any cut at a few robust deviations removes every star and leaves
#: two frames of pure noise, which correlate at zero. The first version did
#: exactly that and reported that two frames aligned to a hundredth of a
#: pixel were not the same sky.
CLIP_SIGMA = 5.0

#: The fewest overlapping pixels worth measuring a scale on.
MIN_OVERLAP = 500


def _noise_from_differences(values: np.ndarray, mask: np.ndarray) -> float:
    """Noise estimated from how much neighbouring pixels differ.

    The fallback for when the caller cannot say. Adjacent pixels of a real
    sky differ by the noise plus a little real structure, so the median
    absolute difference along a row, over root two, is an estimate that is
    slightly high and never catastrophically wrong — which is the right shape
    of error here, because overestimating the noise underestimates the signal
    and makes this function *less* confident rather than more.
    """
    inside = mask[:, :-1] & mask[:, 1:]
    if not inside.any():
        return 0.0
    steps = np.abs(np.diff(values, axis=1))[inside]
    if steps.size > 200_000:
        steps = steps[:: steps.size // 200_000]
    return float(np.median(steps)) * 1.4826 / math.sqrt(2.0)


def scale_to(reference: np.ndarray, incoming: np.ndarray, mask: np.ndarray,
             reference_noise: float | None = None,
             incoming_noise: float | None = None,
             ) -> tuple[float, float, dict[str, Any]]:
    """Put an incoming frame on the same photometric scale as what is already
    there, by measuring it against the overlap.

    This is what lets a community stack work at all. Two telescopes of
    different aperture, at different sites, through nominally the same filter
    of different bandpass, at different exposures, produce numbers with no
    common unit whatsoever — and no amount of header arithmetic recovers the
    relation, because it depends on the transparency of the sky that night.
    But where the two overlap they are looking at the same photons, and that
    is a measurement.

    Returned as the transform to apply to `incoming` to bring it onto the
    reference's scale, so `(incoming - offset) / gain`.

    **Noise is discounted rather than smoothed away, and that is the whole
    trick.** The obvious ways of comparing two frames all measure the wrong
    thing on exactly the frames this exists for. A wide-field sub is mostly
    sky, so the spread of its values is dominated by its own noise rather
    than by anything on the sky, and matching spreads or quantiles gives the
    ratio of the two frames' *noise* — which is not the ratio of their signal
    and need not even point the same way. Binning the overlap first fixes
    that for a nebula and breaks it for a star field, where eight-by-eight
    blocks wash the stars out along with the noise.

    So the noise is subtracted from the statistics instead. Writing the
    overlap as `a = s + n_a` and `b = k*s + n_b`, with the noise in the two
    independent of each other and of the sky:

        cov(a, b)              = k * var(s)
        var(a) - noise_a^2     =     var(s)

    so the sensitivity ratio is the first divided by the second, exactly and
    without bias, whatever the noise is. The same two quantities give a
    correlation between the *signals* rather than between the pictures, which
    is the honest version of "are these looking at the same thing": a pair of
    genuinely overlapping frames scores near one however noisy either of them
    is.

    The noise levels are arguments because the caller usually knows them far
    better than they can be recovered from the pixels — a stack knows the
    variance of its own weighted mean exactly, and a tile was weighed by its
    measured noise when it was made. Without them, a fallback estimates the
    noise from how much neighbouring pixels differ.
    """
    shared = int(mask.sum())
    if shared < MIN_OVERLAP:
        return 1.0, 0.0, {"matched": False,
                          "detail": f"only {shared} pixels overlap, too few to "
                                    "measure a common scale against"}

    left = reference[mask].astype(np.float64)
    right = incoming[mask].astype(np.float64)
    # A few hundred thousand pixels settle every moment here to far better
    # than the measurement deserves, and a full pass over a mosaic-sized
    # overlap is seconds nobody needed to spend.
    if left.size > 400_000:
        step = left.size // 400_000
        left, right = left[::step], right[::step]

    if reference_noise is None:
        reference_noise = _noise_from_differences(reference, mask)
    if incoming_noise is None:
        incoming_noise = _noise_from_differences(incoming, mask)

    def moments(a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
        da, db = a - a.mean(), b - b.mean()
        return (float((da * da).mean()), float((db * db).mean()),
                float((da * db).mean()))

    # Two passes. The first fits the relation over everything; the second
    # throws out the pixels that relation cannot explain and fits again. That
    # order is what makes the clip safe: a star is bright in both frames and
    # therefore agrees, while a satellite trail is bright in one and not the
    # other and therefore does not.
    variance_a, variance_b, covariance = moments(left, right)
    if variance_a > 0:
        slope = covariance / variance_a
        residual = right - slope * left
        middle = float(np.median(residual))
        scatter = float(np.median(np.abs(residual - middle))) * 1.4826
        if scatter > 0:
            keep = np.abs(residual - middle) < CLIP_SIGMA * scatter
            if int(keep.sum()) >= MIN_OVERLAP:
                left, right = left[keep], right[keep]
                variance_a, variance_b, covariance = moments(left, right)

    # What is left once each frame's own noise is taken out: the variance of
    # the sky itself, as each of them measures it.
    signal_a = variance_a - float(reference_noise) ** 2
    signal_b = variance_b - float(incoming_noise) ** 2
    if signal_a <= 0 or signal_b <= 0:
        return 1.0, 0.0, {
            "matched": False,
            "detail": ("the overlap is all noise and no structure, so there "
                       "is nothing to measure a scale against"),
        }

    correlation = covariance / math.sqrt(signal_a * signal_b)
    if not math.isfinite(correlation) or correlation < MIN_CORRELATION:
        return 1.0, 0.0, {
            "matched": False,
            "correlation": round(correlation, 4) if math.isfinite(correlation) else None,
            "detail": ("the overlap does not look like the same sky "
                       f"(correlation {correlation:.2f}) — check the frame's "
                       "plate solution and that it is the filter it claims "
                       "to be"),
        }

    gain = covariance / signal_a
    offset = float(right.mean()) - gain * float(left.mean())
    if not (math.isfinite(gain) and math.isfinite(offset)) or gain <= 1e-6:
        return 1.0, 0.0, {"matched": False,
                          "detail": "the scale measurement did not converge"}

    return float(gain), float(offset), {
        "matched": True,
        "gain": round(float(gain), 5),
        "offset": round(float(offset), 3),
        "pixels": shared,
        "correlation": round(min(correlation, 1.0), 4),
        "detail": (f"matched to the stack on {shared} shared pixels "
                   f"(gain {gain:.3f}, correlation {min(correlation, 1.0):.2f})"),
    }
