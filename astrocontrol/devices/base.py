"""Abstract device interfaces.

Every backend (ASCOM COM, Alpaca, PHD2) implements these same classes, so the
API layer and the UI never need to know what is actually driving the hardware.

Conventions used throughout:
  * RA is in hours (0..24), Dec in degrees (-90..+90).
  * Temperatures are degrees Celsius.
  * Focuser positions are integer steps.
  * Filter wheel positions are 0-based.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np


class DeviceError(RuntimeError):
    """Raised for any driver-level failure; surfaced to the UI as an error toast."""


# Device kinds understood by the application.
#: `piercam` is an ordinary camera in a different job: it watches the telescope
#: rather than the sky, so it shares every backend's camera class and differs
#: only in what the program does with the frames.
KINDS = ("camera", "mount", "filterwheel", "focuser", "rotator", "flatpanel",
         "guider", "piercam", "safetymonitor", "dome", "switch")


class Device:
    """Base class for all devices."""

    kind: str = "device"

    def __init__(self, driver_id: str, name: str) -> None:
        self.driver_id = driver_id
        self.name = name
        self._connected = False
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def _require(self) -> None:
        if not self._connected:
            raise DeviceError(f"{self.kind} is not connected")

    # -- reporting ---------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """Static information that does not change while connected."""
        return {"driverId": self.driver_id, "name": self.name, "kind": self.kind}

    def status(self) -> dict[str, Any]:
        """Live state, polled by the UI several times a second."""
        return {"connected": self._connected}


class Camera(Device):
    kind = "camera"

    # Static capabilities -------------------------------------------------
    sensor_width: int = 0
    sensor_height: int = 0
    pixel_size_um: float = 0.0
    max_bin: int = 4
    can_cool: bool = False
    can_abort: bool = True
    gain_min: int = 0
    gain_max: int = 0
    offset_min: int = 0
    offset_max: int = 0
    bayer_pattern: str | None = None

    # Mutable settings ----------------------------------------------------
    binning: int = 1
    gain: int = 0
    offset: int = 0

    def start_exposure(self, seconds: float, light: bool = True) -> None:
        raise NotImplementedError

    def abort_exposure(self) -> None:
        raise NotImplementedError

    @property
    def image_ready(self) -> bool:
        raise NotImplementedError

    def get_image(self) -> np.ndarray:
        """Return the last completed frame as a 2-D uint16 array."""
        raise NotImplementedError

    # Cooling -------------------------------------------------------------
    @property
    def temperature(self) -> float | None:
        return None

    @property
    def cooler_on(self) -> bool:
        return False

    def set_cooler(self, on: bool) -> None:
        raise DeviceError("camera has no cooler")

    @property
    def setpoint(self) -> float | None:
        return None

    def set_setpoint(self, celsius: float) -> None:
        raise DeviceError("camera has no cooler")

    @property
    def cooler_power(self) -> float | None:
        return None

    def set_settings(self, binning: int | None = None, gain: int | None = None,
                     offset: int | None = None) -> None:
        self._require()
        if binning is not None:
            if not 1 <= binning <= self.max_bin:
                raise DeviceError(f"binning must be 1..{self.max_bin}")
            self.binning = int(binning)
        if gain is not None:
            self.gain = int(np.clip(gain, self.gain_min, self.gain_max))
        if offset is not None:
            self.offset = int(np.clip(offset, self.offset_min, self.offset_max))


class Mount(Device):
    kind = "mount"

    can_park: bool = True
    can_slew: bool = True
    can_sync: bool = True
    can_set_tracking: bool = True
    #: Whether the mount can drive itself to its home switches. Plenty cannot,
    #: so everything that homes has to ask first.
    can_find_home: bool = False

    @property
    def ra(self) -> float:
        """Right ascension in hours."""
        raise NotImplementedError

    @property
    def dec(self) -> float:
        """Declination in degrees."""
        raise NotImplementedError

    @property
    def altitude(self) -> float | None:
        return None

    @property
    def azimuth(self) -> float | None:
        return None

    @property
    def slewing(self) -> bool:
        raise NotImplementedError

    @property
    def tracking(self) -> bool:
        raise NotImplementedError

    @property
    def at_park(self) -> bool:
        return False

    @property
    def at_home(self) -> bool:
        return False

    @property
    def side_of_pier(self) -> str | None:
        return None

    @property
    def site(self) -> dict[str, float] | None:
        """Observing site the driver reports, as latitude/longitude/elevation.

        Longitude follows the ASCOM convention: degrees east of Greenwich, so
        the western hemisphere is negative.  None when the driver has no idea.
        """
        return None

    def slew_to(self, ra_hours: float, dec_deg: float) -> None:
        raise NotImplementedError

    def sync_to(self, ra_hours: float, dec_deg: float) -> None:
        raise NotImplementedError

    def abort_slew(self) -> None:
        raise NotImplementedError

    def set_tracking(self, on: bool) -> None:
        raise NotImplementedError

    def park(self) -> None:
        raise NotImplementedError

    def unpark(self) -> None:
        raise NotImplementedError

    def find_home(self) -> None:
        """Drive to the home switches. Asynchronous: watch `slewing`."""
        raise NotImplementedError

    def jog(self, direction: str, rate_deg_s: float) -> None:
        """Start a manual slew. direction is one of north/south/east/west."""
        raise NotImplementedError

    def stop_jog(self) -> None:
        raise NotImplementedError


class FilterWheel(Device):
    """A filter wheel, and what the filters in it are called.

    The names are the part worth being careful about, because almost everything
    downstream matches on them rather than on slot numbers: the sequencer looks
    up "Ha" to decide where to move the wheel, the FITS header records the name,
    the planner allocates against it and the calibration library files flats by
    it.  A wheel that calls its slots "1".."7" therefore does not merely look
    wrong — the plan asks for Ha, nothing matches, and the night is shot through
    whichever slot happened to be loaded.

    ASCOM's `Names` is read-only, so there is no standard way to write a name
    into a driver that is only counting.  The names typed into Equipment are
    therefore the authority, laid over the slots the wheel reports.
    """

    kind = "filterwheel"

    def __init__(self, driver_id: str, name: str) -> None:
        super().__init__(driver_id, name)
        #: Names from the settings, in slot order. What the operator typed wins
        #: over what the driver says, because the driver's cannot be edited.
        self._overrides: list[str] = []

    def set_name_overrides(self, names: list[str] | None) -> None:
        self._overrides = [str(name).strip() for name in (names or [])]

    def _named(self, driver_names: list[str]) -> list[str]:
        """The driver's slots, under the names the operator gave them.

        Slot by slot, so naming the first three of seven leaves the other four
        as the driver has them rather than dropping them. A blank entry means
        "nothing said about this slot", not "no filter".

        Every name comes out in the program's one spelling - "Ha" off a
        driver is "H" here - so that nothing downstream ever sees two
        spellings of one filter.
        """
        from ..filters import canonical
        driver_names = [canonical(name) or str(name) for name in driver_names]
        if not self._overrides:
            return list(driver_names)
        if not driver_names:
            # A driver that reports no slots at all: the settings are all there
            # is, and a named list beats an empty one.
            return [canonical(name) for name in self._overrides if name]
        return [(canonical(self._overrides[index])
                 if index < len(self._overrides) and self._overrides[index].strip()
                 else driver_names[index])
                for index in range(len(driver_names))]

    @property
    def names(self) -> list[str]:
        raise NotImplementedError

    @property
    def position(self) -> int:
        """0-based slot index, or -1 while moving."""
        raise NotImplementedError

    @property
    def moving(self) -> bool:
        return self.position < 0

    def current_name(self) -> str:
        names = self.names
        position = self.position
        return names[position] if 0 <= position < len(names) else "-"

    def set_position(self, index: int) -> None:
        raise NotImplementedError

    def set_names(self, names: list[str]) -> None:
        raise DeviceError("driver does not support renaming filters")

    def status(self) -> dict[str, Any]:
        """The same for every backend, and here so it stays that way.

        It was written out twice, identically, and the copies had to be found
        and changed together the moment names stopped being whatever the driver
        said they were.
        """
        if not self._connected:
            return {"connected": False}
        names = self.names
        position = self.position
        return {
            "connected": True,
            "names": names,
            "position": position,
            "moving": position < 0,
            "target": position,
            "currentName": names[position] if 0 <= position < len(names) else "-",
        }


class Focuser(Device):
    kind = "focuser"

    max_step: int = 0
    step_size_um: float | None = None
    is_absolute: bool = True

    @property
    def position(self) -> int:
        raise NotImplementedError

    @property
    def moving(self) -> bool:
        raise NotImplementedError

    @property
    def temperature(self) -> float | None:
        return None

    def move_to(self, position: int) -> None:
        raise NotImplementedError

    def move_relative(self, delta: int) -> None:
        self.move_to(self.position + int(delta))

    def halt(self) -> None:
        raise NotImplementedError


class Rotator(Device):
    """A camera rotator.

    `position` is the sky position angle in degrees: the angle of the camera's
    up axis measured from north through east, which is the number a framing plan
    cares about.  `mechanical_position` is the raw angle of the hardware, which
    differs by whatever offset the driver has been synced to.
    """

    kind = "rotator"

    can_reverse: bool = False
    step_size: float | None = None

    @property
    def position(self) -> float:
        """Sky position angle in degrees, 0..360."""
        raise NotImplementedError

    @property
    def mechanical_position(self) -> float | None:
        return None

    @property
    def target_position(self) -> float | None:
        return None

    @property
    def moving(self) -> bool:
        raise NotImplementedError

    @property
    def reversed(self) -> bool:
        return False

    def move_absolute(self, position_angle: float) -> None:
        """Rotate to a sky position angle."""
        raise NotImplementedError

    def move_relative(self, delta: float) -> None:
        raise NotImplementedError

    def halt(self) -> None:
        raise NotImplementedError

    def sync(self, position_angle: float) -> None:
        """Tell the rotator what sky angle it is currently at."""
        raise DeviceError("driver cannot sync the rotator")

    def set_reversed(self, reverse: bool) -> None:
        raise DeviceError("driver cannot reverse the rotator")


class FlatPanel(Device):
    """ASCOM CoverCalibrator: a light source, optionally with a motorised cover."""

    kind = "flatpanel"

    max_brightness: int = 100
    has_cover: bool = False

    @property
    def brightness(self) -> int:
        raise NotImplementedError

    @property
    def light_on(self) -> bool:
        raise NotImplementedError

    @property
    def cover_state(self) -> str:
        """One of: notpresent, closed, moving, open, unknown."""
        return "notpresent"

    def turn_on(self, brightness: int) -> None:
        raise NotImplementedError

    def turn_off(self) -> None:
        raise NotImplementedError

    def open_cover(self) -> None:
        raise DeviceError("device has no cover")

    def close_cover(self) -> None:
        raise DeviceError("device has no cover")


class SafetyMonitor(Device):
    """One boolean that decides whether the sky may be pointed at.

    Deliberately the whole interface. A safety monitor aggregates cloud sensors,
    rain detectors, wind, and whatever else the observatory cares about, and
    hands back a single answer — and the value of it is that the answer is
    simple enough to be acted on without judgement at three in the morning.

    `is_safe` is False when the driver says unsafe **and when it cannot be
    asked**. A monitor that has stopped answering is not evidence that the
    weather is fine.
    """

    kind = "safetymonitor"

    @property
    def is_safe(self) -> bool:
        raise NotImplementedError


class Dome(Device):
    """A dome or roll-off roof.

    Slaving is left to the driver where it offers it. Working out a dome
    azimuth from mount coordinates needs the geometry of the building — pier
    offset, dome radius, mount dimensions — which the driver already has and
    this program does not.
    """

    kind = "dome"

    can_park: bool = True
    can_shutter: bool = False
    can_slave: bool = False

    @property
    def shutter_state(self) -> str:
        """One of: open, closed, opening, closing, error, notpresent."""
        return "notpresent"

    @property
    def at_park(self) -> bool:
        return False

    @property
    def slewing(self) -> bool:
        return False

    @property
    def slaved(self) -> bool:
        return False

    @property
    def azimuth(self) -> float | None:
        return None

    def open_shutter(self) -> None:
        raise DeviceError("dome has no shutter")

    def close_shutter(self) -> None:
        raise DeviceError("dome has no shutter")

    def park(self) -> None:
        raise DeviceError("dome cannot park")

    def set_slaved(self, on: bool) -> None:
        raise DeviceError("dome cannot slave itself")


class SwitchBank(Device):
    """Switched outputs: power, dew heaters, anything with a state.

    ASCOM's Switch is a bank of numbered channels, each either a boolean or a
    value in a range, each with a name the driver supplies. That is exactly what
    a Pegasus Powerbox presents through its own ASCOM Switch driver, and what a
    Lunatico or a Digital Loggers strip presents through theirs — so nothing
    here is specific to one make, and everything with a Switch driver works.

    The names matter: "channel 3" is not something anybody wants to reason about
    at two in the morning, and `Pegasus Powerbox: Camera` is.
    """

    kind = "switch"

    @property
    def channels(self) -> list[dict[str, Any]]:
        """Every channel, as {index, name, description, value, min, max, step,
        boolean, writable}."""
        return []

    def set_value(self, index: int, value: float) -> None:
        raise NotImplementedError

    def get_value(self, index: int) -> float:
        raise NotImplementedError


class Guider(Device):
    """An autoguider.  The only backend today is PHD2 over its JSON socket.

    Guiding is not an ASCOM device: PHD2 owns its own camera and mount
    connection, so this interface is about starting, stopping and dithering a
    guiding session, plus the RMS error that says how well it is going.
    """

    kind = "guider"

    @property
    def state(self) -> str:
        """Stopped, Selected, Calibrating, Guiding, LostLock, Paused, Looping."""
        return "Unknown"

    @property
    def guiding(self) -> bool:
        return False

    @property
    def settling(self) -> bool:
        return False

    def start_guiding(self, settle_pixels: float = 1.5, settle_time: float = 8.0,
                      settle_timeout: float = 60.0, recalibrate: bool = False) -> None:
        raise NotImplementedError

    def stop_guiding(self) -> None:
        raise NotImplementedError

    def dither(self, pixels: float = 3.0, ra_only: bool = False,
               settle_pixels: float = 1.5, settle_time: float = 8.0,
               settle_timeout: float = 60.0) -> None:
        raise NotImplementedError

    def set_paused(self, paused: bool) -> None:
        raise DeviceError("driver cannot pause guiding")
