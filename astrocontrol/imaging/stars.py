"""Measuring how well focused a frame is.

Half-flux radius is the metric: the flux-weighted mean distance of a star's
light from its centre.  It beats peak brightness or FWHM for autofocus because
it degrades smoothly and symmetrically either side of focus, assumes nothing
about the star's profile, and stays meaningful when the star is nowhere near
Gaussian — which, halfway through a focus sweep, it emphatically is not.

**Stars are found as blobs, not as peaks.**  This is the whole difficulty, and
getting it wrong is what makes an autofocus run wander.  A defocused star is a
disc twenty or thirty pixels across; its light is spread over hundreds of
pixels, so its *peak* value is low — often lower than a single hot pixel, a
cosmic ray, or a warm column.  Anything that ranks candidates by peak brightness
therefore finds the sensor's defects first and the actual stars last, and
measures a half-flux radius of nearly zero for them.  So the frame is
thresholded against a *local* background and the connected regions above it are
what get measured, with blobs that are too small, too big, too elongated or too
hollow thrown away.  That is the same shape of pipeline NINA uses (structure
mask, blob detection, size and roundness filters, local background per star),
which is worth following because it is known to work on real frames.

Deliberately no SciPy: NumPy alone keeps the install small.  The connected
components are found by run-length encoding each row and merging runs that touch
between rows, which is cheap because a frame holds a few thousand runs rather
than sixty million pixels.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# Detection runs on a downsampled copy. Working at full resolution on a 60
# megapixel frame is both slow and needlessly fussy: a defocused disc is tens of
# pixels across, so the shape survives shrinking, while a hot pixel becomes a
# single faint dot that the minimum-size filter then discards. Measurement
# happens back at full resolution, so nothing is lost from the number itself.
#
# Not too far down, though. At 2200 across, a 60 megapixel frame was shrunk
# four times, and a focused star became a one-pixel dot the size filter threw
# away - so the only stars that survived were the brightest few hundred, whose
# wings at 3.5 sigma spread over a dozen shrunken pixels, and the "star" that
# was measured was a fifty-pixel box of wings. The median came out at nine
# pixels on a sub ASTAP measured at 1.6, and every star size this program
# reported to a collaboration was five or six times too large.
TARGET_WIDTH = 4400

# Blob filters, in downsampled pixels.
MIN_BLOB_PIXELS = 5          # below this it is a hot pixel or a cosmic ray
MIN_BLOB_SIDE = 2
MAX_BLOB_SIDE = 90           # a whole defocused disc, but not a nebula
MAX_ASPECT = 2.5             # trails, diffraction spikes and gradients
MIN_FILL = 0.30              # blob area against its bounding box

# Measured per frame. The median wants the frame's ordinary stars, and the
# brightest hundred of a deep sub are not ordinary: they are the ones with
# wings, halos and a core past the sensor's linear range.
MAX_STARS = 500
DETECTION_SIGMA = 3.5        # above the local background
SATURATION = 65000
# A star whose peak is above this is left out of the measurement: the top of
# its profile is clipped or nonlinear, so its radius reads large however
# well focused it is. Defocused discs are spread thin and never reach it.
LINEAR_PEAK = 40000


def _downsample(image: np.ndarray) -> tuple[np.ndarray, int]:
    """Block-average the frame down to roughly `TARGET_WIDTH` across."""
    height, width = image.shape
    factor = max(1, int(round(width / TARGET_WIDTH)))
    if factor == 1:
        return image.astype(np.float32), 1
    rows, columns = height // factor, width // factor
    trimmed = image[:rows * factor, :columns * factor].astype(np.float32)
    return trimmed.reshape(rows, factor, columns, factor).mean(axis=(1, 3)), factor


def _upsample(coarse: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Bilinear expansion of a coarse map back to `shape`."""
    coarse_rows, coarse_columns = coarse.shape
    rows, columns = shape
    ys = np.clip((np.arange(rows) + 0.5) * coarse_rows / rows - 0.5,
                 0, coarse_rows - 1)
    xs = np.clip((np.arange(columns) + 0.5) * coarse_columns / columns - 0.5,
                 0, coarse_columns - 1)
    y0 = np.floor(ys).astype(np.intp)
    x0 = np.floor(xs).astype(np.intp)
    y1 = np.minimum(y0 + 1, coarse_rows - 1)
    x1 = np.minimum(x0 + 1, coarse_columns - 1)
    wy = (ys - y0)[:, None].astype(np.float32)
    wx = (xs - x0)[None, :].astype(np.float32)
    top = coarse[y0][:, x0] * (1 - wx) + coarse[y0][:, x1] * wx
    bottom = coarse[y1][:, x0] * (1 - wx) + coarse[y1][:, x1] * wx
    return top * (1 - wy) + bottom * wy


