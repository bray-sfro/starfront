"""Turn a one-shot colour mosaic into one luminance image.

A colour camera does not record a picture. Each pixel is only red, only
green or only blue, and stacking that mosaic as though it were mono writes a
chequerboard through every star. This is the ordinary bilinear demosaic:
good to a fraction of a pixel, cheap enough to run on the way into a live
stack, and with no extra dependency.

It returns luminance, not three channels. A community stack is one picture
per filter. An OSC frame through a UV/IR cut is luminance; the colour file
stays on disk for whoever wants to process it properly later.
"""

from __future__ import annotations

from typing import Any

import numpy as np

#: Where the red, green and blue samples sit, as (row, column) offsets of a
#: 2x2 cell whose (0, 0) is the first pixel of the array as stored.
_PHASES: dict[str, dict[str, tuple[tuple[int, int], ...]]] = {
    "RGGB": {"R": ((0, 0),), "G": ((0, 1), (1, 0)), "B": ((1, 1),)},
    "BGGR": {"B": ((0, 0),), "G": ((0, 1), (1, 0)), "R": ((1, 1),)},
    "GRBG": {"G": ((0, 0), (1, 1)), "R": ((0, 1),), "B": ((1, 0),)},
    "GBRG": {"G": ((0, 0), (1, 1)), "B": ((0, 1),), "R": ((1, 0),)},
}

#: Rec. 709. A live stack only needs one plane, and this is the one a person
#: means by "how bright".
_LUMA = (0.2126, 0.7152, 0.0722)

_PATTERN_KEYS = ("BAYERPAT", "BAYPAT", "COLORTYP")
_OFFSET_X = ("XBAYROFF", "BAYROFFX")
_OFFSET_Y = ("YBAYROFF", "BAYROFFY")
_NONE = {"", "NONE", "MONO", "MONOCHROME", "N/A", "NA"}


class DebayerError(ValueError):
    """A colour mosaic this stack will not pretend is mono."""


def _card(header: dict[str, Any], key: str) -> Any:
    value = header.get(key)
    if isinstance(value, tuple) and len(value) == 2:
        return value[0]
    return value


def pattern_of(header: dict[str, Any]) -> str | None:
    """The Bayer pattern a header names, or None when the frame is mono.

    An unrecognised pattern is raised rather than ignored: stacking the
    mosaic raw is worse than leaving the frame out.
    """
    raw = ""
    for key in _PATTERN_KEYS:
        value = _card(header, key)
        if value is None:
            continue
        raw = str(value).strip().upper().replace(" ", "")
        if raw:
            break
    if raw in _NONE:
        return None
    compact = raw.replace("-", "").replace("_", "")
    for name in _PHASES:
        if name in compact:
            return name
    raise DebayerError(
        f"Bayer pattern {raw!r} is not one this stack can debayer "
        f"({', '.join(_PHASES)})")


def _offset(header: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = _card(header, key)
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        return number % 2
    return 0


def _channel(frame: np.ndarray, sites: tuple[tuple[int, int], ...]) -> np.ndarray:
    """One colour, bilinearly filled from the sites the sensor measured."""
    samples = np.zeros(frame.shape, dtype=np.float32)
    present = np.zeros(frame.shape, dtype=np.float32)
    for y0, x0 in sites:
        samples[y0::2, x0::2] = frame[y0::2, x0::2]
        present[y0::2, x0::2] = 1.0

    horizontal = np.zeros(frame.shape, dtype=np.float32)
    horizontal_n = np.zeros(frame.shape, dtype=np.float32)
    horizontal[:, 1:] += samples[:, :-1]
    horizontal_n[:, 1:] += present[:, :-1]
    horizontal[:, :-1] += samples[:, 1:]
    horizontal_n[:, :-1] += present[:, 1:]

    vertical = np.zeros(frame.shape, dtype=np.float32)
    vertical_n = np.zeros(frame.shape, dtype=np.float32)
    vertical[1:, :] += samples[:-1, :]
    vertical_n[1:, :] += present[:-1, :]
    vertical[:-1, :] += samples[1:, :]
    vertical_n[:-1, :] += present[1:, :]

    diagonal = np.zeros(frame.shape, dtype=np.float32)
    diagonal_n = np.zeros(frame.shape, dtype=np.float32)
    for dy, dx in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
        src_y = slice(max(0, -dy), frame.shape[0] - max(0, dy))
        src_x = slice(max(0, -dx), frame.shape[1] - max(0, dx))
        dst_y = slice(max(0, dy), frame.shape[0] - max(0, -dy))
        dst_x = slice(max(0, dx), frame.shape[1] - max(0, -dx))
        diagonal[dst_y, dst_x] += samples[src_y, src_x]
        diagonal_n[dst_y, dst_x] += present[src_y, src_x]

    filled = samples.copy()
    missing = present == 0
    axis_n = horizontal_n + vertical_n
    axis = missing & (axis_n > 0)
    filled[axis] = (horizontal[axis] + vertical[axis]) / axis_n[axis]
    diag = missing & (axis_n == 0) & (diagonal_n > 0)
    filled[diag] = diagonal[diag] / diagonal_n[diag]
    return filled


def debayer(image: np.ndarray, pattern: str) -> np.ndarray:
    """Luminance from a Bayer mosaic. `pattern` is RGGB, BGGR, GRBG or GBRG."""
    name = pattern.strip().upper()
    phases = _PHASES.get(name)
    if phases is None:
        raise DebayerError(f"Bayer pattern {pattern!r} is not supported")
    frame = np.asarray(image, dtype=np.float32)
    if frame.ndim != 2 or min(frame.shape) < 2:
        raise DebayerError("a Bayer frame needs a 2-D image at least 2 pixels across")
    red = _channel(frame, phases["R"])
    green = _channel(frame, phases["G"])
    blue = _channel(frame, phases["B"])
    return (_LUMA[0] * red + _LUMA[1] * green + _LUMA[2] * blue).astype(np.float32)


def apply(image: np.ndarray, header: dict[str, Any]) -> tuple[np.ndarray, int, int] | None:
    """Debayer `image` when the header says it is a mosaic.

    Returns the luminance and how many rows and columns were dropped from
    the origin so the pattern's first pixel really is (0, 0). The caller
    shifts the plate solution's reference pixel by the same amount. Returns
    None when the frame is mono.
    """
    pattern = pattern_of(header)
    if pattern is None:
        return None
    dy = _offset(header, _OFFSET_Y)
    dx = _offset(header, _OFFSET_X)
    cropped = np.asarray(image)[dy:, dx:]
    return debayer(cropped, pattern), dx, dy
