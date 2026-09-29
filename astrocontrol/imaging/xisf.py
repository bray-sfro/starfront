"""Reading XISF, PixInsight's own format, far enough to take a master frame.

A master built in PixInsight is what most people already have, and it is an
XISF file: a small XML header describing the image and its FITS-style
keywords, followed by the pixels as an attachment. This reads the monolithic
form - one file, one image - which is what PixInsight writes for a master.
It is not a general XISF library: one image, planar or first-channel, the
five sample formats a master can be in, and zlib compression with or
without byte shuffling. LZ4 needs a library this program does not carry and
is refused with a message that says so.

Only the parts a calibration master needs. A colour master is read as its
first channel; a flat with three planes is three flats, and the other two
are somebody else's problem to split.
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any

import numpy as np

SIGNATURE = b"XISF0100"
NAMESPACE = "http://www.pixinsight.com/xisf"

_FORMATS = {
    "UInt8": np.uint8, "UInt16": np.uint16, "UInt32": np.uint32,
    "Float32": np.float32, "Float64": np.float64,
}


def is_xisf(path: str | Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(8) == SIGNATURE
    except OSError:
        return False


def _header(handle) -> tuple[ET.Element, int]:
    """The XML root and the byte offset the attachments count from."""
    if handle.read(8) != SIGNATURE:
        raise ValueError("not an XISF file (no XISF0100 signature)")
    length = struct.unpack("<I", handle.read(4))[0]
    handle.read(4)                                   # reserved
    text = handle.read(length).decode("utf-8", "replace")
    root = ET.fromstring(text)
    return root, 16 + length


def _tag(name: str) -> str:
    return f"{{{NAMESPACE}}}{name}"


def _first_image(root: ET.Element) -> ET.Element:
    for element in root.iter():
        if element.tag in (_tag("Image"), "Image"):
            return element
    raise ValueError("the XISF file holds no image")


def _keywords(image: ET.Element) -> dict[str, Any]:
    """The FITS keywords PixInsight carried along, as a header dict."""
    header: dict[str, Any] = {}
    for element in image.iter():
        if element.tag not in (_tag("FITSKeyword"), "FITSKeyword"):
            continue
        name = (element.get("name") or "").strip().upper()
        if not name:
            continue
        raw = (element.get("value") or "").strip()
        header[name] = _value(raw)
    return header


def _value(raw: str) -> Any:
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].strip()
    if raw in ("T", "F"):
        return raw == "T"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def read_header(path: str | Path) -> dict[str, Any]:
    """The image's FITS keywords plus its geometry, without the pixels."""
    with open(path, "rb") as handle:
        root, _ = _header(handle)
    image = _first_image(root)
    header = _keywords(image)
    geometry = [int(v) for v in (image.get("geometry") or "0:0:1").split(":")]
    header.setdefault("NAXIS1", geometry[0])
    header.setdefault("NAXIS2", geometry[1] if len(geometry) > 1 else 0)
    header["XISF_FORMAT"] = image.get("sampleFormat") or ""
    header["XISF_CHANNELS"] = geometry[2] if len(geometry) > 2 else 1
    return header


def read(path: str | Path) -> tuple[np.ndarray, dict[str, Any]]:
    """The first channel of the image as a 2-D array in its own sample
    format, and the header. Floats come back as the file stored them
    (PixInsight normalises to 0-1); the caller decides how to scale them."""
    with open(path, "rb") as handle:
        root, base = _header(handle)
        image = _first_image(root)
        header = _keywords(image)

        geometry = [int(v) for v in (image.get("geometry") or "").split(":") if v]
        if len(geometry) < 2:
            raise ValueError("the XISF image has no geometry")
        width, height = geometry[0], geometry[1]
        channels = geometry[2] if len(geometry) > 2 else 1
        fmt = image.get("sampleFormat") or "UInt16"
        dtype = _FORMATS.get(fmt)
        if dtype is None:
            raise ValueError(f"unsupported XISF sample format {fmt}")
        little = (image.get("byteOrder") or "little").lower() != "big"

        location = (image.get("location") or "").split(":")
        if len(location) < 3 or location[0] != "attachment":
            raise ValueError("the XISF image is not stored as an attachment "
                             "(only monolithic files are supported)")
        position, size = int(location[1]), int(location[2])
        del base                                     # positions are absolute
        handle.seek(position)
        raw = handle.read(size)
    if len(raw) < size:
        raise ValueError("truncated XISF data")

    compression = image.get("compression") or ""
    if compression:
        raw = _decompress(raw, compression, np.dtype(dtype).itemsize)

    count = width * height * channels
    kind = np.dtype(dtype).newbyteorder("<" if little else ">")
    data = np.frombuffer(raw, dtype=kind, count=count)
    # Planar storage is the default and the only one PixInsight writes for
    # masters: channel after channel, each a whole plane.
    if (image.get("pixelStorage") or "Planar").lower() == "normal":
        planes = data.reshape(height, width, channels)
        first = planes[:, :, 0]
    else:
        first = data[:width * height].reshape(height, width)
    header.setdefault("NAXIS1", width)
    header.setdefault("NAXIS2", height)
    header["XISF_FORMAT"] = fmt
    header["XISF_CHANNELS"] = channels
    return np.ascontiguousarray(first.astype(dtype)), header


def _decompress(raw: bytes, spec: str, item_size: int) -> bytes:
    """`zlib:<size>` or `zlib+sh:<size>:<shuffle item size>`; LZ4 is refused."""
    parts = spec.split(":")
    codec = parts[0].lower()
    if codec.startswith("lz4"):
        raise ValueError("this XISF file is LZ4-compressed, which this program "
                         "cannot read - save it uncompressed or with zlib "
                         "from PixInsight, or as FITS")
    if not codec.startswith("zlib"):
        raise ValueError(f"unsupported XISF compression {codec!r}")
    out = zlib.decompress(raw)
    if codec.endswith("+sh"):
        shuffle = int(parts[2]) if len(parts) > 2 else item_size
        out = _unshuffle(out, shuffle)
    return out


def _unshuffle(data: bytes, item_size: int) -> bytes:
    """Undo XISF byte shuffling: bytes grouped by position, back to items."""
    if item_size <= 1:
        return data
    count = len(data) // item_size
    body = np.frombuffer(data[:count * item_size], dtype=np.uint8)
    body = body.reshape(item_size, count).T.reshape(-1)
    return body.tobytes() + data[count * item_size:]
