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
# N.I.N.A.'s own ZWO driver
# ---------------------------------------------------------------------------

#: How N.I.N.A. identifies a camera opened through its built-in ZWO driver:
#: the maker, the model and the alias written into the body, joined by
#: underscores — "ZWOptical_ZWO ASI2600MM Pro_RASA-L".  The alias is the only
#: part that tells three identical bodies apart, and it is exactly what this
#: program's ZWO backend can address a camera by.
_ZWO_NATIVE_PREFIX = "ZWOptical_"


def zwo_native_device(kind: str, driver_id: str, name: str
                      ) -> dict[str, str] | None:
    """A slot for a camera N.I.N.A. drove through its own ZWO driver, or None.

    Returns a ZWO-direct slot addressed by alias.  The alias is resolved to a
    serial number when the body can next be opened — which may not be now, if
    N.I.N.A. is still holding it — so nothing here needs the hardware.
    """
    if kind not in ("camera", "piercam") or not driver_id.startswith(_ZWO_NATIVE_PREFIX):
        return None
    from .devices.zwo import alias_id
    rest = driver_id[len(_ZWO_NATIVE_PREFIX):]
    model, _, alias = rest.rpartition("_")
    if not model:
        # No alias in the id at all: nothing distinguishes this body from
        # another of the same model, so it has to be picked by serial here.
        return None
    alias = alias.strip()
    model = model.strip()
    if not alias:
        return None
    return {"backend": "zwo", "driverId": alias_id(alias),
            "name": f"{model} ({alias})", "alias": alias, "model": model}


def camera_alias(plan: dict[str, Any]) -> str:
    """The alias of the camera a plan names, or ""."""
    camera = (plan.get("devices") or {}).get("camera") or {}
    if camera.get("alias"):
        return str(camera["alias"])
    driver = str(camera.get("driverId") or "")
    from .devices.zwo import ALIAS_PREFIX
    if driver.startswith(ALIAS_PREFIX):
        return driver[len(ALIAS_PREFIX):]
    return ""


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
            return
        native = zwo_native_device(kind, driver, name)
        if native is not None:
            devices[kind] = native
            return
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


# ---------------------------------------------------------------------------
# Several profiles at once: one per telescope
# ---------------------------------------------------------------------------
#
# Somebody running three copies of N.I.N.A. on a triple rig has three
# profiles, and between them they already answer the question that is nearly
# impossible to answer by looking at the hardware: which camera is on which
# telescope.  Each profile names its camera by alias, says whether it carries
# the mount, and holds that telescope's own gain, offset and focus settings.
# This turns the lot into one plan — a telescope per profile, the master
# chosen, every slot resolved as far as it can be — that is shown, edited and
# then carried out in one go.

#: Sections that belong to one telescope.  Everything else in a profile
#: describes the observatory and is taken from the master's profile alone, so
#: three profiles that disagree about the site do not write it three times.
PER_TELESCOPE_SECTIONS = ("optics", "camera", "sequencer")


def _alias_key(text: str) -> str:
    """"RASA-L", "rasa l" and "RASA_L" are the same name."""
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def _match_focusers(cameras: list[str], focusers: list[dict[str, Any]]
                    ) -> dict[int, dict[str, Any]]:
    """Pair ZWO focusers with cameras by alias, as camera index -> focuser.

    Two ways, in order of trust.  A focuser whose alias is the camera's alias
    is a match a person made on purpose.  After that, if exactly one camera
    and one focuser are left over, they are offered to each other with the
    confidence that deserves: it is the only possibility, not a known fact.
    """
    chosen: dict[int, dict[str, Any]] = {}
    taken: set[str] = set()
    for index, alias in enumerate(cameras):
        key = _alias_key(alias)
        if not key:
            continue
        for focuser in focusers:
            if focuser["id"] in taken:
                continue
            if _alias_key(focuser.get("alias", "")) == key:
                chosen[index] = {**focuser, "how": "alias"}
                taken.add(focuser["id"])
                break
    left_cameras = [i for i in range(len(cameras)) if i not in chosen]
    left_focusers = [f for f in focusers if f["id"] not in taken]
    if len(left_cameras) == 1 and len(left_focusers) == 1:
        chosen[left_cameras[0]] = {**left_focusers[0], "how": "leftover"}
    return chosen


