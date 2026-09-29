"""Minimal FITS reader/writer for single-image 16-bit files.

Files written here follow the standard closely enough to open in PixInsight,
Siril, ASTAP, DeepSkyStacker and astropy: 2880-byte blocks, 80-character cards,
BITPIX=16 with BZERO=32768 for unsigned data, big-endian.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import numpy as np

BLOCK = 2880
CARD = 80


def _card(key: str, value: Any, comment: str = "") -> bytes:
    key = key.upper()[:8]
    if value is True or value is False:
        rendered = "T" if value else "F"
        body = f"{rendered:>20}"
    elif isinstance(value, (int, np.integer)):
        body = f"{int(value):>20}"
    elif isinstance(value, (float, np.floating)):
        # Fourteen significant digits, not eight: a Julian date is seven
        # digits before the point, and eight significant digits would round
        # it to a tenth of a day. Fourteen still fits the twenty-column field.
        body = f"{float(value):>20.14G}"
    else:
        text = str(value).replace("'", "''")[:66]
        body = f"'{text}'".ljust(20)
    card = f"{key:<8}= {body}"
    if comment:
        card = f"{card} / {comment}"
    return card[:CARD].ljust(CARD).encode("ascii", "replace")


def _pad(data: bytes) -> bytes:
    remainder = len(data) % BLOCK
    return data if remainder == 0 else data + b"\0" * (BLOCK - remainder)


def write(path: str | Path, image: np.ndarray, header: dict[str, Any] | None = None) -> Path:
    """Write a 2-D uint16 image to `path` and return the path."""
    if image.ndim != 2:
        raise ValueError("only 2-D images are supported")
    data = np.asarray(image, dtype=np.uint16)
    height, width = data.shape

    cards = [
        _card("SIMPLE", True, "conforms to FITS standard"),
        _card("BITPIX", 16, "16-bit integers"),
        _card("NAXIS", 2),
        _card("NAXIS1", width),
        _card("NAXIS2", height),
        _card("BZERO", 32768, "offset for unsigned data"),
        _card("BSCALE", 1),
    ]
    for key, value in (header or {}).items():
        comment = ""
        # Unpacked *before* the None check, not after it. Almost every card here
        # is written as (value, comment), so testing the tuple for None never
        # fired — and a card whose value was simply not known went to disk as
        # the four letters "None". A dark frame claiming FILTER = 'None' is a
        # frame that lies to whatever reads it next.
        if isinstance(value, tuple) and len(value) == 2:
            value, comment = value
        if value is None:
            continue
        cards.append(_card(key, value, comment))
    cards.append(b"END".ljust(CARD))

    # Signed big-endian on disk; BZERO shifts it back to unsigned on read.
    payload = (data.astype(np.int32) - 32768).astype(">i2").tobytes()

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(_pad(b"".join(cards)))
        handle.write(_pad(payload))
    return path


def _parse_value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("'"):
        end = raw.rfind("'")
        return raw[1:end].replace("''", "'").strip() if end > 0 else raw
    token = raw.split("/")[0].strip()
    if token in ("T", "F"):
        return token == "T"
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token.replace("D", "E"))
    except ValueError:
        return token


def _consume_header(handle) -> dict[str, Any]:
    """Read the header blocks from an open file, leaving it at the pixel data.

    The END card can fall anywhere inside a block, but the data always starts at
    the next 2880-byte boundary — and blocks are read whole, so returning here
    leaves the handle exactly where the pixels begin.
    """
    header: dict[str, Any] = {}
    while True:
        block = handle.read(BLOCK)
        if len(block) < BLOCK:
            raise ValueError("truncated FITS header")
        text = block.decode("ascii", "replace")
        for i in range(0, BLOCK, CARD):
            card = text[i:i + CARD]
            key = card[:8].strip()
            if key == "END":
                return header
            if not key or card[8:10] != "= ":
                continue
            header[key] = _parse_value(card[10:])


def read_header(path: str | Path) -> dict[str, Any]:
    """The header alone.

    Scanning a calibration library means reading the metadata of every master in
    it, and a master is fifty megabytes of pixels nobody wants yet.
    """
    with open(path, "rb") as handle:
        return _consume_header(handle)


def read_values(path: str | Path) -> tuple[np.ndarray, dict[str, Any]]:
    """The pixels as the file holds them, unclipped, as float64.

    `read` gives back a 16-bit frame, which is what every frame this program
    takes is. A master built elsewhere is often 32-bit float in the range
    0-1, and clipping that to integers would leave a frame of zeros and
    ones; the importer needs the real values to decide how to scale them.
    """
    with open(path, "rb") as handle:
        header = _consume_header(handle)
        bitpix = int(header.get("BITPIX", 16))
        width, height = int(header["NAXIS1"]), int(header["NAXIS2"])
        dtype = {8: ">u1", 16: ">i2", 32: ">i4", -32: ">f4", -64: ">f8"}.get(bitpix)
        if dtype is None:
            raise ValueError(f"unsupported BITPIX {bitpix}")
        count = width * height
        raw = handle.read(count * np.dtype(dtype).itemsize)
    if len(raw) < count * np.dtype(dtype).itemsize:
        raise ValueError("truncated FITS data")
    data = np.frombuffer(raw, dtype=dtype, count=count)
    values = (data.astype(np.float64) * float(header.get("BSCALE", 1))
              + float(header.get("BZERO", 0)))
    return values.reshape(height, width), header


def read(path: str | Path) -> tuple[np.ndarray, dict[str, Any]]:
    """Read a FITS file written by `write` (or any simple 2-D 8/16/32-bit file)."""
    with open(path, "rb") as handle:
        header = _consume_header(handle)

        bitpix = int(header.get("BITPIX", 16))
        width, height = int(header["NAXIS1"]), int(header["NAXIS2"])
        dtype = {8: ">u1", 16: ">i2", 32: ">i4", -32: ">f4", -64: ">f8"}.get(bitpix)
        if dtype is None:
            raise ValueError(f"unsupported BITPIX {bitpix}")

        count = width * height
        raw = handle.read(count * np.dtype(dtype).itemsize)
    if len(raw) < count * np.dtype(dtype).itemsize:
        raise ValueError("truncated FITS data")

    data = np.frombuffer(raw, dtype=dtype, count=count)
    values = data.astype(np.float64) * float(header.get("BSCALE", 1)) + float(header.get("BZERO", 0))
    image = np.clip(values, 0, 65535).astype(np.uint16).reshape(height, width)
    return image, header


def utc_now() -> str:
    """DATE-OBS timestamp in the format the standard expects."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