def background_map(small: np.ndarray, tile: int = 48
                   ) -> tuple[np.ndarray, np.ndarray]:
    """Sky level and noise across the frame, as smooth maps.

    A single number for the whole frame is not good enough on a real sub: light
    pollution, amp glow and vignetting all tilt the background, and a global
    threshold then lights up an entire corner of the frame while missing stars
    in the darkest part.  Tiles are reduced by median and MAD, which ignore the
    stars sitting in them, and the result is smoothed back up.
    """
    rows = max(1, small.shape[0] // tile)
    columns = max(1, small.shape[1] // tile)
    usable = small[:rows * tile, :columns * tile]
    blocks = usable.reshape(rows, tile, columns, tile).transpose(0, 2, 1, 3)
    flat = blocks.reshape(rows, columns, -1)

    level = np.median(flat, axis=2)
    spread = np.median(np.abs(flat - level[:, :, None]), axis=2) * 1.4826
    spread = np.maximum(spread, 1e-3)
    return _upsample(level, small.shape), _upsample(spread, small.shape)


def _blobs(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """Connected regions of `mask`, as (y0, y1, x0, x1, area).

    Run-length encoding per row, then union-find over runs that touch between
    adjacent rows.  There are a few thousand runs in a frame full of stars, so
    this stays cheap where a per-pixel flood fill would not.
    """
    height = mask.shape[0]
    parent: list[int] = []

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: int, b: int) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)

    runs: list[tuple[int, int, int]] = []      # (row, start, end-exclusive)
    previous: list[int] = []                   # run ids on the row above

    for row in range(height):
        line = mask[row]
        if not line.any():
            previous = []
            continue
        padded = np.concatenate(([0], line.view(np.int8), [0]))
        edges = np.diff(padded)
        starts = np.flatnonzero(edges == 1)
        ends = np.flatnonzero(edges == -1)

        current: list[int] = []
        for start, end in zip(starts, ends):
            run_id = len(runs)
            runs.append((row, int(start), int(end)))
            parent.append(run_id)
            current.append(run_id)
            # Eight-connectivity: a run touches one above if their spans
            # overlap when widened by a pixel at each end.
            for other in previous:
                _, other_start, other_end = runs[other]
                if other_start <= end and start <= other_end:
                    union(run_id, other)
        previous = current

    grouped: dict[int, list[int]] = {}
    for run_id in range(len(runs)):
        grouped.setdefault(find(run_id), []).append(run_id)

    found = []
    for members in grouped.values():
        rows_ = [runs[i][0] for i in members]
        y0, y1 = min(rows_), max(rows_) + 1
        x0 = min(runs[i][1] for i in members)
        x1 = max(runs[i][2] for i in members)
        area = sum(runs[i][2] - runs[i][1] for i in members)
        found.append((y0, y1, x0, x1, area))
    return found


def _acceptable(blob: tuple[int, int, int, int, int]) -> bool:
    """Whether a blob has the shape of a star rather than a defect."""
    y0, y1, x0, x1, area = blob
    height, width = y1 - y0, x1 - x0
    if area < MIN_BLOB_PIXELS:
        return False                            # hot pixel or cosmic ray
    if height < MIN_BLOB_SIDE or width < MIN_BLOB_SIDE:
        return False
    if height > MAX_BLOB_SIDE or width > MAX_BLOB_SIDE:
        return False                            # nebulosity, or a whole gradient
    if max(height, width) / max(1, min(height, width)) > MAX_ASPECT:
        return False                            # trailed, or a diffraction spike
    if area / float(height * width) < MIN_FILL:
        return False                            # hollow or straggly
    return True


def measure_star(image: np.ndarray, y0: int, y1: int, x0: int,
                 x1: int) -> tuple[float, float, float] | None:
    """Centre and half-flux radius of one star, at full resolution.

    The background comes from a ring around the star rather than from the frame
    as a whole, exactly as NINA does it: the sky under a star in the corner of a
    vignetted frame is not the sky in the middle, and an error there goes
    straight into the radius.

    The radius is then measured inside an aperture of three times itself
    around the centroid, twice over, rather than across the detection box with
    its margin. The box is the star's footprint above the detection threshold
    plus a margin of the same order, and on a long sub that is wings, noise
    and whatever gradient the ring's median missed, every pixel of it weighted
    by its distance; the aperture holds the star and little else, and
    tightens onto it as the estimate improves. On the same subs this agrees
    with ASTAP to a tenth of a pixel where the box read five times too large.
    """
    height, width = image.shape
    box_height, box_width = y1 - y0, x1 - x0
    margin = max(3, int(round(max(box_height, box_width) * 0.5)))

    oy0, oy1 = max(0, y0 - margin), min(height, y1 + margin)
    ox0, ox1 = max(0, x0 - margin), min(width, x1 + margin)
    outer = image[oy0:oy1, ox0:ox1].astype(np.float32)
    if outer.size == 0:
        return None

    # The ring: everything in the outer box that is not the star's own box.
    ring = np.ones(outer.shape, dtype=bool)
    ring[y0 - oy0:y1 - oy0, x0 - ox0:x1 - ox0] = False
    if ring.sum() < 16:
        return None
    surrounding = outer[ring]
    sky = float(np.median(surrounding))
    noise = float(np.median(np.abs(surrounding - sky))) * 1.4826

    # A first centre and radius from the star's own box.
    inner = image[y0:y1, x0:x1].astype(np.float32) - sky
    # Only pixels that are actually the star contribute; anything at or below
    # the sky would otherwise pull the centroid towards the middle of the box.
    inner[inner < max(2.0 * noise, 1e-3)] = 0.0
    total = float(inner.sum())
    if total <= 0:
        return None
    rows, columns = np.mgrid[0:inner.shape[0], 0:inner.shape[1]]
    centre_y = y0 + float((inner * rows).sum() / total)
    centre_x = x0 + float((inner * columns).sum() / total)
    hfr = float((inner * np.hypot(rows - (centre_y - y0),
                                  columns - (centre_x - x0))).sum() / total)
    if not np.isfinite(hfr) or hfr <= 0:
        return None

    # Then the aperture, tightened onto the star.
    for _ in range(2):
        reach = max(3.0, 3.0 * hfr)
        ay0, ay1 = max(0, int(centre_y - reach)), min(height, int(centre_y + reach) + 1)
        ax0, ax1 = max(0, int(centre_x - reach)), min(width, int(centre_x + reach) + 1)
        patch = image[ay0:ay1, ax0:ax1].astype(np.float32) - sky
        if patch.size == 0:
            return None
        rows, columns = np.mgrid[ay0:ay1, ax0:ax1]
        distance = np.hypot(rows - centre_y, columns - centre_x)
        patch[(distance > reach) | (patch < 0)] = 0.0
        total = float(patch.sum())
        if total <= 0:
            return None
        centre_y = float((patch * rows).sum() / total)
        centre_x = float((patch * columns).sum() / total)
        hfr = float((patch * distance).sum() / total)
        if not np.isfinite(hfr) or hfr <= 0:
            return None
    return centre_y, centre_x, hfr


def detect(image: np.ndarray, sigma: float = DETECTION_SIGMA
           ) -> list[tuple[float, float, float]]:
    """Every star in the frame, as (y, x, half-flux radius) in image pixels."""
    if image.ndim != 2 or image.size == 0:
        return []
    small, factor = _downsample(image)
    if min(small.shape) < 8:
        return []

    sky, noise = background_map(small)
    mask = small > sky + sigma * noise
    if not mask.any():
        return []

    blobs = [blob for blob in _blobs(mask) if _acceptable(blob)]
    if not blobs:
        return []

    # Brightest first, by total flux above the sky rather than by peak: a
    # defocused star is spread thin, and its peak says nothing about it.
    flux = []
    above = np.where(mask, small - sky, 0.0)
    for y0, y1, x0, x1, _ in blobs:
        flux.append(float(above[y0:y1, x0:x1].sum()))
    order = np.argsort(flux)[::-1]

    stars: list[tuple[float, float, float]] = []
    for index in order:
        y0, y1, x0, x1, _ = blobs[index]
        # Back to full resolution, where the measurement actually happens.
        fy0, fy1 = y0 * factor, min(image.shape[0], y1 * factor)
        fx0, fx1 = x0 * factor, min(image.shape[1], x1 * factor)
        if fy1 <= fy0 or fx1 <= fx0:
            continue
        patch = image[fy0:fy1, fx0:fx1]
        if patch.size == 0 or float(patch.max()) > LINEAR_PEAK:
            continue                            # clipped or nonlinear; the radius is a lie
        measured = measure_star(image, fy0, fy1, fx0, fx1)
        if measured is None:
            continue
        stars.append(measured)
        if len(stars) >= MAX_STARS:
            break
    return stars


def measure(image: np.ndarray, threshold_sigma: float = DETECTION_SIGMA
            ) -> dict[str, Any]:
    """The frame's focus metric: median half-flux diameter over its stars.

    The median rather than the mean, because one bloated double star, a
    satellite trail or a cosmic ray should not move the answer.
    """
    stars = detect(image, threshold_sigma)
    if not stars:
        return {"hfd": None, "hfr": None, "stars": 0,
                "detail": "no stars found"}

    radii = np.array([star[2] for star in stars], dtype=float)
    hfr = float(np.median(radii))
    return {
        "hfd": round(hfr * 2.0, 3),
        "hfr": round(hfr, 3),
        "hfdSpread": round(float(np.std(radii * 2.0)), 3),
        "stars": int(radii.size),
        "detail": "",
    }
