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
import itertools
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from queue import PriorityQueue, Queue
from typing import Any, Callable

import numpy as np

from .base import (Camera, CameraLostError, DeviceError, Dome, FilterWheel,
                   FlatPanel, Focuser, Mount, Rotator, SafetyMonitor, SwitchBank)

try:                                    # pragma: no cover - platform dependent
    import pythoncom
    import win32com.client
    _HAVE_PYWIN32 = True
except Exception:                       # pragma: no cover
    _HAVE_PYWIN32 = False


class _ComExecutor:
    """Runs every COM call on a single STA thread.

    Two lanes.  Status polling fills the queue continuously — ten property
    reads a second against a mount on a serial link — and a command that
    merely joins the back of that queue waits for every read ahead of it.
    That was a hand-controller press arriving at the mount ten seconds
    late.  An urgent call goes to the front: a jog, a stop, a park, an
    abort - the things a person is waiting on with their hand on a button.
    """

    def __init__(self, name: str = "ascom-com") -> None:
        self._queue: PriorityQueue = PriorityQueue()
        self._order = itertools.count()
        #: What the thread is inside right now, and since when (0.2.52e): a
        #: driver that has put up a modal message box never returns from the
        #: call that raised it, and everything queued behind it waits too.
        self.current: str | None = None
        self.current_since: float | None = None
        self.native_id: int | None = None
        #: Set when this thread was given up on and replaced (`reset_channel`).
        self.abandoned = False
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)
        self._thread.start()

    def _run(self) -> None:
        self.native_id = threading.get_native_id()
        pythoncom.CoInitialize()
        try:
            while True:
                _lane, _seq, job, future, label = self._queue.get()
                if job is None:
                    return
                if future.set_running_or_notify_cancel():
                    self.current, self.current_since = label, time.monotonic()
                    try:
                        future.set_result(job())
                    except Exception as exc:       # noqa: BLE001 - relayed to caller
                        future.set_exception(exc)
                    finally:
                        self.current, self.current_since = None, None
        finally:
            pythoncom.CoUninitialize()

    def call(self, job: Callable[[], Any], timeout: float = 180.0,
             urgent: bool = False, label: str = "") -> Any:
        future: Future = Future()
        self._queue.put((0 if urgent else 1, next(self._order), job, future, label))
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            # Not left queued: a call nobody is waiting for any more must not
            # run later, against a camera that has since moved on.
            future.cancel()
            busy = self.stuck_for()
            inside = (f" (the driver has been inside {self.current} for {busy:.0f}s)"
                      if busy and self.current else "")
            raise DeviceError(f"{label or 'the driver call'} did not answer within "
                              f"{timeout:g}s{inside}") from None

    def stuck_for(self) -> float:
        """Seconds the call in progress has taken so far; 0 when idle."""
        since = self.current_since
        return 0.0 if since is None else max(0.0, time.monotonic() - since)


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


def channel_stuck(channel: str) -> tuple[str | None, float]:
    """What a channel's apartment thread is stuck in, and for how long."""
    with _executor_lock:
        existing = _executors.get(channel)
    if existing is None:
        return None, 0.0
    return existing.current, existing.stuck_for()


def channel_thread_id(channel: str) -> int | None:
    """The Windows thread id of a channel's apartment thread, if it has one."""
    with _executor_lock:
        existing = _executors.get(channel)
    return None if existing is None else existing.native_id


