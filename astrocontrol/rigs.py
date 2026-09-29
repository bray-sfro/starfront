"""The live telescopes: a device manager, a capture service and a focuser each.

`equipment.py` holds what the telescopes *are*; this holds what they are *doing*.
One `Rig` is everything that belongs to one optical train, and `RigSet` keeps
that collection in step with the stored definitions.

Two things are deliberately shared across every rig:

  * **the event log** — the operator watches one log, not three, so every
    manager appends to the same deque and prefixes slave messages with the
    telescope's name.
  * **the Alpaca device cache** — a network scan finds the devices on the
    network, not the devices of one telescope, so scanning once is enough.

Everything else is per rig, including the COM apartment thread the ASCOM
backend marshals onto.  One thread per telescope means a 20-second image
download on one camera cannot stall the other camera's `ImageReady` poll, which
is exactly what has to keep working for two scopes to expose together.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from .capture import CaptureService
from .config import Config
from .devices.base import DeviceError
from .devices.manager import DeviceManager
from .equipment import (MASTER_DEVICE_KINDS, RIG_DEVICE_KINDS, RIG_SECTIONS,
                        EquipmentStore, safe_name)
from .focusing import AutoFocuser
from .solving import Solver


class RigConfig:
    """`Config` as one telescope sees it.

    Reads of a per-telescope section come back as the shared values with that
    telescope's own overrides laid on top; everything else passes straight
    through.  The master keeps no overrides, so it *is* the shared settings —
    which is what makes a single-telescope observatory behave exactly as it did
    before rigs existed, settings file included.
    """

    def __init__(self, config: Config, store: EquipmentStore, rig_id: str,
                 primary: bool) -> None:
        self._config = config
        self._store = store
        self.rig_id = rig_id
        self.primary = primary

    def section(self, name: str) -> dict[str, Any]:
        values = self._config.section(name)
        if not self.primary and name in RIG_SECTIONS:
            values.update(self._store.rig_settings(self.rig_id, name))
        return values

    def get(self, section: str, key: str, default: Any = None) -> Any:
        if not self.primary and section in RIG_SECTIONS:
            overlay = self._store.rig_settings(self.rig_id, section)
            if key in overlay:
                value = overlay[key]
                return default if value is None else value
        return self._config.get(section, key, default)

    def update(self, section: str, values: dict[str, Any]) -> dict[str, Any]:
        if self.primary or section not in RIG_SECTIONS:
            return self._config.update(section, values)
        self._store.update_rig_settings(self.rig_id, section, values)
        return self.section(section)

    def all(self) -> dict[str, dict[str, Any]]:
        merged = self._config.all()
        if not self.primary:
            for name in RIG_SECTIONS:
                merged[name] = self.section(name)
        return merged

    @property
    def overrides(self) -> dict[str, dict[str, Any]]:
        """Only the keys this telescope sets for itself."""
        if self.primary:
            return {}
        return {name: self._store.rig_settings(self.rig_id, name)
                for name in RIG_SECTIONS
                if self._store.rig_settings(self.rig_id, name)}


class Rig:
    """One telescope and everything that drives it."""

    def __init__(self, definition: dict[str, Any], config: Config,
                 store: EquipmentStore, events: deque, alpaca_cache: list,
                 library: Any = None, overheads: Any = None) -> None:
        self.id = definition["id"]
        self.name = definition["name"]
        self.role = definition["role"]
        self.store = store

        self.config = RigConfig(config, store, self.id, primary=self.is_master)
        self.manager = DeviceManager(label="" if self.is_master else self.name,
                                     events=events, alpaca_cache=alpaca_cache,
                                     channel=self.id, config=self.config)
        self.capture = CaptureService(self.manager, self.config)
        self.capture.telescope = self.name
        # One library serves every telescope — the masters are told apart by
        # what is in their headers, not by which folder they sit in.
        self.capture.library = library
        # One record of what this observatory costs between exposures, shared by
        # every telescope: they download over the same bus and slew on the same
        # mount, and one set of measurements is what makes it a measurement
        # rather than a guess per scope.
        self.capture.overheads = overheads
        self.overheads = overheads
        self.solver = Solver(self.manager, self.capture, self.config)
        self.focuser = AutoFocuser(self.manager, self.capture, self.config)
        self.focuser.overheads = overheads

    # -- identity ----------------------------------------------------------
    @property
    def is_master(self) -> bool:
        return self.role == "master"

    @property
    def kinds(self) -> tuple[str, ...]:
        """The device slots this telescope actually has."""
        return (RIG_DEVICE_KINDS + MASTER_DEVICE_KINDS if self.is_master
                else RIG_DEVICE_KINDS)

    def adopt(self, definition: dict[str, Any], multiple: bool) -> None:
        """Take on a changed name or role without dropping any connection."""
        self.name = definition["name"]
        self.role = definition["role"]
        self.manager.label = "" if self.is_master else self.name
        self.config.primary = self.is_master
        self.capture.telescope = self.name
        # Frames from two telescopes must not land on each other, so each gets
        # its own folder as soon as there is more than one.  A lone telescope
        # keeps the original layout.
        self.capture.subfolder = safe_name(self.name) if multiple else None

    # -- reporting ---------------------------------------------------------
    def definition(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "role": self.role,
                "devices": self.store.rig(self.id)["devices"],
                "overrides": self.config.overrides}

    def status(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "kinds": list(self.kinds),
            "devices": self.manager.status(),
            "capture": self.capture.status(),
            "solver": self.solver.status(),
            "focus": self.focuser.status(),
        }

    def close(self) -> None:
        self.manager.disconnect_all()


class RigSet:
    """Every telescope, kept in step with the stored definitions."""

    def __init__(self, config: Config, store: EquipmentStore,
                 library: Any = None, overheads: Any = None) -> None:
        self.config = config
        self.store = store
        self.library = library
        self.overheads = overheads
        self.events: deque[dict[str, Any]] = deque(maxlen=600)
        self._alpaca_cache: list[dict[str, Any]] = []
        self._rigs: list[Rig] = []
        self._lock = threading.RLock()
        self.sync()

    # -- keeping up with the definitions -----------------------------------
    def sync(self) -> None:
        """Create, drop and relabel rigs so the runtime matches the store.

        Called after every change to the telescope list.  A rig that is still
        there keeps its object, and therefore its connections: renaming the
        second scope must not disconnect its camera.
        """
        definitions = self.store.rigs()
        wanted = {d["id"]: d for d in definitions}
        with self._lock:
            existing = {rig.id: rig for rig in self._rigs}
            for rig_id, rig in list(existing.items()):
                if rig_id not in wanted:
                    rig.close()
                    existing.pop(rig_id)

            ordered: list[Rig] = []
            multiple = len(definitions) > 1
            for definition in definitions:
                rig = existing.get(definition["id"])
                if rig is None:
                    rig = Rig(definition, self.config, self.store, self.events,
                              self._alpaca_cache, self.library, self.overheads)
                rig.adopt(definition, multiple)
                ordered.append(rig)
            self._rigs = ordered

    # -- access ------------------------------------------------------------
    @property
    def all(self) -> list[Rig]:
        with self._lock:
            return list(self._rigs)

    @property
    def master(self) -> Rig:
        with self._lock:
            for rig in self._rigs:
                if rig.is_master:
                    return rig
            return self._rigs[0]

    @property
    def slaves(self) -> list[Rig]:
        return [rig for rig in self.all if not rig.is_master]

    def get(self, rig_id: str | None) -> Rig:
        """The named telescope, or the master when nothing is named."""
        if not rig_id:
            return self.master
        with self._lock:
            for rig in self._rigs:
                if rig.id == rig_id:
                    return rig
        raise DeviceError(f"unknown telescope {rig_id!r}")

    def imaging(self) -> list[Rig]:
        """The telescopes that can actually take a frame right now."""
        return [rig for rig in self.all
                if (camera := rig.manager.get("camera")) is not None and camera.connected]

    # -- shared services ---------------------------------------------------
    def log(self, message: str, level: str = "info") -> None:
        self.master.manager.log(message, level)

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self.events)[:limit]

    def scan_alpaca(self, host: str | None = None, port: int | None = None) -> int:
        """One scan serves every telescope; they share the device cache."""
        return self.master.manager.scan_alpaca(host, port)

    def disconnect_all(self) -> None:
        for rig in self.all:
            rig.manager.disconnect_all()

    # -- connecting from a stored definition -------------------------------
    def connect_remembered(self, rig: Rig, kinds: tuple[str, ...] | None = None
                           ) -> dict[str, list[str]]:
        """Connect the drivers a rig has remembered, and report what happened.

        Failures are collected rather than raised: half a rig connected is a
        useful state to be in at the start of a night, and the operator needs to
        see which half.
        """
        remembered = self.store.rig(rig.id)["devices"]
        connected: list[str] = []
        failed: list[str] = []
        for kind in (kinds or rig.kinds):
            spec = remembered.get(kind)
            if spec is None:
                continue
            device = rig.manager.get(kind)
            if device is not None and device.connected:
                continue
            try:
                rig.manager.connect(kind, spec["backend"], spec["driverId"],
                                    spec.get("name"), spec.get("options"))
                connected.append(kind)
            except Exception as exc:              # noqa: BLE001 - reported, not raised
                failed.append(f"{kind}: {exc}")
        return {"connected": connected, "failed": failed}

    # -- reporting ---------------------------------------------------------
    def status(self) -> list[dict[str, Any]]:
        return [rig.status() for rig in self.all]
