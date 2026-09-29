"""Bring a N.I.N.A. profile across: every setting that has a home here.

Somebody arriving from N.I.N.A. has already answered a hundred questions
about their rig - focal length, pixel size, which ASCOM driver is the camera,
what is in the wheel and in what order, the backlash on the focuser, the
dither, the settle, where ASTAP is. Asking them all again is the surest way
to lose them at the door. N.I.N.A. keeps its answers in an XML profile under
`%LOCALAPPDATA%\\NINA\\Profiles`; this reads the one they last used and maps
what it can onto Starfront's settings and device slots.

Two halves, kept apart on purpose: `read` produces a *plan* - what would be
set, from what, and what was left out and why - and `apply` carries a plan
out. The plan is shown before anything is written, because a migration that
silently overwrote a working setup would cost more trust than it saved.

Only fields that mean the same thing on both sides are mapped. Where the two
programs model something differently the field is listed under "left out"
with the reason, rather than mapped to the nearest thing and quietly wrong.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

#: Where N.I.N.A. keeps profiles on Windows.
PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "NINA" / "Profiles"

NS = {"p": "http://schemas.datacontract.org/2004/07/NINA.Profile",
      "e": "http://schemas.datacontract.org/2004/07/NINA.Core.Model.Equipment",
      "i": "http://www.w3.org/2001/XMLSchema-instance"}

#: N.I.N.A.'s way of saying "no device in this slot".
NO_DEVICE = "No_Device"

#: N.I.N.A. curve fits -> Starfront focus methods.
CURVES = {"HYPERBOLIC": "hyperbolic", "PARABOLIC": "parabolic",
          "TRENDLINES": "trendlines", "TRENDPARABOLIC": "trendparabolic",
          "TRENDHYPERBOLIC": "trendhyperbolic"}


class NinaError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Finding and reading profiles
# ---------------------------------------------------------------------------

def profiles(folder: Path | None = None) -> list[dict[str, Any]]:
    """Every N.I.N.A. profile on this machine, most recently used first."""
    folder = folder or PROFILE_DIR
    found = []
    if not folder.is_dir():
        return found
    for path in folder.glob("*.profile"):
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        used = _text(root, "p:LastUsed") or ""
        found.append({
            "id": _text(root, "p:Id") or path.stem,
            "name": _text(root, "p:Name") or path.stem,
            "lastUsed": used,
            "path": str(path),
        })
    found.sort(key=lambda row: row["lastUsed"], reverse=True)
    return found


def _text(node: ET.Element, path: str, default: str | None = None) -> str | None:
    found = node.find(path, NS)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _number(node: ET.Element, path: str) -> float | None:
    raw = _text(node, path)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _flag(node: ET.Element, path: str) -> bool | None:
    raw = _text(node, path)
    if raw is None:
        return None
    return raw.strip().lower() == "true"


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------

def read(path: Path | str) -> dict[str, Any]:
    """What a profile would set here, and what it would not.

    Returns `{"profile": {...}, "settings": {section: {key: value}},
    "devices": {kind: {backend, driverId, name}}, "from": {section.key:
    "NINA field"}, "leftOut": [str]}`. Nothing is written.
    """
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise NinaError(f"{path.name} could not be read: {exc}") from exc

    settings: dict[str, dict[str, Any]] = {}
    origin: dict[str, str] = {}
    left_out: list[str] = []

    def put(section: str, key: str, value: Any, source: str) -> None:
        if value is None:
            return
        settings.setdefault(section, {})[key] = value
        origin[f"{section}.{key}"] = source

    # -- site --------------------------------------------------------------
    astro = root.find("p:AstrometrySettings", NS)
    if astro is not None:
        lat, lon = _number(astro, "p:Latitude"), _number(astro, "p:Longitude")
        if lat is not None and lon is not None and (lat or lon):
            # Six places is a tenth of a metre; N.I.N.A. stores what a double
            # happens to hold, which reads as noise in a dialog.
            put("site", "latitude", round(lat, 6), "Astrometry › Latitude")
            put("site", "longitude", round(lon, 6), "Astrometry › Longitude")
            put("site", "elevation", _number(astro, "p:Elevation") or 0.0,
                "Astrometry › Elevation")
            put("site", "useMount", False, "set here: the profile's site wins")

    # -- optics --------------------------------------------------------------
    scope = root.find("p:TelescopeSettings", NS)
    camera = root.find("p:CameraSettings", NS)
    framing = root.find("p:FramingAssistantSettings", NS)
    if scope is not None:
        focal = _number(scope, "p:FocalLength")
        if focal and focal > 0:
            put("optics", "focalLength", focal, "Telescope › Focal length")
    if camera is not None:
        pixel = _number(camera, "p:PixelSize")
        if pixel and pixel > 0:
            put("optics", "pixelSize", pixel, "Camera › Pixel size")
    if framing is not None:
        width = _number(framing, "p:CameraWidth")
        height = _number(framing, "p:CameraHeight")
        if width and height and width > 0 and height > 0:
            put("optics", "sensorWidth", int(width), "Framing assistant › Camera width")
            put("optics", "sensorHeight", int(height), "Framing assistant › Camera height")
    left_out.append("Framing assistant › Last rotation angle: that is the angle "
                    "of a framing, not of the camera; the camera's angle is "
                    "measured from the first plate solve here")

    # -- camera ------------------------------------------------------------
    if camera is not None:
        gain = _number(camera, "p:Gain")
        if gain is not None and gain >= 0:
            put("camera", "gain", int(gain), "Camera › Gain")
        offset = _number(camera, "p:Offset")
        if offset is not None and offset >= 0:
            put("camera", "offset", int(offset), "Camera › Offset")
        setpoint = _number(camera, "p:Temperature")
        if setpoint is not None:
            put("camera", "setpoint", setpoint, "Camera › Temperature")
        bayer = (_text(camera, "p:BayerPattern") or "").upper()
        if bayer and bayer not in ("AUTO", "NONE", ""):
            put("camera", "colour", True, f"Camera › Bayer pattern {bayer}")
        max_flat = _number(camera, "p:MaxFlatExposureTime")
        if max_flat and max_flat > 0:
            put("calibration", "flatMaxExposure", max_flat, "Camera › Max flat exposure")
        min_flat = _number(camera, "p:MinFlatExposureTime")
        if min_flat and min_flat > 0:
            put("calibration", "flatMinExposure", min_flat, "Camera › Min flat exposure")
        left_out.append("Camera › Binning: chosen per frame on the Plan tab here")

    sequence = root.find("p:SequenceSettings", NS)
    if sequence is not None:
        cool = _flag(sequence, "p:CoolCameraAtSequenceStart")
        if cool is not None:
            put("camera", "coolAtStart", cool, "Sequence › Cool camera at start")
        warm = _flag(sequence, "p:WarmCamAtSequenceEnd")
        if warm is not None:
            put("camera", "warmAtEnd", warm, "Sequence › Warm camera at end")
        park = _flag(sequence, "p:ParkMountAtSequenceEnd")
        if park is not None:
            put("sequencer", "parkAtEnd", park, "Sequence › Park mount at end")
        flip = _flag(sequence, "p:DoMeridianFlip")
        if flip is not None:
            put("sequencer", "meridianFlipEnabled", flip, "Sequence › Do meridian flip")

    # -- filters -------------------------------------------------------------
    wheel = root.find("p:FilterWheelSettings", NS)
    names: list[str] = []
    offsets: dict[str, int] = {}
    focus_filter = ""
    if wheel is not None:
        rows = []
        for info in wheel.findall("p:FilterWheelFilters/e:FilterInfo", NS):
            name = (_text(info, "e:_name") or "").strip()
            if not name:
                continue
            position = _number(info, "e:_position")
            rows.append((position if position is not None else len(rows), name, info))
        rows.sort(key=lambda row: row[0])
        from .filters import canonical
        for _, name, info in rows:
            # N.I.N.A.'s "Ha", "H", "H-alpha" are all "H" here: one spelling
            # per filter, everywhere in the program.
            name = canonical(name) or name
            names.append(name)
            step = _number(info, "e:_focusOffset")
            if step:
                offsets[name] = int(step)
            if _flag(info, "e:_autoFocusFilter"):
                focus_filter = name
        if names:
            put("camera", "filterNames", names, "Filter wheel › Filters, in slot order")
        if offsets:
            put("sequencer", "filterOffsets", offsets, "Filter wheel › Focus offsets")
        if focus_filter:
            put("sequencer", "autofocusFilter", focus_filter,
                "Filter wheel › Autofocus filter")

    # -- focus -------------------------------------------------------------
    focuser = root.find("p:FocuserSettings", NS)
    if focuser is not None:
        exposure = _number(focuser, "p:AutoFocusExposureTime")
        if exposure and exposure > 0:
            put("sequencer", "focusExposure", min(120.0, exposure), "Focuser › Autofocus exposure")
        step = _number(focuser, "p:AutoFocusStepSize")
        if step and step > 0:
            put("sequencer", "focusStepSize", int(step), "Focuser › Step size")
        steps = _number(focuser, "p:AutoFocusInitialOffsetSteps")
        if steps and steps > 0:
            put("sequencer", "focusPoints", max(5, min(31, int(2 * steps + 1))),
                "Focuser › Initial offset steps (each side of focus)")
        frames = _number(focuser, "p:AutoFocusNumberOfFramesPerPoint")
        if frames and frames >= 1:
            put("sequencer", "focusFramesPerPoint", max(1, min(10, int(frames))),
                "Focuser › Frames per point")
        attempts = _number(focuser, "p:AutoFocusTotalNumberOfAttempts")
        if attempts and attempts >= 1:
            put("sequencer", "focusAttempts", max(1, min(5, int(attempts))),
                "Focuser › Total attempts")
        curve = CURVES.get((_text(focuser, "p:AutoFocusCurveFitting") or "").upper())
        if curve:
            put("sequencer", "focusMethod", curve, "Focuser › Curve fitting")
        backlash_in = _number(focuser, "p:BacklashIn") or 0.0
        backlash_out = _number(focuser, "p:BacklashOut") or 0.0
        backlash = max(backlash_in, backlash_out)
        if backlash > 0:
            put("sequencer", "focusBacklash", int(backlash),
                "Focuser › Backlash (the larger of in and out)")
        use_offsets = _flag(focuser, "p:UseFilterWheelOffsets")
        if use_offsets is not None:
            put("sequencer", "useFilterOffsets", use_offsets, "Focuser › Use filter offsets")
        method = (_text(focuser, "p:AutoFocusMethod") or "").upper()
        if method and method != "STARHFR":
            left_out.append(f"Focuser › Autofocus method {method}: this program "
                            "measures star size (HFR) only")

    # -- meridian flip -----------------------------------------------------
    flip = root.find("p:MeridianFlipSettings", NS)
    if flip is not None:
        after = _number(flip, "p:MinutesAfterMeridian")
        if after is not None and after >= 0:
            put("sequencer", "flipAfterMinutes", min(120.0, after), "Meridian flip › Minutes after meridian")
        pause = _number(flip, "p:PauseTimeBeforeMeridian")
        if pause is not None and pause >= 0:
            put("sequencer", "flipPauseMinutes", min(120.0, pause), "Meridian flip › Pause before meridian")
        recentre = _flag(flip, "p:Recenter")
        if recentre is not None:
            put("sequencer", "flipSolve", recentre, "Meridian flip › Recenter after flip")
    if scope is not None:
        settle = _number(scope, "p:SettleTime")
        if settle is not None and settle >= 0:
            put("sequencer", "settleSeconds", min(600.0, settle), "Telescope › Settle time")

    # -- guiding -----------------------------------------------------------
    guider = root.find("p:GuiderSettings", NS)
    phd2_address = ""
    if guider is not None:
        phd2 = _text(guider, "p:PHD2Path")
        if phd2:
            put("guiding", "phd2Path", phd2, "Guider › PHD2 path")
        dither = _number(guider, "p:DitherPixels")
        if dither and dither > 0:
            put("guiding", "ditherPixels", min(100.0, dither), "Guider › Dither pixels")
        ra_only = _flag(guider, "p:DitherRAOnly")
        if ra_only is not None:
            put("guiding", "ditherRaOnly", ra_only, "Guider › Dither RA only")
        settle_px = _number(guider, "p:SettlePixels")
        if settle_px and settle_px > 0:
            put("guiding", "settlePixels", min(50.0, settle_px), "Guider › Settle pixels")
        settle_time = _number(guider, "p:SettleTime")
        if settle_time is not None and settle_time >= 0:
            put("guiding", "settleTime", min(600.0, settle_time), "Guider › Settle time")
        settle_timeout = _number(guider, "p:SettleTimeout")
        if settle_timeout and settle_timeout >= 5:
            put("guiding", "settleTimeout", min(600.0, settle_timeout), "Guider › Settle timeout")
        host = (_text(guider, "p:PHD2ServerUrl") or "localhost").strip()
        port = int(_number(guider, "p:PHD2ServerPort") or 4400)
        if host.lower() in ("localhost", ""):
            host = "127.0.0.1"
        phd2_address = f"{host}:{port}"
        which = (_text(guider, "p:GuiderName") or "").upper()
        if which and "PHD2" not in which:
            left_out.append(f"Guider › {which}: only PHD2 is driven here")

    # -- plate solving -----------------------------------------------------
    solving = root.find("p:PlateSolveSettings", NS)
    if solving is not None:
        astap = _text(solving, "p:ASTAPLocation")
        if astap:
            put("solver", "astapPath", astap, "Plate solving › ASTAP location")
        radius = _number(solving, "p:SearchRadius")
        if radius and radius > 0:
            put("solver", "searchRadius", min(180.0, radius), "Plate solving › Search radius")
        down = _number(solving, "p:DownSampleFactor")
        if down is not None and 0 <= down <= 4:
            put("solver", "downsample", int(down), "Plate solving › Downsample")
        stars = _number(solving, "p:MaxObjects")
        if stars and stars >= 10:
            put("solver", "maxStars", min(10000, int(stars)), "Plate solving › Max objects")
        exposure = _number(solving, "p:ExposureTime")
        if exposure and exposure > 0:
            put("solver", "exposure", min(600.0, exposure), "Plate solving › Exposure time")
        attempts = _number(solving, "p:NumberOfAttempts")
        if attempts and attempts >= 1:
            put("solver", "attempts", max(1, min(10, int(attempts))), "Plate solving › Attempts")
        threshold = _number(solving, "p:Threshold")
        if threshold and threshold > 0:
            put("solver", "tolerance", min(120.0, threshold), "Plate solving › Threshold (arcmin)")
        key = _text(solving, "p:AstrometryAPIKey")
        if key:
            put("solver", "astrometryKey", key, "Plate solving › Astrometry.net API key")
        url = (_text(solving, "p:AstrometryURL") or "").strip()
        if url and "nova.astrometry.net" not in url:
            put("solver", "astrometryUrl", url.rstrip("/") + "/api/",
                "Plate solving › Astrometry URL")
        kind = (_text(solving, "p:PlateSolverType") or "").upper()
        if kind and kind != "ASTAP":
            left_out.append(f"Plate solving › {kind}: this program solves with "
                            "ASTAP, with astrometry.net as the fallback")

    # -- files -------------------------------------------------------------
    files = root.find("p:ImageFileSettings", NS)
    if files is not None:
        folder = (_text(files, "p:FilePath") or "").strip()
        if folder:
            put("capture", "rootDirectory", folder, "Image file › File path")
        left_out.append("Image file › File pattern: frames are filed as "
                        "<root>/<target>/<night>/ here, which is what the "
                        "collaboration and the night log rely on")

    # -- flats -------------------------------------------------------------
    wizard = root.find("p:FlatWizardSettings", NS)
    if wizard is not None:
        mean = _number(wizard, "p:HistogramMeanTarget")
        depth = _number(camera, "p:BitDepth") if camera is not None else None
        full = (2 ** int(depth) - 1) if depth and depth in (8, 10, 12, 14, 16) else 65535
        if mean and 0 < mean < 1:
            put("calibration", "flatTargetAdu", round(mean * full),
                f"Flat wizard › Histogram mean target ({mean:g} of {full})")
        tolerance = _number(wizard, "p:HistogramTolerance")
        if tolerance and 0 < tolerance < 1:
            put("calibration", "flatTolerancePercent", round(tolerance * 100, 1),
                "Flat wizard › Histogram tolerance")

    # -- devices -----------------------------------------------------------
    devices: dict[str, dict[str, str]] = {}

    def slot(kind: str, section: str) -> None:
        node = root.find(f"p:{section}", NS)
        if node is None:
            return
        driver = (_text(node, "p:Id") or "").strip()
        if not driver or driver == NO_DEVICE:
            return
        name = (_text(node, "p:LastDeviceName") or driver).strip()
        if driver.upper().startswith("ASCOM."):
            devices[kind] = {"backend": "ascom", "driverId": driver, "name": name}
        else:
            left_out.append(f"{section.replace('Settings', '')} › {driver}: not an "
                            "ASCOM driver, so it has to be chosen by hand here")

    slot("camera", "CameraSettings")
    slot("filterwheel", "FilterWheelSettings")
    slot("focuser", "FocuserSettings")
    slot("rotator", "RotatorSettings")
    slot("flatpanel", "FlatDeviceSettings")
    slot("mount", "TelescopeSettings")
    slot("safetymonitor", "SafetyMonitorSettings")
    slot("dome", "DomeSettings")
    slot("switch", "SwitchSettings")
    if phd2_address and guider is not None:
        which = (_text(guider, "p:GuiderName") or "").upper()
        if "PHD2" in which or not which:
            devices["guider"] = {"backend": "phd2", "driverId": phd2_address,
                                 "name": f"PHD2 ({phd2_address})"}

    return {
        "profile": {"id": _text(root, "p:Id") or path.stem,
                    "name": _text(root, "p:Name") or path.stem,
                    "lastUsed": _text(root, "p:LastUsed") or "",
                    "path": str(path)},
        "settings": settings,
        "devices": devices,
        "from": origin,
        "leftOut": left_out,
    }


# ---------------------------------------------------------------------------
# Carrying it out
# ---------------------------------------------------------------------------

def apply(plan: dict[str, Any], config: Any, rig: Any, equipment: Any,
          sections: list[str] | None = None,
          include_devices: bool = True) -> dict[str, Any]:
    """Write a plan's settings and device slots.

    Per-telescope sections (optics, camera, sequencer) go to the rig's own
    settings; the rest are the observatory's. Devices are *remembered*, not
    connected: the slot is set the way choosing a driver in the dialog sets
    it, and Connect brings it up when the person is ready.
    """
    written: dict[str, list[str]] = {}
    wanted = set(sections) if sections else None
    for section, values in (plan.get("settings") or {}).items():
        if wanted is not None and section not in wanted:
            continue
        if not values:
            continue
        target = rig.config if section in ("optics", "camera", "sequencer") else config
        # Filter offsets merge onto what is there rather than replacing it;
        # everything else is a plain value.
        if section == "sequencer" and "filterOffsets" in values:
            have = dict(target.get("sequencer", "filterOffsets", {}) or {})
            have.update(values["filterOffsets"])
            values = {**values, "filterOffsets": have}
        target.update(section, values)
        written[section] = sorted(values)
    remembered: list[str] = []
    if include_devices:
        for kind, spec in (plan.get("devices") or {}).items():
            try:
                equipment.set_device(rig.id, kind, spec)
                remembered.append(kind)
            except Exception:                      # noqa: BLE001 - a slot this rig lacks
                continue
    return {"settings": written, "devices": remembered}


def when(stamp: str) -> str:
    """A profile's LastUsed as something a person reads."""
    try:
        return datetime.fromisoformat(stamp).strftime("%d %b %Y, %H:%M")
    except (TypeError, ValueError):
        return stamp or "never"
