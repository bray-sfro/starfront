"""ASCOM Platform (COM) backend for Windows.

All COM traffic is marshalled onto one dedicated apartment thread.  ASCOM drivers
are overwhelmingly single-threaded apartment objects, and calling them from the
web server's thread pool is the classic source of random RPC_E_* failures, so the
executor below is not optional politeness - it is what makes this reliable.

If the ASCOM Platform is not installed, importing this module still works;
`available()` simply returns False and the UI hides the backend.
"""

from __future__ import annotations

import contextlib
import threading
import time
from concurrent.futures import Future
from queue import Queue
from typing import Any, Callable

import numpy as np

from .base import (Camera, DeviceError, Dome, FilterWheel, FlatPanel, Focuser,
                   Mount, Rotator, SafetyMonitor, SwitchBank)

try:                                    # pragma: no cover - platform dependent
    import pythoncom
    import win32com.client
    _HAVE_PYWIN32 = True
except Exception:                       # pragma: no cover
    _HAVE_PYWIN32 = False


class _ComExecutor:
    """Runs every COM call on a single STA thread."""

    def __init__(self, name: str = "ascom-com") -> None:
        self._queue: Queue = Queue()
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)
        self._thread.start()

    def _run(self) -> None:
        pythoncom.CoInitialize()
        try:
            while True:
                job, future = self._queue.get()
                if job is None:
                    return
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(job())
                    except Exception as exc:       # noqa: BLE001 - relayed to caller
                        future.set_exception(exc)
        finally:
            pythoncom.CoUninitialize()

    def call(self, job: Callable[[], Any], timeout: float = 180.0) -> Any:
        future: Future = Future()
        self._queue.put((job, future))
        return future.result(timeout=timeout)


# One apartment thread per channel.  A channel is a telescope: its devices are
# created on, and only ever called from, that one thread.  Two telescopes each
# get their own, so a slow ImageArray download on one camera does not sit in
# front of the other camera's ImageReady poll — which is what has to keep
# working for two scopes to expose together.  Driver discovery uses "default".
_executors: dict[str, _ComExecutor] = {}
_executor_lock = threading.Lock()


def _com(channel: str = "default") -> _ComExecutor:
    with _executor_lock:
        existing = _executors.get(channel)
        if existing is None:
            if not _HAVE_PYWIN32:
                raise DeviceError("pywin32 is not installed; ASCOM is unavailable")
            existing = _ComExecutor(name=f"ascom-com-{channel}")
            _executors[channel] = existing
        return existing


def available() -> bool:
    """True when the ASCOM Platform can be reached on this machine."""
    if not _HAVE_PYWIN32:
        return False
    try:
        _com().call(lambda: win32com.client.Dispatch("ASCOM.Utilities.Profile"), timeout=10)
        return True
    except Exception:
        return False


_ASCOM_TYPES = {
    "camera": "Camera",
    "mount": "Telescope",
    "filterwheel": "FilterWheel",
    "focuser": "Focuser",
    "rotator": "Rotator",
    "flatpanel": "CoverCalibrator",
    # The camera watching the telescope is an ASCOM Camera like any other.
    "piercam": "Camera",
    "safetymonitor": "SafetyMonitor",
    "dome": "Dome",
    # Pegasus Powerbox, Lunatico, Digital Loggers: all of them present a Switch.
    "switch": "Switch",
}


# The registered driver list only changes when someone installs a driver, but
# enumerating it is a COM round trip of well over a hundred milliseconds - and
# the dialog asks for six kinds at once, every time it opens or the selected
# telescope changes. Cached, with the Refresh drivers button clearing it.
_DRIVER_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
_DRIVER_CACHE_TTL = 120.0
_DRIVER_CACHE_LOCK = threading.Lock()


def forget_drivers() -> None:
    """Drop the cached driver lists, so the next ask really enumerates."""
    with _DRIVER_CACHE_LOCK:
        _DRIVER_CACHE.clear()


