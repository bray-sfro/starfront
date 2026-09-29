"""ZWO native backend: the ASI camera and EAF focuser SDKs, over ctypes.

Why this exists alongside the ASCOM backend.

ZWO's ASCOM drivers are registered a fixed number of times — two instances of
each device type — so on a three-telescope rig the third camera and the third
focuser have no ProgID to connect to at all.  They are plugged in, the SDK can
see them, and ASCOM cannot reach them.  The SDK underneath has no such limit:
it enumerates every connected device and hands back each one's own serial
number.  So this backend exists to make a third (and fourth) telescope
possible, and as a side effect it names devices by what they actually are
rather than by "(1)" and "(2)".

Devices are addressed **by serial number**, not by the index the SDK happens to
hand them out in.  That index changes when a USB hub enumerates in a different
order, which on a multi-telescope rig means the master silently connecting to a
slave's camera.  A serial number does not move.

The libraries ship with ZWO's ASCOM drivers, so anyone who has used the
hardware on this machine already has them; `ASTRO_ZWO_SDK` overrides the
search if they live somewhere unusual.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import threading
import time
from typing import Any

import numpy as np

from .base import Camera, DeviceError, Focuser

# ---------------------------------------------------------------------------
# Where the libraries live
# ---------------------------------------------------------------------------

#: Searched in order.  The ASCOM folder first: if ZWO's drivers were ever
#: installed the SDK is certainly there, and it is the copy the rest of the
#: machine is already using.
_SEARCH_DIRS = (
    os.environ.get("ASTRO_ZWO_SDK", ""),
    r"C:\Program Files (x86)\Common Files\ASCOM\ZWO",
    r"C:\Program Files\Common Files\ASCOM\ZWO",
    r"C:\Program Files\ZWO\ASI SDK\lib\x64",
    r"C:\Program Files (x86)\ZWO\ASI SDK\lib\x86",
)

_SIXTY_FOUR = ctypes.sizeof(ctypes.c_void_p) == 8

#: Both the name ZWO's ASCOM installer uses and the plain SDK name, because
#: the same library ships under both.
_CAMERA_DLLS = (("ASICamera2_ASCOM_x64.dll", "ASICamera2.dll") if _SIXTY_FOUR
                else ("ASICamera2_ASCOM.dll", "ASICamera2.dll"))
_FOCUSER_DLLS = (("EAF_focuser_ASCOM_x64.dll", "EAF_focuser.dll") if _SIXTY_FOUR
                 else ("EAF_focuser_ASCOM.dll", "EAF_focuser.dll"))

_load_lock = threading.Lock()
_libraries: dict[str, Any] = {}


def _find(names: tuple[str, ...]) -> str | None:
    for folder in _SEARCH_DIRS:
        if not folder:
            continue
        for name in names:
            candidate = os.path.join(folder, name)
            if os.path.exists(candidate):
                return candidate
    return None


def _library(kind: str):
    """The loaded SDK for a device kind, or None when it is not installed."""
    with _load_lock:
        if kind in _libraries:
            return _libraries[kind]
        names = _CAMERA_DLLS if kind == "camera" else _FOCUSER_DLLS
        path = _find(names)
        library = None
        if path:
            with contextlib.suppress(OSError):
                library = ctypes.CDLL(path)
        _libraries[kind] = library
        return library


def available() -> bool:
    """True when either SDK can be loaded on this machine."""
    return _library("camera") is not None or _library("focuser") is not None


def sdk_paths() -> dict[str, str]:
    """Which library file each kind resolved to, for the diagnostics screen."""
    return {"camera": _find(_CAMERA_DLLS) or "",
            "focuser": _find(_FOCUSER_DLLS) or ""}


# ---------------------------------------------------------------------------
# The SDK's own types
# ---------------------------------------------------------------------------
#
# Field order here is load-bearing and silent when wrong: a mismatched layout
# does not fail, it returns plausible nonsense.  Checked against the real
# hardware — the names, sensor sizes and pixel sizes that come back are the
# ones printed on the cameras.

class _CameraInfo(ctypes.Structure):
    _fields_ = [
        ("Name", ctypes.c_char * 64),
        ("CameraID", ctypes.c_int),
        ("MaxHeight", ctypes.c_long),
        ("MaxWidth", ctypes.c_long),
        ("IsColorCam", ctypes.c_int),
        ("BayerPattern", ctypes.c_int),
        ("SupportedBins", ctypes.c_int * 16),
        ("SupportedVideoFormat", ctypes.c_int * 8),
        ("PixelSize", ctypes.c_double),
        ("MechanicalShutter", ctypes.c_int),
        ("ST4Port", ctypes.c_int),
        ("IsCoolerCam", ctypes.c_int),
        ("IsUSB3Host", ctypes.c_int),
        ("IsUSB3Camera", ctypes.c_int),
        ("ElecPerADU", ctypes.c_float),
        ("BitDepth", ctypes.c_int),
        ("IsTriggerCam", ctypes.c_int),
        ("Unused", ctypes.c_char * 16),
    ]


class _ControlCaps(ctypes.Structure):
    _fields_ = [
        ("Name", ctypes.c_char * 64),
        ("Description", ctypes.c_char * 128),
        ("MaxValue", ctypes.c_long),
        ("MinValue", ctypes.c_long),
        ("DefaultValue", ctypes.c_long),
        ("IsAutoSupported", ctypes.c_int),
        ("IsWritable", ctypes.c_int),
        ("ControlType", ctypes.c_int),
        ("Unused", ctypes.c_char * 32),
    ]


class _FocuserInfo(ctypes.Structure):
    _fields_ = [
        ("ID", ctypes.c_int),
        ("Name", ctypes.c_char * 64),
        ("MaxStep", ctypes.c_int),
    ]


# ASI_CONTROL_TYPE, the handful we use.
_GAIN, _EXPOSURE, _OFFSET = 0, 1, 5
_TEMPERATURE = 8
_COOLER_POWER, _TARGET_TEMP, _COOLER_ON = 15, 16, 17

# ASI_IMG_TYPE
_RAW8, _RAW16 = 0, 2

# ASI_EXPOSURE_STATUS
_EXP_IDLE, _EXP_WORKING, _EXP_SUCCESS, _EXP_FAILED = 0, 1, 2, 3

_BAYER = {0: "RGGB", 1: "BGGR", 2: "GRBG", 3: "GBRG"}

#: The SDK reports and takes temperature in tenths of a degree.
_TENTHS = 10.0


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").strip("\x00").strip()


def _serial_text(buffer) -> str:
    """A serial number as the hex string ZWO's own software shows."""
    text = "".join(f"{byte:02x}" for byte in buffer)
    return "" if set(text) <= {"0"} else text


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------
#
# Reading a camera's serial number requires opening it, and opening one that
# another program is already using fails.  So serials are learned whenever they
# can be and remembered for the life of the process: they do not change, and a
# camera we already hold open has already told us.
#
# They are also written to disk, because they never change for a given piece of
# hardware and re-reading them is the one fragile step in here: a device that is
# in use, or has not finished being released by the last process, refuses to
# open and the identity would fall back to an index — exactly the thing serial
# numbers are here to avoid.  Learned once, known thereafter.
_serials: dict[str, dict[int, str]] = {"camera": {}, "focuser": {}}
_serial_lock = threading.Lock()
_serials_loaded = False


