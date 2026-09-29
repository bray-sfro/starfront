"""The target list: framings you have decided you want to shoot.

A target is a saved framing — where to point, which way up, and for a mosaic,
the panels that make it up.  Panels are sub-targets: each one is a real pointing
with its own coordinates, so a sequencer can later work through them without
recomputing anything.

Deliberately inert.  Nothing here moves the telescope; it is a notebook that the
planner writes and, in time, the sequencer will read.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import framing
from .config import data_root
from .devices.base import DeviceError

MAX_TARGETS = 500

#: How many nights of per-night history to keep on one target.  A target shot
#: every clear night for three years is about this many, and the whole record is
#: a few kilobytes — small enough to live in the target list, which is what lets
#: the Plan tab show it without a second request.
MAX_NIGHT_LOG = 300


def _clean(text: str, limit: int = 120) -> str:
    return " ".join(str(text or "").split())[:limit]


def _fold_filter_names(target: dict[str, Any]) -> None:
    """Every filter name in a target's record, in the program's one spelling.

    The integration log is keyed by filter, and a record written when the
    wheel said "Ha" would otherwise sit beside tonight's "H" as a second
    filter, with the total split between them.
    """
    from .filters import canonical_keys
    totals = target.get("integration")
    if not isinstance(totals, dict):
        return
    if isinstance(totals.get("byFilter"), dict):
        totals["byFilter"] = _merge_filter_totals(totals["byFilter"])
    for row in totals.get("log") or []:
        if isinstance(row, dict) and isinstance(row.get("byFilter"), dict):
            row["byFilter"] = _merge_filter_totals(row["byFilter"])
    panels = totals.get("panels")
    if isinstance(panels, dict):
        for night in panels.values():
            if not isinstance(night, dict):
                continue
            for index, per_filter in night.items():
                if isinstance(per_filter, dict):
                    night[index] = canonical_keys(per_filter)


def _merge_filter_totals(table: dict[str, Any]) -> dict[str, Any]:
    """Re-key per-filter totals, adding together any that fold to one name."""
    from .filters import canonical
    out: dict[str, Any] = {}
    for name, value in table.items():
        key = canonical(name) or str(name)
        if key in out and isinstance(value, dict) and isinstance(out[key], dict):
            merged = dict(out[key])
            merged["seconds"] = round(float(merged.get("seconds", 0.0))
                                      + float(value.get("seconds", 0.0)), 1)
            merged["frames"] = int(merged.get("frames", 0)) + int(value.get("frames", 0))
            out[key] = merged
        elif key not in out:
            out[key] = value
    return out


def _empty_integration() -> dict[str, Any]:
    return {"seconds": 0.0, "frames": 0, "byFilter": {}, "nights": [],
            "lastFrame": None, "log": []}


def _empty_night(night: str) -> dict[str, Any]:
    """One night's row.

    Sums and counts rather than averages, so another frame can be added to it
    without re-reading anything: the mean is worked out when it is displayed.
    """
    return {
        "night": night,
        "seconds": 0.0,
        "frames": 0,
        "byFilter": {},
        "first": None,
        "last": None,
        # Star size and guiding over the night: the two numbers that say whether
        # the frames are worth keeping, and the reason a night log beats a total.
        "hfrSum": 0.0, "hfrCount": 0, "hfrMin": None, "hfrMax": None,
        "guideSum": 0.0, "guideCount": 0,
        # Frames taken while the guider had no lock, and how many times the run
        # had to be put back on its feet.
        "guideLost": 0,
        "recoveries": 0,
        "telescopes": {},
    }


class TargetStore:
    """Targets on disk, written through on every change."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_root() / "targets.json")
        self._lock = threading.RLock()
        self._targets: list[dict[str, Any]] = []
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(stored, dict):
            stored = stored.get("targets", [])
        if isinstance(stored, list):
            targets = [t for t in stored if isinstance(t, dict) and t.get("id")]
            for target in targets:
                _fold_filter_names(target)
            with self._lock:
                self._targets = targets

    def save(self) -> None:
        with self._lock:
            payload = json.dumps({"targets": self._targets}, indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(payload, "utf-8")
        except OSError:
            pass

    # -- access ------------------------------------------------------------
    def listing(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(target) for target in self._targets]

    def get(self, target_id: str) -> dict[str, Any]:
        with self._lock:
            for target in self._targets:
                if target["id"] == target_id:
                    return dict(target)
        raise DeviceError(f"unknown target {target_id!r}")

    # -- editing -----------------------------------------------------------
    def create(self, name: str, ra: float, dec: float, rotation: float = 0.0,
               panel_width: float = 0.0, panel_height: float = 0.0,
               rows: int = 1, columns: int = 1, overlap: float = 0.1,
               notes: str = "", survey: str = "",
               align: str = "aligned",
               collab: dict[str, Any] | None = None) -> dict[str, Any]:
        """Save a framing.

        Panels are computed here rather than accepted from the caller: the
        planner draws its own preview, but what gets stored has to come from one
        canonical implementation or the two will drift.

        `collab` marks a target that came from a collaboration's task, carrying
        the project and task it answers. It is what stops the same task being
        adopted twice, and what lets the Planner say where a target came from —
        a framing that appeared in the list on its own is otherwise a mystery.
        """
        name = _clean(name) or "Untitled"
        rows, columns = max(1, int(rows)), max(1, int(columns))
        is_mosaic = rows > 1 or columns > 1

        if is_mosaic and (panel_width <= 0 or panel_height <= 0):
            raise DeviceError(
                "a mosaic needs the camera field size — set the focal length in "
                "Site & Optics first")

        panels: list[dict[str, Any]] = []
        seams: dict[str, Any] = {}
        if is_mosaic:
            computed = framing.mosaic_panels(
                ra * 15.0, dec, panel_width, panel_height,
                rows=rows, columns=columns, overlap=overlap, position_angle=rotation,
                align=align)
            seams = framing.mosaic_seams(computed, panel_width, panel_height, overlap)
            for index, panel in enumerate(computed, start=1):
                panels.append({
                    "id": uuid.uuid4().hex[:10],
                    "name": f"{name} — panel {index}",
                    "index": index,
                    **panel,
                })

        target = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "type": "mosaic" if is_mosaic else "single",
            "ra": round(float(ra) % 24.0, 6),
            "dec": round(float(dec), 5),
            "rotation": round(float(rotation) % 360.0, 3),
            "panelWidth": round(float(panel_width), 6),
            "panelHeight": round(float(panel_height), 6),
            "rows": rows,
            "columns": columns,
            "overlap": round(float(overlap), 4),
            "align": align,
            "extent": (framing.mosaic_extent(panel_width, panel_height, rows, columns, overlap)
                       if panel_width and panel_height
                       else {"width": 0.0, "height": 0.0}),
            "seams": seams,
            "panels": panels,
            "notes": _clean(notes, 500),
            "survey": _clean(survey, 80),
            # What has actually been shot on this target, across every night.
            # The plan is saved, so this is the number that tells you whether a
            # target is finished or wants another session.
            "integration": _empty_integration(),
            "created": time.time(),
            "updated": time.time(),
        }
        if collab:
            target["collab"] = {
                "project": str(collab.get("project") or ""),
                "projectName": _clean(collab.get("projectName") or "", 120),
                "task": str(collab.get("task") or ""),
                # Which revision of the task this was built from, so a task the
                # coordinator has since changed can be told from one that is
                # still current.
                "version": int(collab.get("version") or 1),
                "server": _clean(collab.get("server") or "", 200),
            }

        with self._lock:
            if len(self._targets) >= MAX_TARGETS:
                raise DeviceError(f"the target list is full ({MAX_TARGETS})")
            self._targets.insert(0, target)
        self.save()
        return target

    def reframe(self, target_id: str, rotation: float, rows: int, columns: int,
                align: str, panel_width: float | None = None,
                panel_height: float | None = None) -> dict[str, Any]:
        """Lay a mosaic out again at a different angle or grid.

        For a collaboration target, whose geometry is not the operator's
        decision but the camera's: a rig with no rotator that turns out to
        sit at 271 degrees rather than the 268 in the settings has to have
        its panels re-laid at 271, and one that gains a rotator can be laid
        along the project's grid instead of its own. Everything that is not
        geometry - the id, the name, the notes, the integration log, the
        collaboration stamp - stays exactly as it is, so the plan entry
        pointing at this target goes on pointing at it.
        """
        rows, columns = max(1, int(rows)), max(1, int(columns))
        with self._lock:
            target = next((t for t in self._targets if t["id"] == target_id), None)
            if target is None:
                raise DeviceError(f"unknown target {target_id!r}")
            width = float(panel_width or target.get("panelWidth") or 0.0)
            height = float(panel_height or target.get("panelHeight") or 0.0)
            is_mosaic = rows > 1 or columns > 1
            if is_mosaic and (width <= 0 or height <= 0):
                raise DeviceError("a mosaic needs the camera field size")
            overlap = float(target.get("overlap") or 0.1)
            panels: list[dict[str, Any]] = []
            seams: dict[str, Any] = {}
            if is_mosaic:
                computed = framing.mosaic_panels(
                    float(target["ra"]) * 15.0, float(target["dec"]), width, height,
                    rows=rows, columns=columns, overlap=overlap,
                    position_angle=rotation, align=align)
                seams = framing.mosaic_seams(computed, width, height, overlap)
                for index, panel in enumerate(computed, start=1):
                    panels.append({
                        "id": uuid.uuid4().hex[:10],
                        "name": f"{target['name']} — panel {index}",
                        "index": index,
                        **panel,
                    })
            target.update({
                "type": "mosaic" if is_mosaic else "single",
                "rotation": round(float(rotation) % 360.0, 3),
                "panelWidth": round(width, 6),
                "panelHeight": round(height, 6),
                "rows": rows, "columns": columns,
                "align": align,
                "extent": (framing.mosaic_extent(width, height, rows, columns, overlap)
                           if width and height else {"width": 0.0, "height": 0.0}),
                "seams": seams,
                "panels": panels,
                "updated": time.time(),
            })
            result = dict(target)
        self.save()
        return result

    def stamp_collab(self, target_id: str, values: dict[str, Any]) -> None:
        """Update a target's collaboration stamp in place.

        Only the stamp: a re-sync must not touch the framing, the integration
        log or anything the operator chose.
        """
        with self._lock:
            for target in self._targets:
                if target["id"] != target_id:
                    continue
                stamp = dict(target.get("collab") or {})
                stamp.update(values)
                target["collab"] = stamp
                target["updated"] = time.time()
                break
            else:
                return
        self.save()

    def create_survey(self, name: str, region: dict[str, Any],
                      settings: dict[str, Any], panels: list[dict[str, Any]],
                      field: dict[str, Any],
                      night: dict[str, Any] | None = None) -> dict[str, Any]:
        """Save a twilight sweep as a target whose panels are its fields.

        The Sun-relative region is stored alongside the panels, and it is the
        part that matters: the RA and Dec below are only where this sweep lands
        *tonight*.  Re-planning the same region for another date puts it
        somewhere else entirely, because the Sun will have moved, and a sweep
        that could not be regenerated would be stale within the week.
        """
        name = _clean(name) or "Survey sweep"
        stored: list[dict[str, Any]] = []
        for index, panel in enumerate(panels, start=1):
            stored.append({
                "id": uuid.uuid4().hex[:10],
                "name": f"{name} - field {index}",
                "index": index,
                "ra": panel["ra"],
                "dec": panel["dec"],
                # The sky angle that makes this panel abut its neighbours: the
                # grid runs along the ecliptic, not along the meridian, so a
                # camera left at north-up sits skewed across it.
                "rotation": round(float(panel.get("rotation") or 0.0), 3),
                # What makes it a survey panel rather than a mosaic tile.
                "cell": panel.get("cell"),
                "dLambda": panel.get("dLambda"),
                "beta": panel.get("beta"),
                "elongation": panel.get("elongation"),
                "altitude": panel.get("altitude"),
                "airmass": panel.get("airmass"),
                "side": panel.get("side"),
                "startAt": panel.get("startAt"),
                "endAt": panel.get("endAt"),
                "score": panel.get("score"),
            })

        centre = stored[0] if stored else {"ra": 0.0, "dec": 0.0}
        target = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "type": "survey",
            "ra": centre["ra"],
            "dec": centre["dec"],
            "rotation": 0.0,
            "panelWidth": round(float(field.get("width") or 0.0), 6),
            "panelHeight": round(float(field.get("height") or 0.0), 6),
            "rows": 1,
            "columns": len(stored),
            "overlap": round(float(settings.get("overlap", 0.08)), 4),
            "align": "aligned",
            "extent": {"width": 0.0, "height": 0.0},
            "seams": {},
            "panels": stored,
            # Everything needed to produce this sweep again for another date.
            "survey": {
                "region": dict(region),
                "settings": {k: settings.get(k) for k in (
                    "sunHigh", "sunLow", "minAltitude", "maxAirmass",
                    "moonAvoidance", "moonScaleByPhase", "galacticAvoidance",
                    "revisitNights", "exposure", "exposureCount", "binning",
                    "dither", "ditherPixels", "overlap", "filter")},
                "field": dict(field),
                "plannedFor": (night or {}).get("date"),
                "generated": time.time(),
            },
            "notes": "",
            "survey_name": "",
            "integration": _empty_integration(),
            "created": time.time(),
            "updated": time.time(),
        }

        with self._lock:
            if len(self._targets) >= MAX_TARGETS:
                raise DeviceError(f"the target list is full ({MAX_TARGETS})")
            self._targets.insert(0, target)
        self.save()
        return target

    def create_allsky(self, name: str, parameters: dict[str, Any],
                      goal: list[dict[str, Any]],
                      shape: dict[str, Any]) -> dict[str, Any]:
        """Save an all-sky survey: a grid, and a goal for every field in it.

        The fields themselves are deliberately *not* stored.  A full sky at a
        degree-and-a-half is fourteen thousand of them, and writing that into
        the target list would put a megabyte into every reply the Plan tab asks
        for.  What is stored is the handful of numbers the grid is generated
        from, which is enough to rebuild it identically — and freezing those
        numbers is what makes a progress record taken in March still mean the
        same fields in October.
        """
        name = _clean(name) or "All-sky survey"
        target = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "type": "allsky",
            # A nominal pointing, so anything that expects a target to have one
            # keeps working. The survey does not have a centre in any real sense.
            "ra": 0.0,
            "dec": round((parameters["decMin"] + parameters["decMax"]) / 2.0, 5),
            "rotation": 0.0,
            "panelWidth": round(float(parameters["fieldWidth"]), 6),
            "panelHeight": round(float(parameters["fieldHeight"]), 6),
            "rows": int(shape.get("rings") or 1),
            "columns": 0,
            "overlap": round(float(parameters["overlap"]), 4),
            "align": "aligned",
            "extent": {"width": 0.0, "height": 0.0},
            "seams": {},
            # No panels: they are generated from `allsky` below, every time.
            "panels": [],
            "allsky": {
                # Everything the grid is a function of. Changing any of it makes
                # a different survey, not a modified one.
                "fieldWidth": round(float(parameters["fieldWidth"]), 6),
                "fieldHeight": round(float(parameters["fieldHeight"]), 6),
                "overlap": round(float(parameters["overlap"]), 4),
                "decMin": round(float(parameters["decMin"]), 4),
                "decMax": round(float(parameters["decMax"]), 4),
                "stagger": bool(parameters.get("stagger", True)),
                # The camera angle the grid was cut for, and the sensor it was
                # cut from. A tilted sensor does not cover a north-up rectangle
                # its own size, so the tiling only holds while the camera sits
                # the way it did — which is worth being able to check.
                "rotation": round(float(parameters.get("rotation") or 0.0), 3),
                "sensorWidth": round(float(parameters.get("sensorWidth")
                                           or parameters["fieldWidth"]), 6),
                "sensorHeight": round(float(parameters.get("sensorHeight")
                                            or parameters["fieldHeight"]), 6),
                # What every field is to be shot with. This one *is* editable:
                # deciding half way through that the survey wants another filter
                # is a normal thing to do, and the fields already finished stay
                # finished for the filters they have.
                "goal": [dict(item) for item in goal],
                "shape": dict(shape),
                "created": time.time(),
            },
            "notes": "",
            "survey_name": "",
            "integration": _empty_integration(),
            "created": time.time(),
            "updated": time.time(),
        }

        with self._lock:
            if len(self._targets) >= MAX_TARGETS:
                raise DeviceError(f"the target list is full ({MAX_TARGETS})")
            self._targets.insert(0, target)
        self.save()
        return target

    def set_allsky_goal(self, target_id: str,
                        goal: list[dict[str, Any]]) -> dict[str, Any]:
        """Change what every field is to be shot with."""
        with self._lock:
            for target in self._targets:
                if target["id"] != target_id:
                    continue
                if target.get("type") != "allsky":
                    raise DeviceError(f"{target['name']} is not an all-sky survey")
                target["allsky"]["goal"] = [dict(item) for item in goal]
                target["updated"] = time.time()
                result = json.loads(json.dumps(target))
                break
            else:
                raise DeviceError(f"unknown target {target_id!r}")
        self.save()
        return result

    def update(self, target_id: str, name: str | None = None,
               notes: str | None = None) -> dict[str, Any]:
        """Rename or re-annotate. Geometry is not editable in place: a different
        framing is a different shot, and should be saved as one."""
        with self._lock:
            for target in self._targets:
                if target["id"] != target_id:
                    continue
                if name is not None:
                    target["name"] = _clean(name) or target["name"]
                if notes is not None:
                    target["notes"] = _clean(notes, 500)
                target["updated"] = time.time()
                result = dict(target)
                break
            else:
                raise DeviceError(f"unknown target {target_id!r}")
        self.save()
        return result

    def add_integration(self, target_id: str, filter_name: str, seconds: float,
                        night: str, *, hfr: float | None = None,
                        guide_rms: float | None = None,
                        guide_lost: bool = False, telescope: str = "",
                        ) -> dict[str, Any] | None:
        """Credit one finished sub to a target's totals and to its night.

        The running total answers "is this target finished?".  The per-night row
        answers the other question, which a total cannot: "how did last night
        actually go?"  Four hours of integration made of forty good subs and
        four hours made of eighty subs half of which were trailed are the same
        number, and they are not the same night.

        Returns None rather than raising for an unknown target: a sequence in
        progress should not fall over because someone deleted the target from
        the list while it was being shot.
        """
        now = time.time()
        with self._lock:
            for target in self._targets:
                if target["id"] != target_id:
                    continue
                totals = target.setdefault("integration", _empty_integration())
                totals["seconds"] = round(totals.get("seconds", 0.0) + seconds, 1)
                totals["frames"] = int(totals.get("frames", 0)) + 1
                name = _clean(filter_name, 24) or "unfiltered"
                by_filter = totals.setdefault("byFilter", {})
                by_filter[name] = round(by_filter.get(name, 0.0) + seconds, 1)
                nights = totals.setdefault("nights", [])
                if night and night not in nights:
                    nights.append(night)
                totals["lastFrame"] = now

                if night:
                    self._credit_night(totals, night, name, seconds, now,
                                       hfr, guide_rms, guide_lost, telescope)
                result = json.loads(json.dumps(totals))
                break
            else:
                return None
        self.save()
        return result

    def add_panel_integration(self, target_id: str, night: str, panel: int,
                              filter_name: str, seconds: float, *,
                              hfr: float | None = None,
                              guide_rms: float | None = None) -> None:
        """Credit one sub to the panel it was taken on.

        The night log answers "how did last night go?"; this answers "which
        panels have I actually got, and how much on each?" — which is the
        question a collaboration asks, because depth is per point on the sky
        and a mosaic's panels are different points. Kept per night so that a
        night's panels can be reported once and marked as reported, and a
        report that could not be sent tonight is sent tomorrow with nothing
        lost.
        """
        name = _clean(filter_name, 24) or "unfiltered"
        with self._lock:
            target = next((t for t in self._targets if t["id"] == target_id), None)
            if target is None or not night:
                return
            totals = target.setdefault("integration", _empty_integration())
            nights = totals.setdefault("panels", {})
            row = (nights.setdefault(night, {})
                   .setdefault(str(int(panel)), {})
                   .setdefault(name, {"seconds": 0.0, "frames": 0,
                                      "hfrSum": 0.0, "hfrCount": 0,
                                      "rmsSum": 0.0, "rmsCount": 0,
                                      "reported": False}))
            row["seconds"] = round(row["seconds"] + float(seconds), 1)
            row["frames"] = int(row["frames"]) + 1
            # Frames after a report re-open it: a night that grows after it was
            # sent is sent again, and the server counts the larger figure.
            row["reported"] = False
            if hfr is not None:
                row["hfrSum"] = round(row["hfrSum"] + float(hfr), 4)
                row["hfrCount"] = int(row["hfrCount"]) + 1
            if guide_rms is not None:
                row["rmsSum"] = round(row["rmsSum"] + float(guide_rms), 4)
                row["rmsCount"] = int(row["rmsCount"]) + 1
        self.save()

    def unreported_panels(self, target_id: str) -> list[dict[str, Any]]:
        """Every (night, panel, filter) tally not yet sent to a collaboration."""
        with self._lock:
            target = next((t for t in self._targets if t["id"] == target_id), None)
            if target is None:
                return []
            out = []
            nights = (target.get("integration") or {}).get("panels") or {}
            for night, panels in nights.items():
                for index, filters in panels.items():
                    for name, row in filters.items():
                        if not row.get("reported") and row.get("frames"):
                            out.append({"night": night, "panel": int(index),
                                        "filter": name, **row})
            return out

    def mark_reported(self, target_id: str, night: str, panel: int,
                      filter_name: str) -> None:
        with self._lock:
            target = next((t for t in self._targets if t["id"] == target_id), None)
            if target is None:
                return
            row = (((target.get("integration") or {}).get("panels") or {})
                   .get(night, {}).get(str(int(panel)), {}).get(filter_name))
            if row is None:
                return
            row["reported"] = True
        self.save()

    @staticmethod
    def _credit_night(totals: dict[str, Any], night: str, filter_name: str,
                      seconds: float, when: float, hfr: float | None,
                      guide_rms: float | None, guide_lost: bool,
                      telescope: str) -> None:
        """Add one sub to the night it belongs to, newest row last."""
        log = totals.setdefault("log", [])
        row = next((r for r in log if r.get("night") == night), None)
        if row is None:
            row = _empty_night(night)
            log.append(row)
            # Nights arrive in order on a real rig, but a plan re-run for an
            # earlier date would not, and a report that is out of order is worse
            # than one that costs a sort.
            log.sort(key=lambda r: r.get("night") or "")
            del log[:-MAX_NIGHT_LOG]

        row["seconds"] = round(row.get("seconds", 0.0) + seconds, 1)
        row["frames"] = int(row.get("frames", 0)) + 1
        per_filter = row.setdefault("byFilter", {}).setdefault(
            filter_name, {"seconds": 0.0, "frames": 0})
        per_filter["seconds"] = round(per_filter["seconds"] + seconds, 1)
        per_filter["frames"] += 1

        if row.get("first") is None:
            row["first"] = when
        row["last"] = when

        if hfr is not None:
            row["hfrSum"] = round(row.get("hfrSum", 0.0) + float(hfr), 4)
            row["hfrCount"] = int(row.get("hfrCount", 0)) + 1
            low, high = row.get("hfrMin"), row.get("hfrMax")
            row["hfrMin"] = hfr if low is None else min(low, hfr)
            row["hfrMax"] = hfr if high is None else max(high, hfr)
        if guide_rms is not None:
            row["guideSum"] = round(row.get("guideSum", 0.0) + float(guide_rms), 4)
            row["guideCount"] = int(row.get("guideCount", 0)) + 1
        if guide_lost:
            row["guideLost"] = int(row.get("guideLost", 0)) + 1
        if telescope:
            scopes = row.setdefault("telescopes", {})
            scopes[telescope] = int(scopes.get(telescope, 0)) + 1

    def note_recovery(self, target_id: str, night: str) -> None:
        """Count a rescue against the night it happened on.

        Kept on the night rather than only in the session log because the log is
        gone by morning and the question "was last Tuesday a fight?" is asked
        long after.
        """
        if not target_id or not night:
            return
        with self._lock:
            for target in self._targets:
                if target["id"] != target_id:
                    continue
                totals = target.setdefault("integration", _empty_integration())
                log = totals.setdefault("log", [])
                row = next((r for r in log if r.get("night") == night), None)
                if row is None:
                    row = _empty_night(night)
                    log.append(row)
                    log.sort(key=lambda r: r.get("night") or "")
                    del log[:-MAX_NIGHT_LOG]
                row["recoveries"] = int(row.get("recoveries", 0)) + 1
                break
            else:
                return
        self.save()

    def reset_integration(self, target_id: str) -> dict[str, Any]:
        with self._lock:
            for target in self._targets:
                if target["id"] == target_id:
                    target["integration"] = {"seconds": 0.0, "frames": 0,
                                             "byFilter": {}, "nights": [],
                                             "lastFrame": None}
                    result = json.loads(json.dumps(target))
                    break
            else:
                raise DeviceError(f"unknown target {target_id!r}")
        self.save()
        return result

    def delete(self, target_id: str) -> None:
        with self._lock:
            before = len(self._targets)
            self._targets = [t for t in self._targets if t["id"] != target_id]
            if len(self._targets) == before:
                raise DeviceError(f"unknown target {target_id!r}")
        self.save()

    def clear(self) -> int:
        with self._lock:
            removed = len(self._targets)
            self._targets = []
        self.save()
        return removed
