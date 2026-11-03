"""The upload rules: every frame on a collaboration must be calibratable.

Bray's rule for the files page, in one place: a contributor's lights only
count once the flats, and the darks (or bias), that calibrate them are on
the server too. Image quality - stars, focus, cloud - is *not* judged here;
the stacker (Siril) weighs and rejects subs at stacking time.

What happens to each file the moment it lands:

* Its header is read (FITS, compressed FITS or XISF; header only, never
  the pixels), and the few facts calibration matching needs are kept.
* A light whose header cannot be read, or that lacks a fact matching needs
  (exposure, filter, camera, gain), is **quarantined**: kept on disk, never
  deleted, never counted, with the reason shown to the contributor.
* A light with everything it needs is **ready**; one still waiting for its
  flats or darks is **pending calibration**. It moves to ready by itself
  when the missing calibration is uploaded - nothing to upload twice.
* A calibration frame that cannot be matched to anything (a dark with no
  exposure, a flat with no filter) is quarantined the same way.

Matching is per contributor and per camera (and per telescope label, when
both sides carry one). The tolerances are per collaboration: `DEFAULTS`
below, overridden by the project's `uploadRules`, which only its owner can
change. Everything here is pure: no database, no web framework, so it can be
tested on headers alone.
"""

from __future__ import annotations

import math
import re
import struct
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:  # the server ships astrocontrol beside it; the tests run from the repo
    from astrocontrol.filters import canonical as _canonical_filter
except Exception:  # pragma: no cover - only without astrocontrol on the path
    def _canonical_filter(name: Any) -> str:
        return " ".join(str(name or "").split())

# ---------------------------------------------------------------------------
# The rules and their defaults
# ---------------------------------------------------------------------------

#: Every rule a coordinator can change, with its default and what it means.
#: (name, default, kind, min, max, plain words). The page's checklist, the
#: owner's settings form and the validation all come from this one table.
RULES: tuple[tuple[str, Any, str, Any, Any, str], ...] = (
    ("enforce", True, "bool", None, None,
     "Check uploads against these rules (off: everything is taken as ready)"),
    ("requireFlats", True, "bool", None, None,
     "Lights need flats in the same filter, camera and binning"),
    ("darkPolicy", "darkOrBias", "choice", ("darkOrBias", "dark", "none"), None,
     "Lights need matching darks, or bias when darks are not used "
     "(darkOrBias), darks only (dark), or neither (none)"),
    ("darkExposureTolerancePct", 2.0, "float", 0.0, 100.0,
     "A dark's exposure may differ from the light's by this many percent"),
    ("darkExposureToleranceS", 1.0, "float", 0.0, 3600.0,
     "... or by this many seconds, whichever is larger"),
    ("temperatureToleranceC", 3.0, "float", 0.0, 50.0,
     "A dark's or bias's sensor temperature may differ by this many degrees C "
     "(bias: only checked when biasTemperature is on)"),
    ("biasTemperature", False, "bool", None, None,
     "Also match bias frames on temperature"),
    ("matchGain", True, "bool", None, None,
     "Darks and bias must have the light's gain (or ISO)"),
    ("matchOffset", True, "bool", None, None,
     "Darks and bias must have the light's offset"),
    ("matchReadoutMode", True, "bool", None, None,
     "Darks and bias must have the light's readout mode "
     "(e.g. High vs Low Conversion Gain)"),
    ("flatRotatorToleranceDeg", 2.0, "float_or_none", 0.0, 360.0,
     "Flats must be shot within this many degrees of the light's rotator angle "
     "(blank: rotator not checked)"),
    ("flatMaxAgeDays", None, "float_or_none", 0.0, 3650.0,
     "Flats may be at most this many days older or newer than the light "
     "(blank: any age)"),
    ("darkMaxAgeDays", None, "float_or_none", 0.0, 3650.0,
     "Darks and bias may be at most this many days from the light "
     "(blank: any age)"),
    ("allowUnrecorded", True, "bool", None, None,
     "A calibration frame that does not record a setting (PixInsight masters "
     "often drop gain, offset and temperature) is accepted for it, with a note; "
     "off: it must record everything the light records"),
    ("matchTelescopeLabel", False, "bool", None, None,
     "Calibration must carry the same telescope label as the lights (the label typed "
     "on the upload page); off: camera, focal length, binning and sensor size decide"),
    ("requireTemperature", False, "bool", None, None,
     "Lights must record their sensor temperature"),
)