def reset_channel(channel: str) -> bool:
    """Give up on a channel's apartment thread and start a fresh one.

    Only ever for a camera's own channel, whose one device is being
    reconnected: a thread stuck inside the driver cannot be interrupted, so
    the new connection is opened on a new thread instead of queueing behind
    the old one for ever. True when there was a stuck thread to replace.
    """
    with _executor_lock:
        existing = _executors.get(channel)
        if existing is None or not existing.stuck_for():
            return False
        existing.abandoned = True
        _executors.pop(channel, None)
    return True


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

    #: Per-instance in `_open`; the class-level None keeps a wrapper that was
    #: never opened from sharing one dict with every other.
    _cache: dict[str, tuple[Any, float]] | None = None

    def _open(self, prog_id: str) -> None:
        self._cache = {}
        self._driver = _com(self._channel).call(
            lambda: win32com.client.Dispatch(prog_id), timeout=60)

    def _get(self, name: str, default: Any = None) -> Any:
        try:
            return _com(self._channel).call(lambda: getattr(self._driver, name),
                                            label=name)
        except Exception:
            return default

    def _read(self, name: str, timeout: float) -> Any:
        """A live property read that raises, with a deadline of its own."""
        try:
            return _com(self._channel).call(lambda: getattr(self._driver, name),
                                            timeout=timeout, label=name)
        except DeviceError:
            raise
        except Exception as exc:
            raise DeviceError(f"{name}: {exc}") from exc

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

    def _invoke(self, name: str, *args: Any, urgent: bool = False,
                timeout: float = 180.0) -> Any:
        try:
            return _com(self._channel).call(lambda: getattr(self._driver, name)(*args),
                                            urgent=urgent, timeout=timeout, label=name)
        except DeviceError:
            raise                                  # a deadline: already says which
        except Exception as exc:
            raise DeviceError(f"{name}: {exc}") from exc

    def _set_urgent(self, name: str, value: Any) -> None:
        """A property write that jumps the polling queue."""
        try:
            _com(self._channel).call(lambda: setattr(self._driver, name, value),
                                     urgent=True)
        except Exception as exc:
            raise DeviceError(f"{name}: {exc}") from exc

    def _get_cached(self, name: str, ttl: float, default: Any = None) -> Any:
        """A property read answered from the last read while it is fresh.

        For the status panel only — the control loops keep reading live.
        Every read is a round trip over the mount's serial link, and the
        panel asked for ten of them twice a second; the ones that change
        slowly (parked, home, tracking, pier side, sidereal time) are not
        worth the bandwidth that a jog command then has to queue behind.
        """
        if self._cache is None:
            self._cache = {}
        now = time.monotonic()
        hit = self._cache.get(name)
        if hit is not None and now - hit[1] < ttl:
            return hit[0]
        value = self._get(name, default)
        self._cache[name] = (value, now)
        return value

    def _forget_cached(self) -> None:
        if self._cache:
            self._cache.clear()


