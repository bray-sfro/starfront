"""Device registry: what can be connected, what is connected, and its live state."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque
from typing import Any

from . import alpaca, ascom, phd2, zwo
from .base import KINDS, Device, DeviceError

_FILE_LOG = logging.getLogger("astrocontrol.session")
_LEVELS = {"info": logging.INFO, "success": logging.INFO,
           "warn": logging.WARNING, "error": logging.ERROR}

KIND_LABELS = {
    "camera": "Camera",
    "mount": "Mount",
    "filterwheel": "Filter Wheel",
    "focuser": "Focuser",
    "rotator": "Rotator",
    "flatpanel": "Flat Panel",
    "guider": "Guider",
    "piercam": "Pier Camera",
    "safetymonitor": "Safety Monitor",
    "dome": "Dome / Roof",
    "switch": "Power / Switches",
}


class DeviceManager:
    """The devices of one telescope.

    A multi-telescope observatory has one manager per optical train, but the
    operator watches a single event log and scans the network once, so both the
    log and the Alpaca cache can be handed in and shared.  `channel` names the
    ASCOM apartment thread this manager's devices are created on: one per
    telescope, so a long download on one camera cannot stall the other.
    """

    def __init__(self, label: str = "", events: deque | None = None,
                 alpaca_cache: list[dict[str, Any]] | None = None,
                 channel: str = "default", config: Any = None) -> None:
        self._devices: dict[str, Device | None] = {kind: None for kind in KINDS}
        self._backends: dict[str, str] = {}
        self._lock = threading.RLock()
        self._alpaca_cache: list[dict[str, Any]] = (
            alpaca_cache if alpaca_cache is not None else [])
        self.events: deque[dict[str, Any]] = (
            events if events is not None else deque(maxlen=400))
        # Prefixed onto this manager's log lines so two cameras connecting are
        # told apart.  Blank for the master, whose messages read as they always
        # did.
        self.label = label
        self.channel = channel
        # Only PHD2 needs this: connecting to it means starting the program,
        # picking a profile and connecting its equipment, and all three are
        # settings rather than driver properties.
        self.config = config

    # -- event log ---------------------------------------------------------
    def log(self, message: str, level: str = "info") -> None:
        text = f"{self.label}: {message}" if self.label else message
        self.events.appendleft({"time": time.time(), "level": level, "message": text})
        # Also to the file, so the last thing the rig was doing survives a crash
        # and is sitting next to the traceback in the morning.
        _FILE_LOG.log(_LEVELS.get(level, logging.INFO), text)

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self.events)[:limit]

    # -- discovery ---------------------------------------------------------
    def backends(self) -> list[dict[str, Any]]:
        return [
            {"id": "ascom", "name": "ASCOM", "available": ascom.available(),
             "note": "Windows ASCOM Platform drivers."},
            {"id": "zwo", "name": "ZWO direct", "available": zwo.available(),
             "note": "ASI cameras and EAF focusers through ZWO's own SDK, "
                     "addressed by serial number. Reaches every connected "
                     "device, not the two ZWO registers with ASCOM."},
            {"id": "alpaca", "name": "Alpaca", "available": alpaca.available(),
             "note": "Network devices. Scan or enter host:port."},
            {"id": "phd2", "name": "PHD2", "available": phd2.available(),
             "note": "Autoguider. Enter the host and port PHD2 listens on."},
        ]

    def drivers(self, kind: str, refresh: bool = False) -> list[dict[str, Any]]:
        if kind not in KINDS:
            raise DeviceError(f"unknown device kind {kind!r}")
        found: list[dict[str, Any]] = []
        if kind == "guider":
            # PHD2 is not a driver you pick from a list; it is an address.
            return [{"backend": "phd2", "id": entry["id"], "name": entry["name"]}
                    for entry in phd2.list_devices()]
        for entry in ascom.list_devices(kind, refresh=refresh):
            found.append({"backend": "ascom", "id": entry["id"],
                          "name": entry["name"],
                          "device": entry.get("device", "")})
        # ZWO's own SDK, listed alongside. These are the physical devices the
        # SDK can see, so a third camera appears here even when ZWO's ASCOM
        # side has only two registrations to offer.
        for entry in zwo.list_devices(kind):
            found.append({"backend": "zwo", "id": entry["id"],
                          "name": entry["name"],
                          "device": entry.get("device", "")})
        for entry in self._alpaca_cache:
            if entry["kind"] == kind:
                found.append({"backend": "alpaca", "id": entry["id"], "name": entry["name"]})
        return found

    def scan_alpaca(self, host: str | None = None, port: int | None = None) -> int:
        """Refresh the Alpaca device cache, either by broadcast or a direct address."""
        if host:
            devices = alpaca.configured_devices(host, int(port or 11111))
        else:
            devices = alpaca.discover()
        known = {entry["id"] for entry in self._alpaca_cache}
        added = [entry for entry in devices if entry["id"] not in known]
        self._alpaca_cache.extend(added)
        self.log(f"Alpaca scan found {len(devices)} device(s), {len(added)} new")
        return len(devices)

    # -- connection --------------------------------------------------------
    def connect(self, kind: str, backend: str, driver_id: str,
                name: str | None = None,
                driver_options: dict[str, str] | None = None) -> Device:
        """Bring a device up.

        `driver_options` are settings belonging to the ASCOM driver itself,
        written into its profile in the moment before it is opened.  That is how
        three telescopes share one driver: each writes the device id it wants,
        connects, and the driver reads it on the way up.
        """
        if kind not in KINDS:
            raise DeviceError(f"unknown device kind {kind!r}")
        with self._lock:
            if self._devices[kind] is not None:
                raise DeviceError(f"{KIND_LABELS[kind]} is already connected")

            if backend == "phd2":
                if kind != "guider":
                    raise DeviceError("the PHD2 backend only provides a guider")
                options: dict[str, Any] = {}
                if self.config is not None:
                    try:
                        options = self.config.section("guiding")
                    except Exception:           # noqa: BLE001 - fall back to defaults
                        options = {}
                device = phd2.create(driver_id, name, options)
            elif backend == "ascom":
                device = ascom.create(kind, driver_id, name or driver_id,
                                      channel=self.channel)
            elif backend == "zwo":
                device = zwo.create(kind, driver_id, name or driver_id)
            elif backend == "alpaca":
                device = alpaca.create(kind, driver_id, name or driver_id)
            else:
                raise DeviceError(f"unknown backend {backend!r}")

            # A driver's own settings and the opening of it are one step: the
            # profile is shared by every telescope using that ProgID, so another
            # one must not write over the choice in between.
            options = driver_options if backend == "ascom" else None
            guard = ascom.connect_guard() if options else contextlib.nullcontext()
            try:
                with guard:
                    if options:
                        ascom.write_profile_settings(kind, driver_id, options)
                        self.log(f"{KIND_LABELS[kind]}: set "
                                 + ", ".join(f"{key}={value}" for key, value
                                             in sorted(options.items()))
                                 + f" on {driver_id}")
                    device.connect()
            except Exception as exc:
                self.log(f"{KIND_LABELS[kind]} failed to connect: {exc}", "error")
                raise DeviceError(str(exc)) from exc

            self._devices[kind] = device
            self._backends[kind] = backend
            if kind == "filterwheel":
                self.apply_filter_names()
            self.log(f"{KIND_LABELS[kind]} connected: {device.name}", "success")
            return device

    def apply_filter_names(self) -> None:
        """Lay the names from Equipment over the wheel's slots.

        Done here rather than at every place that reads a name, so the sequencer
        matching "Ha", the FITS header, the calibration library and the buttons
        on screen are all looking at the same list. ASCOM's `Names` is read-only,
        so a driver that reports "1".."7" cannot be told otherwise — and left
        alone it means the plan asks for Ha, nothing matches, and the whole night
        is shot through whichever slot was loaded.
        """
        wheel = self._devices.get("filterwheel")
        if wheel is None or self.config is None:
            return
        with contextlib.suppress(Exception):
            wheel.set_name_overrides(
                list(self.config.get("camera", "filterNames", []) or []))

    def driver_settings(self, kind: str, driver_id: str) -> list[dict[str, str]]:
        """What an ASCOM driver keeps in its own profile for this ProgID."""
        if kind not in KINDS:
            raise DeviceError(f"unknown device kind {kind!r}")
        if not str(driver_id).startswith("ASCOM.") and "." not in str(driver_id):
            # A ZWO serial number or an Alpaca address has no ASCOM profile to
            # read, and there is nothing to pin: the serial *is* the device.
            raise DeviceError(
                "only ASCOM drivers keep settings we can read. This device is "
                "addressed directly, so there is nothing to choose.")
        return ascom.profile_settings(kind, driver_id)

    def open_driver_setup(self, kind: str, driver_id: str) -> None:
        """Open the driver's own setup window for this telescope's slot."""
        if kind not in KINDS:
            raise DeviceError(f"unknown device kind {kind!r}")
        existing = self._devices.get(kind)
        if existing is not None and existing.connected:
            raise DeviceError(
                f"{KIND_LABELS[kind]} is connected; disconnect it first — a "
                "driver will not take its settings while it is in use")
        ascom.setup_dialog(kind, driver_id, channel=self.channel)
        self.log(f"{KIND_LABELS[kind]}: opened {driver_id}'s setup window")

    def disconnect(self, kind: str) -> None:
        with self._lock:
            device = self._devices.get(kind)
            if device is None:
                return
            try:
                device.disconnect()
            except Exception as exc:
                self.log(f"{KIND_LABELS[kind]} disconnect error: {exc}", "warn")
            finally:
                self._devices[kind] = None
                self._backends.pop(kind, None)
                self.log(f"{KIND_LABELS[kind]} disconnected")

    def disconnect_all(self) -> None:
        for kind in KINDS:
            self.disconnect(kind)

    # -- access ------------------------------------------------------------
    def get(self, kind: str) -> Device | None:
        return self._devices.get(kind)

    def require(self, kind: str) -> Device:
        device = self._devices.get(kind)
        if device is None or not device.connected:
            raise DeviceError(f"{KIND_LABELS.get(kind, kind)} is not connected")
        return device

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for kind in KINDS:
            device = self._devices.get(kind)
            if device is None:
                out[kind] = {"connected": False, "name": None, "backend": None}
                continue
            try:
                state = device.status()
            except Exception as exc:
                state = {"connected": False, "error": str(exc)}
            state["name"] = device.name
            state["backend"] = self._backends.get(kind)
            state["driverId"] = device.driver_id
            out[kind] = state
        return out
