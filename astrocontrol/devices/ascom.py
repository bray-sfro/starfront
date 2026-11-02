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
import logging
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from queue import Empty, PriorityQueue, Queue
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


LOG = logging.getLogger("astrocontrol.devices")

#: A call that has been running this long has not been slow, it has stopped:
#: nothing a driver does legitimately takes a minute without returning — an
#: ImageArray download is seconds, a Dispatch is seconds, a StartExposure is
#: instant — but a driver whose USB device has gone sits inside the SDK
#: forever, and every call queued behind it sits with it.
WEDGE_SECONDS = 60.0


class _ComExecutor:
    """Runs every COM call on a single STA thread.

    Two lanes.  Status polling fills the queue continuously — ten property
    reads a second against a mount on a serial link — and a command that
    merely joins the back of that queue waits for every read ahead of it.
    That was a hand-controller press arriving at the mount ten seconds
    late.  An urgent call goes to the front: a jog, a stop, a park, an
    abort - the things a person is waiting on with their hand on a button.

    And one way out.  A driver call that never returns — the ZWO SDK with
    the camera gone from the bus — would otherwise hold this thread, and
    the queue behind it, until the program exits: the status feed waited
    three minutes per property for a camera that was never going to
    answer, and the window went dead for twenty minutes.  A caller that
    times out behind a call stuck for longer than `WEDGE_SECONDS` declares
    the executor dead: everything queued fails at once, the channel gets a
    fresh thread for the reconnect, and the devices opened on this one
    report themselves disconnected rather than waiting again.  The stuck
    thread is left where it is; it is a daemon and dies with the process.
    """

    def __init__(self, name: str = "ascom-com", channel: str = "default") -> None:
        self.channel = channel
        self.dead = False
        self.dead_reason = ""
        self._queue: PriorityQueue = PriorityQueue()
        self._order = itertools.count()
        self._running: tuple[str, float] | None = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)
        self._thread.start()

    def _run(self) -> None:
        pythoncom.CoInitialize()
        try:
            while True:
                _lane, _seq, job, future, label = self._queue.get()
                if job is None:
                    return
                if self.dead:
                    future.set_exception(DeviceError(self.dead_reason))
                    continue
                if future.set_running_or_notify_cancel():
                    self._running = (label, time.monotonic())
                    try:
                        future.set_result(job())
                    except Exception as exc:       # noqa: BLE001 - relayed to caller
                        future.set_exception(exc)
                    finally:
                        self._running = None
        finally:
            pythoncom.CoUninitialize()

    def call(self, job: Callable[[], Any], timeout: float = 180.0,
             urgent: bool = False, label: str = "a driver call") -> Any:
        if self.dead:
            raise DeviceError(self.dead_reason)
        future: Future = Future()
        self._queue.put((0 if urgent else 1, next(self._order), job, future, label))
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            running = self._running
            if (running is not None and not self.dead
                    and time.monotonic() - running[1] > WEDGE_SECONDS):
                self._declare_dead(running[0], time.monotonic() - running[1])
                raise DeviceError(self.dead_reason) from None
            raise DeviceError(f"no answer within {timeout:g}s") from None

    def _declare_dead(self, label: str, stuck_for: float) -> None:
        with self._lock:
            if self.dead:
                return
            self.dead = True
            self.dead_reason = (
                f"the driver has stopped answering — {label} has been stuck for "
                f"{stuck_for:.0f}s. The device must be reconnected; if that fails, "
                f"power-cycle it and check its USB link")
        LOG.error("%s: %s", self.channel, self.dead_reason)
        with _executor_lock:
            if _executors.get(self.channel) is self:
                del _executors[self.channel]
        # Everything waiting behind the stuck call fails now rather than each
        # after its own three minutes.
        while True:
            try:
                _lane, _seq, _job, future, _label = self._queue.get_nowait()
            except Empty:
                break
            with contextlib.suppress(Exception):
                future.set_exception(DeviceError(self.dead_reason))


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
        if existing is None or existing.dead:
            if not _HAVE_PYWIN32:
                raise DeviceError("pywin32 is not installed; ASCOM is unavailable")
            existing = _ComExecutor(name=f"ascom-com-{channel}", channel=channel)
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

    #: Per-instance in `_open`; the class-level None keeps a wrapper that was
    #: never opened from sharing one dict with every other.
    _cache: dict[str, tuple[Any, float]] | None = None

    #: The apartment thread this wrapper's driver object lives on.  A COM
    #: object belongs to the thread that created it, so once that thread is
    #: declared dead the object is unreachable - a call from any other
    #: thread would marshal back to the stuck one and stick with it - and
    #: the only way on is a fresh Dispatch on a fresh thread, which is what
    #: `connect` does.
    _executor: _ComExecutor | None = None

    def _open(self, prog_id: str) -> None:
        self._cache = {}
        self._executor = _com(self._channel)
        self._driver = self._executor.call(
            lambda: win32com.client.Dispatch(prog_id), timeout=60, label="opening the driver")

    def _exec(self) -> _ComExecutor:
        executor = self._executor
        if executor is None:
            return _com(self._channel)
        if executor.dead:
            # The thread this driver lived on is gone with it.  Say so once
            # on the device's own state: the status panel shows it
            # disconnected, and the sequencer's reconnect is what brings it
            # back, on a new thread.
            self._connected = False
            raise DeviceError(executor.dead_reason)
        return executor

    def _get(self, name: str, default: Any = None) -> Any:
        try:
            return self._exec().call(lambda: getattr(self._driver, name), label=name)
        except Exception:
            return default

    def _need(self, name: str) -> Any:
        try:
            return self._exec().call(lambda: getattr(self._driver, name), label=name)
        except Exception as exc:
            raise DeviceError(f"{name}: {exc}") from exc

    def _set(self, name: str, value: Any) -> None:
        try:
            self._exec().call(lambda: setattr(self._driver, name, value), label=name)
        except Exception as exc:
            raise DeviceError(f"{name}: {exc}") from exc

    def _invoke(self, name: str, *args: Any, urgent: bool = False) -> Any:
        try:
            return self._exec().call(lambda: getattr(self._driver, name)(*args),
                                     urgent=urgent, label=name)
        except Exception as exc:
            raise DeviceError(f"{name}: {exc}") from exc

    def _set_urgent(self, name: str, value: Any) -> None:
        """A property write that jumps the polling queue."""
        try:
            self._exec().call(lambda: setattr(self._driver, name, value),
                              urgent=True, label=name)
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
    def __init__(self, prog_id: str, name: str) -> None:
        Camera.__init__(self, prog_id, name)
        self._prog_id = prog_id
        self._light = True

    def connect(self) -> None:
        self._open(self._prog_id)
        self._set("Connected", True)
        self._connected = True
        self._forget_cooler()
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
    #: A reading that has not moved by a hundredth of a degree in this long,
    #: while the driver says nothing is cooling it, is a driver that has
    #: stopped asking the camera. Seen after a reconnect: ZWO's answered
    #: -2.0 C for sixteen minutes while the sensor warmed to +3, and woke up
    #: the moment the cooler was commanded. So that is what is done.
    _COOLER_STALE_S = 180.0

    def _cooler(self) -> dict[str, Any]:
        now = time.monotonic()
        cached = getattr(self, "_cooler_cache", None)
        if cached is not None and now - cached["at"] < self._COOLER_TTL:
            return cached
        # 3 is "reading out" in ASCOM's CameraStates; the driver is busiest then.
        if cached is not None and int(self._get("CameraState", 0) or 0) == 3:
            return cached
        self._nudge_if_stale(cached, now)

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
            "stale": False,
        }
        # Three exact zeros at once is not a reading, it is a driver that
        # has lost the camera: ZWO's answers 0 for every number once the USB
        # link drops, with the cooler still "on". Taken as no reading at all,
        # so the last real one stands and the status says the link is gone.
        if (reading["temperature"] == 0.0 and reading["setpoint"] == 0.0
                and (reading["coolerPower"] or 0.0) == 0.0):
            reading["temperature"] = None
            reading["setpoint"] = None
            reading["coolerPower"] = None
            reading["stale"] = True
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

    def _nudge_if_stale(self, cached: dict[str, Any] | None, now: float) -> None:
        """Re-send the setpoint when the driver's temperature has frozen.

        Only when the reading is identical for minutes *and* the driver says
        the cooler is off or idle *and* the sensor is below room temperature:
        a cooler at its setpoint can honestly report the same number for an
        hour, and that case is left alone. A sensor below fifteen degrees
        with nothing cooling it cannot hold still, so a driver saying it does
        has stopped reading - and writing the setpoint back, unchanged, is
        the one call that was seen to start it reading again.
        """
        if cached is None:
            self._cooler_same_since = now
            self._cooler_nudged = False
            return
        temperature = cached.get("temperature")
        last = getattr(self, "_cooler_last_seen", None)
        if temperature is None or temperature != last:
            self._cooler_last_seen = temperature
            self._cooler_same_since = now
            self._cooler_nudged = False
            return
        idle = (not cached.get("coolerOn")) or not (cached.get("coolerPower") or 0)
        if (now - getattr(self, "_cooler_same_since", now) < self._COOLER_STALE_S
                or not idle or temperature >= 15.0 or getattr(self, "_cooler_nudged", False)):
            return
        self._cooler_nudged = True
        # The setpoint this program asked for, never the one the driver
        # reports: a driver that has stopped reading reports nonsense, and
        # writing its nonsense back as the setpoint set a camera to 0 C once.
        setpoint = getattr(self, "_wanted_setpoint", None)
        if setpoint is None:
            return
        try:
            if self.can_cool:
                self._set("SetCCDTemperature", float(setpoint))
            self._log(f"the camera's temperature has read {temperature:g} C for "
                      f"{self._COOLER_STALE_S / 60:.0f} minutes with the cooler reported "
                      f"off; re-sent the {setpoint:g} C setpoint to wake the driver's "
                      "reading", "warn")
        except Exception:                          # noqa: BLE001 - a nudge, not a need
            pass

    def _log(self, message: str, level: str = "info") -> None:
        """Into the file log: a device has no line to the event strip."""
        import logging
        logging.getLogger("astrocontrol.devices").log(
            logging.WARNING if level == "warn" else logging.INFO, message)

    def _forget_cooler(self) -> None:
        self._cooler_cache = None
        self._cooler_pending = None
        self._cooler_same_since = time.monotonic()
        self._cooler_nudged = False

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
        self._wanted_setpoint = float(celsius)
        self._forget_cooler()

    @property
    def reading_stale(self) -> bool:
        """Whether the driver has stopped giving real cooler numbers.

        True while every number it answers is zero at once, which is how
        ZWO's driver reports a camera whose USB link has dropped, and the
        values on show are the last real ones. A sequencer waiting for the
        camera to cool must not wait on these.
        """
        return bool(self._cooler().get("stale"))

    @property
    def cooler_power(self) -> float | None:
        return self._cooler()["coolerPower"]

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
            # The numbers above are the last real ones when this is set.
            "readingStale": bool(cooler.get("stale")),
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
                ranges = self._exec().call(read, label="AxisRates")
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
        if value is None:
            return None
        # ZWO's focuser driver answers -273 when it cannot reach the probe -
        # a device that has dropped off its USB hub, as much as one with no
        # probe fitted. Absolute zero is not a temperature anybody wants a
        # refocus trigger or a header built on; it is "no reading".
        if float(value) < -100.0:
            return None
        return round(float(value), 2)

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
