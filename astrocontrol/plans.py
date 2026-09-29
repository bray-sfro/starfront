"""The imaging plan: what to shoot tonight, and how much of it.

A plan is a list of targets taken from the target list, each with a set of
filters and how many frames to take through them.  It lives in one file that is
written on every change and read back at startup, so closing the program and
opening it again lands you exactly where you were.

The one rule the plan enforces is that you cannot ask for more than the night
will give you.  Every count is checked against the target's own observable
window, and for a mosaic against that window divided among its tiles — asking
for twenty frames a panel on a four-panel mosaic really is eighty frames, and
the plan says so before the night is wasted rather than after.

With more than one telescope on the mount, an entry carries an allocation per
telescope: the master might want twenty five-minute luminance frames while the
faster scope alongside it takes ten ten-minute Ha frames of the same field.
They are still shot in step — the sequencer starts a frame on every telescope
together and waits for the slowest before it dithers — so the night costs
whatever the *slowest* telescope's allocation costs, not the sum of them.  A
telescope with no allocation of its own mirrors the master's, which is what a
second scope carrying the same filters usually wants.
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import schedule
from .config import data_root
from .devices.base import DeviceError

MAX_ENTRIES = 60

#: How an entry's filters are walked.  `grouped` shoots all the
#: luminance and then all the red, which is the fewest filter changes; `rotate`
#: cycles L,R,G,B,L,R,G,B, which costs a change per frame and means a session cut
#: short by cloud is still colour-balanced rather than all luminance.
FILTER_ORDERS = ("grouped", "rotate")

#: Per-target settings the sequencer honours.  Defaults chosen so that an entry
#: with no options at all behaves exactly as entries did before they existed.
ENTRY_OPTIONS: dict[str, Any] = {
    # Off without being removed: weather, a mount that will not reach it, or a
    # target you want to keep in the plan for tomorrow.
    "enabled": True,
    # Which targets get the good hours when the plan is cycled, and which order
    # auto-arrange prefers. Higher wins; equal priorities keep list order.
    "priority": 5,
    # This target's own floor, where it differs from the observatory's — a
    # bright planetary nebula is worth shooting at 25 degrees and a faint galaxy
    # is not. 0 means "use the site's".
    "minAltitude": 0.0,
    # Skip while the Moon is within this many degrees of it. 0 turns it off.
    # Narrowband barely cares and broadband cares enormously, which is why this
    # belongs to the target rather than to the night.
    "moonAvoidance": 0.0,
    "filterOrder": "grouped",
    # Sweep the focuser when this target starts, whatever the clock and
    # temperature triggers think. On by default: a target begins with a long
    # slew across the sky, usually into different air and often onto the other
    # side of the pier, and the first frames of a target are the ones most
    # likely to be thrown away for being soft.
    "focusOnStart": True,
    # This target's dither interval, where it differs from the shared one.
    # 0 means "use the shared one".
    "ditherEveryFrames": 0,
    # Stop shooting this target once its *lifetime* integration reaches this
    # many hours. 0 means no goal. This is what makes a plan you can leave in
    # place for a season: the target drops out of the rotation when it is done.
    "goalHours": 0.0,
    # Which panels of a mosaic to shoot, by index. Empty means all of them,
    # which is the normal case and what every target that is not a mosaic uses.
    #
    # For going back over a mosaic: one panel came out under cloud, or with a
    # satellite through it, or it simply wants another hour. Pick the panels,
    # set the exposure and counts the way you would for any target, and the run
    # shoots only those — everything else about the target stays as it is, so
    # nothing has to be un-set afterwards except the selection itself.
    "panels": [],
    # A collaboration target on a rig with a rotator: turn the camera to the
    # project's angle (its panels then lie along the project's grid, a single
    # target is framed as it was framed), or leave the camera where it sits.
    # Meaningless without a rotator, and ignored.
    "collabMatchRotation": True,
}


def _clean(text: str, limit: int = 120) -> str:
    return " ".join(str(text or "").split())[:limit]


def options_for(entry: dict[str, Any]) -> dict[str, Any]:
    """An entry's sequence options, with every default filled in.

    Entries written before options existed have none, and every default is the
    old behaviour — so the absence of the field is the answer rather than a
    migration.
    """
    stored = entry.get("options") or {}
    merged = dict(ENTRY_OPTIONS)
    if isinstance(stored, dict):
        merged.update({k: v for k, v in stored.items() if k in ENTRY_OPTIONS})
    return merged


def kind_of(entry: dict[str, Any]) -> str:
    """What sort of task a plan entry is.

    Entries written before calibration tasks existed have no `kind` at all, and
    they are all targets — so the absence of the field is the answer rather than
    a migration.
    """
    return str(entry.get("kind") or "target")


def is_calibration(entry: dict[str, Any]) -> bool:
    return kind_of(entry) == "calibration"


class PlanStore:
    """The imaging plan on disk, written through on every change."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_root() / "plan.json")
        self._lock = threading.RLock()
        self._plan: dict[str, Any] = self._empty()
        self.load()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"name": "Tonight", "date": None, "entries": [],
                "updated": time.time()}

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(stored, dict):
            return
        entries = [e for e in stored.get("entries", [])
                   if isinstance(e, dict) and e.get("id")]
        # Filter names in the plan, in the program's one spelling: a plan
        # written when the wheel said "Ha" has to ask for "H" now.
        from .filters import canonical
        for entry in entries:
            for row in entry.get("filters") or []:
                if isinstance(row, dict) and row.get("name"):
                    row["name"] = canonical(row["name"]) or row["name"]
            for rows in (entry.get("rigFilters") or {}).values():
                for row in rows or []:
                    if isinstance(row, dict) and row.get("name"):
                        row["name"] = canonical(row["name"]) or row["name"]
        with self._lock:
            self._plan = {
                "name": _clean(stored.get("name") or "Tonight"),
                "date": stored.get("date") or None,
                "entries": entries,
                "updated": stored.get("updated") or time.time(),
            }

    def save(self) -> None:
        with self._lock:
            self._plan["updated"] = time.time()
            payload = json.dumps(self._plan, indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(payload, "utf-8")
        except OSError:
            pass

    def raw(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._plan))

    # -- editing -----------------------------------------------------------
    def replace_entries(self, entries: list[dict[str, Any]],
                        name: str | None = None) -> dict[str, Any]:
        """Put a saved sequence in place of whatever is planned now.

        Fresh ids, because the same sequence may be loaded twice in a season and
        two entries sharing an id would be one entry as far as every lookup in
        the program is concerned.
        """
        with self._lock:
            rebuilt: list[dict[str, Any]] = []
            for entry in entries[:MAX_ENTRIES]:
                if not isinstance(entry, dict):
                    continue
                copied = json.loads(json.dumps(entry))
                copied["id"] = uuid.uuid4().hex[:10]
                copied.pop("startAt", None)
                copied.pop("endAt", None)
                copied.pop("timesPinned", None)
                rebuilt.append(copied)
            self._plan["entries"] = rebuilt
            if name:
                self._plan["name"] = _clean(name)
        self.save()
        return self.raw()

    #: Seconds in a day. Slot times roll forward by whole days so that the clock
    #: time the operator chose is the clock time they get.
    DAY = 86400.0

    def roll_times(self, window_start: float | None,
                   window_end: float | None) -> int:
        """Carry slot times over to tonight, keeping the time of day.

        Start and end times are stored as absolute moments, so by the next
        evening last night's are eighteen hours in the past and describe a
        window that closed before the sun went down.  They used to be thrown
        away for that reason, which was the wrong conclusion from the right
        observation: what the operator chose was never an instant, it was *a
        time of night* — "start Cygnus at half nine, stop at one" — and that
        choice is as true tonight as it was yesterday.  Dropping it made them
        set the same times again every evening.

        So the times move forward a whole number of days rather than being
        deleted.  Whole days because that is what keeps the clock time: 21:30
        stays 21:30.  A time that still falls outside tonight's dark window
        afterwards — the nights draw in, and a plan made in December does not
        fit June — is pulled to the nearest edge of it rather than discarded,
        because a target the operator wanted first is still the one they want
        first.

        The count returned is how many times were moved, for the log.

        Whole days keep the clock time rather than the sidereal time, so a
        target drifts about four minutes earlier against the stars each night.
        That is the right trade for a time somebody typed: they meant half nine,
        not 21:26. Auto-arrange is what re-places a plan against the sky.
        """
        if not window_start or not window_end:
            return 0
        moved = 0
        with self._lock:
            for entry in self._plan["entries"]:
                for key in ("startAt", "endAt"):
                    at = entry.get(key)
                    if at is None or window_start <= at <= window_end:
                        continue
                    entry[key] = self._roll_into(at, window_start, window_end)
                    moved += 1
                # A start that has ended up after its end — possible when the
                # two were pulled to opposite edges of a shorter night — is not
                # a slot at all.
                start, end = entry.get("startAt"), entry.get("endAt")
                if start is not None and end is not None and end <= start:
                    entry["endAt"] = window_end
        if moved:
            self.save()
        return moved

    @classmethod
    def _roll_into(cls, at: float, window_start: float,
                   window_end: float) -> float:
        """One time, moved forward or back by whole days into tonight."""
        # Aim at the middle of the window so a time lands inside it wherever in
        # the night it belongs, rather than being biased to one end.
        middle = (window_start + window_end) / 2.0
        days = round((middle - at) / cls.DAY)
        rolled = at + days * cls.DAY
        # Still outside: the night is a different length now, so pull it to the
        # nearest edge rather than throwing the operator's choice away.
        return min(max(rolled, window_start), window_end)

    def set_meta(self, name: str | None = None, date: str | None = None,
                 clear_date: bool = False) -> dict[str, Any]:
        with self._lock:
            if name is not None:
                self._plan["name"] = _clean(name) or "Tonight"
            if clear_date:
                self._plan["date"] = None
            elif date is not None:
                try:
                    _dt.date.fromisoformat(date)
                except ValueError as exc:
                    raise DeviceError(f"{date!r} is not a date (YYYY-MM-DD)") from exc
                self._plan["date"] = date
        self.save()
        return self.raw()

    def set_options(self, entry_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """Change one target's sequence options.

        Only the keys given are touched, and only keys that mean something are
        stored — an option the sequencer does not read is worse than no option.

        What is written back is only what actually *differs* from the default.
        Storing the whole merged set instead would quietly pin every option on
        the target the first time any one of them was touched, so a later change
        to what the program does by default would reach the targets nobody had
        opened and silently skip the ones they had.
        """
        with self._lock:
            for entry in self._plan["entries"]:
                if entry["id"] != entry_id:
                    continue
                current = options_for(entry)
                for key, value in values.items():
                    if value is None or key not in ENTRY_OPTIONS:
                        continue
                    if key == "filterOrder" and value not in FILTER_ORDERS:
                        raise DeviceError(f"{value!r} is not a filter order")
                    if key == "panels":
                        # Sorted and de-duplicated, so the order the panels were
                        # clicked in never becomes the order they are shot in —
                        # a mosaic is walked in a worked-out order and this is a
                        # filter on it, not a replacement for it.
                        value = sorted({int(index) for index in value
                                        if str(index).lstrip("-").isdigit()})
                    current[key] = value
                entry["options"] = {key: value for key, value in current.items()
                                    if value != ENTRY_OPTIONS[key]}
                result = json.loads(json.dumps(entry))
                break
            else:
                raise DeviceError(f"unknown plan entry {entry_id!r}")
        self.save()
        return result

    def add(self, target_id: str, name: str) -> dict[str, Any]:
        with self._lock:
            if len(self._plan["entries"]) >= MAX_ENTRIES:
                raise DeviceError(f"the plan is full ({MAX_ENTRIES} targets)")
            if any(e.get("targetId") == target_id for e in self._plan["entries"]):
                raise DeviceError(f"{name} is already in the plan")
            entry = {
                "id": uuid.uuid4().hex[:10],
                "kind": "target",
                "targetId": target_id,
                "name": _clean(name),
                "filters": [],
                "notes": "",
            }
            self._plan["entries"].append(entry)
        self.save()
        return entry

    def add_calibration(self, recipe_id: str, name: str,
                        rig_id: str | None = None) -> dict[str, Any]:
        """Put a calibration recipe in the plan as a task of its own.

        Unlike a target, the same recipe may reasonably appear twice — flats at
        dusk and darks at dawn are two tasks off one library — so this does not
        refuse a duplicate the way `add` does.
        """
        with self._lock:
            if len(self._plan["entries"]) >= MAX_ENTRIES:
                raise DeviceError(f"the plan is full ({MAX_ENTRIES} tasks)")
            entry = {
                "id": uuid.uuid4().hex[:10],
                "kind": "calibration",
                "recipeId": recipe_id,
                # Which telescope to run it on; blank means all of them, which
                # is what flats and darks normally want.
                "rigId": rig_id or "",
                "name": _clean(name),
                "notes": "",
            }
            self._plan["entries"].append(entry)
        self.save()
        return entry

    def reorder(self, entry_ids: list[str]) -> dict[str, Any]:
        """Put the entries in the given order.

        Anything the caller left out keeps its place at the end, so a stale
        drag from a page that has not refreshed cannot silently drop a target.
        """
        with self._lock:
            by_id = {e["id"]: e for e in self._plan["entries"]}
            unknown = [i for i in entry_ids if i not in by_id]
            if unknown:
                raise DeviceError(f"unknown plan entry {unknown[0]!r}")
            ordered = [by_id[i] for i in entry_ids]
            ordered += [e for e in self._plan["entries"] if e["id"] not in set(entry_ids)]
            self._plan["entries"] = ordered
        self.save()
        return self.raw()

    def set_times(self, entry_id: str, start_at: float | None = None,
                  end_at: float | None = None, clear_start: bool = False,
                  clear_end: bool = False) -> dict[str, Any]:
        """Pin when a target may run.  Either end can be left open.

        Times set here are *pinned*: Auto-arrange works around them rather than
        over them.  A time the operator chose is a decision, and an arranger
        that quietly overwrites decisions is one nobody can use for the one
        target that has to run at a particular hour.
        """
        with self._lock:
            for entry in self._plan["entries"]:
                if entry["id"] != entry_id:
                    continue
                if clear_start:
                    entry.pop("startAt", None)
                elif start_at is not None:
                    entry["startAt"] = float(start_at)
                if clear_end:
                    entry.pop("endAt", None)
                elif end_at is not None:
                    entry["endAt"] = float(end_at)

                start, end = entry.get("startAt"), entry.get("endAt")
                if start and end and end <= start:
                    raise DeviceError("the end time must be after the start time")
                if start or end:
                    entry["timesPinned"] = True
                else:
                    entry.pop("timesPinned", None)
                result = json.loads(json.dumps(entry))
                break
            else:
                raise DeviceError(f"unknown plan entry {entry_id!r}")
        self.save()
        return result

    def apply_arrangement(self, arrangement: list[dict[str, Any]]) -> dict[str, Any]:
        """Adopt an auto-arranged running order and its start and end times.

        Times the arranger wrote are marked as its own, so a later arrange is
        free to move them; times the operator pinned are never touched here,
        because the arranger was told to work around them.
        """
        placed = [item["id"] for item in arrangement]
        with self._lock:
            by_id = {e["id"]: e for e in self._plan["entries"]}
            for item in arrangement:
                entry = by_id.get(item["id"])
                if entry is None or entry.get("timesPinned"):
                    continue
                entry["startAt"] = float(item["start"])
                entry["endAt"] = float(item["end"])
            # Targets that could not be placed keep their frames but lose their
            # times, so they are not left pinned to a slot that no longer exists.
            # Calibration tasks are never arranged — nothing about the sky says
            # when to take darks — so their times are the operator's and are
            # left exactly as they were.
            for entry in self._plan["entries"]:
                if (entry["id"] not in set(placed) and not is_calibration(entry)
                        and not entry.get("timesPinned")):
                    entry.pop("startAt", None)
                    entry.pop("endAt", None)
            ordered = [by_id[i] for i in placed if i in by_id]
            ordered += [e for e in self._plan["entries"]
                        if e["id"] not in set(placed) and not is_calibration(e)]
            # Calibration tasks keep the position they were put in.  "Flats at
            # dusk, then the targets, then darks" is an order the operator
            # chose, and the arranger has no view about it.
            arranged = iter(ordered)
            self._plan["entries"] = [
                entry if is_calibration(entry) else next(arranged)
                for entry in self._plan["entries"]]
        self.save()
        return self.raw()

    def remove(self, entry_id: str) -> None:
        with self._lock:
            before = len(self._plan["entries"])
            self._plan["entries"] = [e for e in self._plan["entries"]
                                     if e["id"] != entry_id]
            if len(self._plan["entries"]) == before:
                raise DeviceError(f"unknown plan entry {entry_id!r}")
        self.save()

    def set_filters(self, entry_id: str, filters: list[dict[str, Any]],
                    panels: int, available_seconds: float,
                    overheads: dict[str, float],
                    rig_id: str | None = None) -> dict[str, Any]:
        """Replace a filter allocation, trimmed to what the night allows.

        Counts are clamped rather than rejected: the useful answer to "can I
        have 200 frames?" is "you can have 96", not an error.

        `rig_id` says which telescope's allocation this is.  None is the
        master's, which is also what any telescope without one of its own
        follows.
        """
        cleaned: list[dict[str, Any]] = []
        for item in filters:
            name = _clean(item.get("name", ""), 24)
            exposure = float(item.get("exposure", 0) or 0)
            count = int(item.get("count", 0) or 0)
            if not name or exposure <= 0 or count <= 0:
                continue
            cleaned.append({"name": name, "exposure": round(exposure, 3),
                            "count": max(0, count)})

        trimmed = trim_to_budget(cleaned, panels, available_seconds, overheads)

        with self._lock:
            for entry in self._plan["entries"]:
                if entry["id"] == entry_id:
                    if rig_id is None:
                        entry["filters"] = trimmed["filters"]
                    else:
                        entry.setdefault("rigFilters", {})[rig_id] = trimmed["filters"]
                    result = json.loads(json.dumps(entry))
                    break
            else:
                raise DeviceError(f"unknown plan entry {entry_id!r}")
        self.save()
        return {"entry": result, "clamped": trimmed["clamped"]}

    def clear_rig_filters(self, entry_id: str, rig_id: str) -> dict[str, Any]:
        """Put a telescope back onto mirroring the master's allocation."""
        with self._lock:
            for entry in self._plan["entries"]:
                if entry["id"] == entry_id:
                    (entry.get("rigFilters") or {}).pop(rig_id, None)
                    if not entry.get("rigFilters"):
                        entry.pop("rigFilters", None)
                    result = json.loads(json.dumps(entry))
                    break
            else:
                raise DeviceError(f"unknown plan entry {entry_id!r}")
        self.save()
        return result

    def prune_rigs(self, known_rig_ids: set[str]) -> int:
        """Drop allocations for telescopes that are no longer in the setup."""
        removed = 0
        with self._lock:
            for entry in self._plan["entries"]:
                stored = entry.get("rigFilters")
                if not stored:
                    continue
                stale = [rig_id for rig_id in stored if rig_id not in known_rig_ids]
                for rig_id in stale:
                    del stored[rig_id]
                    removed += 1
                if not stored:
                    entry.pop("rigFilters", None)
        if removed:
            self.save()
        return removed

    def set_notes(self, entry_id: str, notes: str) -> dict[str, Any]:
        with self._lock:
            for entry in self._plan["entries"]:
                if entry["id"] == entry_id:
                    entry["notes"] = _clean(notes, 400)
                    result = json.loads(json.dumps(entry))
                    break
            else:
                raise DeviceError(f"unknown plan entry {entry_id!r}")
        self.save()
        return result

    def prune(self, known_target_ids: set[str],
              known_recipe_ids: set[str] | None = None) -> int:
        """Drop tasks whose target or recipe no longer exists.

        Calibration tasks are only pruned when the caller actually knows what
        recipes there are; passing nothing leaves them alone, so a plan is never
        emptied of them by a caller that was only checking targets.
        """
        def keep(entry: dict[str, Any]) -> bool:
            if is_calibration(entry):
                return (known_recipe_ids is None
                        or entry.get("recipeId") in known_recipe_ids)
            return entry.get("targetId") in known_target_ids

        with self._lock:
            before = len(self._plan["entries"])
            self._plan["entries"] = [e for e in self._plan["entries"] if keep(e)]
            removed = before - len(self._plan["entries"])
        if removed:
            self.save()
        return removed


MAX_SEQUENCES = 100


class SequenceStore:
    """Named plans kept on disk, to be brought back another night.

    A plan is a night's worth of decisions — which targets, how many frames of
    what through which filter, what each one's floor and Moon limit are — and
    those decisions are worth more than one night.  "The winter narrowband run"
    is a thing you build once and want back in October.

    What is *not* saved is the times.  Start and end times are absolute moments
    belonging to the night they were worked out for, and restoring them in
    November would pin every target to a slot that has been over for months.
    The shape of the night comes back; where it sits in the night is for
    Auto-arrange to say again.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_root() / "sequences.json")
        self._lock = threading.RLock()
        self._items: list[dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        items = stored.get("sequences") if isinstance(stored, dict) else stored
        if isinstance(items, list):
            with self._lock:
                self._items = [s for s in items
                               if isinstance(s, dict) and s.get("id")]

    def save(self) -> None:
        with self._lock:
            payload = json.dumps({"sequences": self._items}, indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(payload, "utf-8")
        except OSError:
            pass

    @staticmethod
    def _strip(entry: dict[str, Any]) -> dict[str, Any]:
        """One entry as it should be kept: everything but when it ran."""
        kept = {k: v for k, v in entry.items()
                if k not in ("startAt", "endAt", "timesPinned")}
        return json.loads(json.dumps(kept))

    def listing(self) -> list[dict[str, Any]]:
        """Every saved sequence, newest first, without the entries."""
        with self._lock:
            return [{k: v for k, v in item.items() if k != "entries"}
                    | {"targets": len(item.get("entries") or [])}
                    for item in sorted(self._items,
                                       key=lambda s: s.get("saved") or 0,
                                       reverse=True)]

    def get(self, sequence_id: str) -> dict[str, Any]:
        with self._lock:
            for item in self._items:
                if item["id"] == sequence_id:
                    return json.loads(json.dumps(item))
        raise DeviceError(f"unknown sequence {sequence_id!r}")

    def save_as(self, name: str, plan: dict[str, Any],
                sequence_id: str | None = None) -> dict[str, Any]:
        """Snapshot a plan under a name, replacing one of the same name."""
        name = _clean(name) or "Untitled sequence"
        entries = [self._strip(e) for e in (plan.get("entries") or [])]
        if not entries:
            raise DeviceError("there is nothing in the plan to save")

        with self._lock:
            existing = None
            if sequence_id:
                existing = next((s for s in self._items if s["id"] == sequence_id), None)
            if existing is None:
                existing = next((s for s in self._items
                                 if s["name"].lower() == name.lower()), None)
            record = {
                "id": existing["id"] if existing else uuid.uuid4().hex[:10],
                "name": name,
                "saved": time.time(),
                "entries": entries,
            }
            if existing is not None:
                self._items[self._items.index(existing)] = record
            else:
                if len(self._items) >= MAX_SEQUENCES:
                    raise DeviceError(f"there are already {MAX_SEQUENCES} saved "
                                      "sequences; delete one first")
                self._items.append(record)
        self.save()
        return {k: v for k, v in record.items() if k != "entries"}

    def delete(self, sequence_id: str) -> None:
        with self._lock:
            before = len(self._items)
            self._items = [s for s in self._items if s["id"] != sequence_id]
            if len(self._items) == before:
                raise DeviceError(f"unknown sequence {sequence_id!r}")
        self.save()


def allocation_for(entry: dict[str, Any], rig_id: str,
                   master_id: str) -> list[dict[str, Any]]:
    """What one telescope is asked to shoot for this entry.

    A telescope with nothing of its own mirrors the master, so adding a second
    scope that carries the same filters needs no planning at all.
    """
    if rig_id != master_id:
        own = (entry.get("rigFilters") or {}).get(rig_id)
        if own is not None:
            return [dict(item) for item in own]
    return [dict(item) for item in entry.get("filters") or []]


def entry_seconds(entry: dict[str, Any], rig_ids: list[str], master_id: str,
                  panels: int, overheads: dict[str, float]) -> float:
    """How long an entry occupies the night.

    The telescopes shoot together, so the entry takes as long as its slowest
    allocation rather than the total of all of them.
    """
    return max(
        (schedule.plan_seconds(allocation_for(entry, rig_id, master_id),
                               panels, overheads)
         for rig_id in (rig_ids or [master_id])),
        default=0.0)


def trim_to_budget(filters: list[dict[str, Any]], panels: int,
                   available_seconds: float,
                   overheads: dict[str, float]) -> dict[str, Any]:
    """Reduce counts, in order, until the whole allocation fits the night."""
    kept: list[dict[str, Any]] = []
    clamped: list[dict[str, Any]] = []

    for item in filters:
        trial = kept + [item]
        if schedule.plan_seconds(trial, panels, overheads) <= available_seconds:
            kept.append(dict(item))
            continue
        allowed = schedule.max_count(item["exposure"], panels,
                                     available_seconds, kept, overheads)
        if allowed > 0:
            kept.append({**item, "count": allowed})
        clamped.append({"name": item["name"], "asked": item["count"], "given": allowed})
    return {"filters": kept, "clamped": clamped}
