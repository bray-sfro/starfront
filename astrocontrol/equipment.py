"""Telescopes and equipment profiles.

Two related things live in one file because they are the same data seen from
two angles.

**Telescopes.**  A rig is one optical train: a camera, and whatever filter
wheel, focuser and rotator sit in front of it.  One rig is the *master*: it owns
the mount and the guider, and it is the one that decides where everything is
pointed and when everything dithers.  Every other rig is a *slave* bolted to the
same mount, so it inherits the pointing it is given and takes its frames in step
with the master.

Each rig also carries the settings that belong to its own optics rather than to
the site — focal length, sensor, gain, cooling, focus step size.  Those are
stored as an overlay on the shared settings: a key set on a rig wins, anything
it does not mention falls through to the shared value.  The master keeps no
overlay at all, so a single-telescope rig behaves exactly as it did before any
of this existed.

**Profiles.**  A profile is a named snapshot of all of that — every telescope,
which driver each of its slots connects to, and the settings around them.  Rigs
get rebuilt: the refractor comes off for a season of narrowband on the RASA, and
in March it goes back on.  A profile means that is a two-click job rather than
an evening of remembering what the gain was.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import data_root
from .devices.base import DeviceError

# Settings sections a telescope may hold its own values for.  Everything else
# (the site, the solver, the schedule, dithering) describes the observatory
# rather than one optical train, and stays shared.
RIG_SECTIONS = ("optics", "camera", "sequencer")

# Device slots that belong to one optical train.
RIG_DEVICE_KINDS = ("camera", "filterwheel", "focuser", "rotator", "flatpanel")

# Device slots there is only ever one of, however many telescopes are riding on
# it.  These live on the master.
MASTER_DEVICE_KINDS = ("mount", "guider", "piercam", "safetymonitor", "dome",
                       "switch")

MAX_RIGS = 4
MAX_PROFILES = 40


def _clean(text: str, limit: int = 60) -> str:
    return " ".join(str(text or "").split())[:limit]


def safe_name(text: str) -> str:
    """A rig name reduced to something safe to use as a folder name."""
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", (text or "").strip()).strip("_")


def default_rig(name: str = "Telescope 1") -> dict[str, Any]:
    return {
        "id": "main",
        "name": name,
        "role": "master",
        "devices": {},
        "settings": {},
    }


#: How many of a driver's own settings a telescope may pin.  This is for
#: naming a device, not for configuring a driver from here, so it is small on
#: purpose: a handful of values, not a copy of the driver's whole profile.
MAX_DRIVER_OPTIONS = 12


def _clean_driver_options(raw: Any) -> dict[str, str]:
    """The driver settings a telescope pins, tidied.

    These belong to the driver, not to us — they are written into its ASCOM
    profile just before it is opened, which is how three telescopes sharing one
    driver each get the device they meant.
    """
    if not isinstance(raw, dict):
        return {}
    options: dict[str, str] = {}
    for name, value in raw.items():
        key = _clean(str(name), 80)
        if key and len(options) < MAX_DRIVER_OPTIONS:
            options[key] = _clean(str(value), 200)
    return options


def _clean_device_spec(spec: Any) -> dict[str, Any] | None:
    """Normalise a remembered driver choice, or None if it is unusable."""
    if not isinstance(spec, dict):
        return None
    backend = _clean(spec.get("backend", ""), 20)
    driver_id = _clean(spec.get("driverId", ""), 200)
    if not backend or not driver_id:
        return None
    return {"backend": backend, "driverId": driver_id,
            "name": _clean(spec.get("name", "") or driver_id, 120),
            "options": _clean_driver_options(spec.get("options"))}


def _canonical_filters(section: str, values: dict[str, Any]) -> dict[str, Any]:
    """Filter names in a telescope's own settings, in the one spelling."""
    from .filters import fold_settings
    return fold_settings(section, values)