def _serial_file():
    from ..config import data_root         # local: config imports no devices
    return data_root() / "zwo-serials.json"


def _load_serials() -> None:
    global _serials_loaded
    if _serials_loaded:
        return
    _serials_loaded = True
    import json
    try:
        stored = json.loads(_serial_file().read_text("utf-8"))
    except Exception:                      # noqa: BLE001 - absent or unreadable
        return
    for kind in ("camera", "focuser"):
        for device_id, serial in (stored.get(kind) or {}).items():
            with contextlib.suppress(ValueError):
                _serials[kind][int(device_id)] = str(serial)


def _save_serials() -> None:
    import json
    with _serial_lock:
        snapshot = {kind: {str(k): v for k, v in found.items()}
                    for kind, found in _serials.items()}
    try:
        path = _serial_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, indent=1), "utf-8")
    except OSError:
        pass                               # a read-only home is not fatal


def _remember(kind: str, device_id: int, serial: str) -> None:
    if not serial:
        return
    with _serial_lock:
        if _serials[kind].get(device_id) == serial:
            return
        _serials[kind][device_id] = serial
    _save_serials()


def _known_serial(kind: str, device_id: int) -> str:
    _load_serials()
    with _serial_lock:
        return _serials[kind].get(device_id, "")


def _camera_serial(lib, info: _CameraInfo) -> str:
    """This camera's serial, from the cache or by briefly opening it."""
    cached = _known_serial("camera", info.CameraID)
    if cached:
        return cached
    if not hasattr(lib, "ASIGetSerialNumber"):
        return ""
    buffer = (ctypes.c_ubyte * 8)()
    if lib.ASIOpenCamera(info.CameraID) != 0:
        return ""                      # in use by something else; not an error
    try:
        if lib.ASIGetSerialNumber(info.CameraID, ctypes.byref(buffer)) != 0:
            return ""
        serial = _serial_text(buffer)
    finally:
        with contextlib.suppress(Exception):
            lib.ASICloseCamera(info.CameraID)
    _remember("camera", info.CameraID, serial)
    return serial


