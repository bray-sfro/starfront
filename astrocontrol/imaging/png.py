"""A minimal PNG encoder.

Only what the viewer needs (8-bit greyscale and 8-bit RGB), which keeps the
application free of an image-library dependency.
"""

from __future__ import annotations

import struct
import zlib

import numpy as np


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def encode(image: np.ndarray, level: int = 6) -> bytes:
    """Encode a uint8 array: (H, W) greyscale or (H, W, 3) RGB."""
    if image.dtype != np.uint8:
        raise ValueError("PNG encoder expects uint8 data")

    if image.ndim == 2:
        colour_type = 0
        height, width = image.shape
    elif image.ndim == 3 and image.shape[2] == 3:
        colour_type = 2
        height, width = image.shape[:2]
    else:
        raise ValueError(f"unsupported image shape {image.shape}")

    # Each scanline is prefixed with a filter-type byte; 0 means "no filter".
    rows = np.ascontiguousarray(image).reshape(height, -1)
    prefixed = np.hstack([np.zeros((height, 1), dtype=np.uint8), rows])

    header = struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(prefixed.tobytes(), level))
            + _chunk(b"IEND", b""))
