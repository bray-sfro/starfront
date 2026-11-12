"""Lightweight fake devices for the checks: camera, mount, focuser, filter wheel.

    from fakes import install, FakeCamera, ...

No hardware and no SDK. Each fake has a `delay` (seconds) that every driver
call sleeps first, so a check can stand in for a slow or hung ASCOM/Alpaca
driver, and a `fail` flag that makes the next call raise DeviceError.
`install(rig, kind, device)` puts a connected fake straight into a rig's
DeviceManager slot, which is what connecting through the UI ends with.
"""

from __future__ import annotations

import threading
import time

import numpy as np

from astrocontrol.devices.base import Camera, DeviceError, FilterWheel, Focuser, Mount


class _Slow:
    delay = 0.0          # every command sleeps this long first
    status_delay = 0.0   # every status read sleeps this long (a driver stuck behind a long call)
    fail = False

    def _status_wait(self):
        if self.status_delay:
            time.sleep(self.status_delay)

    def _wait(self):
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise DeviceError(f"{self.kind}: simulated driver failure")


class FakeCamera(_Slow, Camera):
    sensor_width = 320
    sensor_height = 240
    pixel_size_um = 3.76
    can_cool = True
    gain_max = 300
    offset_max = 100

    def __init__(self, name="Fake Camera", seed=0):
        Camera.__init__(self, f"fake:{name}", name)
        self._started = 0.0
        self._seconds = 0.0
        self._exposing = False
        self._cooler = False
        self._setpoint = -10.0
        self._rng = np.random.default_rng(seed)
        self.exposures = 0

    def start_exposure(self, seconds, light=True):
        self._wait()
        self._require()
        self._started = time.monotonic()
        self._seconds = float(seconds)
        self._exposing = True
        self.exposures += 1

    def abort_exposure(self):
        self._exposing = False

    @property
    def image_ready(self):
        return self._exposing and time.monotonic() - self._started >= self._seconds

    def get_image(self):
        self._wait()
        self._exposing = False
        frame = self._rng.normal(1000, 20, (self.sensor_height, self.sensor_width))
        return np.clip(frame, 0, 65535).astype(np.uint16)

    @property
    def temperature(self):
        return self._setpoint if self._cooler else 15.0

    @property
    def cooler_on(self):
        return self._cooler

    def set_cooler(self, on):
        self._wait()
        self._cooler = bool(on)

    @property
    def setpoint(self):
        return self._setpoint

    def set_setpoint(self, celsius):
        self._wait()
        self._setpoint = float(celsius)

    @property
    def cooler_power(self):
        return 30.0 if self._cooler else 0.0

    def status(self):
        self._status_wait()
        return {"connected": self._connected, "exposing": self._exposing}


class FakeMount(_Slow, Mount):
    def __init__(self, name="Fake Mount"):
        Mount.__init__(self, f"fake:{name}", name)
        self._ra, self._dec = 5.0, 20.0
        self._slew_until = 0.0
        self._tracking = True
        self._parked = False
        self.slew_seconds = 0.5

    ra = property(lambda self: self._ra)
    dec = property(lambda self: self._dec)
    altitude = property(lambda self: 45.0)
    azimuth = property(lambda self: 180.0)
    slewing = property(lambda self: time.monotonic() < self._slew_until)
    tracking = property(lambda self: self._tracking)
    at_park = property(lambda self: self._parked)
    at_home = property(lambda self: False)
    pulse_guiding = property(lambda self: False)
    side_of_pier = property(lambda self: "east")
    site = property(lambda self: None)

    def slew_to(self, ra_hours, dec_deg):
        self._wait()
        self._ra, self._dec = float(ra_hours), float(dec_deg)
        self._slew_until = time.monotonic() + self.slew_seconds

    def sync_to(self, ra_hours, dec_deg):
        self._wait()
        self._ra, self._dec = float(ra_hours), float(dec_deg)

    def abort_slew(self):
        self._slew_until = 0.0

    def set_tracking(self, on):
        self._wait()
        self._tracking = bool(on)

    def park(self):
        self._wait()
        self._parked = True

    def unpark(self):
        self._wait()
        self._parked = False

    def status(self):
        self._status_wait()
        return {"connected": self._connected, "ra": self._ra, "dec": self._dec,
                "slewing": self.slewing, "tracking": self._tracking, "atPark": self._parked}


class FakeFocuser(_Slow, Focuser):
    max_step = 100000

    def __init__(self, name="Fake Focuser"):
        Focuser.__init__(self, f"fake:{name}", name)
        self._pos = 30000
        self._target = 30000
        self._until = 0.0
        self.move_seconds = 0.2

    @property
    def position(self):
        return self._target if time.monotonic() >= self._until else self._pos

    @property
    def moving(self):
        return time.monotonic() < self._until

    def move_to(self, position):
        self._wait()
        self._pos = self.position
        self._target = int(position)
        self._until = time.monotonic() + self.move_seconds

    def halt(self):
        self._until = 0.0

    def status(self):
        self._status_wait()
        return {"connected": self._connected, "position": self.position, "moving": self.moving}


class FakeWheel(_Slow, FilterWheel):
    def __init__(self, name="Fake Wheel", names=("L", "R", "G", "B", "Ha")):
        FilterWheel.__init__(self, f"fake:{name}", name)
        self._names = list(names)
        self._pos = 0

    @property
    def names(self):
        return self._named(self._names)

    @property
    def position(self):
        return self._pos

    def set_position(self, index):
        self._wait()
        self._pos = int(index)


def install(rig, kind, device):
    """Put a connected fake into one telescope's slot."""
    device.connect()
    with rig.manager._lock if hasattr(rig.manager, "_lock") else threading.Lock():
        rig.manager._devices[kind] = device
    return device