def _focuser_serial(lib, device_id: int) -> str:
    cached = _known_serial("focuser", device_id)
    if cached:
        return cached
    if not hasattr(lib, "EAFGetSerialNumber"):
        return ""
    buffer = (ctypes.c_ubyte * 8)()
    if lib.EAFOpen(device_id) != 0:
        return ""
    try:
        if lib.EAFGetSerialNumber(device_id, ctypes.byref(buffer)) != 0:
            return ""
        serial = _serial_text(buffer)
    finally:
        with contextlib.suppress(Exception):
            lib.EAFClose(device_id)
    _remember("focuser", device_id, serial)
    return serial


def list_devices(kind: str) -> list[dict[str, str]]:
    """Every connected ZWO device of this kind, as {id, name, device}.

    The id is the serial number where the device will give one — it survives a
    reboot and a USB hub that enumerates in a different order, which is the
    whole point on a rig where three identical cameras are in play.  Only when
    a device refuses to give a serial does it fall back to the SDK's index, and
    the name says so.
    """
    if kind in ("camera", "piercam"):
        return _list_cameras()
    if kind == "focuser":
        return _list_focusers()
    return []


def _list_cameras() -> list[dict[str, str]]:
    lib = _library("camera")
    if lib is None:
        return []
    lib.ASIGetNumOfConnectedCameras.restype = ctypes.c_int
    found: list[dict[str, str]] = []
    try:
        count = lib.ASIGetNumOfConnectedCameras()
    except Exception:                     # noqa: BLE001 - a missing SDK is not fatal
        return []
    for index in range(count):
        info = _CameraInfo()
        if lib.ASIGetCameraProperty(ctypes.byref(info), index) != 0:
            continue
        model = _text(info.Name) or f"ASI camera {info.CameraID}"
        serial = _camera_serial(lib, info)
        # The whole serial, not a prefix: this is the moment the right body is
        # picked out of three identical ones, and two units of the same model
        # can share the first several characters.
        identity = (f"sn {serial}" if serial
                    else f"index {info.CameraID} (no serial)")
        found.append({
            "id": serial or f"index:{info.CameraID}",
            "name": f"{model} — {identity}",
            "device": identity,
            "model": model,
        })
    return found


def _list_focusers() -> list[dict[str, str]]:
    lib = _library("focuser")
    if lib is None:
        return []
    lib.EAFGetNum.restype = ctypes.c_int
    found: list[dict[str, str]] = []
    try:
        count = lib.EAFGetNum()
    except Exception:                     # noqa: BLE001
        return []
    for index in range(count):
        ident = ctypes.c_int()
        if lib.EAFGetID(index, ctypes.byref(ident)) != 0:
            continue
        serial = _focuser_serial(lib, ident.value)
        # EAFGetProperty needs the device open; the name is "EAF" on every unit
        # anyway, so the serial is what actually tells them apart.
        identity = (f"sn {serial}" if serial
                    else f"index {ident.value} (no serial)")
        found.append({
            "id": serial or f"index:{ident.value}",
            "name": f"ZWO EAF — {identity}",
            "device": identity,
            "model": "ZWO EAF",
        })
    return found


