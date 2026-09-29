"""Turning 16-bit linear data into something a human can see.

Raw astronomical frames are almost black when shown linearly - the interesting
signal sits in the bottom couple of percent of the range.  This module implements
the usual screen-transfer approach: pick a black point from the noise statistics,
then apply a midtone transfer function.  The pixel data itself is never modified;
this only affects the preview.
"""

from __future__ import annotations

from typing import Any

import numpy as np

MAX_ADU = 65535.0
HISTOGRAM_BINS = 512
TARGET_BACKGROUND = 0.25   # where the sky background lands after auto-stretch


def _sample(image: np.ndarray, limit: int = 1_500_000) -> np.ndarray:
    """Decimate large frames before computing statistics; the result is identical
    to within a fraction of an ADU and far faster."""
    flat = image.reshape(-1)
    if flat.size <= limit:
        return flat
    return flat[:: max(1, flat.size // limit)]


def mtf(x: np.ndarray | float, midtone: float) -> np.ndarray | float:
    """Midtone transfer function. midtone=0.5 is the identity."""
    if abs(midtone - 0.5) < 1e-9:
        return x
    x = np.asarray(x, dtype=np.float32)
    denominator = (2.0 * midtone - 1.0) * x - midtone
    numerator = (midtone - 1.0) * x
    return np.divide(numerator, denominator,
                     out=np.zeros_like(x), where=np.abs(denominator) > 1e-12)


def statistics(image: np.ndarray) -> dict[str, Any]:
    """Basic frame statistics plus a histogram, all in ADU."""
    sample = _sample(image).astype(np.float32)
    median = float(np.median(sample))
    mad = float(np.median(np.abs(sample - median))) * 1.4826

    counts, _ = np.histogram(sample, bins=HISTOGRAM_BINS, range=(0.0, MAX_ADU))
    saturated = int(np.count_nonzero(image >= 65500))

    return {
        "min": int(image.min()),
        "max": int(image.max()),
        "mean": round(float(sample.mean()), 2),
        "median": round(median, 2),
        "stdDev": round(float(sample.std()), 2),
        "mad": round(mad, 2),
        "saturatedPixels": saturated,
        "saturatedPercent": round(100.0 * saturated / image.size, 4),
        "histogram": counts.tolist(),
        "histogramBins": HISTOGRAM_BINS,
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
    }


def auto_stretch(image: np.ndarray) -> dict[str, float]:
    """Derive black point and midtone from the frame's own noise statistics.

    Returns values normalised to 0..1, matching the sliders in the UI.
    """
    sample = _sample(image).astype(np.float32) / MAX_ADU
    median = float(np.median(sample))
    mad = float(np.median(np.abs(sample - median))) * 1.4826

    if mad <= 1e-9:                       # perfectly flat frame
        return {"black": 0.0, "white": 1.0, "midtone": 0.5}

    black = float(np.clip(median - 2.8 * mad, 0.0, 1.0))
    white = 1.0
    span = max(median - black, 1e-6)
    midtone = float(np.clip(mtf(span, TARGET_BACKGROUND), 1e-4, 0.9999))
    return {"black": black, "white": white, "midtone": midtone}


def apply_stretch(image: np.ndarray, black: float, white: float, midtone: float,
                  invert: bool = False) -> np.ndarray:
    """Map a 16-bit frame to 8-bit for display."""
    lo = float(black) * MAX_ADU
    hi = float(white) * MAX_ADU
    if hi - lo < 1.0:
        hi = lo + 1.0

    x = (image.astype(np.float32) - lo) / (hi - lo)
    np.clip(x, 0.0, 1.0, out=x)
    y = np.asarray(mtf(x, float(midtone)), dtype=np.float32)
    np.clip(y, 0.0, 1.0, out=y)
    if invert:
        y = 1.0 - y
    return (y * 255.0 + 0.5).astype(np.uint8)


def downsample(image: np.ndarray, factor: int) -> np.ndarray:
    """Integer block-average, which preserves faint signal better than sub-sampling."""
    if factor <= 1:
        return image
    h = image.shape[0] // factor * factor
    w = image.shape[1] // factor * factor
    if h == 0 or w == 0:
        return image
    block = image[:h, :w].reshape(h // factor, factor, w // factor, factor)
    return block.mean(axis=(1, 3)).astype(np.uint16)


def fit_factor(width: int, height: int, max_dim: int) -> int:
    """Smallest integer decimation that fits the frame inside `max_dim`."""
    longest = max(width, height)
    if longest <= max_dim or max_dim <= 0:
        return 1
    return int(np.ceil(longest / max_dim))


def render_png_array(image: np.ndarray, stretch: dict[str, float] | None = None,
                     max_dim: int = 1600,
                     region: tuple[int, int, int, int] | None = None) -> tuple[np.ndarray, dict]:
    """Produce the 8-bit array the viewer displays.

    `region` is (x, y, w, h) in full-resolution pixels; when given, that crop is
    returned at 1:1 so the user can inspect focus at native scale.
    """
    source = image
    if region is not None:
        x, y, w, h = region
        x = int(np.clip(x, 0, max(0, image.shape[1] - 1)))
        y = int(np.clip(y, 0, max(0, image.shape[0] - 1)))
        w = int(np.clip(w, 1, image.shape[1] - x))
        h = int(np.clip(h, 1, image.shape[0] - y))
        source = image[y:y + h, x:x + w]
        factor = 1
    else:
        factor = fit_factor(image.shape[1], image.shape[0], max_dim)
        source = downsample(image, factor)

    # Stretch parameters always come from the full frame, so zooming in does not
    # change the brightness of what you are looking at.
    params = stretch or auto_stretch(image)
    out = apply_stretch(source, params["black"], params["white"], params["midtone"],
                        bool(params.get("invert", False)))
    return out, {"factor": factor, "stretch": params,
                 "width": int(out.shape[1]), "height": int(out.shape[0])}
