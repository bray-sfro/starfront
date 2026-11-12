"""Small, valid FITS / fpack-shaped / XISF files for the upload-rule tests."""

from __future__ import annotations

import struct
from pathlib import Path


def _card(key: str, value) -> bytes:
    if isinstance(value, bool):
        v = "T" if value else "F"
        text = f"{key:<8}= {v:>20}"
    elif isinstance(value, (int, float)):
        text = f"{key:<8}= {value!r:>20}"
    else:
        s = str(value).replace("'", "''")
        text = f"{key:<8}= '{s:<8}'"
    return text[:80].ljust(80).encode("ascii")


def _header(cards: list[tuple[str, object]]) -> bytes:
    out = b"".join(_card(k, v) for k, v in cards) + b"END".ljust(80)
    return out + b" " * ((-len(out)) % 2880)


def light_cards(**over) -> dict:
    cards = {"IMAGETYP": "LIGHT", "EXPTIME": 300.0, "GAIN": 100, "OFFSET": 50,
             "CCD-TEMP": -10.0, "SET-TEMP": -10.0, "XBINNING": 1, "YBINNING": 1,
             "INSTRUME": "ZWO ASI2600MM Pro", "TELESCOP": "Test 80", "FOCALLEN": 480.0,
             "XPIXSZ": 3.76, "FILTER": "Ha", "DATE-OBS": "2026-10-20T03:00:00",
             "ROTATOR": 90.0}
    for k, v in over.items():
        k = k.replace("_", "-") if k in ("CCD_TEMP", "SET_TEMP", "DATE_OBS") else k
        if v is None:
            cards.pop(k, None)
        else:
            cards[k] = v
    return cards


def write_fits(path: Path, cards: dict, width: int = 64, height: int = 48,
               truncate: int = 0) -> Path:
    base = [("SIMPLE", True), ("BITPIX", 16), ("NAXIS", 2), ("NAXIS1", width),
            ("NAXIS2", height), ("BZERO", 32768)]
    data = b"\x00\x10" * (width * height)
    data += b"\x00" * ((-len(data)) % 2880)
    blob = _header(base + list(cards.items())) + data
    if truncate:
        blob = blob[:-truncate]
    path.write_bytes(blob)
    return path


def write_fz(path: Path, cards: dict, width: int = 64, height: int = 48) -> Path:
    """The shape fpack writes: an empty primary, then a compressed-image table."""
    primary = _header([("SIMPLE", True), ("BITPIX", 16), ("NAXIS", 0), ("EXTEND", True)])
    ext = [("XTENSION", "BINTABLE"), ("BITPIX", 8), ("NAXIS", 2), ("NAXIS1", 8),
           ("NAXIS2", 1), ("PCOUNT", 0), ("GCOUNT", 1), ("TFIELDS", 1),
           ("ZIMAGE", True), ("ZBITPIX", 16), ("ZNAXIS", 2), ("ZNAXIS1", width),
           ("ZNAXIS2", height)] + list(cards.items())
    data = b"\x00" * 8
    data += b"\x00" * ((-len(data)) % 2880)
    path.write_bytes(primary + _header(ext) + data)
    return path


def write_xisf(path: Path, cards: dict, width: int = 64, height: int = 48) -> Path:
    kws = "".join(
        f'<FITSKeyword name="{k}" value="{(chr(39) + str(v) + chr(39)) if isinstance(v, str) else v}" comment=""/>'
        for k, v in cards.items())
    pixels = b"\x00\x10" * (width * height)
    for _ in range(2):  # the attachment offset depends on the header length
        offset = 16 + len(xml) if "xml" in dir() else 4096
        xml = (f'<?xml version="1.0" encoding="UTF-8"?><xisf version="1.0" '
               f'xmlns="http://www.pixinsight.com/xisf"><Image geometry="{width}:{height}:1" '
               f'sampleFormat="UInt16" location="attachment:{offset}:{len(pixels)}">{kws}</Image></xisf>')
    head = xml.encode()
    path.write_bytes(b"XISF0100" + struct.pack("<I", len(head)) + b"\x00" * 4 + head + pixels)
    return path