def _resolve(kind: str, driver_id: str) -> int:
    """Turn a stored id back into the SDK's current device number.

    Called at connect rather than remembered, because the index a device is
    given depends on the order the USB tree came up in.
    """
    wanted = str(driver_id or "").strip()
    if not wanted:
        raise DeviceError("no ZWO device chosen")
    if wanted.startswith("index:"):
        return int(wanted.split(":", 1)[1])

    # The cache first.  Enumeration cannot read the serial of a device that
    # another program is holding open, so going straight to the list would turn
    # "your camera is in use by NINA" into "your camera is not plugged in".
    # From the cache we get the device number and let the SDK give the real
    # reason when it refuses to open.
    known = _cached_index(kind, wanted)
    if known is not None:
        return known
    list_devices(kind)                    # learn what is there, then ask again
    known = _cached_index(kind, wanted)
    if known is not None:
        return known
    raise DeviceError(
        f"no ZWO {kind} with serial {wanted} is connected — check it is "
        "plugged in and powered, and not held open by another program")


def _cached_index(kind: str, serial: str) -> int | None:
    _load_serials()
    with _serial_lock:
        for device_id, found in _serials[kind].items():
            if found == serial:
                return device_id
    return None


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

class ZwoCamera(Camera):
    """An ASI camera, driven through the SDK rather than through ASCOM."""

    def __init__(self, driver_id: str, name: str) -> None:
        Camera.__init__(self, driver_id, name)
        self._lib = _library("camera")
        self._id: int | None = None
        self._controls: dict[int, _ControlCaps] = {}
        self._light = True
        self._roi = (0, 0, 1)             # width, height, binning in force
        self._started = 0.0
        self._exposure = 0.0

    # -- plumbing ---------------------------------------------------------
    def _call(self, function: str, *args: Any) -> None:
        if self._lib is None:
            raise DeviceError("the ZWO camera SDK is not installed")
        code = getattr(self._lib, function)(*args)
        if code != 0:
            raise DeviceError(f"{function} failed ({_ASI_ERRORS.get(code, code)})")

    def _set_control(self, control: int, value: int, auto: bool = False) -> None:
        caps = self._controls.get(control)
        if caps is None:
            raise DeviceError(f"this camera has no control {control}")
        clamped = int(np.clip(value, caps.MinValue, caps.MaxValue))
        self._call("ASISetControlValue", self._id, control,
                   ctypes.c_long(clamped), 1 if auto else 0)

    def _get_control(self, control: int) -> int | None:
        if control not in self._controls or self._lib is None:
            return None
        value, auto = ctypes.c_long(), ctypes.c_int()
        code = self._lib.ASIGetControlValue(self._id, control,
                                            ctypes.byref(value),
                                            ctypes.byref(auto))
        return None if code != 0 else int(value.value)

    # -- lifecycle --------------------------------------------------------
    def connect(self) -> None:
        if self._lib is None:
            raise DeviceError("the ZWO camera SDK is not installed")
        with self._lock:
            device_id = _resolve("camera", self.driver_id)
            self._call("ASIOpenCamera", device_id)
            self._id = device_id
            try:
                self._call("ASIInitCamera", device_id)
                info = _CameraInfo()
                self._call("ASIGetCameraPropertyByID", device_id,
                           ctypes.byref(info))
                self._describe(info)
                self._read_controls()
                # RAW16 from the start: this is an imaging program, and 8-bit
                # data is not worth the disk it is written to.
                self._apply_roi(self.binning)
            except Exception:
                with contextlib.suppress(Exception):
                    self._lib.ASICloseCamera(device_id)
                self._id = None
                raise
            self._connected = True

    def _describe(self, info: _CameraInfo) -> None:
        self.name = _text(info.Name) or self.name
        self.sensor_width = int(info.MaxWidth)
        self.sensor_height = int(info.MaxHeight)
        self.pixel_size_um = float(info.PixelSize)
        bins = [int(b) for b in info.SupportedBins if b]
        self.max_bin = max(bins) if bins else 1
        self.can_cool = bool(info.IsCoolerCam)
        self.can_abort = True
        self.bayer_pattern = (_BAYER.get(int(info.BayerPattern))
                              if info.IsColorCam else None)
        self._bit_depth = int(info.BitDepth)
        # We are holding it open, which is the one moment the serial can always
        # be read. Learned here, it survives the device later being busy.
        if hasattr(self._lib, "ASIGetSerialNumber"):
            buffer = (ctypes.c_ubyte * 8)()
            if self._lib.ASIGetSerialNumber(int(info.CameraID),
                                            ctypes.byref(buffer)) == 0:
                _remember("camera", int(info.CameraID), _serial_text(buffer))

    def _read_controls(self) -> None:
        count = ctypes.c_int()
        self._call("ASIGetNumOfControls", self._id, ctypes.byref(count))
        self._controls = {}
        for index in range(count.value):
            caps = _ControlCaps()
            if self._lib.ASIGetControlCaps(self._id, index,
                                           ctypes.byref(caps)) == 0:
                self._controls[int(caps.ControlType)] = caps
        gain = self._controls.get(_GAIN)
        if gain is not None:
            self.gain_min, self.gain_max = int(gain.MinValue), int(gain.MaxValue)
            self.gain = self._get_control(_GAIN) or self.gain
        offset = self._controls.get(_OFFSET)
        if offset is not None:
            self.offset_min = int(offset.MinValue)
            self.offset_max = int(offset.MaxValue)
            self.offset = self._get_control(_OFFSET) or self.offset

    def _apply_roi(self, binning: int) -> None:
        """Full frame at this binning, in 16-bit."""
        binning = max(1, int(binning))
        width = (self.sensor_width // binning) & ~0x03      # multiple of 4
        height = (self.sensor_height // binning) & ~0x01     # multiple of 2
        self._call("ASISetROIFormat", self._id, width, height, binning, _RAW16)
        self._call("ASISetStartPos", self._id, 0, 0)
        self._roi = (width, height, binning)

    def disconnect(self) -> None:
        with self._lock:
            if self._id is not None and self._lib is not None:
                with contextlib.suppress(Exception):
                    self._lib.ASIStopExposure(self._id)
                with contextlib.suppress(Exception):
                    self._lib.ASICloseCamera(self._id)
            self._id = None
            self._connected = False

    # -- settings ---------------------------------------------------------
    def set_settings(self, binning: int | None = None, gain: int | None = None,
                     offset: int | None = None) -> None:
        self._require()
        with self._lock:
            if binning is not None:
                if not 1 <= int(binning) <= self.max_bin:
                    raise DeviceError(f"binning must be 1..{self.max_bin}")
                if int(binning) != self._roi[2]:
                    self._apply_roi(int(binning))
                self.binning = int(binning)
            if gain is not None:
                self._set_control(_GAIN, int(gain))
                self.gain = self._get_control(_GAIN) or int(gain)
            if offset is not None:
                self._set_control(_OFFSET, int(offset))
                self.offset = self._get_control(_OFFSET) or int(offset)

    # -- exposing ---------------------------------------------------------
    def start_exposure(self, seconds: float, light: bool = True) -> None:
        self._require()
        with self._lock:
            caps = self._controls.get(_EXPOSURE)
            micros = int(round(float(seconds) * 1_000_000))
            if caps is not None and micros > caps.MaxValue:
                raise DeviceError(
                    f"this camera's longest exposure is "
                    f"{caps.MaxValue / 1_000_000:g}s")
            self._set_control(_EXPOSURE, max(micros, 1))
            self._light = bool(light)
            self._exposure = float(seconds)
            self._started = time.monotonic()
            # isDark=1 asks the camera to keep its shutter shut, which only
            # means anything on a body that has one.
            self._call("ASIStartExposure", self._id, 0 if light else 1)

    def abort_exposure(self) -> None:
        if not self._connected or self._id is None:
            return
        with self._lock, contextlib.suppress(Exception):
            self._lib.ASIStopExposure(self._id)

    @property
    def image_ready(self) -> bool:
        if not self._connected or self._id is None:
            return False
        status = ctypes.c_int()
        with self._lock:
            code = self._lib.ASIGetExpStatus(self._id, ctypes.byref(status))
        if code != 0:
            return False
        if status.value == _EXP_FAILED:
            raise DeviceError("the camera reported the exposure failed")
        return status.value == _EXP_SUCCESS

    def get_image(self) -> np.ndarray:
        self._require()
        width, height, _ = self._roi
        if not width or not height:
            raise DeviceError("the camera has no frame size set")
        with self._lock:
            frame = np.empty((height, width), dtype=np.uint16)
            self._call("ASIGetDataAfterExp", self._id,
                       frame.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte)),
                       ctypes.c_long(frame.nbytes))
        return frame

    # -- cooling ----------------------------------------------------------
    @property
    def temperature(self) -> float | None:
        raw = self._get_control(_TEMPERATURE)
        return None if raw is None else round(raw / _TENTHS, 2)

    @property
    def cooler_on(self) -> bool:
        return bool(self._get_control(_COOLER_ON))

    def set_cooler(self, on: bool) -> None:
        self._require()
        if not self.can_cool:
            raise DeviceError("this camera has no cooler")
        with self._lock:
            self._set_control(_COOLER_ON, 1 if on else 0)

    @property
    def setpoint(self) -> float | None:
        raw = self._get_control(_TARGET_TEMP)
        return None if raw is None else float(raw)

    def set_setpoint(self, celsius: float) -> None:
        self._require()
        if not self.can_cool:
            raise DeviceError("this camera has no cooler")
        with self._lock:
            # Unlike the reading, the target is in whole degrees.
            self._set_control(_TARGET_TEMP, int(round(celsius)))

    @property
    def cooler_power(self) -> float | None:
        raw = self._get_control(_COOLER_POWER)
        return None if raw is None else float(raw)

    # -- reporting --------------------------------------------------------
    def status(self) -> dict[str, Any]:
        state = {
            "connected": self._connected,
            "backend": "zwo",
            "temperature": self.temperature if self._connected else None,
            "coolerOn": self.cooler_on if self._connected else False,
            "coolerPower": self.cooler_power if self._connected else None,
            "setpoint": self.setpoint if self._connected else None,
            "binning": self.binning,
            "gain": self.gain,
            "offset": self.offset,
        }
        return state

    def describe(self) -> dict[str, Any]:
        return {**Camera.describe(self), "backend": "zwo",
                "serial": self.driver_id,
                "bitDepth": getattr(self, "_bit_depth", 16)}


