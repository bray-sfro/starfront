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

import contextlib
import re
import threading
import time
from collections import deque
from typing import Any, Callable

from .capture import CaptureService
from .config import Config, data_root
from .devices.base import DeviceError
from .devices.manager import KIND_LABELS, DeviceManager
from .equipment import (MASTER_DEVICE_KINDS, RIG_DEVICE_KINDS, RIG_SECTIONS,
                        EquipmentStore, safe_name)
from .camerahealth import CameraHealth
from .focushealth import FocuserHealth
from .focusing import AutoFocuser
from .pipeline import StackPipeline
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


def _norm(text: Any) -> str:
    """"RASA-1", "rasa1" and "Rasa 1" are the same outlet label."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _match_outlets(available: list[dict[str, Any]], names: list[str]
                   ) -> list[dict[str, Any]]:
    """The channels on the box that a list of outlet names picks out.

    A name is taken exactly when the box has it exactly — "RASA1" and
    "rasa1" are two different ports on the user's box — and loosely
    ("RASA-1") only when nothing matches exactly.  "index:4" names a port by
    its position, for a box whose ports are not labelled.
    """
    chosen: list[dict[str, Any]] = []
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        if name.lower().startswith("index:") and name[6:].isdigit():
            hits = [c for c in available if c.get("index") == int(name[6:])]
        else:
            hits = [c for c in available if str(c.get("name")) == name]
            if not hits:
                hits = [c for c in available if _norm(c.get("name")) == _norm(name)]
        chosen.extend(c for c in hits if c not in chosen)
    chosen.sort(key=lambda c: c.get("index", 0))
    return chosen


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
        self.capture.role = self.role
        self.capture.rig_key = self.id
        self.capture.upload_collab = self._upload_collab_setting()
        # One library serves every telescope — the masters are told apart by
        # what is in their headers, not by which folder they sit in.
        self.capture.library = library
        # One record of what this observatory costs between exposures, shared by
        # every telescope: they download over the same bus and slew on the same
        # mount, and one set of measurements is what makes it a measurement
        # rather than a guess per scope.
        self.capture.overheads = overheads
        self.overheads = overheads
        # Tonight's picture, built a sub at a time. One per telescope, in a
        # folder of its own: two cameras on one mount see two fields.
        self.stacker = StackPipeline(
            self.config, root=data_root() / "livestacks" / safe_name(self.id),
            log=self.manager.log)
        self.capture.stacker = self.stacker
        self.solver = Solver(self.manager, self.capture, self.config)
        self.focuser = AutoFocuser(self.manager, self.capture, self.config)
        self.focuser.overheads = overheads
        # A focuser that has stopped answering is reconnected - it alone -
        # rather than reported as "at the end of its travel" all night (0.2.52d).
        self.focus_health = FocuserHealth(self.name, self.manager, self._focuser_spec)
        self.focuser.health = self.focus_health
        # A camera its driver has lost (exposure limits 0 to 0 s, -273 C, no
        # answer) is reconnected - it alone - before an exposure is sent to it
        # (0.2.52e). How it is reconnected is set by the RigSet (`sync`).
        self.camera_health = CameraHealth(self.name, self.manager, None, self.capture)
        self.capture.health = self.camera_health
        #: What the cooler was last asked for - by the sequence's cool-down,
        #: the warm-down, or the Cooler button - so a camera brought back
        #: mid-run goes back to it.  None until somebody asks.
        self.cooler_wanted: dict[str, Any] | None = None
        #: Gain and offset the camera was running at when the telescope's
        #: own settings leave them blank (0.2.53): learned from the camera
        #: at the first light frame, or from the Camera panel, so a
        #: reconnect puts them back instead of the driver's 0 / 0.
        self.camera_settings_seen: dict[str, int] = {}
        self.capture.wanted_settings = self.camera_settings_wanted
        self.capture.note_settings = self.note_camera_settings

    # -- the camera's running settings -------------------------------------
    def note_camera_settings(self, gain: int | None = None, offset: int | None = None,
                             source: str = "") -> None:
        """Remember the gain/offset the camera is meant to run at this session."""
        if gain is not None:
            self.camera_settings_seen["gain"] = int(gain)
        if offset is not None:
            self.camera_settings_seen["offset"] = int(offset)
        if source and (gain is not None or offset is not None):
            self.camera_settings_seen_from = source

    def camera_settings_wanted(self) -> dict[str, Any] | None:
        """The gain and offset every light frame must be taken at.

        The telescope's own settings (Equipment > Camera: gain, offset) win,
        key by key; a blank one falls back to what this session learned from
        the camera or the panel. A collaboration task never carries a gain:
        the rig's profile is the one source. None when nothing is known.
        """
        settings = self.config.section("camera")
        seen = getattr(self, "camera_settings_seen", None) or {}
        seen_from = getattr(self, "camera_settings_seen_from", "") or "remembered"
        wanted: dict[str, Any] = {"gain": None, "offset": None, "source": {}}
        history: Any = False                       # not looked up yet
        for key in ("gain", "offset"):
            value = settings.get(key)
            if value not in (None, ""):
                wanted[key] = int(value)
                wanted["source"][key] = "profile"
                continue
            # Set by hand this session (the Camera panel, a manual capture):
            # an explicit choice, so it outranks history.
            if key in seen and seen_from != "the camera at the first light frame":
                wanted[key] = int(seen[key])
                wanted["source"][key] = seen_from
                continue
            # 0.2.53a, Bray: blank means "whatever it has been for all my other
            # images" - the mode of this camera's own past lights, not whatever
            # the driver happened to be at for the first light tonight.
            if history is False:
                history = None
                with contextlib.suppress(Exception):
                    camera = self.manager.get("camera")
                    history = self.capture.historical_settings(
                        getattr(camera, "name", "") or "")
            if history and history.get(key) is not None:
                wanted[key] = int(history[key])
                wanted["source"][key] = "history"
                wanted["history"] = history
            elif key in seen:
                wanted[key] = int(seen[key])
                wanted["source"][key] = seen_from
        if wanted["gain"] is None and wanted["offset"] is None:
            return None
        return wanted

    def note_cooler(self, on: bool | None = None, setpoint: float | None = None) -> None:
        """Remember what the cooler was asked for."""
        wanted = dict(self.cooler_wanted or {"on": False, "setpoint": None})
        if on is not None:
            wanted["on"] = bool(on)
        if setpoint is not None:
            wanted["setpoint"] = float(setpoint)
        self.cooler_wanted = wanted

    def restore_camera(self) -> list[str]:
        """Put the camera's running settings back after a reconnect.

        A reopened ZWO body comes up with its cooler off and the driver's
        default gain.  On 2026-10-19 all three cameras reconnected cleanly
        at 20:38 after a USB dropout and then shot the rest of the night at
        +18 C.  Gain and offset come from the telescope's own settings; the
        cooler goes back to whatever last asked for it.
        """
        camera = self.manager.get("camera")
        if camera is None or not camera.connected:
            return []
        done: list[str] = []
        # The profile's gain/offset, else the ones this session was running
        # at (0.2.53): on 2026-11-04 the twin FSQ had both blank, so this put
        # nothing back and the reopened ASICamera2 shot the rest of the night
        # at gain 0 / offset 0 ("Camera settings restored: cooler off").
        wanted = Rig.camera_settings_wanted(self) or {}
        gain = wanted.get("gain")
        offset = wanted.get("offset")
        if gain is not None or offset is not None:
            # Read first: a driver whose Gain could not be read the instant
            # it reopened is believed again, so the write is not skipped.
            with contextlib.suppress(Exception):
                camera.read_settings()
            camera.set_settings(gain=int(gain) if gain is not None else None,
                                offset=int(offset) if offset is not None else None)
            with contextlib.suppress(Exception):
                camera.read_settings()
            done.append(f"gain {camera.gain}, offset {camera.offset}")
        else:
            self.manager.log(f"No gain/offset set for {getattr(self, 'name', 'this telescope')} "
                             f"and none seen yet this "
                             f"session: the camera stays at its driver's gain {camera.gain} "
                             f"/ offset {camera.offset}. Set them under Equipment > Camera.",
                             "warn")
        wanted = self.cooler_wanted
        if wanted and getattr(camera, "can_cool", False):
            setpoint = wanted.get("setpoint")
            if setpoint is not None:
                camera.set_setpoint(float(setpoint))
            camera.set_cooler(bool(wanted.get("on")))
            if wanted.get("on"):
                done.append("cooler on" + (f" at {float(setpoint):g} C"
                                           if setpoint is not None else ""))
            else:
                done.append("cooler off")
        if done:
            self.manager.log("Camera settings restored: " + ", ".join(done))
        return done

    # -- the outlets that feed this telescope's gear -----------------------
    def device_key(self, kind: str) -> str:
        """How the Power page names one of this telescope's slots.

        The gear on an optical train is the telescope's own ("main:camera");
        the things there is one of on the mount are named bare ("mount"), so
        an assignment survives the master role moving to another scope.
        """
        return kind if kind in MASTER_DEVICE_KINDS else f"{self.id}:{kind}"

    def outlet_table(self) -> dict[str, str]:
        """Every outlet assignment on the Power page: outlet name -> device key."""
        table = self.config.get("power", "outlets", {}) or {}
        if not isinstance(table, dict):
            return {}
        return {str(name): str(who) for name, who in table.items() if who}

    def assigned_outlets(self, kind: str) -> list[str]:
        """The outlet names the Power page gives this slot, as typed there."""
        key = self.device_key(kind)
        return [name for name, who in self.outlet_table().items() if who == key]

    def configured_power_channels(self) -> list[str]:
        """The outlets a 0.2.46 Camera page gave this camera, and only this one's.

        Read from the telescope's own overrides rather than through the
        merged settings on purpose: a follower with nothing set must not
        inherit the master's outlets, or a failed camera on rasa2 would cut
        the power to rasa1.
        """
        if self.is_master:
            raw = self.config.get("camera", "powerChannels", []) or []
        else:
            raw = (self.config.overrides.get("camera") or {}).get("powerChannels") or []
        return [str(name).strip() for name in raw if str(name).strip()]

    def power_channels(self, switch: Any, kind: str = "camera"
                       ) -> tuple[list[dict[str, Any]], str]:
        """Which of the power box's channels feed one of this telescope's
        devices, and why.

        Returns (channels, source) where source is "assigned" when the
        outlets were given to this device on the Power page, "configured"
        when a 0.2.46 Camera page named them, "byName" when an outlet nobody
        has assigned simply carries the telescope's own name ("rasa1" on a
        port labelled rasa1 or RASA1 — taken as the camera), and "none"
        when nothing matches.
        """
        if switch is None or not getattr(switch, "connected", False):
            return [], "none"
        try:
            available = list(switch.describe_channels())
        except Exception:                         # noqa: BLE001 - a box that cannot say
            return [], "none"
        assigned = self.assigned_outlets(kind)
        if assigned:
            return _match_outlets(available, assigned), "assigned"
        if kind != "camera":
            return [], "none"
        wanted = self.configured_power_channels()
        if wanted:
            return _match_outlets(available, wanted), "configured"
        mine = _norm(self.name)
        if not mine:
            return [], "none"
        # An outlet given to something else on the Power page is not taken
        # for the camera just because it is named after the telescope.
        taken = {c.get("index") for c in _match_outlets(available, list(self.outlet_table()))}
        chosen = [c for c in available
                  if _norm(c.get("name")) == mine and c.get("index") not in taken]
        return chosen, ("byName" if chosen else "none")

    # -- identity ----------------------------------------------------------
    def _upload_collab_setting(self):
        """Explicit camera.uploadCollab, or None for the role default.

        The master uploads unless the key is set off. A follower does not,
        unless that follower's own overlay sets the key on. The shared
        default is not copied onto the follower: a missing key stays missing.
        """
        if self.is_master:
            section = self.config.section("camera")
            if "uploadCollab" in section:
                return bool(section["uploadCollab"])
            return None
        overlay = self.store.rig_settings(self.id, "camera") or {}
        if "uploadCollab" in overlay:
            return bool(overlay["uploadCollab"])
        return None

    @property
    def is_master(self) -> bool:
        return self.role == "master"

    @property
    def kinds(self) -> tuple[str, ...]:
        """The device slots this telescope actually has."""
        return (RIG_DEVICE_KINDS + MASTER_DEVICE_KINDS if self.is_master
                else RIG_DEVICE_KINDS)

    def _focuser_spec(self) -> dict[str, Any] | None:
        """The driver this telescope's focuser is reconnected with: the one
        remembered for it, or failing that the one connected now."""
        with contextlib.suppress(Exception):
            spec = self.store.rig(self.id)["devices"].get("focuser")
            if spec and spec.get("backend") and spec.get("driverId"):
                return spec
        device = self.manager.get("focuser")
        backend = getattr(self.manager, "_backends", {}).get("focuser")
        if device is not None and backend:
            return {"backend": backend, "driverId": device.driver_id,
                    "name": device.name, "options": None}
        return None

    def adopt(self, definition: dict[str, Any], multiple: bool) -> None:
        """Take on a changed name or role without dropping any connection."""
        self.name = definition["name"]
        if hasattr(self, "focus_health"):
            self.focus_health.name = self.name
        if hasattr(self, "camera_health"):
            self.camera_health.name = self.name
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
            "livestack": self.stacker.status(brief=True),
        }

    def close(self) -> None:
        self.stacker.stop()
        self.stacker.save_all()
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
        # When each camera was last brought back (by telescope id), and when
        # each device was last power-cycled (by "telescope id:kind"): the
        # second reconnect of one camera in a quarter of an hour is not a
        # glitch, it is a body that has left the bus.
        self._reconnects: dict[str, list[float]] = {}
        self._cycled_at: dict[str, float] = {}
        self._cycling: dict[str, threading.Lock] = {}
        self.sync()

    #: Reconnects of one camera closer together than this count as repeats.
    POWER_CYCLE_WINDOW_SECONDS = 15 * 60
    #: Two power cycles of one camera closer together than this are not
    #: going to help; the second falls back to a plain reconnect.
    POWER_CYCLE_MIN_GAP_SECONDS = 90.0

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
            for index, definition in enumerate(definitions, start=1):
                rig = existing.get(definition["id"])
                if rig is None:
                    rig = Rig(definition, self.config, self.store, self.events,
                              self._alpaca_cache, self.library, self.overheads)
                    # How a failed frame gets its camera back: the remembered
                    # driver, the same way Connect everything brings it up.
                    rig.capture.reconnect_camera = (
                        lambda rig=rig: self.reconnect_camera(rig))
                    rig.camera_health.reconnect = (
                        lambda stop, rig=rig: self.reconnect_camera(rig, stop=stop))
                rig.adopt(definition, multiple)
                rig.capture.rig_index = index
                rig.capture.rig_count = len(definitions)
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

    #: One camera reconnect at a time, across every telescope (one RigSet per
    #: program, so a class-wide lock). The ZWO SDK numbers the cameras it
    #: finds when it is asked to open one; three telescopes reconnecting at
    #: once (2026-10-12 20:59, 10-14 20:40, 10-19 23:58) had one open land
    #: while another was enumerating, and that camera came back
    #: "ASIOpenCamera failed (invalid id)".
    _reconnect_lock = threading.RLock()

    #: An open that failed with a stale camera id is tried once more after
    #: this long: the enumeration it raced has finished by then.
    INVALID_ID_RETRY_SECONDS = 3.0

    def reconnect_camera(self, rig: Rig, stop: Callable[[], bool] | None = None,
                         progress: Callable[[str], None] | None = None
                         ) -> dict[str, list[str]]:
        """One camera reconnect at a time; see `_reconnect_camera`."""
        with self._reconnect_lock:
            outcome = self._reconnect_camera(rig, stop, progress)
            if ("camera" not in outcome["connected"]
                    and any("invalid id" in str(m).lower() for m in outcome["failed"])
                    and not (stop is not None and stop())):
                time.sleep(self.INVALID_ID_RETRY_SECONDS)
                again = self._plain_reconnect(rig)
                if "camera" in again["connected"]:
                    return again
                again["failed"] = outcome["failed"] + again["failed"]
                return again
            return outcome

    def _reconnect_camera(self, rig: Rig, stop: Callable[[], bool] | None = None,
                          progress: Callable[[str], None] | None = None
                          ) -> dict[str, list[str]]:
        """Bring a camera back mid-run, with the settings it was running at.

        The remembered driver, the same way Connect everything brings it up,
        and then `Rig.restore_camera` for the gain, offset and cooler a
        reopened body has forgotten.

        When that is not enough — the driver cannot open a body the PC no
        longer sees, or the same camera is back for its second rescue inside
        a quarter of an hour — and a power box has outlets mapped to this
        camera, those outlets are switched off and on first (Equipment >
        Recovery).  `stop` is asked between waits so a Stop press is not
        held up by a boot; `progress` is told what is being waited for.
        """
        settings = self.config.section("recovery")
        now = time.time()
        recent = [t for t in self._reconnects.get(rig.id, [])
                  if now - t < self.POWER_CYCLE_WINDOW_SECONDS]
        self._reconnects[rig.id] = recent + [now]

        cycle_wanted = (bool(settings.get("enabled", True))
                        and bool(settings.get("powerCycleCameras", True)))
        channels, _source = rig.power_channels(self.master.manager.get("switch")) \
            if cycle_wanted else ([], "none")
        too_soon = (now - self._cycled_at.get(self._slot(rig, "camera"), 0.0)
                    < self.POWER_CYCLE_MIN_GAP_SECONDS)
        threshold = int(settings.get("powerCycleAfterReconnects", 1) or 0)
        # Straight to the mains when this camera has already had its plain
        # reconnect(s) recently: the last time it "came back" it failed the
        # next frame, and the night is running out while that repeats.
        repeat = len(recent) >= threshold
        if channels and not too_soon and repeat:
            self._power_cycle(rig, channels, "it has been reconnected "
                              f"{len(recent)} time(s) in the last "
                              f"{self.POWER_CYCLE_WINDOW_SECONDS // 60} minutes",
                              stop, progress)
            return self._plain_reconnect(rig)

        outcome = self._plain_reconnect(rig)
        if "camera" in outcome["connected"] or not channels or too_soon:
            return outcome
        if stop is not None and stop():
            return outcome
        first = outcome["failed"][0] if outcome["failed"] else "it did not come back"
        self._power_cycle(rig, channels, f"the reconnect failed ({first})", stop, progress)
        again = self._plain_reconnect(rig)
        # Both attempts' failures are worth reading in the morning.
        again["failed"] = outcome["failed"] + again["failed"] if again["failed"] else []
        return again

    def _plain_reconnect(self, rig: Rig) -> dict[str, list[str]]:
        outcome = self.connect_remembered(rig, ("camera",))
        if "camera" in outcome["connected"]:
            try:
                rig.restore_camera()
            except Exception as exc:              # noqa: BLE001 - reported, not raised
                outcome["failed"].append(f"camera settings: {exc}")
        return outcome

    # -- power-cycling a device --------------------------------------------
    @staticmethod
    def _slot(rig: Rig, kind: str) -> str:
        return f"{rig.id}:{kind}"

    def power_mapping(self, rig: Rig) -> dict[str, Any]:
        """Which outlets a power cycle of each of this telescope's devices
        would switch, for the UI.

        The camera's mapping is also spread at the top level — it is the one
        recovery acts on by itself, and the Camera page reads it there.
        """
        switch = self.master.manager.get("switch")
        devices: dict[str, dict[str, Any]] = {}
        for kind in rig.kinds:
            if kind == "switch":
                continue
            channels, source = rig.power_channels(switch, kind)
            devices[kind] = {
                "source": source,
                "channels": [{"index": c.get("index"), "name": c.get("name"),
                              "group": c.get("group", "")} for c in channels],
                "lastCycled": self._cycled_at.get(self._slot(rig, kind)),
            }
        camera = devices.get("camera") or {"source": "none", "channels": [],
                                           "lastCycled": None}
        return {
            "available": switch is not None and bool(getattr(switch, "connected", False)),
            **camera,
            "configured": rig.configured_power_channels(),
            "devices": devices,
        }

    def power_cycle_camera(self, rig: Rig, reason: str = "asked for by hand",
                           stop: Callable[[], bool] | None = None,
                           progress: Callable[[str], None] | None = None
                           ) -> dict[str, list[str]]:
        """Cut the camera's outlets, restore them, and reconnect the camera."""
        return self.power_cycle_device(rig, "camera", reason, stop, progress)

    def power_cycle_device(self, rig: Rig, kind: str, reason: str = "asked for by hand",
                           stop: Callable[[], bool] | None = None,
                           progress: Callable[[str], None] | None = None
                           ) -> dict[str, list[str]]:
        """Cut one device's outlets, restore them, and reconnect the device.

        The hand version: no policy, no window, just the cycle and the
        reconnect.  Raises when there is nothing assigned to switch.
        """
        label = KIND_LABELS.get(kind, kind).lower()
        if kind == "switch" or kind not in rig.kinds:
            raise DeviceError(f"{rig.name} has no {label} slot to power-cycle")
        switch = self.master.manager.get("switch")
        if switch is None or not getattr(switch, "connected", False):
            raise DeviceError("no power box is connected")
        channels, _source = rig.power_channels(switch, kind)
        if not channels:
            raise DeviceError(f"no outlet on the power box is assigned to {rig.name}'s "
                              f"{label} — assign its outlets under Equipment > Power")
        self._power_cycle(rig, channels, reason, stop, progress, kind)
        if kind == "camera":
            return self._plain_reconnect(rig)
        return self.connect_remembered(rig, (kind,))

    def _power_cycle(self, rig: Rig, channels: list[dict[str, Any]], reason: str,
                     stop: Callable[[], bool] | None,
                     progress: Callable[[str], None] | None,
                     kind: str = "camera") -> None:
        """Off, wait, on, wait — with every outlet put back whatever happens."""
        switch = self.master.manager.get("switch")
        if switch is None or not getattr(switch, "connected", False):
            raise DeviceError("no power box is connected")
        label = KIND_LABELS.get(kind, kind).lower()
        lock = self._cycling.setdefault(self._slot(rig, kind), threading.Lock())
        if not lock.acquire(blocking=False):
            raise DeviceError(f"{rig.name}'s {label} is already being power-cycled")
        settings = self.config.section("recovery")
        off_for = max(1.0, float(settings.get("powerCycleOffSeconds") or 10.0))
        boot_for = max(0.0, float(settings.get("powerCycleBootSeconds") or 25.0))
        names = ", ".join(str(c.get("name")) for c in channels)

        def say(text: str) -> None:
            if progress is not None:
                with contextlib.suppress(Exception):
                    progress(text)

        try:
            self._cycled_at[self._slot(rig, kind)] = time.time()
            rig.manager.log(f"Power-cycling the {label} because {reason}: "
                            f"{names} off for {off_for:g}s, then {boot_for:g}s to boot", "warn")
            # What each outlet was at, so a dew heater at 40 % goes back to
            # 40 % rather than to full.
            before: dict[int, float] = {}
            with contextlib.suppress(Exception):
                for live in switch.channels:
                    if live.get("value") is not None:
                        before[int(live["index"])] = float(live["value"])
            # The driver is closed first: a body that loses power under an
            # open handle leaves the driver wedged on the old handle.
            if kind == "camera":
                with contextlib.suppress(Exception):
                    if getattr(rig.capture, "busy", False):
                        rig.capture.abort()
            with contextlib.suppress(Exception):
                rig.manager.disconnect(kind)
            cut: list[dict[str, Any]] = []
            try:
                say(f"{rig.name}: switching {names} off")
                for channel in channels:
                    switch.set_value(int(channel["index"]), 0.0)
                    cut.append(channel)
                self._wait(off_for, stop)
            finally:
                # Back on even if the wait was interrupted or an outlet
                # refused: the one thing worse than a dead camera is one
                # left without power by the thing meant to rescue it.
                say(f"{rig.name}: switching {names} back on")
                trouble: list[str] = []
                for channel in cut:
                    index = int(channel["index"])
                    restore = before.get(index)
                    if restore is None or restore <= 0 or channel.get("boolean", True):
                        restore = 1.0 if channel.get("boolean", True) else float(
                            channel.get("max") or 1.0)
                    try:
                        switch.set_value(index, restore)
                    except Exception as exc:      # noqa: BLE001 - collected, raised below
                        trouble.append(f"{channel.get('name')}: {exc}")
                if trouble:
                    rig.manager.log("Could not restore power to " + "; ".join(trouble),
                                    "error")
                    raise DeviceError("could not restore power to " + "; ".join(trouble))
            if stop is not None and stop():
                rig.manager.log("Power restored; the boot wait was skipped because the "
                                "run is stopping", "warn")
                return
            say(f"{rig.name}: waiting {boot_for:g}s for the {label} to boot")
            self._wait(boot_for, stop)
            rig.manager.log(f"Power cycled: {names} back on", "success")
        finally:
            lock.release()

    @staticmethod
    def _wait(seconds: float, stop: Callable[[], bool] | None) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if stop is not None and stop():
                return
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    # -- reporting ---------------------------------------------------------
    def status(self) -> list[dict[str, Any]]:
        out = []
        for rig in self.all:
            entry = rig.status()
            # Which outlets would be cycled for this camera: shown on the
            # Camera page and the rig card, decided here because only the
            # set knows the master's power box.
            with contextlib.suppress(Exception):
                entry["power"] = self.power_mapping(rig)
            out.append(entry)
        return out