def setup_plan(plans: list[dict[str, Any]], rigs: list[dict[str, Any]],
               zwo_cameras: list[dict[str, Any]] | None = None,
               zwo_focusers: list[dict[str, Any]] | None = None,
               camera_serials: dict[str, str] | None = None) -> dict[str, Any]:
    """One telescope per profile, with every slot resolved as far as it can be.

    `plans` are `read()` results.  `rigs` are the telescopes the program has
    now (master first).  `zwo_cameras` and `zwo_focusers` are what the ZWO
    backend can see, each `{id, name, alias}`; `camera_serials` maps a camera
    alias to a serial already learned, for a body that is not visible right
    now because another program holds it.  Nothing here touches hardware.
    """
    zwo_cameras = zwo_cameras or []
    zwo_focusers = zwo_focusers or []
    camera_serials = {_alias_key(k): v for k, v in (camera_serials or {}).items()}
    notes: list[str] = []

    # -- who is the master -------------------------------------------------
    def rank(plan: dict[str, Any]) -> tuple[int, int, str]:
        devices = plan.get("devices") or {}
        return (0 if "mount" in devices else 1,
                0 if "guider" in devices else 1,
                plan["profile"]["name"].lower())

    ordered = sorted(plans, key=rank)
    if ordered and "mount" not in (ordered[0].get("devices") or {}):
        notes.append("None of these profiles names a mount, so the first one "
                     "is made the master; choose another if that is wrong, and "
                     "connect the mount by hand.")
    with_mount = [p for p in plans if "mount" in (p.get("devices") or {})]
    if len(with_mount) > 1:
        notes.append(f"{len(with_mount)} profiles name a mount; only the master "
                     "carries it here, and the others ride along with it.")

    # -- cameras -------------------------------------------------------------
    aliases = [camera_alias(plan) for plan in ordered]
    visible = {_alias_key(cam.get("alias", "")): cam for cam in zwo_cameras
               if cam.get("alias")}
    focusers = _match_focusers(aliases, zwo_focusers)

    # -- the ASCOM focuser shared by every profile ---------------------------
    ascom_focusers = [((p.get("devices") or {}).get("focuser") or {}).get("driverId")
                      for p in ordered]
    shared_progid = {d for d in ascom_focusers if d and ascom_focusers.count(d) > 1}

    telescopes: list[dict[str, Any]] = []
    for index, plan in enumerate(ordered):
        devices = dict(plan.get("devices") or {})
        entry: dict[str, Any] = {
            "profile": plan["profile"],
            "name": plan["profile"]["name"],
            "master": index == 0,
            "rig": rigs[index]["id"] if index < len(rigs) else None,
            "rigName": rigs[index]["name"] if index < len(rigs) else None,
            "devices": {},
            "settings": {section: sorted(values)
                         for section, values in (plan.get("settings") or {}).items()},
            "summary": _summary(plan),
            "notes": [],
        }

        # Camera: by alias, resolved to a serial when one is known.
        camera = devices.get("camera")
        alias = aliases[index]
        if camera and camera.get("backend") == "zwo" and alias:
            key = _alias_key(alias)
            seen = visible.get(key)
            serial = (seen or {}).get("id") or camera_serials.get(key, "")
            if serial and not str(serial).startswith("index:"):
                entry["devices"]["camera"] = {
                    "backend": "zwo", "driverId": serial,
                    "name": camera["name"], "alias": alias, "status": "found",
                    "note": f"{alias} is plugged in — serial {serial}"}
            else:
                entry["devices"]["camera"] = {
                    **{k: camera[k] for k in ("backend", "driverId", "name")},
                    "alias": alias, "status": "byAlias",
                    "note": f"remembered by its name, {alias}; the body is "
                            "found by that name the first time it can be "
                            "opened (close N.I.N.A. first)"}
        elif camera:
            entry["devices"]["camera"] = {**camera, "status": "driver",
                                          "note": "the driver N.I.N.A. used"}
        else:
            entry["notes"].append("no camera in this profile")

        # Focuser: a ZWO body matched by alias beats a shared ASCOM ProgID.
        focuser = focusers.get(index)
        nina_focuser = devices.get("focuser")
        if focuser is not None:
            how = focuser.get("how")
            entry["devices"]["focuser"] = {
                "backend": "zwo", "driverId": focuser["id"],
                "name": focuser.get("name") or f"ZWO EAF ({focuser.get('alias', '')})",
                "alias": focuser.get("alias", ""),
                "status": "matched" if how == "alias" else "leftover",
                "note": (f"the EAF named {focuser.get('alias')} matches the camera"
                         if how == "alias" else
                         f"the only EAF left unassigned ({focuser.get('alias') or focuser['id']}) "
                         "— confirm it with Which focuser is which")}
        elif nina_focuser:
            shared = nina_focuser.get("driverId") in shared_progid
            entry["devices"]["focuser"] = {
                **nina_focuser,
                "status": "shared" if shared else "driver",
                "note": ("every profile names this same ASCOM driver, which can "
                         "only open one focuser at a time here — pick this "
                         "telescope's EAF by name instead, or run Which focuser "
                         "is which" if shared else "the driver N.I.N.A. used")}

        # Everything else comes across as it is.
        for kind, spec in devices.items():
            if kind in ("camera", "focuser"):
                continue
            if kind in ("mount", "guider", "safetymonitor", "dome", "switch",
                        "piercam") and index != 0:
                entry["notes"].append(f"{kind} left to the master")
                continue
            entry["devices"][kind] = {**spec, "status": "driver",
                                      "note": "the driver N.I.N.A. used"}
        telescopes.append(entry)

    if len(rigs) > len(ordered):
        extra = [r["name"] for r in rigs[len(ordered):]]
        notes.append(f"{', '.join(extra)} " + ("has" if len(extra) == 1 else "have")
                     + " no profile here and will be left as "
                     + ("it is" if len(extra) == 1 else "they are") + ".")
    if zwo_focusers and len(zwo_focusers) < len(ordered):
        notes.append(f"Only {len(zwo_focusers)} ZWO focuser(s) can be seen right "
                     "now; the rest are read when they are free.")
    return {"telescopes": telescopes, "notes": notes,
            "perTelescope": list(PER_TELESCOPE_SECTIONS)}