DEFAULTS: dict[str, Any] = {name: default for name, default, *_ in RULES}
_SPEC = {name: (kind, lo, hi) for name, _d, kind, lo, hi, _w in RULES}

#: Statuses a file can be in. '' is a file uploaded before the rules existed.
READY, PENDING, QUARANTINE, UNCHECKED = "ready", "pending", "quarantine", ""
STATUS_LABELS = {READY: "Ready", PENDING: "Pending calibration",
                 QUARANTINE: "Quarantined", UNCHECKED: "Not checked"}


def rules_of(project: dict[str, Any] | None) -> dict[str, Any]:
    """The rules a project runs under: the defaults with its own on top."""
    own = ((project or {}).get("payload") or {}).get("uploadRules") or {}
    merged = dict(DEFAULTS)
    if isinstance(own, dict):
        for key, value in own.items():
            if key in merged:
                merged[key] = value
    return merged


def clean_rules(asked: Any) -> dict[str, Any]:
    """What a coordinator sent, checked: only known rules, in range.

    Raises ValueError with the plain reason. Returns only what was sent, so a
    project stores its *changes* from the defaults and picks up better
    defaults later.
    """
    if not isinstance(asked, dict):
        raise ValueError("upload rules must be an object")
    out: dict[str, Any] = {}
    for key, value in asked.items():
        if key not in _SPEC:
            raise ValueError(f"unknown upload rule: {key}")
        kind, lo, hi = _SPEC[key]
        if kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
            out[key] = value
        elif kind == "choice":
            if value not in lo:
                raise ValueError(f"{key} must be one of " + ", ".join(lo))
            out[key] = value
        else:
            if value is None or value == "":
                if kind != "float_or_none":
                    raise ValueError(f"{key} needs a number")
                out[key] = None
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} needs a number") from None
            if not math.isfinite(number) or number < lo or number > hi:
                raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
            out[key] = number
    return out


def checklist(rules: dict[str, Any]) -> list[str]:
    """The rules in plain words, as the upload page shows them."""
    items = ["FITS or XISF, as the camera saved it: the header must say the exposure, "
             "filter (or Bayer pattern for colour cameras), camera and gain or ISO"
             + (", and sensor temperature" if rules.get("requireTemperature") else "")]
    if not rules.get("enforce", True):
        return items + ["This collaboration does not check calibration: every frame counts"]
    if rules.get("requireFlats", True):
        rot = rules.get("flatRotatorToleranceDeg")
        age = rules.get("flatMaxAgeDays")
        items.append("Flats for every filter you shot, same camera and binning"
                     + (f", rotator within {rot:g}°" if rot is not None else "")
                     + (f", within {age:g} days of the lights" if age is not None else ""))
    policy = rules.get("darkPolicy", "darkOrBias")
    match = [w for w, on in (("gain", rules.get("matchGain", True)),
                             ("offset", rules.get("matchOffset", True)),
                             ("readout mode", rules.get("matchReadoutMode", True))) if on]
    same = (", same " + ", ".join(match)) if match else ""
    tol = (f"exposure within {rules.get('darkExposureTolerancePct', 2):g}% "
           f"or {rules.get('darkExposureToleranceS', 1):g} s")
    temp = f"temperature within {rules.get('temperatureToleranceC', 3):g} °C"
    if policy == "dark":
        items.append(f"Darks for every exposure you shot: {tol}, {temp}{same}")
    elif policy == "darkOrBias":
        items.append(f"Darks for every exposure you shot ({tol}, {temp}{same}), "
                     f"or bias frames{same} if you do not use darks")
    if rules.get("allowUnrecorded", True):
        items.append("Masters that do not record gain, offset or temperature are accepted "
                     "for those settings")
    items.append("Lights without their calibration wait as 'pending calibration' and do not "
                 "count until it arrives; upload it any time and they move to ready by themselves")
    items.append("Frames that fail a rule are quarantined with the reason, never deleted; "
                 "image quality is judged later, at stacking")
    return items


# ---------------------------------------------------------------------------
# Reading headers
# ---------------------------------------------------------------------------