#: The SDK's error codes, so a failure reads as something rather than a number.
_ASI_ERRORS = {
    1: "invalid index", 2: "invalid id", 3: "invalid control type",
    4: "camera closed", 5: "camera removed", 6: "invalid path",
    7: "invalid file format", 8: "invalid size", 9: "invalid image type",
    10: "outside the sensor", 11: "not a video mode", 12: "exposure in progress",
    13: "general error", 14: "invalid mode", 15: "got a short frame",
    16: "buffer too small", 17: "the camera is already open",
    18: "the camera is in use by another program",
}


# ---------------------------------------------------------------------------
# Focuser
# ---------------------------------------------------------------------------

class ZwoFocuser(Focuser):
    """An EAF, driven through the SDK.

    The EAF is a relative focuser that reports an absolute step count, which is
    what `Focuser` wants.  Every unit calls itself "EAF", so the serial number
    is the only thing that tells three of them apart.
    """

    def __init__(self, driver_id: str, name: str) -> None:
        Focuser.__init__(self, driver_id, name)
        self._lib = _library("focuser")
        self._id: int | None = None
        self.is_absolute = True

    def _call(self, function: str, *args: Any) -> None:
        if self._lib is None:
            raise DeviceError("the ZWO focuser SDK is not installed")
        code = getattr(self._lib, function)(*args)
        if code != 0:
            raise DeviceError(f"{function} failed ({_EAF_ERRORS.get(code, code)})")

    def connect(self) -> None:
        if self._lib is None:
            raise DeviceError("the ZWO focuser SDK is not installed")
        with self._lock:
            device_id = _resolve("focuser", self.driver_id)
            self._call("EAFOpen", device_id)
            self._id = device_id
            try:
                info = _FocuserInfo()
                self._call("EAFGetProperty", device_id, ctypes.byref(info))
                self.max_step = int(info.MaxStep)
                # As with the camera: read the serial while we hold it open.
                if hasattr(self._lib, "EAFGetSerialNumber"):
                    buffer = (ctypes.c_ubyte * 8)()
                    if self._lib.EAFGetSerialNumber(device_id,
                                                    ctypes.byref(buffer)) == 0:
                        _remember("focuser", device_id, _serial_text(buffer))
                model = _text(info.Name) or "EAF"
                serial = _known_serial("focuser", device_id)
                # Every EAF calls itself "EAF", so the serial is the name.
                self.name = (f"{model} {serial[:8]}" if serial else model)
            except Exception:
                with contextlib.suppress(Exception):
                    self._lib.EAFClose(device_id)
                self._id = None
                raise
            self._connected = True

    def disconnect(self) -> None:
        with self._lock:
            if self._id is not None and self._lib is not None:
                with contextlib.suppress(Exception):
                    self._lib.EAFStop(self._id)
                with contextlib.suppress(Exception):
                    self._lib.EAFClose(self._id)
            self._id = None
            self._connected = False

    @property
    def position(self) -> int:
        self._require()
        step = ctypes.c_int()
        with self._lock:
            self._call("EAFGetPosition", self._id, ctypes.byref(step))
        return int(step.value)

    @property
    def moving(self) -> bool:
        if not self._connected or self._id is None:
            return False
        busy, hand = ctypes.c_bool(), ctypes.c_bool()
        with self._lock:
            code = self._lib.EAFIsMoving(self._id, ctypes.byref(busy),
                                         ctypes.byref(hand))
        return bool(busy.value) if code == 0 else False

    @property
    def hand_controller(self) -> bool:
        """True while somebody is turning the knob, which beats us to it."""
        if not self._connected or self._id is None:
            return False
        busy, hand = ctypes.c_bool(), ctypes.c_bool()
        with self._lock:
            code = self._lib.EAFIsMoving(self._id, ctypes.byref(busy),
                                         ctypes.byref(hand))
        return bool(hand.value) if code == 0 else False

    @property
    def temperature(self) -> float | None:
        """The probe reading, or None when there is genuinely no probe.

        Asked twice before giving up: the EAF returns -273 both for "no probe
        fitted" and for the occasional reading that simply does not arrive, and
        blanking the display — or worse, disabling the temperature-driven
        refocus — because of one dropped sample would be wrong.
        """
        if not self._connected or self._id is None:
            return None
        for attempt in range(2):
            value = ctypes.c_float()
            with self._lock:
                code = self._lib.EAFGetTemp(self._id, ctypes.byref(value))
            reading = float(value.value)
            if code == 0 and reading > -270.0:
                return round(reading, 2)
            if attempt == 0:
                time.sleep(0.05)
        return None

    def move_to(self, position: int) -> None:
        self._require()
        target = int(position)
        if self.max_step and not 0 <= target <= self.max_step:
            raise DeviceError(f"position must be 0..{self.max_step}")
        with self._lock:
            self._call("EAFMove", self._id, target)

    def halt(self) -> None:
        if not self._connected or self._id is None:
            return
        with self._lock:
            self._call("EAFStop", self._id)

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False, "backend": "zwo"}
        return {
            "connected": True,
            "backend": "zwo",
            "position": self.position,
            "moving": self.moving,
            "temperature": self.temperature,
            "maxStep": self.max_step,
            "handController": self.hand_controller,
        }

    def describe(self) -> dict[str, Any]:
        return {**Focuser.describe(self), "backend": "zwo",
                "serial": self.driver_id, "maxStep": self.max_step}


_EAF_ERRORS = {
    1: "invalid index", 2: "invalid id", 3: "invalid value",
    4: "the focuser is closed", 5: "the focuser was removed",
    6: "it is already moving", 7: "general error",
    8: "it is under hand control", 9: "the focuser is in use",
}


FACTORIES = {"camera": ZwoCamera, "focuser": ZwoFocuser, "piercam": ZwoCamera}


def create(kind: str, driver_id: str, name: str):
    factory = FACTORIES.get(kind)
    if factory is None:
        raise DeviceError(f"the ZWO backend has no {kind}")
    return factory(driver_id, name or driver_id)