def list_devices(kind: str, refresh: bool = False) -> list[dict[str, str]]:
    """Registered ASCOM drivers of the given kind, as {id, name}."""
    device_type = _ASCOM_TYPES.get(kind)
    if device_type is None or not _HAVE_PYWIN32:
        return []

    if not refresh:
        with _DRIVER_CACHE_LOCK:
            cached = _DRIVER_CACHE.get(kind)
        if cached is not None and time.monotonic() < cached[0]:
            return list(cached[1])

    def job() -> list[dict[str, str]]:
        profile = win32com.client.Dispatch("ASCOM.Utilities.Profile")
        profile.DeviceType = device_type
        found = []
        for entry in profile.RegisteredDevices(device_type):
            prog_id = str(entry.Key)
            # Simulators shipped with the Platform are useful but noisy; keep them.
            found.append({"id": prog_id, "name": str(entry.Value) or prog_id})
        return found

    def labelled(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        """Add each driver's own idea of which device it is pointed at."""
        for row in rows:
            identity = device_identity(kind, row["id"])
            row["device"] = identity
            if identity:
                row["name"] = f"{row['name']} — {identity}"
        return rows

    try:
        found = _com().call(job, timeout=20)
    except Exception:
        return []
    # One profile read per driver, behind the same cache as the list itself:
    # it is what makes three identically named cameras tellable apart, and it
    # would be far too slow to do on every poll.
    with contextlib.suppress(Exception):
        found = labelled(found)
    with _DRIVER_CACHE_LOCK:
        _DRIVER_CACHE[kind] = (time.monotonic() + _DRIVER_CACHE_TTL, list(found))
    return found


class _AscomDevice:
    """Property/method plumbing shared by every ASCOM device wrapper."""

    # Overwritten by `create` with the telescope's own channel.  A default keeps
    # a wrapper built directly (in a test, say) working.
    _channel: str = "default"

    def _open(self, prog_id: str) -> None:
        self._driver = _com(self._channel).call(
            lambda: win32com.client.Dispatch(prog_id), timeout=60)

    def _get(self, name: str, default: Any = None) -> Any:
        try:
            return _com(self._channel).call(lambda: getattr(self._driver, name))
        except Exception:
            return default

    def _need(self, name: str) -> Any:
        try:
            return _com(self._channel).call(lambda: getattr(self._driver, name))
        except Exception as exc:
            raise DeviceError(f"{name}: {exc}") from exc

    def _set(self, name: str, value: Any) -> None:
        try:
            _com(self._channel).call(lambda: setattr(self._driver, name, value))
        except Exception as exc:
            raise DeviceError(f"{name}: {exc}") from exc

    def _invoke(self, name: str, *args: Any) -> Any:
        try:
            return _com(self._channel).call(lambda: getattr(self._driver, name)(*args))
        except Exception as exc:
            raise DeviceError(f"{name}: {exc}") from exc


class AscomCamera(_AscomDevice, Camera):
    def __init__(self, prog_id: str, name: str) -> None:
        Camera.__init__(self, prog_id, name)
        self._prog_id = prog_id
        self._light = True

    def connect(self) -> None:
        self._open(self._prog_id)
        self._set("Connected", True)
        self._connected = True
        self.sensor_width = int(self._get("CameraXSize", 0) or 0)
        self.sensor_height = int(self._get("CameraYSize", 0) or 0)
        self.pixel_size_um = float(self._get("PixelSizeX", 0.0) or 0.0)
        self.max_bin = int(self._get("MaxBinX", 1) or 1)
        self.can_cool = bool(self._get("CanSetCCDTemperature", False))
        self.can_abort = bool(self._get("CanAbortExposure", True))
        self.gain_min = int(self._get("GainMin", 0) or 0)
        self.gain_max = int(self._get("GainMax", 0) or 0)
        self.offset_min = int(self._get("OffsetMin", 0) or 0)
        self.offset_max = int(self._get("OffsetMax", 0) or 0)
        self.binning = int(self._get("BinX", 1) or 1)
        # A driver without gain or offset (the Platform simulator, most
        # DSLRs, some CCDs) throws on the property; asking it to set one
        # would fail every capture. Remember which it has, and set only those.
        gain = self._get("Gain")
        offset = self._get("Offset")
        self.has_gain = gain is not None
        self.has_offset = offset is not None
        self.gain = int(gain or 0)
        self.offset = int(offset or 0)
        if int(self._get("SensorType", 0) or 0) > 1:
            self.bayer_pattern = "RGGB"

    def disconnect(self) -> None:
        try:
            self._set("Connected", False)
        finally:
            self._connected = False

    def start_exposure(self, seconds: float, light: bool = True) -> None:
        self._require()
        self._light = light
        self._invoke("StartExposure", float(seconds), bool(light))

    def abort_exposure(self) -> None:
        if self._connected and self.can_abort:
            self._invoke("AbortExposure")

    @property
    def image_ready(self) -> bool:
        return bool(self._get("ImageReady", False))

    def get_image(self) -> np.ndarray:
        raw = self._need("ImageArray")
        # ASCOM hands back [x][y]; transpose into the usual row-major layout.
        array = np.array(raw, dtype=np.int64).T
        return np.clip(array, 0, 65535).astype(np.uint16)

    @property
    def temperature(self) -> float | None:
        value = self._get("CCDTemperature")
        return None if value is None else round(float(value), 2)

    @property
    def cooler_on(self) -> bool:
        return bool(self._get("CoolerOn", False))

    def set_cooler(self, on: bool) -> None:
        self._require()
        self._set("CoolerOn", bool(on))

    @property
    def setpoint(self) -> float | None:
        value = self._get("SetCCDTemperature")
        return None if value is None else round(float(value), 2)

    def set_setpoint(self, celsius: float) -> None:
        self._require()
        if not self.can_cool:
            raise DeviceError("camera cannot set a temperature setpoint")
        self._set("SetCCDTemperature", float(celsius))

    @property
    def cooler_power(self) -> float | None:
        value = self._get("CoolerPower")
        return None if value is None else round(float(value), 1)

    def set_settings(self, binning: int | None = None, gain: int | None = None,
                     offset: int | None = None) -> None:
        self._require()
        if binning is not None:
            self._set("BinX", int(binning))
            self._set("BinY", int(binning))
            self.binning = int(binning)
        if gain is not None and getattr(self, "has_gain", True):
            self._set("Gain", int(gain))
            self.gain = int(gain)
        if offset is not None and getattr(self, "has_offset", True):
            self._set("Offset", int(offset))
            self.offset = int(offset)

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        state = int(self._get("CameraState", 0) or 0)   # 0 idle, 2 exposing, 3 reading
        percent = float(self._get("PercentCompleted", 0) or 0)
        duration = float(self._get("LastExposureDuration", 0) or 0)
        return {
            "connected": True,
            "exposing": state in (1, 2, 3),
            "imageReady": self.image_ready,
            "elapsed": round(duration * percent / 100.0, 2),
            "duration": round(duration, 2),
            "temperature": self.temperature,
            "setpoint": self.setpoint,
            "coolerOn": self.cooler_on,
            "coolerPower": self.cooler_power,
            "canCool": self.can_cool,
            "gain": self.gain,
            "offset": self.offset,
            "binning": self.binning,
            "gainRange": [self.gain_min, self.gain_max],
            "offsetRange": [self.offset_min, self.offset_max],
            "maxBin": self.max_bin,
            "sensor": [self.sensor_width, self.sensor_height],
            "pixelSizeUm": self.pixel_size_um,
        }


class AscomMount(_AscomDevice, Mount):
    def __init__(self, prog_id: str, name: str) -> None:
        Mount.__init__(self, prog_id, name)
        self._prog_id = prog_id
        self._jogging = False

    def connect(self) -> None:
        self._open(self._prog_id)
        self._set("Connected", True)
        self._connected = True
        self.can_park = bool(self._get("CanPark", False))
        self.can_slew = bool(self._get("CanSlewAsync", False) or self._get("CanSlew", False))
        self.can_sync = bool(self._get("CanSync", False))
        self.can_set_tracking = bool(self._get("CanSetTracking", False))
        self.can_find_home = bool(self._get("CanFindHome", False))

    def disconnect(self) -> None:
        try:
            self._set("Connected", False)
        finally:
            self._connected = False

    @property
    def ra(self) -> float:
        return float(self._get("RightAscension", 0.0) or 0.0)

    @property
    def dec(self) -> float:
        return float(self._get("Declination", 0.0) or 0.0)

    @property
    def altitude(self) -> float | None:
        value = self._get("Altitude")
        return None if value is None else round(float(value), 2)

    @property
    def azimuth(self) -> float | None:
        value = self._get("Azimuth")
        return None if value is None else round(float(value), 2)

    @property
    def slewing(self) -> bool:
        return bool(self._get("Slewing", False)) or self._jogging

    @property
    def tracking(self) -> bool:
        return bool(self._get("Tracking", False))

    @property
    def at_park(self) -> bool:
        return bool(self._get("AtPark", False))

    @property
    def at_home(self) -> bool:
        return bool(self._get("AtHome", False))

    @property
    def side_of_pier(self) -> str | None:
        value = self._get("SideOfPier")
        if value is None or int(value) < 0:
            return None
        return "east" if int(value) == 0 else "west"

    @property
    def site(self) -> dict[str, float] | None:
        latitude = self._get("SiteLatitude")
        longitude = self._get("SiteLongitude")
        if latitude is None or longitude is None:
            return None
        return {"latitude": float(latitude), "longitude": float(longitude),
                "elevation": float(self._get("SiteElevation", 0.0) or 0.0)}

    def slew_to(self, ra_hours: float, dec_deg: float) -> None:
        self._require()
        if self._get("CanSlewAsync", False):
            self._invoke("SlewToCoordinatesAsync", float(ra_hours), float(dec_deg))
        else:
            self._invoke("SlewToCoordinates", float(ra_hours), float(dec_deg))

    def sync_to(self, ra_hours: float, dec_deg: float) -> None:
        self._require()
        self._invoke("SyncToCoordinates", float(ra_hours), float(dec_deg))

    def abort_slew(self) -> None:
        self._require()
        self.stop_jog()
        self._invoke("AbortSlew")

    def set_tracking(self, on: bool) -> None:
        self._require()
        self._set("Tracking", bool(on))

    def park(self) -> None:
        self._require()
        self._invoke("Park")

    def unpark(self) -> None:
        self._require()
        self._invoke("Unpark")

    def find_home(self) -> None:
        self._require()
        self._invoke("FindHome")

    def jog(self, direction: str, rate_deg_s: float) -> None:
        self._require()
        axis = 0 if direction in ("east", "west") else 1
        # CanMoveAxis is a method, not a property: it is asked per axis.
        if not bool(self._invoke("CanMoveAxis", axis)):
            raise DeviceError("mount does not support manual slewing on this axis")
        sign = 1.0 if direction in ("north", "east") else -1.0
        self._invoke("MoveAxis", axis, sign * float(rate_deg_s))
        self._jogging = True

    def stop_jog(self) -> None:
        if not self._connected:
            return
        for axis in (0, 1):
            try:
                self._invoke("MoveAxis", axis, 0.0)
            except DeviceError:
                pass
        self._jogging = False

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        return {
            "connected": True,
            "ra": round(self.ra, 5),
            "dec": round(self.dec, 4),
            "altitude": self.altitude,
            "azimuth": self.azimuth,
            "lst": round(float(self._get("SiderealTime", 0.0) or 0.0), 4),
            "slewing": self.slewing,
            "tracking": self.tracking,
            "atPark": self.at_park,
            "atHome": self.at_home,
            "canFindHome": self.can_find_home,
            "sideOfPier": self.side_of_pier,
        }


class AscomFilterWheel(_AscomDevice, FilterWheel):
    def __init__(self, prog_id: str, name: str) -> None:
        FilterWheel.__init__(self, prog_id, name)
        self._prog_id = prog_id
        self._names: list[str] = []

    def connect(self) -> None:
        self._open(self._prog_id)
        self._set("Connected", True)
        self._connected = True
        self._names = [str(n) for n in (self._get("Names", []) or [])]

    def disconnect(self) -> None:
        try:
            self._set("Connected", False)
        finally:
            self._connected = False

    @property
    def names(self) -> list[str]:
        # Under the names from Equipment where there are any: ASCOM's `Names` is
        # read-only, so a driver that only counts its slots cannot be told any
        # better, and everything downstream matches on the name.
        return self._named(self._names)

    @property
    def position(self) -> int:
        # Drivers report -1 while the wheel is between slots.
        value = self._get("Position")
        return -1 if value is None else int(value)

    def set_position(self, index: int) -> None:
        self._require()
        if not 0 <= index < len(self._names):
            raise DeviceError(f"filter slot must be 0..{len(self._names) - 1}")
        self._set("Position", int(index))

    # `status` comes from FilterWheel: it is the same for every backend.


class AscomFocuser(_AscomDevice, Focuser):
    def __init__(self, prog_id: str, name: str) -> None:
        Focuser.__init__(self, prog_id, name)
        self._prog_id = prog_id

    def connect(self) -> None:
        self._open(self._prog_id)
        self._set("Connected", True)
        self._connected = True
        self.max_step = int(self._get("MaxStep", 0) or 0)
        self.is_absolute = bool(self._get("Absolute", True))
        try:
            self.step_size_um = float(self._get("StepSize") or 0) or None
        except Exception:
            self.step_size_um = None

    def disconnect(self) -> None:
        try:
            self._set("Connected", False)
        finally:
            self._connected = False

    @property
    def position(self) -> int:
        return int(self._get("Position", 0) or 0)

    @property
    def moving(self) -> bool:
        return bool(self._get("IsMoving", False))

    @property
    def temperature(self) -> float | None:
        value = self._get("Temperature")
        return None if value is None else round(float(value), 2)

    def move_to(self, position: int) -> None:
        self._require()
        target = int(position)
        if self.max_step and not 0 <= target <= self.max_step:
            raise DeviceError(f"position must be 0..{self.max_step}")
        self._invoke("Move", target if self.is_absolute else target - self.position)

    def move_relative(self, delta: int) -> None:
        self._require()
        if self.is_absolute:
            self.move_to(self.position + int(delta))
        else:
            self._invoke("Move", int(delta))

    def halt(self) -> None:
        self._require()
        self._invoke("Halt")

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        position = self.position
        return {
            "connected": True,
            "position": position,
            "target": position,
            "moving": self.moving,
            "maxStep": self.max_step,
            "stepSizeUm": self.step_size_um,
            "temperature": self.temperature,
        }


class AscomRotator(_AscomDevice, Rotator):
    def __init__(self, prog_id: str, name: str) -> None:
        Rotator.__init__(self, prog_id, name)
        self._prog_id = prog_id

    def connect(self) -> None:
        self._open(self._prog_id)
        self._set("Connected", True)
        self._connected = True
        self.can_reverse = bool(self._get("CanReverse", False))
        try:
            self.step_size = float(self._get("StepSize") or 0) or None
        except Exception:
            self.step_size = None

    def disconnect(self) -> None:
        try:
            self._set("Connected", False)
        finally:
            self._connected = False

    @property
    def position(self) -> float:
        return float(self._get("Position", 0.0) or 0.0) % 360.0

    @property
    def mechanical_position(self) -> float | None:
        value = self._get("MechanicalPosition")
        return None if value is None else round(float(value) % 360.0, 3)

    @property
    def target_position(self) -> float | None:
        value = self._get("TargetPosition")
        return None if value is None else round(float(value) % 360.0, 3)

    @property
    def moving(self) -> bool:
        return bool(self._get("IsMoving", False))

    @property
    def reversed(self) -> bool:
        return bool(self._get("Reverse", False))

    def move_absolute(self, position_angle: float) -> None:
        self._require()
        self._invoke("MoveAbsolute", float(position_angle) % 360.0)

    def move_relative(self, delta: float) -> None:
        self._require()
        self._invoke("Move", float(delta))

    def halt(self) -> None:
        self._require()
        self._invoke("Halt")

    def sync(self, position_angle: float) -> None:
        self._require()
        self._invoke("Sync", float(position_angle) % 360.0)

    def set_reversed(self, reverse: bool) -> None:
        self._require()
        if not self.can_reverse:
            raise DeviceError("this rotator cannot be reversed")
        self._set("Reverse", bool(reverse))

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        return {
            "connected": True,
            "position": round(self.position, 3),
            "mechanical": self.mechanical_position,
            "target": self.target_position,
            "moving": self.moving,
            "canReverse": self.can_reverse,
            "reversed": self.reversed,
            "stepSize": self.step_size,
        }


class AscomFlatPanel(_AscomDevice, FlatPanel):
    _COVER_STATES = {0: "notpresent", 1: "closed", 2: "moving", 3: "open", 4: "unknown",
                     5: "error"}

    def __init__(self, prog_id: str, name: str) -> None:
        FlatPanel.__init__(self, prog_id, name)
        self._prog_id = prog_id

    def connect(self) -> None:
        self._open(self._prog_id)
        self._set("Connected", True)
        self._connected = True
        self.max_brightness = int(self._get("MaxBrightness", 100) or 100)
        self.has_cover = self.cover_state not in ("notpresent", "unknown")

    def disconnect(self) -> None:
        try:
            self._set("Connected", False)
        finally:
            self._connected = False

    @property
    def brightness(self) -> int:
        return int(self._get("Brightness", 0) or 0)

    @property
    def light_on(self) -> bool:
        # CalibratorStatus: 1 = Off, 3 = Ready.
        return int(self._get("CalibratorState", 1) or 1) == 3

    @property
    def cover_state(self) -> str:
        return self._COVER_STATES.get(int(self._get("CoverState", 0) or 0), "unknown")

    def turn_on(self, brightness: int) -> None:
        self._require()
        self._invoke("CalibratorOn", int(brightness))

    def turn_off(self) -> None:
        self._require()
        self._invoke("CalibratorOff")

    def open_cover(self) -> None:
        self._require()
        self._invoke("OpenCover")

    def close_cover(self) -> None:
        self._require()
        self._invoke("CloseCover")

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        return {
            "connected": True,
            "brightness": self.brightness,
            "maxBrightness": self.max_brightness,
            "lightOn": self.light_on,
            "hasCover": self.has_cover,
            "coverState": self.cover_state,
        }


class AscomSafetyMonitor(_AscomDevice, SafetyMonitor):
    def __init__(self, prog_id: str, name: str) -> None:
        SafetyMonitor.__init__(self, prog_id, name)
        self._prog_id = prog_id

    def connect(self) -> None:
        self._open(self._prog_id)
        self._set("Connected", True)
        self._connected = True

    def disconnect(self) -> None:
        try:
            self._set("Connected", False)
        finally:
            self._connected = False

    @property
    def is_safe(self) -> bool:
        # A monitor that cannot be asked is not a monitor saying yes. `_get`
        # hands back the default on any driver error, and the default here is
        # the one that stops the night rather than the one that continues it.
        return bool(self._get("IsSafe", False))

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        return {"connected": True, "safe": self.is_safe}


class AscomDome(_AscomDevice, Dome):
    _SHUTTER_STATES = {0: "open", 1: "closed", 2: "opening", 3: "closing", 4: "error"}

    def __init__(self, prog_id: str, name: str) -> None:
        Dome.__init__(self, prog_id, name)
        self._prog_id = prog_id

    def connect(self) -> None:
        self._open(self._prog_id)
        self._set("Connected", True)
        self._connected = True
        self.can_park = bool(self._get("CanPark", False))
        self.can_shutter = bool(self._get("CanSetShutter", False))
        self.can_slave = bool(self._get("CanSlave", False))

    def disconnect(self) -> None:
        try:
            self._set("Connected", False)
        finally:
            self._connected = False

    @property
    def shutter_state(self) -> str:
        if not self.can_shutter:
            return "notpresent"
        return self._SHUTTER_STATES.get(
            int(self._get("ShutterStatus", 4) or 4), "error")

    @property
    def at_park(self) -> bool:
        return bool(self._get("AtPark", False))

    @property
    def slewing(self) -> bool:
        return bool(self._get("Slewing", False))

    @property
    def slaved(self) -> bool:
        return bool(self._get("Slaved", False))

    @property
    def azimuth(self) -> float | None:
        value = self._get("Azimuth")
        return None if value is None else float(value)

    def open_shutter(self) -> None:
        self._require()
        self._invoke("OpenShutter")

    def close_shutter(self) -> None:
        self._require()
        self._invoke("CloseShutter")

    def park(self) -> None:
        self._require()
        self._invoke("Park")

    def set_slaved(self, on: bool) -> None:
        self._require()
        self._set("Slaved", bool(on))

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        return {
            "connected": True,
            "shutterState": self.shutter_state,
            "canShutter": self.can_shutter,
            "canPark": self.can_park,
            "canSlave": self.can_slave,
            "slaved": self.slaved,
            "atPark": self.at_park,
            "slewing": self.slewing,
            "azimuth": self.azimuth,
        }


class AscomSwitch(_AscomDevice, SwitchBank):
    """An ASCOM Switch bank — which is what a Pegasus Powerbox presents.

    The channel list is read once at connect: names and ranges describe how the
    box is wired and do not change while it is plugged in, whereas the values do
    and are read live.
    """

    def __init__(self, prog_id: str, name: str) -> None:
        SwitchBank.__init__(self, prog_id, name)
        self._prog_id = prog_id
        self._channels: list[dict[str, Any]] = []

    def connect(self) -> None:
        self._open(self._prog_id)
        self._set("Connected", True)
        self._connected = True
        self._channels = self._describe()

    def _describe(self) -> list[dict[str, Any]]:
        count = int(self._get("MaxSwitch", 0) or 0)
        found: list[dict[str, Any]] = []
        for index in range(count):
            try:
                minimum = float(self._invoke("MinSwitchValue", index) or 0.0)
                maximum = float(self._invoke("MaxSwitchValue", index) or 1.0)
                step = float(self._invoke("SwitchStep", index) or 1.0)
                found.append({
                    "index": index,
                    "name": str(self._invoke("GetSwitchName", index) or f"Switch {index}"),
                    "description": str(self._invoke("GetSwitchDescription", index) or ""),
                    "min": minimum,
                    "max": maximum,
                    "step": step,
                    # A channel that only goes from 0 to 1 in one step is a
                    # relay, and is worth showing as a button rather than a
                    # slider — which is most of what a power box has.
                    "boolean": minimum == 0.0 and maximum == 1.0 and step >= 1.0,
                    "writable": bool(self._invoke("CanWrite", index)),
                })
            except Exception:                     # noqa: BLE001 - skip a bad channel
                continue
        return found

    def disconnect(self) -> None:
        try:
            self._set("Connected", False)
        finally:
            self._connected = False

    @property
    def channels(self) -> list[dict[str, Any]]:
        out = []
        for channel in self._channels:
            entry = dict(channel)
            try:
                entry["value"] = float(self._invoke("GetSwitchValue", channel["index"]))
            except Exception:                     # noqa: BLE001 - reported as unknown
                entry["value"] = None
            out.append(entry)
        return out

    def get_value(self, index: int) -> float:
        self._require()
        return float(self._invoke("GetSwitchValue", int(index)))

    def set_value(self, index: int, value: float) -> None:
        self._require()
        channel = next((c for c in self._channels if c["index"] == int(index)), None)
        if channel is None:
            raise DeviceError(f"this switch has no channel {index}")
        if not channel["writable"]:
            raise DeviceError(f"{channel['name']} is read-only")
        if channel["boolean"]:
            self._invoke("SetSwitch", int(index), bool(value))
        else:
            self._invoke("SetSwitchValue", int(index), float(value))

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        return {"connected": True, "channels": self.channels}


FACTORIES = {
    "camera": AscomCamera,
    "mount": AscomMount,
    "filterwheel": AscomFilterWheel,
    "focuser": AscomFocuser,
    "rotator": AscomRotator,
    "flatpanel": AscomFlatPanel,
    "piercam": AscomCamera,
    "safetymonitor": AscomSafetyMonitor,
    "dome": AscomDome,
    "switch": AscomSwitch,
}


def create(kind: str, driver_id: str, name: str, channel: str = "default"):
    factory = FACTORIES.get(kind)
    if factory is None:
        raise DeviceError(f"no ASCOM driver class for {kind}")
    device = factory(driver_id, name)
    device._channel = channel
    return device


# ---------------------------------------------------------------------------
# Telling one device from another when they share a driver
# ---------------------------------------------------------------------------
#
# A driver that handles several identical cameras has two ways of saying which
# one it means.  Some register a ProgID per device (ASCOM.Foo.Camera,
# ASCOM.Foo_2.Camera), which needs nothing from us beyond letting the ProgID be
# chosen.  Others register one ProgID and keep the choice — a serial number, a
# device index — in the ASCOM Profile, where it is normally set by hand in the
# driver's own setup window.  That is the case this section is for: read what
# the driver keeps there, let it be set per telescope, and write it back in the
# moment before that telescope connects.

# One ProgID's profile is one shared store, so the window between writing the
# setting and the driver reading it at connect time must not be entered twice
# at once.  Two telescopes connecting to the same driver together is exactly
# the situation this whole section exists for, so this lock is load-bearing.
_CONNECT_LOCK = threading.RLock()


def connect_guard() -> threading.RLock:
    """Held from writing a driver's settings until it has connected."""
    return _CONNECT_LOCK


def _profile_for(kind: str, prog_id: str):
    """A Profile object pointed at the right device type, on the COM thread."""
    device_type = _ASCOM_TYPES.get(kind)
    if device_type is None:
        raise DeviceError(f"no ASCOM device type for {kind}")
    if not _HAVE_PYWIN32:
        raise DeviceError("pywin32 is not installed; ASCOM is unavailable")
    if not str(prog_id or "").strip():
        raise DeviceError("no driver id given")
    profile = win32com.client.Dispatch("ASCOM.Utilities.Profile")
    profile.DeviceType = device_type
    return profile


def profile_settings(kind: str, prog_id: str) -> list[dict[str, str]]:
    """What the driver itself keeps for this ProgID, as {name, value}.

    These are the driver's own settings, not ours — the same values its setup
    window writes.  Which of them names the device is the driver's business to
    label; we only show what is there.
    """
    def job() -> list[dict[str, str]]:
        profile = _profile_for(kind, prog_id)
        found = []
        # The subkey is optional in the COM interface and mandatory here: late
        # binding cannot supply a default, and leaving it off fails with a
        # "missing parameter" that reads like the driver is not installed.
        for entry in profile.Values(prog_id, ""):
            name = str(entry.Key).strip()
            if name:                      # the unnamed default is the driver's name
                found.append({"name": name, "value": str(entry.Value)})
        return found

    try:
        return _com().call(job, timeout=20)
    except DeviceError:
        raise
    except Exception as exc:              # noqa: BLE001 - driver or platform
        raise DeviceError(f"could not read {prog_id}'s settings: {exc}") from exc


def write_profile_settings(kind: str, prog_id: str,
                           values: dict[str, str]) -> None:
    """Put values back into the driver's own profile."""
    if not values:
        return

    def job() -> None:
        profile = _profile_for(kind, prog_id)
        for name, value in values.items():
            profile.WriteValue(prog_id, str(name), str(value), "")

    try:
        _com().call(job, timeout=30)
    except DeviceError:
        raise
    except Exception as exc:              # noqa: BLE001 - driver or platform
        raise DeviceError(f"could not set {prog_id}'s settings: {exc}") from exc


#: Profile values that name the physical device rather than configure it.
#: Read straight out of each ProgID's profile so the driver list can say which
#: camera is which — a driver registered three times as "ASI Camera (1..3)"
#: tells you nothing about which body is on which telescope, and the answer is
#: sitting in the profile the whole time.
_IDENTITY_KEYS = ("SelectedCamID", "CameraId", "CamName", "CameraName",
                  # ZWO's own spellings: EAF focusers, EFW wheels, CAA rotators
                  # each keep an id and a friendly name under their own key.
                  "EAFID", "EAFRname", "EFWID", "EFWRname", "CAAID", "CAARname",
                  "SerialNumber", "Serial", "DeviceId", "DeviceNumber",
                  "SelectedDevice", "SelectedFocuser", "SelectedWheel",
                  "InstanceNumber", "Name")

#: Keys whose value is a name to show as-is, not an id to prefix with its key.
_IDENTITY_NAMES = ("CamName", "CameraName", "Name", "EAFRname", "EFWRname",
                   "CAARname")


def device_identity(kind: str, prog_id: str) -> str:
    """A short "which device is this" label, or "" when the driver keeps none.

    Best effort by design: it never raises and never blocks the driver list.
    A driver that says nothing simply gets no label.
    """
    try:
        values = {row["name"]: row["value"] for row in
                  profile_settings(kind, prog_id)}
    except Exception:                     # noqa: BLE001 - a label is not worth an error
        return ""
    parts: list[str] = []
    for key in _IDENTITY_KEYS:
        value = str(values.get(key, "")).strip()
        # "0" is a real device index; "" and "None" are not answers.
        if not value or value.lower() in ("none", "null", "n/a", "-1"):
            continue
        parts.append(value if key in _IDENTITY_NAMES
                     else f"{key.replace('Selected', '')} {value}")
        if len(parts) == 2:
            break
    return ", ".join(parts)


def setup_dialog(kind: str, prog_id: str, channel: str = "default",
                 timeout: float = 25.0) -> None:
    """Open the driver's own setup window and return once it is up.

    On an apartment thread of its own, never the telescope's: the dialog is
    modal and sits there until somebody closes it, and the telescope's thread
    has exposures and status polls to answer in the meantime.  We wait only
    long enough to know the driver started, so a bad ProgID is still an error
    the operator sees rather than a window that never appears.
    """
    if kind not in FACTORIES:
        raise DeviceError(f"no ASCOM driver class for {kind}")
    if not _HAVE_PYWIN32:
        raise DeviceError("pywin32 is not installed; ASCOM is unavailable")
    executor = _com(f"setup-{channel}")
    ready = threading.Event()
    trouble: list[BaseException] = []

    def job() -> None:
        try:
            driver = win32com.client.Dispatch(prog_id)
        except BaseException as exc:      # noqa: BLE001 - reported to the caller
            trouble.append(exc)
            ready.set()
            raise
        ready.set()
        driver.SetupDialog()

    def run() -> None:
        try:
            executor.call(job, timeout=3600)
        except Exception:                 # noqa: BLE001 - already reported
            pass

    threading.Thread(target=run, daemon=True,
                     name=f"ascom-setup-{channel}").start()
    if not ready.wait(timeout):
        raise DeviceError(f"{prog_id} did not open its setup window — it may "
                          "already have one open behind this one")
    if trouble:
        raise DeviceError(f"{prog_id} would not start: {trouble[0]}")