def _summary(plan: dict[str, Any]) -> str:
    """The handful of numbers a person checks a profile by."""
    settings = plan.get("settings") or {}
    optics = settings.get("optics") or {}
    camera = settings.get("camera") or {}
    bits = []
    if optics.get("focalLength"):
        bits.append(f"{optics['focalLength']:g} mm")
    if optics.get("pixelSize"):
        bits.append(f"{optics['pixelSize']:g} µm")
    if camera.get("gain") is not None:
        bits.append(f"gain {camera['gain']}")
    if camera.get("offset") is not None:
        bits.append(f"offset {camera['offset']}")
    if camera.get("filterNames"):
        bits.append("filters " + " ".join(camera["filterNames"]))
    folder = (settings.get("capture") or {}).get("rootDirectory")
    if folder:
        bits.append(str(folder))
    return " · ".join(bits)


def apply_setup(telescopes: list[dict[str, Any]], read_plan, config: Any,
                equipment: Any, rigs: Any, sections: list[str] | None = None,
                include_devices: bool = True) -> dict[str, Any]:
    """Carry a setup plan out: telescopes made and named, master set, each
    profile written onto its telescope, devices remembered.

    `telescopes` is the plan's list as edited by the person: each carries
    `profile.path`, `name`, `master`, an optional `rig` to reuse and the
    `devices` to remember.  `read_plan(path)` reads a profile.  Shared
    sections are written from the master's profile only.
    """
    chosen = [t for t in telescopes if t.get("include", True)]
    if not chosen:
        raise NinaError("no telescope was chosen")
    chosen.sort(key=lambda t: 0 if t.get("master") else 1)
    if not chosen[0].get("master"):
        chosen[0]["master"] = True

    existing = {rig["id"]: rig for rig in equipment.rigs()}
    outcome: list[dict[str, Any]] = []
    assigned: list[tuple[dict[str, Any], str]] = []
    for entry in chosen:
        rig_id = entry.get("rig")
        name = str(entry.get("name") or entry["profile"]["name"]).strip()
        if rig_id and rig_id in existing:
            if existing[rig_id]["name"] != name:
                equipment.rename_rig(rig_id, name)
        else:
            rig_id = equipment.add_rig(name)["id"]
        assigned.append((entry, rig_id))

    master_id = assigned[0][1]
    if equipment.master_id() != master_id:
        equipment.set_master(master_id)
    rigs.sync()

    from .equipment import MASTER_DEVICE_KINDS

    plans = {rig_id: read_plan(entry["profile"]["path"]) for entry, rig_id in assigned}
    # The mount, the guider and the rest of the observatory's slots go to
    # whichever telescope is master *now* — the person may have moved it since
    # the plan was drawn.  Its own profile's choice first, then anybody's.
    observatory: dict[str, dict[str, Any]] = {}
    for rig_id in [master_id] + [r for _, r in assigned if r != master_id]:
        for kind, spec in (plans[rig_id].get("devices") or {}).items():
            if kind in MASTER_DEVICE_KINDS and kind not in observatory:
                observatory[kind] = spec
    for entry, rig_id in assigned:
        if rig_id == master_id:
            continue
        for kind, spec in (entry.get("devices") or {}).items():
            if kind in MASTER_DEVICE_KINDS and kind not in observatory:
                observatory[kind] = spec

    for entry, rig_id in assigned:
        rig = rigs.get(rig_id)
        plan = plans[rig_id]
        wanted = list(sections) if sections else list(plan.get("settings") or {})
        if rig_id != master_id:
            wanted = [s for s in wanted if s in PER_TELESCOPE_SECTIONS]
        done = apply(plan, config, rig, equipment, wanted or ["optics"],
                     include_devices=False)
        remembered: list[str] = []
        if include_devices:
            chosen_devices = {kind: spec for kind, spec in (entry.get("devices") or {}).items()
                              if kind not in MASTER_DEVICE_KINDS}
            if rig_id == master_id:
                chosen_devices.update(observatory)
            for kind, spec in chosen_devices.items():
                clean = {k: spec.get(k) for k in ("backend", "driverId", "name")}
                if not clean["backend"] or not clean["driverId"]:
                    continue
                try:
                    equipment.set_device(rig_id, kind, clean)
                    remembered.append(kind)
                except Exception:              # noqa: BLE001 - a slot this rig lacks
                    continue
        outcome.append({"rig": rig_id, "name": rig.name, "master": rig_id == master_id,
                        "profile": plan["profile"]["name"],
                        "settings": done["settings"], "devices": remembered})
    return {"telescopes": outcome, "master": master_id}