class AscomCamera(_AscomDevice, Camera):
    #: Each of the health probe's reads may take this long (0.2.52e). A live
    #: camera answers a property in milliseconds; one that does not answer in
    #: this time is a driver that is stuck.
    PROBE_SECONDS = 8.0
    #: How long Abort waits for the driver before giving up on it.
    ABORT_SECONDS = 10.0
    #: Longer than this inside one driver call, outside an exposure, and the
    #: driver is taken to be stuck (a modal box of its own, as often as not).
    STUCK_SECONDS = 20.0

    def __init__(self, prog_id: str, name: str) -> None:
        Camera.__init__(self, prog_id, name)
        self._prog_id = prog_id
        self._light = True
        #: Set when the camera is known to be gone without asking it: a
        #: driver message box was found and closed (`driverdialogs`).
        self.lost_reason: str | None = None
        self.exposure_min: float | None = None
        self.exposure_max: float | None = None

    def connect(self) -> None:
        # A camera thread left stuck by the last connection (a driver box
        # nobody closed) is not queued behind: the new driver object is made
        # on a fresh thread, and the old one is left to itself.
        if channel_stuck(self._channel)[1] > self.STUCK_SECONDS:
            reset_channel(self._channel)
        self._open(self._prog_id)
        self._set("Connected", True)
        self._connected = True
        self.lost_reason = None
        self._forget_cooler()
        from . import driverdialogs
        driverdialogs.watch(self)
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
        self.exposure_min, self.exposure_max = self._exposure_limits()

    def disconnect(self) -> None:
        from . import driverdialogs
        driverdialogs.unwatch(self)
        try:
            if self.stuck():
                # Closing a driver that is stuck inside a call would queue
                # behind it for ever; its thread is given up on instead.
                driverdialogs.dismiss(self)
                if self.stuck():
                    reset_channel(self._channel)
                    return
            _com(self._channel).call(lambda: setattr(self._driver, "Connected", False),
                                     timeout=15.0, label="Connected")
        except Exception as exc:
            raise DeviceError(f"Connected: {exc}") from exc
        finally:
            self._connected = False

    # -- is anybody there (0.2.52e) ---------------------------------------
    #
    # 2026-11-04, FSQ106: ZWO's ASCOM driver had lost the camera but still
    # said Connected; its exposure limits read 0 to 0 s, and StartExposure put
    # up "Please input Exposure Duration between 0 and 0s, and restart
    # exposure!" - a modal box on the thread making the call, which does not
    # return until somebody clicks OK. The exposure never started, Abort
    # queued behind the box, and the run sat there. So the camera is asked a
    # few cheap questions first, and StartExposure is not called on one that
    # is not there.

    def stuck(self) -> bool:
        """Whether the camera's thread is stuck inside a driver call."""
        label, busy = channel_stuck(self._channel)
        if not busy:
            return False
        limit = self.STUCK_SECONDS
        if label == "ImageArray":
            limit += 180.0                       # a big sensor on a slow hub
        return busy > limit

    def _exposure_limits(self) -> tuple[float | None, float | None]:
        def number(name: str) -> float | None:
            try:
                value = self._read(name, self.PROBE_SECONDS)
                return None if value is None else float(value)
            except Exception:                    # noqa: BLE001 - optional in ASCOM
                return None
        return number("ExposureMin"), number("ExposureMax")

    def probe(self) -> str | None:
        """Why this camera is not there, or None when it answers sensibly."""
        if not self._connected:
            return None
        if self.lost_reason:
            return self.lost_reason
        label, busy = channel_stuck(self._channel)
        if self.stuck():
            return (f"its driver has been stuck inside {label or 'a call'} for "
                    f"{busy:.0f}s")
        try:
            if not bool(self._read("Connected", self.PROBE_SECONDS)):
                return "its driver says it is no longer connected"
        except DeviceError as exc:
            return f"it does not answer ({exc})"
        low, high = self._exposure_limits()
        self.exposure_min, self.exposure_max = low, high
        if high is not None and high <= 0 and (low is None or low <= 0):
            return ("its driver reports exposure limits of 0 to 0 s, which is "
                    "what it says once it has lost the camera")
        try:
            width = int(self._read("CameraXSize", self.PROBE_SECONDS) or 0)
        except DeviceError as exc:
            return f"it does not answer ({exc})"
        if width <= 0:
            return "its driver reports a 0-pixel sensor"
        if self.can_cool:
            with contextlib.suppress(DeviceError, TypeError, ValueError):
                temperature = self._read("CCDTemperature", self.PROBE_SECONDS)
                if temperature is not None and float(temperature) <= -270.0:
                    return (f"its sensor reads {float(temperature):g} C, the value "
                            "its driver gives with no camera behind it")
        return None

    def _checked(self, what: str) -> None:
        reason = self.probe()
        if reason:
            raise CameraLostError(f"{self.name} is not responding: {reason}. "
                                  f"{what} was not sent to the driver")

    def clamp_exposure(self, seconds: float) -> float:
        """An exposure inside the driver's own limits - when they are real."""
        low, high = self.exposure_min, self.exposure_max
        value = float(seconds)
        if high is not None and high > 0 and (low is None or low <= high):
            if value > high:
                value = high
            if low is not None and value < low:
                value = low
        return value

    def start_exposure(self, seconds: float, light: bool = True) -> None:
        self._require()
        self._checked("StartExposure")
        value = self.clamp_exposure(seconds)
        self._light = light
        # A healthy driver returns from StartExposure at once (some take the
        # exposure synchronously); either way it is not waited on for ever.
        self._invoke("StartExposure", value, bool(light),
                     timeout=max(60.0, value + 60.0))

    def abort_exposure(self) -> None:
        if not (self._connected and self.can_abort):
            return
        from . import driverdialogs
        if channel_stuck(self._channel)[1] > 1.0:
            # Something is in the way on the camera's thread. A driver box
            # is closed; the abort is then sent, and waited on only briefly.
            driverdialogs.dismiss(self)
        try:
            self._invoke("AbortExposure", urgent=True, timeout=self.ABORT_SECONDS)
        except DeviceError as exc:
            if "did not answer" in str(exc):
                raise DeviceError(
                    f"the camera driver is not answering - gave up waiting on Abort "
                    f"after {self.ABORT_SECONDS:g}s ({exc})") from exc
            raise

    @property
    def image_ready(self) -> bool:
        return bool(self._get("ImageReady", False))

    def get_image(self) -> np.ndarray:
        raw = self._need("ImageArray")
        # ASCOM hands back [x][y]; transpose into the usual row-major layout.
        array = np.array(raw, dtype=np.int64).T
        return np.clip(array, 0, 65535).astype(np.uint16)

    # -- the cooler, read with some care --------------------------------------
    #
    # The sensor temperature, the setpoint, the cooler switch and its power
    # are asked for together and kept for a couple of seconds, for two
    # reasons. The status poll asks every second, and seven COM round trips a
    # second on the camera's own thread sit in front of the exposure traffic
    # that matters. And some drivers - ZWO's among them - answer a property
    # read made while the camera is reading out with nonsense: a sensor that
    # has sat at -2 C all night reported +1 C for a moment between two frames,
    # cooler still on at full power, which a sensor cannot do. So nothing is
    # read while the camera reports it is reading out, and a lone reading far
    # from the last one is held back until the next reading agrees with it.

    _COOLER_TTL = 2.0
    #: More than this between two readings two seconds apart is not a sensor
    #: cooling or warming - even flat out, a cooler moves about a degree in
    #: that time - so it is a driver misreading until said twice.
    _COOLER_JUMP_C = 1.5

    def _cooler(self) -> dict[str, Any]:
        now = time.monotonic()
        cached = getattr(self, "_cooler_cache", None)
        if cached is not None and now - cached["at"] < self._COOLER_TTL:
            return cached
        # 3 is "reading out" in ASCOM's CameraStates; the driver is busiest then.
        if cached is not None and int(self._get("CameraState", 0) or 0) == 3:
            return cached

        def number(name: str, digits: int) -> float | None:
            value = self._get(name)
            try:
                return None if value is None else round(float(value), digits)
            except (TypeError, ValueError):
                return None

        reading = {
            "at": now,
            "temperature": number("CCDTemperature", 2),
            "setpoint": number("SetCCDTemperature", 2),
            "coolerOn": bool(self._get("CoolerOn", False)),
            "coolerPower": number("CoolerPower", 1),
        }
        if cached is not None:
            before = cached.get("temperature")
            after = reading["temperature"]
            if before is not None and after is not None and abs(after - before) > self._COOLER_JUMP_C:
                pending = getattr(self, "_cooler_pending", None)
                if pending is None or abs(pending - after) > 1.0:
                    # Not believed yet: keep what we had, remember what was
                    # said, and believe it if it is said again.
                    self._cooler_pending = after
                    reading["temperature"] = before
                else:
                    self._cooler_pending = None
            else:
                self._cooler_pending = None
            # A reading the driver would not give keeps the last one it did.
            for key in ("temperature", "setpoint", "coolerPower"):
                if reading[key] is None and cached.get(key) is not None:
                    reading[key] = cached[key]
        self._cooler_cache = reading
        return reading

    def _forget_cooler(self) -> None:
        self._cooler_cache = None
        self._cooler_pending = None

    @property
    def temperature(self) -> float | None:
        return self._cooler()["temperature"]

    @property
    def cooler_on(self) -> bool:
        return bool(self._cooler()["coolerOn"])

    def set_cooler(self, on: bool) -> None:
        self._require()
        self._set("CoolerOn", bool(on))
        self._forget_cooler()

    @property
    def setpoint(self) -> float | None:
        return self._cooler()["setpoint"]

    def set_setpoint(self, celsius: float) -> None:
        self._require()
        if not self.can_cool:
            raise DeviceError("camera cannot set a temperature setpoint")
        self._set("SetCCDTemperature", float(celsius))
        self._forget_cooler()

    @property
    def cooler_power(self) -> float | None:
        return self._cooler()["coolerPower"]

    def set_settings(self, binning: int | None = None, gain: int | None = None,
                     offset: int | None = None) -> None:
        self._require()
        if binning is None and gain is None and offset is None:
            return
        # The same driver puts up the same sort of box for a gain or a bin
        # outside limits it has forgotten (0.2.52e): nothing is written to a
        # camera that is not there, and nothing outside limits that are real.
        self._checked("The camera settings")
        if binning is not None:
            binning = int(binning)
            if self.max_bin and self.max_bin >= 1:
                binning = max(1, min(binning, int(self.max_bin)))
            self._set("BinX", binning)
            self._set("BinY", binning)
            self.binning = binning
        if gain is not None and getattr(self, "has_gain", True):
            gain = int(gain)
            if self.gain_max > self.gain_min:
                gain = max(self.gain_min, min(gain, self.gain_max))
            self._set("Gain", gain)
            self.gain = gain
        if offset is not None and getattr(self, "has_offset", True):
            offset = int(offset)
            if self.offset_max > self.offset_min:
                offset = max(self.offset_min, min(offset, self.offset_max))
            self._set("Offset", offset)
            self.offset = offset

    def read_settings(self) -> dict[str, int | None]:
        """Gain and offset read from the driver, not remembered (0.2.53).

        Only property reads - never a write, so never the driver's "between 0
        and 0" box. A Gain/Offset that could not be read at connect (a driver
        still waking up after a reconnect) made `has_gain` False for the life
        of the connection and every later gain write was silently skipped;
        one that can be read now is believed again.
        """
        self._require()
        found: dict[str, int | None] = {"gain": None, "offset": None}
        for key, prop, flag in (("gain", "Gain", "has_gain"), ("offset", "Offset", "has_offset")):
            try:
                value = self._read(prop, self.READ_SETTINGS_SECONDS)
            except DeviceError:
                continue
            if value is None:
                continue
            found[key] = int(value)
            setattr(self, key, int(value))
            setattr(self, flag, True)
        if found["gain"] is not None and not self.gain_max:
            # Limits that read 0/0 at connect are asked for again, so a write
            # is clamped into the camera's real range rather than refused.
            self.gain_min = int(self._get("GainMin", 0) or 0)
            self.gain_max = int(self._get("GainMax", 0) or 0)
        if found["offset"] is not None and not self.offset_max:
            self.offset_min = int(self._get("OffsetMin", 0) or 0)
            self.offset_max = int(self._get("OffsetMax", 0) or 0)
        return found

    #: How long one Gain/Offset read may take before it counts as unreadable.
    READ_SETTINGS_SECONDS = 10.0

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        state = int(self._get("CameraState", 0) or 0)   # 0 idle, 2 exposing, 3 reading
        percent = float(self._get("PercentCompleted", 0) or 0)
        duration = float(self._get("LastExposureDuration", 0) or 0)
        cooler = self._cooler()
        return {
            "connected": True,
            "exposing": state in (1, 2, 3),
            "imageReady": self.image_ready,
            "elapsed": round(duration * percent / 100.0, 2),
            "duration": round(duration, 2),
            "temperature": cooler["temperature"],
            "setpoint": cooler["setpoint"],
            "coolerOn": cooler["coolerOn"],
            "coolerPower": cooler["coolerPower"],
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
        #: Axes a hand move is running on; a released button clears its own.
        self._jog_axes: set[int] = set()

    def connect(self) -> None:
        self._open(self._prog_id)
        self._set("Connected", True)
        self._connected = True
        self.can_park = bool(self._get("CanPark", False))
        self.can_slew = bool(self._get("CanSlewAsync", False) or self._get("CanSlew", False))
        self.can_sync = bool(self._get("CanSync", False))
        self.can_set_tracking = bool(self._get("CanSetTracking", False))
        self.can_find_home = bool(self._get("CanFindHome", False))
        self._axis_rates = self._read_axis_rates()

    def _read_axis_rates(self) -> dict[int, list[tuple[float, float]]]:
        """What the driver says MoveAxis will accept, per axis, in deg/s.

        Asked once at connect, the way N.I.N.A. asks before its manual slew
        buttons: a rate outside the advertised ranges is refused or, worse,
        quietly mapped to something else.  Empty when the driver cannot say.
        """
        rates: dict[int, list[tuple[float, float]]] = {}
        for axis in (0, 1):
            try:
                def read(axis=axis):
                    found = []
                    collection = self._driver.AxisRates(axis)
                    for index in range(1, int(collection.Count) + 1):
                        item = collection.Item(index)
                        found.append((float(item.Minimum), float(item.Maximum)))
                    return found
                ranges = _com(self._channel).call(read)
                if ranges:
                    rates[axis] = sorted(ranges)
            except Exception:                     # noqa: BLE001 - optional in ASCOM
                continue
        return rates

    def _allowed_rate(self, axis: int, rate: float) -> float:
        """The nearest rate the driver advertises for this axis."""
        ranges = self._axis_rates.get(axis) if hasattr(self, "_axis_rates") else None
        if not ranges:
            return rate
        best, distance = rate, None
        for low, high in ranges:
            candidate = min(max(rate, low), high)
            gap = abs(candidate - rate)
            if distance is None or gap < distance:
                best, distance = candidate, gap
        return best

    def disconnect(self) -> None:
        try:
            self._set("Connected", False)
        finally:
            self._connected = False
            self._forget_cached()

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
    def pulse_guiding(self) -> bool:
        with contextlib.suppress(Exception):
            return bool(self._get("IsPulseGuiding", False))
        return False

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

    def reported_clock(self) -> dict[str, Any]:
        """UTCDate and SiderealTime. One read, and it does not write.

        The same driver properties the status panel already uses for
        sidereal time, plus the UTC clock beside them. Not urgent: a clock
        check must not step in front of a stop or a park. Eight seconds is
        as long as a quiet driver may take. Neither property is required.
        """
        def read() -> dict[str, Any]:
            utc = lst = None
            with contextlib.suppress(Exception):
                utc = self._driver.UTCDate
            with contextlib.suppress(Exception):
                lst = self._driver.SiderealTime
            return {"utc": utc, "lst": lst}

        try:
            return _com(self._channel).call(read, timeout=8.0, label="clock")
        except Exception:
            return {"utc": None, "lst": None}

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
        self._invoke("AbortSlew", urgent=True)
        self._forget_cached()

    def set_tracking(self, on: bool) -> None:
        self._require()
        self._set_urgent("Tracking", bool(on))
        self._forget_cached()

    def park(self) -> None:
        self._require()
        self._invoke("Park", urgent=True)
        self._forget_cached()

    def unpark(self) -> None:
        self._require()
        self._invoke("Unpark", urgent=True)
        self._forget_cached()

    def find_home(self) -> None:
        self._require()
        self._invoke("FindHome")

    def jog(self, direction: str, rate_deg_s: float) -> None:
        self._require()
        axis = 0 if direction in ("east", "west") else 1
        # CanMoveAxis is a method, not a property: it is asked per axis.
        if not bool(self._invoke("CanMoveAxis", axis, urgent=True)):
            raise DeviceError("mount does not support manual slewing on this axis")
        sign = 1.0 if direction in ("north", "east") else -1.0
        rate = self._allowed_rate(axis, float(rate_deg_s))
        # Urgent: a hand on a button is waiting, and the polling queue is not.
        self._invoke("MoveAxis", axis, sign * rate, urgent=True)
        self._jog_axes.add(axis)
        self._jogging = True
        self._forget_cached()

    def stop_jog(self, direction: str | None = None) -> None:
        """End a hand move: MoveAxis with rate 0, as N.I.N.A. does.

        With a direction, only that axis - which is what a released button
        means; without one, both, which is what Stop means.
        """
        if not self._connected:
            return
        axes = ((0,) if direction in ("east", "west")
                else (1,) if direction in ("north", "south") else (0, 1))
        for axis in axes:
            try:
                self._invoke("MoveAxis", axis, 0.0, urgent=True)
            except DeviceError:
                pass
            self._jog_axes.discard(axis)
        self._jogging = bool(self._jog_axes)
        self._forget_cached()

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        # Position and motion live; the slow-changing flags from the last
        # read for a couple of seconds.  Ten round trips a tick became five,
        # which is the difference between a serial link with room for a
        # command and one without.
        pier = self._get_cached("SideOfPier", 3.0)
        side = None if pier is None or int(pier) < 0 else ("east" if int(pier) == 0 else "west")
        return {
            "connected": True,
            "ra": round(self.ra, 5),
            "dec": round(self.dec, 4),
            "altitude": self.altitude,
            "azimuth": self.azimuth,
            "lst": round(float(self._get_cached("SiderealTime", 5.0, 0.0) or 0.0), 4),
            "slewing": self.slewing,
            "tracking": bool(self._get_cached("Tracking", 2.0, False)),
            "atPark": bool(self._get_cached("AtPark", 2.0, False)),
            "atHome": bool(self._get_cached("AtHome", 3.0, False)),
            "canFindHome": self.can_find_home,
            "sideOfPier": side,
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
        # CoverState is not latched here. An Alnitak often reports Unknown
        # until it is asked, and a failed read used to be stored as "no cover"
        # for the rest of the night.

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
        # CalibratorStatus: 1 = Off, 3 = Ready. A failed read is off, not on.
        return self.calibrator_state == "ready"

    @property
    def calibrator_state(self) -> str:
        from .. import coverprep
        missing = object()
        value = self._get("CalibratorState", missing)
        if value is missing or value is None:
            return "unknown"
        return coverprep.map_calibrator(value)

    @property
    def cover_state(self) -> str:
        # 0 is CoverStatus.NotPresent, a real answer. A failed read must not
        # use that number, or a panel whose cover cannot be read is treated
        # as a lamp with no cover and OpenCover is never sent.
        from .. import coverprep
        missing = object()
        value = self._get("CoverState", missing)
        if value is missing or value is None:
            return "unread"
        return coverprep.map_cover(value)

    def probe_cover(self, timeout: float = 1.5) -> dict:
        """One bounded cover/light read for the status cache. Not for commands."""
        from .. import coverprep
        error = ""
        try:
            raw_cover = self._read("CoverState", timeout)
            state = coverprep.map_cover(raw_cover)
        except Exception as exc:                     # noqa: BLE001 - cached, not raised
            state = "unread"
            error = str(exc)
        try:
            raw_cal = self._read("CalibratorState", timeout)
            calibrator = coverprep.map_calibrator(raw_cal)
        except Exception as exc:                     # noqa: BLE001
            calibrator = "unknown"
            error = error or str(exc)
        try:
            brightness = int(self._read("Brightness", timeout) or 0)
        except Exception as exc:                     # noqa: BLE001
            brightness = None
            error = error or str(exc)
        has = False if state == "notpresent" else (
            True if state in ("closed", "moving", "open", "error") else None)
        return {
            "connected": True,
            "coverState": state,
            "hasCover": has,
            "lightOn": calibrator == "ready",
            "calibratorState": calibrator,
            "brightness": brightness,
            "maxBrightness": self.max_brightness,
            "error": error,
            "stale": state == "unread",
        }

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
        # The cache only. CoverState on a stuck Alnitak must not run inside
        # the status request; the probe thread fills the cache.
        if not self._connected:
            return {"connected": False}
        from .. import coverstatus
        snap = coverstatus.cached(self)
        return {
            "connected": True,
            "brightness": snap.get("brightness"),
            "maxBrightness": snap.get("maxBrightness", self.max_brightness),
            "lightOn": snap.get("lightOn"),
            "calibratorState": snap.get("calibratorState") or "",
            "hasCover": snap.get("hasCover"),
            "coverState": snap.get("coverState") or "unknown",
            "readAt": snap.get("readAt"),
            "stale": snap.get("stale", True),
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
        # 0.2.53b: ShutterStatus 0 *is* shutterOpen. `... or 4` turned every
        # open roof into "error", so nothing could ever see one open.
        value = self._get("ShutterStatus", None)
        try:
            return self._SHUTTER_STATES.get(int(value), "error")
        except (TypeError, ValueError):
            return "error"

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
        from ..roofguard import blocked
        why = blocked()
        if why:
            raise DeviceError(f"{why}; OpenShutter was not sent")
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

    def describe_channels(self) -> list[dict[str, Any]]:
        return [dict(channel) for channel in self._channels]

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
    # A camera gets an apartment thread of its own (0.2.52e). Its driver is
    # the one that puts up modal message boxes ("Please input Exposure
    # Duration between 0 and 0s") that never return; on the telescope's
    # shared thread that also froze the focuser, the filter wheel and the
    # mount behind it, and nothing could be reconnected without breaking the
    # others. Each device's COM object still lives on, and is only called
    # from, the one thread it was made on.
    device._channel = f"{channel}/{kind}" if kind in ("camera", "piercam") else channel
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