def _clean_rig(raw: Any, fallback_index: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    rig_id = _clean(raw.get("id", ""), 32) or f"rig{fallback_index}"
    devices = {}
    for kind, spec in (raw.get("devices") or {}).items():
        if kind in RIG_DEVICE_KINDS or kind in MASTER_DEVICE_KINDS:
            cleaned = _clean_device_spec(spec)
            if cleaned is not None:
                devices[kind] = cleaned
    settings = {}
    for section, values in (raw.get("settings") or {}).items():
        if section in RIG_SECTIONS and isinstance(values, dict):
            settings[section] = _canonical_filters(section, dict(values))
    return {
        "id": rig_id,
        "name": _clean(raw.get("name", "")) or f"Telescope {fallback_index}",
        "role": "master" if raw.get("role") == "master" else "slave",
        "devices": devices,
        "settings": settings,
    }


class EquipmentStore:
    """The telescope list and the saved profiles, written through on change."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_root() / "equipment.json")
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {
            "rigs": [default_rig()],
            "profiles": [],
            "activeProfile": None,
            "activeProfileName": "",
        }
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(stored, dict):
            return

        rigs = []
        for index, raw in enumerate(stored.get("rigs") or [], start=1):
            cleaned = _clean_rig(raw, index)
            if cleaned is not None and not any(r["id"] == cleaned["id"] for r in rigs):
                rigs.append(cleaned)
        profiles = [p for p in (stored.get("profiles") or [])
                    if isinstance(p, dict) and p.get("id")]

        with self._lock:
            if rigs:
                self._data["rigs"] = _with_one_master(rigs)
            self._data["profiles"] = profiles[:MAX_PROFILES]
            self._data["activeProfile"] = stored.get("activeProfile") or None
            self._data["activeProfileName"] = _clean(stored.get("activeProfileName", ""))

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self._data, indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(payload, "utf-8")
        except OSError:
            pass                                # a read-only home is not fatal

    # -- telescopes --------------------------------------------------------
    def rigs(self) -> list[dict[str, Any]]:
        """Every telescope, master first."""
        with self._lock:
            return copy.deepcopy(self._data["rigs"])

    def rig(self, rig_id: str) -> dict[str, Any]:
        with self._lock:
            for rig in self._data["rigs"]:
                if rig["id"] == rig_id:
                    return copy.deepcopy(rig)
        raise DeviceError(f"unknown telescope {rig_id!r}")

    def master_id(self) -> str:
        with self._lock:
            for rig in self._data["rigs"]:
                if rig["role"] == "master":
                    return rig["id"]
            return self._data["rigs"][0]["id"]

    def add_rig(self, name: str | None = None) -> dict[str, Any]:
        with self._lock:
            if len(self._data["rigs"]) >= MAX_RIGS:
                raise DeviceError(f"there is room for {MAX_RIGS} telescopes")
            index = len(self._data["rigs"]) + 1
            rig = {
                "id": uuid.uuid4().hex[:8],
                "name": _clean(name) or f"Telescope {index}",
                "role": "slave",
                "devices": {},
                "settings": {},
            }
            self._data["rigs"].append(rig)
            result = copy.deepcopy(rig)
        self.save()
        return result

    def remove_rig(self, rig_id: str) -> None:
        with self._lock:
            rigs = self._data["rigs"]
            found = next((r for r in rigs if r["id"] == rig_id), None)
            if found is None:
                raise DeviceError(f"unknown telescope {rig_id!r}")
            if found["role"] == "master":
                raise DeviceError(
                    "the master telescope cannot be removed; make another one the "
                    "master first")
            self._data["rigs"] = [r for r in rigs if r["id"] != rig_id]
        self.save()

    def rename_rig(self, rig_id: str, name: str) -> dict[str, Any]:
        with self._lock:
            rig = self._find(rig_id)
            rig["name"] = _clean(name) or rig["name"]
            result = copy.deepcopy(rig)
        self.save()
        return result

    def set_master(self, rig_id: str) -> list[dict[str, Any]]:
        """Hand the mount and the guider to a different telescope.

        The slots that only exist on the master move with the role, so promoting
        the second scope does not lose which mount driver was remembered.
        """
        with self._lock:
            rigs = self._data["rigs"]
            target = self._find(rig_id)
            if target["role"] == "master":
                return copy.deepcopy(rigs)
            old = next((r for r in rigs if r["role"] == "master"), None)
            if old is not None:
                for kind in MASTER_DEVICE_KINDS:
                    spec = old["devices"].pop(kind, None)
                    if spec is not None:
                        target["devices"][kind] = spec
                old["role"] = "slave"
            target["role"] = "master"
            self._data["rigs"] = _with_one_master(rigs)
            result = copy.deepcopy(self._data["rigs"])
        self.save()
        return result

    def set_device(self, rig_id: str, kind: str,
                   spec: dict[str, Any] | None) -> dict[str, Any]:
        """Remember (or forget) which driver a slot connects to.

        This is what a profile restores, so it is written whenever a device is
        connected by hand rather than only when a profile is saved.
        """
        with self._lock:
            rig = self._find(rig_id)
            if spec is None:
                rig["devices"].pop(kind, None)
            else:
                cleaned = _clean_device_spec(spec)
                if cleaned is None:
                    raise DeviceError("a driver needs a backend and an id")
                rig["devices"][kind] = cleaned
            result = copy.deepcopy(rig)
        self.save()
        return result

    # -- per-telescope settings -------------------------------------------
    def rig_settings(self, rig_id: str, section: str) -> dict[str, Any]:
        """The keys this telescope overrides in one settings section."""
        if section not in RIG_SECTIONS:
            return {}
        with self._lock:
            for rig in self._data["rigs"]:
                if rig["id"] == rig_id:
                    return dict(rig["settings"].get(section) or {})
        return {}

    def update_rig_settings(self, rig_id: str, section: str,
                            values: dict[str, Any]) -> dict[str, Any]:
        """Override some keys for this telescope.  A None value drops the
        override, so a slave can be put back onto the shared value."""
        if section not in RIG_SECTIONS:
            raise DeviceError(f"{section!r} is shared by every telescope")
        values = _canonical_filters(section, dict(values))
        with self._lock:
            rig = self._find(rig_id)
            overlay = rig["settings"].setdefault(section, {})
            for key, value in values.items():
                if value is None:
                    overlay.pop(key, None)
                else:
                    overlay[key] = value
            if not overlay:
                rig["settings"].pop(section, None)
            result = dict(rig["settings"].get(section) or {})
        self.save()
        return result

    # -- profiles ----------------------------------------------------------
    def profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"id": p["id"], "name": p.get("name", ""),
                     "created": p.get("created"), "updated": p.get("updated"),
                     "telescopes": [r.get("name") for r in (p.get("rigs") or [])]}
                    for p in self._data["profiles"]]

    def profile(self, profile_id: str) -> dict[str, Any]:
        with self._lock:
            for stored in self._data["profiles"]:
                if stored["id"] == profile_id:
                    return copy.deepcopy(stored)
        raise DeviceError(f"unknown profile {profile_id!r}")

    def active(self) -> dict[str, Any]:
        with self._lock:
            return {"id": self._data.get("activeProfile"),
                    "name": self._data.get("activeProfileName") or ""}

    def save_profile(self, name: str, shared: dict[str, dict[str, Any]],
                     profile_id: str | None = None) -> dict[str, Any]:
        """Snapshot the current telescopes and the shared settings under a name.

        Saving over a name that already exists replaces it: the usual reason to
        save a profile twice is that something about the rig changed.
        """
        name = _clean(name)
        if not name:
            raise DeviceError("a profile needs a name")
        with self._lock:
            existing = None
            if profile_id:
                existing = next((p for p in self._data["profiles"]
                                 if p["id"] == profile_id), None)
                if existing is None:
                    raise DeviceError(f"unknown profile {profile_id!r}")
            else:
                existing = next((p for p in self._data["profiles"]
                                 if p.get("name", "").lower() == name.lower()), None)

            payload = {
                "id": existing["id"] if existing else uuid.uuid4().hex[:10],
                "name": name,
                "created": existing.get("created") if existing else time.time(),
                "updated": time.time(),
                "rigs": copy.deepcopy(self._data["rigs"]),
                "shared": copy.deepcopy(shared),
            }
            if existing:
                self._data["profiles"] = [payload if p["id"] == payload["id"] else p
                                          for p in self._data["profiles"]]
            else:
                if len(self._data["profiles"]) >= MAX_PROFILES:
                    raise DeviceError(f"there is room for {MAX_PROFILES} profiles")
                self._data["profiles"].insert(0, payload)
            self._data["activeProfile"] = payload["id"]
            self._data["activeProfileName"] = name
            result = copy.deepcopy(payload)
        self.save()
        return result

    def adopt_profile(self, profile_id: str) -> dict[str, Any]:
        """Make a profile's telescopes the live ones.

        The shared settings come back to the caller rather than being applied
        here: they belong to `Config`, which this store knows nothing about.
        """
        stored = self.profile(profile_id)
        rigs = []
        for index, raw in enumerate(stored.get("rigs") or [], start=1):
            cleaned = _clean_rig(raw, index)
            if cleaned is not None and not any(r["id"] == cleaned["id"] for r in rigs):
                rigs.append(cleaned)
        if not rigs:
            raise DeviceError(f"{stored.get('name')!r} has no telescopes in it")
        with self._lock:
            self._data["rigs"] = _with_one_master(rigs)
            self._data["activeProfile"] = stored["id"]
            self._data["activeProfileName"] = stored.get("name", "")
        self.save()
        return stored

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            before = len(self._data["profiles"])
            self._data["profiles"] = [p for p in self._data["profiles"]
                                      if p["id"] != profile_id]
            if len(self._data["profiles"]) == before:
                raise DeviceError(f"unknown profile {profile_id!r}")
            if self._data.get("activeProfile") == profile_id:
                self._data["activeProfile"] = None
                self._data["activeProfileName"] = ""
        self.save()

    # -- internals ---------------------------------------------------------
    def _find(self, rig_id: str) -> dict[str, Any]:
        """The live dict for a rig.  Callers must hold the lock."""
        for rig in self._data["rigs"]:
            if rig["id"] == rig_id:
                return rig
        raise DeviceError(f"unknown telescope {rig_id!r}")


def _with_one_master(rigs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exactly one master, and it sorts first.

    A file edited by hand, or a profile saved by an older version, can arrive
    with none or several.  Rather than refuse to start, the first one wins.
    """
    masters = [r for r in rigs if r["role"] == "master"]
    if len(masters) != 1:
        for rig in rigs:
            rig["role"] = "slave"
        (masters[0] if masters else rigs[0])["role"] = "master"
    # A master that holds no mount slot is fine; a slave holding one is not.
    for rig in rigs:
        if rig["role"] == "slave":
            for kind in MASTER_DEVICE_KINDS:
                rig["devices"].pop(kind, None)
    return sorted(rigs, key=lambda r: 0 if r["role"] == "master" else 1)