BLOCK = 2880
#: The most header read from a FITS: 100 blocks is 3,600 cards, far beyond
#: anything a camera writes, and bounds what a hostile file can make us read.
MAX_HEADER_BLOCKS = 100
#: The most XISF header XML read.
MAX_XISF_HEADER = 4 * 1024 * 1024

_CARD = re.compile(r"^([A-Z0-9_\-]{1,8})\s*=\s*(.*)$")


class HeaderError(ValueError):
    """A file that starts like a FITS or XISF but whose header cannot be read."""


def _card_value(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("'"):
        # A quoted string; '' inside is a quote.
        out, i = [], 1
        while i < len(raw):
            ch = raw[i]
            if ch == "'":
                if i + 1 < len(raw) and raw[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                break
            out.append(ch)
            i += 1
        return "".join(out).rstrip()
    return raw.split("/", 1)[0].strip()


def _read_fits_header(handle, start: int) -> tuple[dict[str, str], int]:
    """One FITS header from `start`: its cards and where its data begins."""
    handle.seek(start)
    cards: dict[str, str] = {}
    for block in range(MAX_HEADER_BLOCKS):
        chunk = handle.read(BLOCK)
        if len(chunk) < BLOCK:
            raise HeaderError("the header stops before its END card (file cut short?)")
        text = chunk.decode("ascii", "replace")
        for i in range(0, BLOCK, 80):
            card = text[i:i + 80]
            key = card[:8].strip()
            if key == "END":
                return cards, start + (block + 1) * BLOCK
            if card[8:10] == "= ":
                match = _CARD.match(card[:8].rstrip() + card[8:])
                if match and key not in cards:
                    cards[key] = _card_value(match.group(2))
    raise HeaderError("no END card in the first 100 header blocks")


def _data_bytes(cards: dict[str, str]) -> int:
    try:
        bitpix = abs(int(float(cards.get("BITPIX", "0"))))
        naxis = int(float(cards.get("NAXIS", "0")))
        if naxis == 0:
            return 0
        size = 1
        for n in range(1, naxis + 1):
            size *= int(float(cards.get(f"NAXIS{n}", "0")))
        size *= int(float(cards.get("GCOUNT", "1"))) if "GCOUNT" in cards else 1
        size = (size + int(float(cards.get("PCOUNT", "0")))) if "PCOUNT" in cards else size
        size = size * bitpix // 8
    except (TypeError, ValueError):
        raise HeaderError("BITPIX or NAXIS in the header is not a number") from None
    return ((size + BLOCK - 1) // BLOCK) * BLOCK


def read_header(path: Path | str) -> dict[str, Any]:
    """The header cards of a FITS, .fits.fz or XISF file, without its pixels.

    Returns {"format", "cards", "width", "height", "truncated"}. Raises
    HeaderError when it cannot be read.
    """
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as handle:
        head = handle.read(16)
        if head.startswith(b"XISF0100"):
            return _read_xisf(handle, head, size)
        if not head.startswith(b"SIMPLE"):
            raise HeaderError("not a FITS or XISF file")
        cards, data_at = _read_fits_header(handle, 0)
        if cards.get("SIMPLE", "").upper() not in ("T", "TRUE"):
            raise HeaderError("SIMPLE is not T: not a standard FITS file")
        if "BITPIX" not in cards or "NAXIS" not in cards:
            raise HeaderError("the header has no BITPIX or NAXIS")
        primary_end = data_at + _data_bytes(cards)
        fmt = "fits"
        merged = dict(cards)
        width = cards.get("NAXIS1")
        height = cards.get("NAXIS2")
        # Tile-compressed (fpack) or an image in the first extension: the
        # camera's keywords are on that extension's header.
        if int(float(cards.get("NAXIS", "0"))) == 0 and size > primary_end:
            ext, ext_data = _read_fits_header(handle, primary_end)
            for key, value in ext.items():
                if key not in ("SIMPLE", "XTENSION"):
                    merged[key] = value
            if ext.get("ZIMAGE", "").upper() in ("T", "TRUE"):
                fmt = "fits.fz"
                width, height = ext.get("ZNAXIS1"), ext.get("ZNAXIS2")
            else:
                width, height = ext.get("NAXIS1"), ext.get("NAXIS2")
            primary_end = ext_data + _data_bytes(ext)
        truncated = size < primary_end
        return {"format": fmt, "cards": merged, "width": _int(width),
                "height": _int(height), "truncated": truncated}


def _read_xisf(handle, head: bytes, size: int) -> dict[str, Any]:
    import xml.etree.ElementTree as ET
    length = struct.unpack("<I", head[8:12])[0]
    if length <= 0 or length > MAX_XISF_HEADER or 16 + length > size:
        raise HeaderError("the XISF header length is not plausible")
    xml = handle.read(length)
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise HeaderError("the XISF header has a DOCTYPE, which is not taken")
    try:
        root = ET.fromstring(xml.rstrip(b"\x00"))
    except ET.ParseError as exc:
        raise HeaderError(f"the XISF header is not valid XML ({exc})") from None
    cards: dict[str, str] = {}
    width = height = None
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "FITSKeyword":
            name = (el.get("name") or "").strip().upper()
            value = el.get("value") or ""
            value = value.strip()
            if value.startswith("'") and value.endswith("'") and len(value) >= 2:
                value = value[1:-1].replace("''", "'").strip()
            if name and name not in cards:
                cards[name] = value
        elif tag == "Image" and width is None:
            geometry = (el.get("geometry") or "").split(":")
            if len(geometry) >= 2:
                width, height = _int(geometry[0]), _int(geometry[1])
    return {"format": "xisf", "cards": cards, "width": width, "height": height,
            "truncated": False}


# ---------------------------------------------------------------------------
# The facts matching needs
# ---------------------------------------------------------------------------

_KIND_FROM_TYPE = (
    (re.compile(r"dark\s*[_\-]?\s*flat|flat\s*[_\-]?\s*dark|flatdark|darkflat", re.I), "darkflat"),
    (re.compile(r"flat", re.I), "flat"),
    (re.compile(r"dark", re.I), "dark"),
    (re.compile(r"bias|offset|zero", re.I), "bias"),
    (re.compile(r"light|object|science", re.I), "light"),
)
_EXPOSURE_IN_NAME = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds)(?![a-z])", re.I)


def _int(value: Any) -> int | None:
    try:
        return int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def kind_from_type(imagetyp: str) -> str:
    """'Master Dark' -> dark, 'Flat Field' -> flat, 'LIGHT' -> light, else ''."""
    for pattern, kind in _KIND_FROM_TYPE:
        if pattern.search(imagetyp or ""):
            return kind
    return ""


def camera_key(name: str) -> str:
    """A camera name reduced so the same camera from two drivers compares equal:
    'ZWO ASI6200MM Pro (ASCOM)' and 'ASI Camera (1) ? CamID 0, ZWO ASI6200MM Pro'."""
    text = re.sub(r"\((?:ascom|native|indi|alpaca)\)", " ", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", "", text)


def same_camera(a: str, b: str) -> bool:
    if not a or not b:
        return True
    return a == b or a in b or b in a


def facts(header: dict[str, Any], name: str = "") -> dict[str, Any]:
    """What calibration matching needs from a header, in one shape."""
    c = header.get("cards") or {}
    exposure = _float(c.get("EXPTIME")) if "EXPTIME" in c else _float(c.get("EXPOSURE"))
    exposure_from = "header" if exposure is not None else ""
    if exposure is None and name:
        found = _EXPOSURE_IN_NAME.findall(name)
        if found:
            exposure, exposure_from = float(found[-1]), "name"
    bayer = _text(c.get("BAYERPAT"))
    raw_filter = _text(c.get("FILTER"))
    gain = _float(c.get("GAIN"))
    iso = _float(c.get("ISOSPEED")) if "ISOSPEED" in c else _float(c.get("ISO"))
    readout = _text(c.get("READOUTM") or c.get("READMODE") or c.get("READOUT"))
    ccd = _float(c.get("CCD-TEMP")) if "CCD-TEMP" in c else _float(c.get("CCD_TEMP"))
    set_temp = _float(c.get("SET-TEMP")) if "SET-TEMP" in c else _float(c.get("SETTEMP"))
    rotator = _float(c.get("ROTATOR")) if "ROTATOR" in c else _float(c.get("ROTATANG"))
    camera = _text(c.get("INSTRUME"))
    imagetyp = _text(c.get("IMAGETYP") or c.get("FRAME"))
    return {
        "format": header.get("format", ""),
        "imagetyp": imagetyp,
        "headerKind": kind_from_type(imagetyp),
        "filter": _canonical_filter(raw_filter) if raw_filter else ("OSC" if bayer else ""),
        "filterRaw": raw_filter,
        "bayer": bayer,
        "exposure": exposure,
        "exposureFrom": exposure_from,
        "gain": gain if gain is not None else iso,
        "gainIsIso": gain is None and iso is not None,
        "offset": _float(c.get("OFFSET")) if "OFFSET" in c else _float(c.get("BLKLEVEL")),
        "readout": readout,
        # The sensor as it was, else what it was set to.
        "temp": ccd if ccd is not None else set_temp,
        "xbin": _int(c.get("XBINNING")) or 1,
        "ybin": _int(c.get("YBINNING")) or _int(c.get("XBINNING")) or 1,
        "width": header.get("width"),
        "height": header.get("height"),
        "focal": _float(c.get("FOCALLEN")),
        "camera": camera,
        "cameraKey": camera_key(camera),
        "rotator": rotator,
        "date": _text(c.get("DATE-OBS") or c.get("DATE-LOC") or c.get("DATE")),
        "truncated": bool(header.get("truncated")),
    }


def facts_of_file(path: Path | str, name: str = "") -> dict[str, Any]:
    """The facts of a file on disk, or {"error": why} when its header is unreadable."""
    try:
        return facts(read_header(path), name or Path(path).name)
    except HeaderError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"the file could not be read ({exc.strerror or exc})"}


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------

def _fmt(x: float | None, unit: str = "") -> str:
    if x is None:
        return "?"
    return f"{x:g}{unit}"


def header_problems(kind: str, f: dict[str, Any], rules: dict[str, Any]) -> list[str]:
    """Why a file cannot be matched at all, from its own header. [] = fine."""
    if f.get("error"):
        return [f["error"]]
    out = []
    if f.get("truncated"):
        out.append("the file is shorter than its header says (cut off during upload or copy?)")
    hk = f.get("headerKind") or ""
    if kind == "light":
        if hk and hk != "light":
            out.append(f"the header says this is a {hk} frame (IMAGETYP '{f.get('imagetyp')}'); "
                       f"upload it in the {hk} box")
        if f.get("exposure") is None:
            out.append("no exposure time in the header (EXPTIME)")
        if not f.get("filter"):
            out.append("no filter in the header (FILTER), and no Bayer pattern for a colour camera")
        if not f.get("camera"):
            out.append("no camera in the header (INSTRUME)")
        if f.get("gain") is None:
            out.append("no gain or ISO in the header (GAIN)")
        if rules.get("requireTemperature") and f.get("temp") is None:
            out.append("no sensor temperature in the header (CCD-TEMP or SET-TEMP)")
    elif kind == "dark" or kind == "darkflat":
        if f.get("exposure") is None:
            out.append("no exposure time in the header (EXPTIME) or in the file name, "
                       "so it cannot be matched to any light")
    elif kind == "flat":
        if not f.get("filter"):
            out.append("no filter in the header (FILTER), so it cannot be matched to any light")
    return out


def _days_apart(a: str, b: str) -> float | None:
    def parse(text: str):
        try:
            return datetime.fromisoformat(text[:19])
        except ValueError:
            return None
    da, db = parse(a or ""), parse(b or "")
    if da is None or db is None:
        return None
    return abs((da - db).total_seconds()) / 86400.0


def _rot_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


class _Unrecorded:
    """Settings a calibration frame does not record, gathered as notes."""

    def __init__(self) -> None:
        self.names: set[str] = set()


def _common(light: dict, cal: dict, rules: dict) -> bool:
    if not same_camera(light.get("cameraKey", ""), cal.get("cameraKey", "")):
        return False
    if (rules.get("matchTelescopeLabel") and light.get("label") and cal.get("label")
            and light["label"].strip().lower() != cal["label"].strip().lower()):
        return False
    lf, cf = light.get("focal"), cal.get("focal")
    if lf and cf and abs(lf - cf) > 0.05 * lf:
        return False
    if (light.get("xbin"), light.get("ybin")) != (cal.get("xbin"), cal.get("ybin")):
        return False
    lw, lh, cw, ch = light.get("width"), light.get("height"), cal.get("width"), cal.get("height")
    if lw and lh and cw and ch and (lw, lh) != (cw, ch):
        return False
    return True


def _setting(light: dict, cal: dict, key: str, on: bool, rules: dict,
             unrec: _Unrecorded, word: str, close=None) -> bool:
    """Whether a calibration frame agrees with the light on one setting."""
    if not on:
        return True
    lv, cv = light.get(key), cal.get(key)
    if lv in (None, ""):
        return True                      # the light does not say: nothing to compare
    if cv in (None, ""):
        if rules.get("allowUnrecorded", True):
            unrec.names.add(word)
            return True
        return False
    if close is not None:
        return close(lv, cv)
    if isinstance(lv, str):
        return lv.strip().lower() == str(cv).strip().lower()
    return abs(float(lv) - float(cv)) < 1e-6


def _match_dark_like(light: dict, cal: dict, rules: dict, unrec: _Unrecorded,
                     exposure: bool, temperature: bool) -> bool:
    if not _common(light, cal, rules):
        return False
    if exposure:
        le, ce = light.get("exposure"), cal.get("exposure")
        if le is None or ce is None:
            return False
        tol = max(le * float(rules.get("darkExposureTolerancePct", 2.0)) / 100.0,
                  float(rules.get("darkExposureToleranceS", 1.0)))
        if abs(le - ce) > tol + 1e-9:
            return False
    ok = (_setting(light, cal, "gain", rules.get("matchGain", True), rules, unrec, "gain")
          and _setting(light, cal, "offset", rules.get("matchOffset", True), rules, unrec, "offset")
          and _setting(light, cal, "readout", rules.get("matchReadoutMode", True), rules, unrec,
                       "readout mode"))
    if ok and temperature:
        tol = float(rules.get("temperatureToleranceC", 3.0))
        ok = _setting(light, cal, "temp", True, rules, unrec, "temperature",
                      close=lambda a, b: abs(a - b) <= tol + 1e-9)
    age = rules.get("darkMaxAgeDays")
    if ok and age is not None:
        apart = _days_apart(light.get("date", ""), cal.get("date", ""))
        ok = apart is None or apart <= float(age)
    return ok


def _match_flat(light: dict, cal: dict, rules: dict, unrec: _Unrecorded) -> bool:
    if not _common(light, cal, rules):
        return False
    if (light.get("filter") or "").lower() != (cal.get("filter") or "").lower():
        return False
    tol = rules.get("flatRotatorToleranceDeg")
    if tol is not None and light.get("rotator") is not None:
        if cal.get("rotator") is None:
            if not rules.get("allowUnrecorded", True):
                return False
            unrec.names.add("rotator angle (flat)")
        elif _rot_diff(light["rotator"], cal["rotator"]) > float(tol) + 1e-9:
            return False
    age = rules.get("flatMaxAgeDays")
    if age is not None:
        apart = _days_apart(light.get("date", ""), cal.get("date", ""))
        if apart is not None and apart > float(age):
            return False
    return True


def _describe_light(light: dict) -> dict[str, str]:
    parts = {
        "filter": light.get("filterRaw") or light.get("filter") or "?",
        "exposure": _fmt(light.get("exposure"), " s"),
        "gain": ("ISO " if light.get("gainIsIso") else "gain ") + _fmt(light.get("gain")),
        "offset": "offset " + _fmt(light.get("offset")),
        # Rounded: -9.9 and -10.1 °C ask for the same darks.
        "temp": _fmt(None if light.get("temp") is None else float(round(light["temp"])) + 0.0, " °C"),
        "bin": f"{light.get('xbin', 1)}x{light.get('ybin', 1)}",
        "readout": light.get("readout") or "",
        "rotator": _fmt(None if light.get("rotator") is None else round(light["rotator"], 1), "°"),
        "camera": light.get("camera") or "?",
    }
    return parts


def judge_light(light: dict, cals: Iterable[dict], rules: dict) -> tuple[str, list[str], dict]:
    """A light's status, the reasons, and what calibrates it.

    `light` and each of `cals` are facts plus "kind", "id", "label".
    Calibration frames that are themselves quarantined must not be passed.
    """
    problems = header_problems("light", light, rules)
    if problems:
        return QUARANTINE, problems, {}
    notes: list[str] = []
    if not rules.get("enforce", True):
        return READY, ["calibration is not checked on this collaboration"], {}
    cals = list(cals)
    d = _describe_light(light)
    missing: list[str] = []
    used: dict[str, str] = {}
    unrec = _Unrecorded()
    if rules.get("requireFlats", True):
        flat = next((c for c in cals if c.get("kind") == "flat"
                     and _match_flat(light, c, rules, unrec)), None)
        if flat is None:
            what = f"flats in filter {d['filter']}, {d['bin']}, camera {d['camera']}"
            if rules.get("flatRotatorToleranceDeg") is not None and light.get("rotator") is not None:
                what += f", rotator {d['rotator']} (±{rules['flatRotatorToleranceDeg']:g}°)"
            missing.append("needs " + what)
        else:
            used["flat"] = flat.get("id", "")
    policy = rules.get("darkPolicy", "darkOrBias")
    if policy in ("dark", "darkOrBias"):
        dark = next((c for c in cals if c.get("kind") == "dark"
                     and _match_dark_like(light, c, rules, unrec, True, True)), None)
        if dark is not None:
            used["dark"] = dark.get("id", "")
        else:
            settings = [d["gain"], d["offset"]] + ([d["readout"]] if d["readout"] else [])
            dark_what = f"darks at {d['exposure']}, {d['temp']}, " + ", ".join(settings)
            if policy == "darkOrBias":
                bias = next((c for c in cals if c.get("kind") == "bias"
                             and _match_dark_like(light, c, rules, unrec, False,
                                                  bool(rules.get("biasTemperature")))), None)
                if bias is not None:
                    used["bias"] = bias.get("id", "")
                    notes.append("no matching dark: calibrated with bias (fine for cooled "
                                 "cameras with low dark current)")
                else:
                    missing.append("needs " + dark_what + ", or bias at " + ", ".join(settings))
            else:
                missing.append("needs " + dark_what)
    if unrec.names and not missing:
        notes.append("a calibration master does not record its " + ", ".join(sorted(unrec.names))
                     + "; accepted as matching")
    if light.get("exposureFrom") == "name":
        notes.append("exposure read from the file name")
    if missing:
        return PENDING, missing, used
    return READY, notes, used


def judge_calibration(kind: str, f: dict, rules: dict) -> tuple[str, list[str]]:
    problems = header_problems(kind, f, rules)
    if problems:
        return QUARANTINE, problems
    notes = []
    hk = f.get("headerKind") or ""
    if hk and hk != kind and not (kind == "darkflat" and hk == "dark"):
        notes.append(f"the header says '{f.get('imagetyp')}'; kept as {kind} because it was "
                     f"uploaded as {kind}")
    if f.get("exposureFrom") == "name":
        notes.append("exposure read from the file name")
    return READY, notes


def judge_all(rows: list[dict], rules: dict) -> dict[str, tuple[str, list[str]]]:
    """Every file of one contributor on one collaboration, judged together.

    `rows` carry "id", "kind", "label" and the facts under "meta". Returns
    {id: (status, reasons)}. Rows with no facts (uploaded before the rules)
    are left out: they stay 'not checked' until rechecked.
    """
    out: dict[str, tuple[str, list[str]]] = {}
    cals: list[dict] = []
    lights: list[dict] = []
    for row in rows:
        meta = row.get("meta") or {}
        if not meta:
            continue
        item = dict(meta, id=row["id"], kind=row["kind"], label=row.get("label") or "")
        if row["kind"] == "light":
            lights.append(item)
        else:
            status, reasons = judge_calibration(row["kind"], meta, rules)
            out[row["id"]] = (status, reasons)
            if status != QUARANTINE:
                cals.append(item)
    # Lights shot the same way get the same answer: judge one of each.
    seen: dict[tuple, tuple[str, list[str]]] = {}
    keys = ("error", "truncated", "headerKind", "exposure", "exposureFrom", "filter",
            "filterRaw", "camera", "cameraKey", "gain", "gainIsIso", "offset", "readout",
            "temp", "xbin", "ybin", "width", "height", "rotator", "label", "imagetyp", "focal")
    dated = rules.get("flatMaxAgeDays") is not None or rules.get("darkMaxAgeDays") is not None
    for light in lights:
        sig = tuple(str(light.get(k)) for k in keys) + ((light.get("date", "")[:10],) if dated else ())
        if sig not in seen:
            status, reasons, _used = judge_light(light, cals, rules)
            seen[sig] = (status, reasons)
        out[light["id"]] = seen[sig]
    return out
