"""Taking calibration frames: the recipes, and the thing that shoots them.

A **recipe** is a list of sets — thirty darks at 300 seconds, twenty flats
through each filter, a hundred bias frames — saved by name so the same library
gets rebuilt the same way every season.  It can be run from the Calibrate tab
while the dome is shut, or dropped into the plan as a task the sequencer runs
itself: darks at the end of the night are worth having and nobody wants to sit
up for them.

With more than one telescope every set is shot on all of them at once.  That is
not just a speed-up: flats are per telescope by nature — different dust,
different vignetting, different exposure through the same filter — so each
telescope needs its own, and the only sensible time to take them is the same
time.  Each rig's frames are stacked into its own master, tagged with its own
name, and the matcher will only ever give a telescope's flats back to that
telescope.

Nothing here is clever about the sky.  Flats need a light source and darks need
darkness; this makes sure of the cover and the panel where it can, says so
plainly where it cannot, and otherwise just takes the frames.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import math
import random
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np

from . import astro, calibration
from .capture import clean_target
from .config import Config, data_root, effective_site
from .devices.base import DeviceError

# The sets a recipe can hold.  `darkflat` is a dark at the flat's own exposure.
SET_TYPES = ("bias", "dark", "darkflat", "flat")

MAX_RECIPES = 40
MAX_SETS = 24
MAX_COUNT = 500

# How many test frames to spend finding a flat exposure before settling for the
# closest one tried.  The response is linear in both the panel and the shutter,
# so two are normally enough — but a first frame that comes back saturated
# carries no usable level, and backing off from one costs a frame before the
# search proper has started.  Eight leaves room for two of those and still
# converges on the filter that needs it most.
FLAT_ATTEMPTS = 8


def _clean(text: Any, limit: int = 60) -> str:
    return " ".join(str(text or "").split())[:limit]


def _clean_set(raw: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("frameType") or "").strip().lower()
    if kind not in SET_TYPES:
        return None
    exposure = float(raw.get("exposure") or 0.0)
    return {
        "id": _clean(raw.get("id"), 32) or uuid.uuid4().hex[:8],
        "frameType": kind,
        "count": max(1, min(MAX_COUNT, int(raw.get("count") or 1))),
        # A bias is an exposure of zero by definition; the driver is asked for
        # the shortest it can do and reports what it actually gave.
        "exposure": 0.0 if kind == "bias" else max(0.0, exposure),
        "binning": max(1, min(8, int(raw.get("binning") or 1))),
        "gain": None if raw.get("gain") in (None, "") else int(raw["gain"]),
        "offset": None if raw.get("offset") in (None, "") else int(raw["offset"]),
        "filter": _clean(raw.get("filter"), 24),
        # One set covering every filter in the wheel, rather than a row each.
        # Expanded when it is shot, and per telescope — so two scopes carrying
        # different wheels are both covered by the one line.
        "allFilters": bool(raw.get("allFilters", False))
        and kind in ("flat", "darkflat"),
        # Flats only: where the light comes from.  A panel is a lamp on the
        # front of the telescope; the sky is twilight, which needs the mount to
        # point at it and fades while you are using it.
        "source": ("sky" if str(raw.get("source") or "").lower() == "sky"
                   else "panel"),
        # Flats only: the exposure is always found by measuring. What reaches
        # the sensor depends on the panel, the filter and the optics, so a
        # typed-in number is a guess and a measured one is not - and a sky
        # flat re-measures every frame. There used to be a box to turn this
        # off; it was only ever a way to get flats at the wrong level.
        "autoExposure": kind == "flat",
        "brightness": (None if raw.get("brightness") in (None, "")
                       else max(0, min(100, int(raw["brightness"])))),
        # A dark for a flat has to match the exposure the flat actually used,
        # which is not known until the flats have been taken.
        "followsFlat": (kind == "darkflat" if raw.get("followsFlat") is None
                        else bool(raw.get("followsFlat"))),
        "index": index,
    }


class RecipeStore:
    """Saved calibration recipes, written through on every change."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_root() / "calibration.json")
        self._lock = threading.RLock()
        self._recipes: list[dict[str, Any]] = []
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        raw = stored.get("recipes") if isinstance(stored, dict) else stored
        if not isinstance(raw, list):
            return
        cleaned: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            sets = [s for s in (_clean_set(entry, i)
                                for i, entry in enumerate(item.get("sets") or []))
                    if s is not None]
            cleaned.append({
                "id": _clean(item["id"], 32),
                "name": _clean(item.get("name")) or "Calibration",
                "sets": sets,
                "created": item.get("created") or time.time(),
                "updated": item.get("updated") or time.time(),
            })
        with self._lock:
            self._recipes = cleaned[:MAX_RECIPES]

    def save(self) -> None:
        with self._lock:
            payload = json.dumps({"recipes": self._recipes}, indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(payload, "utf-8")
        except OSError:
            pass

    # -- access ------------------------------------------------------------
    def listing(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._recipes))

    def get(self, recipe_id: str) -> dict[str, Any]:
        with self._lock:
            for recipe in self._recipes:
                if recipe["id"] == recipe_id:
                    return json.loads(json.dumps(recipe))
        raise DeviceError(f"no calibration recipe called {recipe_id!r}")

    def save_recipe(self, name: str, sets: list[dict[str, Any]],
                    recipe_id: str | None = None) -> dict[str, Any]:
        cleaned = [s for s in (_clean_set(entry, i) for i, entry in enumerate(sets))
                   if s is not None]
        if not cleaned:
            raise DeviceError("a recipe needs at least one set of frames")
        if len(cleaned) > MAX_SETS:
            raise DeviceError(f"a recipe holds at most {MAX_SETS} sets")

        with self._lock:
            if recipe_id:
                for recipe in self._recipes:
                    if recipe["id"] == recipe_id:
                        recipe["name"] = _clean(name) or recipe["name"]
                        recipe["sets"] = cleaned
                        recipe["updated"] = time.time()
                        result = json.loads(json.dumps(recipe))
                        break
                else:
                    raise DeviceError(f"no calibration recipe called {recipe_id!r}")
            else:
                if len(self._recipes) >= MAX_RECIPES:
                    raise DeviceError(f"there are already {MAX_RECIPES} recipes")
                result = {
                    "id": uuid.uuid4().hex[:10],
                    "name": _clean(name) or "Calibration",
                    "sets": cleaned,
                    "created": time.time(),
                    "updated": time.time(),
                }
                self._recipes.append(result)
                result = json.loads(json.dumps(result))
        self.save()
        return result

    def remove(self, recipe_id: str) -> None:
        with self._lock:
            before = len(self._recipes)
            self._recipes = [r for r in self._recipes if r["id"] != recipe_id]
            if len(self._recipes) == before:
                raise DeviceError(f"no calibration recipe called {recipe_id!r}")
        self.save()


class _Aborted(DeviceError):
    """The operator stopped the run."""


class CalibrationRunner:
    """Shoots a recipe on every telescope and stacks what comes back.

    The same object serves the Calibrate tab and the sequencer: the tab calls
    `start`, which runs it on a thread of its own, and the sequencer calls `run`
    on the thread it is already on so that a calibration task in the plan blocks
    the plan the way any other task does.
    """

    def __init__(self, rigs, config: Config, library: calibration.Library) -> None:
        self.rigs = rigs
        self.config = config
        self.library = library
        # Where a finished run is reported to somebody not in the room.
        self.notifier: Any = None
        # How the cameras are warmed once a run from the Calibrate tab ends:
        # the sequencer's own warm-down, handed in by the program. A run the
        # sequencer drives as a plan task leaves that to the sequence's end.
        self.warm_cameras: Any = None
        # Whether this run is its own thing (the Calibrate tab) rather than a
        # task inside a night, and whether it has moved the mount.
        self._standalone = False
        self._moved_mount = False

        self._thread: threading.Thread | None = None
        self._abort = threading.Event()
        self._lock = threading.RLock()
        self._state = "idle"
        self._message = ""
        self._error: str | None = None
        self._recipe: str = ""
        self._set_index = 0
        self._set_count = 0
        self._set_name = ""
        self._frame = 0
        self._frames = 0
        self._started: float | None = None
        self._finished: float | None = None
        self._results: list[dict[str, Any]] = []
        # Sets that have been shot but not yet stacked — the flats, which are
        # waiting for the darks that go with them.
        self._pending: list[dict[str, Any]] = []
        self._rig_state: dict[str, str] = {}

    # -- state -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "stopping": self._abort.is_set(),
                "state": self._state,
                "message": self._message,
                "error": self._error,
                "recipe": self._recipe,
                "set": self._set_index,
                "sets": self._set_count,
                "setName": self._set_name,
                "frame": self._frame,
                "frames": self._frames,
                "started": self._started,
                "finished": self._finished,
                # A set of flats waiting for its darks carries the whole job
                # under "pending" - the telescope object, the paths - which
                # is not something a status feed can carry. Left in, the
                # first finished flat set turned every status call into a
                # 500 and the page froze on whatever it last showed, which
                # read as "both cameras stuck downloading" for the rest of
                # the run. The record without the job is what the page needs.
                "results": [{key: value for key, value in item.items()
                             if key != "pending"} for item in self._results],
                "telescopes": dict(self._rig_state),
            }

    def _set(self, state: str, message: str = "") -> None:
        with self._lock:
            self._state = state
            self._message = message

    def _say(self, message: str, level: str = "info") -> None:
        self.rigs.log(message, level)
        self._set(self._state, message)

    def _check(self) -> None:
        if self._abort.is_set():
            raise _Aborted("the calibration run was stopped")

    # -- control -----------------------------------------------------------
    def _prepare(self, recipe: dict[str, Any],
                 rig_id: str | None) -> tuple[list[Any], list[dict[str, Any]]]:
        """The telescopes and the sets this run will use, or a clean refusal.

        Checked before a thread is started rather than inside it: "no camera is
        connected" is something to be told when the button is pressed, not
        something to find in the log afterwards.
        """
        rigs = ([self.rigs.get(rig_id)] if rig_id else self.rigs.imaging())
        rigs = [rig for rig in rigs
                if (camera := rig.manager.get("camera")) is not None and camera.connected]
        if not rigs:
            raise DeviceError("no camera is connected")

        # Normalised here as well as in the store: a recipe can arrive straight
        # off the tab without ever being saved, and everything below expects
        # every field to be present.
        sets = [s for s in (_clean_set(entry, index)
                            for index, entry in enumerate(recipe.get("sets") or []))
                if s is not None]
        if not sets:
            raise DeviceError(f"{recipe.get('name')} has no sets of frames in it")

        # Sky flats need the mount and the site, and finding that out at dusk
        # after the first two sets have been shot is finding it out too late.
        if any(s["frameType"] == "flat" and s["source"] == "sky" for s in sets):
            mount = self.rigs.master.manager.get("mount")
            if mount is None or not mount.connected:
                raise DeviceError(
                    "sky flats need the mount to point at the sky; connect it, "
                    "or set those sets to use the flat panel")
            self._site()
        return rigs, sets

    def start(self, recipe: dict[str, Any], rig_id: str | None = None) -> None:
        if self.running:
            raise DeviceError("a calibration run is already going")
        self._prepare(recipe, rig_id)
        self._abort.clear()
        self._thread = threading.Thread(
            target=self._background, args=(recipe, rig_id), daemon=True,
            name="calibration")
        self._thread.start()

    def abort(self) -> None:
        # Stopping something that has already stopped is not an error worth
        # refusing. It used to raise, which put "no calibration run is going" in
        # front of somebody whose run had died silently a minute earlier - an
        # answer about the wrong thing entirely.
        self._abort.set()
        for rig in self.rigs.all:
            with contextlib.suppress(Exception):
                rig.capture.abort()
        if self.running:
            self._say("Stopping the calibration run", "warn")
        else:
            self._say("Nothing was running; the cameras were stopped anyway",
                      "warn")

    def _background(self, recipe: dict[str, Any], rig_id: str | None) -> None:
        try:
            self.run(recipe, rig_id)
        except Exception as exc:                 # noqa: BLE001 - shown to the operator
            with self._lock:
                self._error = str(exc)
            # Logged, not merely stored. A run that fell over in its own thread
            # used to put the reason in `status().error` and nowhere else - and
            # the tab hides the progress box when nothing is running and nothing
            # was built, which is exactly the state a failed start leaves. The
            # operator saw a button that did nothing and a log with no trace of
            # it, which is the worst way for anything here to fail.
            self._set("failed", str(exc))
            self.rigs.log(f"Calibration run failed: {exc}", "error")
            self._tell("failure", f"Calibration run failed: {exc}")

    def _tell(self, event: str, subject: str, body: str = "") -> None:
        if self.notifier is None:
            return
        with contextlib.suppress(Exception):
            self.notifier.send(event, subject, body)

    # -- the run -----------------------------------------------------------
    def run(self, recipe: dict[str, Any], rig_id: str | None = None,
            should_abort=None, report=None) -> dict[str, Any]:
        """Shoot a whole recipe.  Blocks until it is done or stopped."""
        if should_abort is not None:
            # The sequencer owns the stop button when it is driving.
            self._abort.clear()

        def stopped() -> bool:
            return self._abort.is_set() or (should_abort is not None and should_abort())

        rigs, sets = self._prepare(recipe, rig_id)

        with self._lock:
            self._standalone = should_abort is None
            self._moved_mount = False
            self._error = None
            self._recipe = recipe.get("name") or "Calibration"
            self._set_count = len(sets)
            self._set_index = 0
            self._results = []
            self._pending = []
            self._started = time.time()
            self._finished = None
            self._rig_state = {}

        self._say(f"Calibration run: {self._recipe} on "
                  + ", ".join(rig.name for rig in rigs), "success")

        # Flats settle on an exposure per telescope and per filter; the darks
        # for those flats have to use the same numbers, which is the whole
        # reason they are shot in the same run.
        flat_exposures: dict[tuple[str, str], float] = {}
        # The cameras are borrowed, not taken: whatever the Image tab or a
        # half-finished sequence had them set to comes back afterwards.
        saved = {rig.id: rig.capture.snapshot_output() for rig in rigs}
        try:
            for index, spec in enumerate(sets, start=1):
                if stopped():
                    raise _Aborted("the calibration run was stopped")
                with self._lock:
                    self._set_index = index
                    self._set_name = _describe_set(spec)
                if report is not None:
                    report(f"{index}/{len(sets)}: {self._set_name}")
                self._run_set(rigs, spec, flat_exposures, stopped)
            self._build_pending(stopped, report)
        except _Aborted as exc:
            self._set("idle", str(exc))
            self._say("Calibration run stopped", "warn")
        finally:
            for rig in rigs:
                rig.capture.restore_output(saved[rig.id])
                with contextlib.suppress(Exception):
                    self._park_light(rig)
            with self._lock:
                self._finished = time.time()
                self._frame = self._frames = 0
                self._rig_state = {}
            self.library.forget()
            # A run from the Calibrate tab is the whole of what the rig is
            # doing, so it ends the way a night ends: the mount parked if the
            # sky flats moved it, and the cameras warmed. Inside a night the
            # sequencer owns both and does them when the night ends.
            if self._standalone:
                if self._moved_mount:
                    with contextlib.suppress(Exception):
                        self._park_mount()
                if self.warm_cameras is not None:
                    with contextlib.suppress(Exception):
                        self.warm_cameras()

        built = [r for r in self._results if r.get("master")]
        if not self._abort.is_set() and (should_abort is None or not should_abort()):
            self._set("idle", f"{len(built)} master frame(s) built")
            self._say(f"Calibration run finished: {len(built)} master frame(s)",
                      "success")
            failed = [r for r in self._results if not r.get("master")]
            lines = [f"{r['telescope']}: {r['set']}" for r in built]
            if failed:
                lines.append("Could not build: " + ", ".join(
                    f"{r['set']} ({r.get('detail') or 'failed'})" for r in failed))
            self._tell("calibration",
                       f"Calibration finished: {len(built)} master frame(s) built"
                       + (f", {len(failed)} failed" if failed else ""),
                       "\n".join(lines))
        return self.status()

    def _build_pending(self, stopped, report) -> None:
        """Stack the flats, now that the darks that go with them exist."""
        if not self._pending:
            return
        self._set("stacking", f"stacking {len(self._pending)} set(s) of flats")
        if report is not None:
            report(f"stacking {len(self._pending)} set(s) of flats")

        # One at a time rather than a thread each: the frames are already safely
        # on disk, so nothing is waiting on this, and there may be several sets
        # per telescope — one per filter — which a thread-per-telescope split
        # could not express anyway.
        for item in self._pending:
            job = item["pending"]
            rig = job["rig"]

            def note(text: str, rig=rig) -> None:
                with self._lock:
                    self._rig_state[rig.id] = text

            try:
                built = self._build(job, stopped, note)
            except _Aborted:
                raise
            except Exception as exc:              # noqa: BLE001 - reported per set
                self._say(f"{rig.name}: the flats could not be stacked — {exc}",
                          "error")
                built = {**{k: v for k, v in item.items() if k != "pending"},
                         "detail": str(exc)}
            note("")
            with self._lock:
                for index, existing in enumerate(self._results):
                    if existing is item:
                        self._results[index] = built
                        break
        self._pending = []

    # -- one set -----------------------------------------------------------
    def _run_set(self, rigs, spec: dict[str, Any],
                 flat_exposures: dict[tuple[str, str], float], stopped) -> None:
        kind = spec["frameType"]
        self._set("calibrating", _describe_set(spec))
        with self._lock:
            self._frame = 0
            self._frames = int(spec["count"])

        # Sky flats share one mount, so they cannot be a telescope each going
        # its own way; they run in rounds instead.
        if kind == "flat" and spec.get("source") == "sky":
            # One filter at a time: there is one mount and one patch of sky, so
            # the telescopes have to be on the same filter together. Twilight
            # rarely lasts for more than a couple of them, and each says for
            # itself whether it got its frames.
            for one in self._sky_filter_specs(spec):
                if stopped():
                    raise _Aborted("the calibration run was stopped")
                with self._lock:
                    self._set_name = _describe_set(one)
                try:
                    produced = self._sky_flat_set(rigs, one, flat_exposures,
                                                  stopped)
                except _Aborted:
                    raise
                except Exception as exc:          # noqa: BLE001 - reported, not fatal
                    self._say(f"{_describe_set(one)} failed — {exc}", "error")
                    produced = [{"telescope": "every telescope", "rig": "",
                                 "set": _describe_set(one), "frameType": kind,
                                 "master": None, "detail": str(exc)}]
                for item in produced:
                    if item.get("pending"):
                        self._pending.append(item)
                    with self._lock:
                        self._results.append(item)
            return

        outcome = _parallel(
            [(rig, lambda rig=rig: self._set_on_rig(rig, spec, flat_exposures,
                                                    stopped))
             for rig in rigs], f"cal-{kind}")

        for rig in rigs:
            message = outcome["errors"].get(rig.id)
            if message is None:
                continue
            # One telescope failing a set is not a reason to abandon the rest of
            # the library; it is a reason to say so loudly.
            self._say(f"{rig.name}: {_describe_set(spec)} failed — {message}",
                      "error")
            with self._lock:
                self._results.append({
                    "telescope": rig.name, "rig": rig.id, "set": _describe_set(spec),
                    "frameType": kind, "master": None, "detail": message,
                })
        for rig in rigs:
            # One entry per filter the set covered on that telescope: a set that
            # says "every filter" is one line on screen and several masters.
            for result in outcome["results"].get(rig.id) or []:
                if result.get("pending"):
                    self._pending.append(result)
                with self._lock:
                    self._results.append(result)

    # -- sky flats ---------------------------------------------------------
    def _site(self) -> tuple[float, float]:
        site = effective_site(self.config, self.rigs.master.manager)
        if site.get("latitude") is None or site.get("longitude") is None:
            raise DeviceError(
                "sky flats need the observing site, to work out where the "
                "zenith is; set it in Site & Optics")
        return float(site["latitude"]), float(site["longitude"])

    def flat_spot(self, latitude: float, longitude: float,
                  when: float | None = None) -> dict[str, Any]:
        """Where to point for sky flats, right now.

        Two choices, both horizon-relative and both therefore moving:

          * **the zenith**.  The traditional answer, and the one that needs no
            thought.  A German equatorial parked there is sitting on the
            meridian, which would matter if it were tracking — but flats do not
            need tracking, so it is not, and it never crosses.  The offset
            setting is there for anyone who leaves tracking on.
          * **the anti-solar point**, high up.  Measurably flatter still, being
            the part of the twilight sky furthest from both the solar gradient
            and the horizon glow.
        """
        settings = self.library.settings()
        when = when or time.time()
        lst = astro.local_sidereal_hours(
            longitude, _dt.datetime.fromtimestamp(when, _dt.timezone.utc))

        # Which way twilight is going decides which side of the meridian to
        # sit on: the side the sky is coming *from* stays usable longest.
        rising = (astro.sun_altitude(when + 300.0, latitude, longitude)
                  > astro.sun_altitude(when, latitude, longitude))
        half = "morning" if rising else "evening"

        if str(settings.get("skyFlatPointing") or "zenith").lower() == "antisolar":
            jd = astro.julian_from_timestamp(when)
            sun_ra, sun_dec = astro.sun_position(jd)
            _, sun_az = astro.ra_dec_to_alt_az(sun_ra, sun_dec, lst, latitude)
            azimuth = (sun_az + 180.0) % 360.0
            altitude = float(settings.get("skyFlatAltitude") or 80.0)
            label = "the anti-solar point"
        else:
            offset = float(settings.get("skyFlatMeridianOffset") or 0.0)
            # East before dawn is where the Sun is coming from, so in the
            # morning the darker side is west, and the other way round at dusk.
            azimuth = 270.0 if rising else 90.0
            altitude = 90.0 - abs(offset)
            label = ("the zenith" if not offset
                     else f"{offset:g}° {'west' if rising else 'east'} of the zenith")

        ra, dec = astro.alt_az_to_ra_dec(altitude, azimuth, lst, latitude)
        return {"ra": ra, "dec": dec, "altitude": altitude, "azimuth": azimuth,
                "label": label, "half": half}

    def _point_at_sky(self, mount, latitude: float, longitude: float,
                      dither: bool, check) -> dict[str, Any]:
        """Slew to the flat spot, with a nudge so the stars land elsewhere.

        Recomputed every round rather than tracked, which does three jobs at
        once: it puts the telescope back on a spot that is moving through the
        sky, it undoes the drift from not tracking, and it is the dither.
        """
        settings = self.library.settings()
        track = bool(settings.get("skyFlatTracking", False))
        spot = self.flat_spot(latitude, longitude)
        ra, dec = spot["ra"], spot["dec"]

        if dither:
            spread = float(settings.get("skyFlatDitherArcmin") or 2.0) / 60.0
            dec = max(-89.5, min(89.5, dec + random.uniform(-spread, spread)))
            # An RA offset is a smaller angle on the sky the nearer the pole,
            # so it has to be divided by cos(dec) to be the same nudge.
            widen = max(0.05, math.cos(math.radians(dec)))
            ra += random.uniform(-spread, spread) / 15.0 / widen

        # A parked mount will not move, and most drivers refuse a slew with
        # tracking off - which is how a mount comes out of park, and how it
        # is left between rounds here. Release, track, slew; tracking is
        # turned back off below once it is pointed.
        if getattr(mount, "at_park", False):
            with contextlib.suppress(Exception):
                mount.unpark()
                self._say("Mount released from park for the sky flats")
        with contextlib.suppress(Exception):
            if not mount.tracking:
                mount.set_tracking(True)
        mount.slew_to(astro.normalise_ra_hours(ra), dec)
        with self._lock:
            self._moved_mount = True
        deadline = time.monotonic() + 300.0
        time.sleep(0.5)
        while mount.slewing:
            check()
            if time.monotonic() > deadline:
                raise DeviceError("the mount did not reach the flat spot in time")
            time.sleep(0.3)

        # Asserted after the slew, not only before it: plenty of ASCOM drivers
        # switch tracking back on as a side effect of slewing.
        if not track:
            with contextlib.suppress(Exception):
                if mount.tracking:
                    mount.set_tracking(False)

        settle = float(settings.get("skyFlatSettleSeconds") or 2.0)
        end = time.monotonic() + settle
        while time.monotonic() < end:
            check()
            time.sleep(min(0.3, max(0.0, end - time.monotonic())))
        return spot

    def _sky_flat_set(self, rigs, spec: dict[str, Any],
                      flat_exposures: dict[tuple[str, str], float],
                      stopped) -> list[dict[str, Any]]:
        """Shoot a set of sky flats on every telescope at once.

        Unlike everything else here, this cannot be a telescope each going its
        own way: there is one mount, and moving it between frames is not an
        optimisation but the mechanism that gets the stars out of the flat.  So
        the run goes in rounds — point, expose on every telescope, point again
        — and each telescope carries its own exposure, because three telescopes
        on one mount see three different amounts of the same sky.

        Twilight fades by a factor of two every few minutes, so the exposure is
        re-derived from each frame, and a frame that lands too far off is
        deleted rather than stacked.  The set ends when it has the frames it was
        asked for, or when the sky leaves the range the camera can follow — the
        second is normal, and is what "the twilight ran out" looks like.
        """
        settings = self.library.settings()
        latitude, longitude = self._site()
        mount = self.rigs.master.manager.get("mount")
        if mount is None or not mount.connected:
            raise DeviceError(
                "sky flats need the mount to point at the sky; connect it, or "
                "set this set to use the flat panel instead")

        def check() -> None:
            if stopped():
                raise _Aborted("the calibration run was stopped")

        wanted = int(spec["count"])
        lowest = float(settings.get("flatMinExposure") or 0.000032)
        highest = float(settings.get("flatMaxExposure") or 30.0)
        aim = float(settings.get("flatTargetAdu") or 25000.0)
        accept = max(0.02, float(settings.get("skyFlatAcceptPercent") or 40.0) / 100.0)

        # Every telescope: cover open, panel out, filter on, output redirected.
        outcome = _parallel([(rig, lambda rig=rig: self._begin_sky(rig, spec))
                             for rig in rigs], "cal-sky-open")
        results: list[dict[str, Any]] = []
        active = []
        for rig in rigs:
            if rig.id in outcome["errors"]:
                self._say(f"{rig.name}: {_describe_set(spec)} failed — "
                          f"{outcome['errors'][rig.id]}", "error")
                results.append({"telescope": rig.name, "rig": rig.id,
                                "set": _describe_set(spec), "frameType": "flat",
                                "master": None,
                                "detail": outcome["errors"][rig.id]})
            else:
                active.append(rig)
        if not active:
            return results

        # Flats do not need tracking — nothing has to stay still for a fraction
        # of a second — and a mount that is not tracking cannot cross the
        # meridian, so there is no flip to work around. Whatever the mount was
        # doing before is put back at the end, however the set ends.
        track = bool(settings.get("skyFlatTracking", False))
        was_tracking = None
        if not track:
            with contextlib.suppress(Exception):
                was_tracking = bool(mount.tracking)
            if was_tracking:
                with contextlib.suppress(Exception):
                    mount.set_tracking(False)
                    self._say("Tracking off for the sky flats")

        try:
            return self._sky_flat_rounds(rigs, active, results, spec, mount,
                                         latitude, longitude, settings, stopped,
                                         check, wanted, lowest, highest, aim,
                                         accept, flat_exposures)
        finally:
            if was_tracking:
                with contextlib.suppress(Exception):
                    mount.set_tracking(True)
                    self._say("Tracking back on")

    def _park_mount(self) -> None:
        """Park the mount after the sky flats moved it, and check that it did.

        Only after a run from the Calibrate tab: the flats pointed the
        telescope at the zenith and left it there, and a telescope left
        pointing at the sky with nobody watching is the thing every end of
        a night exists to prevent. Waits for the driver to report it parked
        rather than trusting the command.
        """
        mount = self.rigs.master.manager.get("mount")
        if mount is None or not mount.connected or not getattr(mount, "can_park", True):
            return
        if getattr(mount, "at_park", False):
            return
        timeout = float(self.config.get("sequencer", "parkTimeoutSeconds", 300.0) or 300.0)
        self._set("parking", "parking the mount")
        self._say("Sky flats done: parking the mount")
        if getattr(mount, "slewing", False):
            with contextlib.suppress(Exception):
                mount.abort_slew()
            settle_until = time.monotonic() + 15.0
            while getattr(mount, "slewing", False) and time.monotonic() < settle_until:
                time.sleep(0.25)
        mount.park()
        deadline = time.monotonic() + timeout
        stopped_at: float | None = None
        while True:
            if getattr(mount, "at_park", False):
                self._say("Mount parked", "success")
                return
            now = time.monotonic()
            if now >= deadline:
                break
            if getattr(mount, "slewing", False):
                stopped_at = None
            elif stopped_at is None:
                stopped_at = now
            elif now - stopped_at >= 5.0:
                break
            time.sleep(0.25)
        self._say("The mount does not report being parked after the sky flats - "
                  "check it", "error")
        self._tell("failure", "The mount did not park after the sky flats",
                   "The calibration run finished but the mount does not report "
                   "being parked. Check it before leaving the telescope.")

    def _sky_flat_rounds(self, rigs, active, results, spec, mount, latitude,
                         longitude, settings, stopped, check, wanted, lowest,
                         highest, aim, accept, flat_exposures
                         ) -> list[dict[str, Any]]:
        """The round loop, split out so tracking is always put back."""
        spot = self._point_at_sky(mount, latitude, longitude, False, check)
        self._say(f"Sky flats: pointing at {spot['label']} — "
                  f"{spot['altitude']:.0f}° up, in the {spot['half']} twilight")

        state = {rig.id: {"exposure": max(lowest, min(highest,
                                                      float(spec["exposure"]) or 1.0)),
                          "paths": [], "temps": [], "levels": [], "rejected": 0,
                          "shape": (0, 0), "done": False, "reason": "",
                          "waiting": "", "pedestal": self._pedestal(rig, spec)}
                 for rig in active}

        # A wall-clock budget rather than a round count, because most of what
        # this does may be waiting: a fast telescope saturates at its shortest
        # exposure while the sky is still bright, and in the evening the fix for
        # that is a couple of minutes rather than an error.
        budget = float(settings.get("skyFlatMaxMinutes") or 20.0) * 60.0
        poll = float(settings.get("skyFlatPollSeconds") or 5.0)
        deadline = time.monotonic() + budget
        round_number = 0
        announced: set[str] = set()

        while time.monotonic() < deadline:
            check()
            running = [rig for rig in active if not state[rig.id]["done"]]
            if not running:
                break
            round_number += 1
            with self._lock:
                self._frame = min(wanted, max(len(state[r.id]["paths"])
                                              for r in active))
                self._frames = wanted

            # Moved between every frame, and never while one is being taken:
            # the whole point is that the stars land somewhere different.
            self._point_at_sky(mount, latitude, longitude, round_number > 1, check)

            frame_outcome = _parallel(
                [(rig, lambda rig=rig: self._sky_flat_frame(
                    rig, state[rig.id], spec, aim, accept, lowest, highest,
                    wanted, spot["half"]))
                 for rig in running], "cal-sky")
            for rig in running:
                message = frame_outcome["errors"].get(rig.id)
                if message is not None:
                    state[rig.id]["done"] = True
                    state[rig.id]["reason"] = message
                    self._say(f"{rig.name}: sky flats stopped — {message}", "warn")

            # Said once, not once a round: a telescope waiting five minutes for
            # the sky to fade should not fill the log while it does.
            for rig in running:
                note = state[rig.id]["waiting"]
                if note and rig.id not in announced:
                    announced.add(rig.id)
                    self._say(f"{rig.name}: {note}")
                elif not note:
                    announced.discard(rig.id)

            if any(state[rig.id]["waiting"] for rig in running):
                self._set("calibrating",
                          f"{_describe_set(spec)} — waiting for the sky")
                end = time.monotonic() + poll
                while time.monotonic() < end:
                    check()
                    time.sleep(min(0.3, max(0.0, end - time.monotonic())))

        for rig in active:
            st = state[rig.id]
            if not st["done"] and st["waiting"]:
                window = (f"{budget / 60:.0f}-minute" if budget >= 60
                          else f"{budget:.0f}-second")
                st["reason"] = f"{st['waiting']}, and the {window} window ran out"

        camera_names = {rig.id: rig.manager.require("camera").name for rig in active}
        for rig in active:
            st = state[rig.id]
            kept = len(st["paths"])
            self._say(f"{rig.name}: {kept} sky flat(s) kept, {st['rejected']} "
                      f"thrown away for level"
                      + (f" — {st['reason']}" if st["reason"] else ""),
                      "success" if kept else "warn")
            if not kept:
                results.append({
                    "telescope": rig.name, "rig": rig.id,
                    "set": _describe_set(spec), "frameType": "flat", "master": None,
                    "detail": st["reason"] or "no frame came out at a usable level"})
                continue

            exposures = sorted(st["exposures"]) if st.get("exposures") else []
            middle = (exposures[len(exposures) // 2] if exposures
                      else st["exposure"])
            kept_levels = st.get("kept") or st["levels"]
            average = sum(kept_levels) / len(kept_levels)
            # A dark for these flats has no single exposure to match, so the
            # middle one is what a `follows the flats` set will use.  It is not
            # what the sky flats themselves are corrected by: that is the bias,
            # which is right for exposures this short and is the only thing that
            # applies to all of them at once.
            flat_exposures[(rig.id, spec["filter"].lower())] = middle

            results.append({
                "pending": {
                    "rig": rig, "kind": "flat", "spec": spec,
                    "label": _describe_set(spec), "paths": st["paths"],
                    "exposure": middle, "measured": average,
                    "shape": st["shape"],
                    "sky": True,
                    "meta": {
                        "exposure": middle, "binning": spec["binning"],
                        "gain": _resolve(spec["gain"],
                                         rig.manager.require("camera").gain),
                        "offset": _resolve(spec["offset"],
                                           rig.manager.require("camera").offset),
                        "temperature": (round(sum(st["temps"]) / len(st["temps"]), 2)
                                        if st["temps"] else None),
                        "filter": spec["filter"], "telescope": rig.name,
                        "camera": camera_names[rig.id] or "",
                    },
                },
                "telescope": rig.name, "rig": rig.id, "set": _describe_set(spec),
                "frameType": "flat", "master": None, "exposure": middle,
                "measuredAdu": average,
                "detail": "waiting to be stacked",
            })
        return results

    def _begin_sky(self, rig, spec: dict[str, Any]) -> None:
        """Get one telescope ready to look at the twilight sky."""
        camera = rig.manager.require("camera")
        camera.set_settings(binning=spec["binning"], gain=spec["gain"],
                            offset=spec["offset"])
        if spec["filter"]:
            self._select_filter(rig, spec["filter"], lambda: None)

        # The sky is the light source, so the panel must be out and the cover
        # open — the same thing a light frame needs, and the same code does it.
        rig.capture.open_light_path()

        night = rig.capture.night_name()
        folder = self.library.subs_dir / night / clean_target(rig.name) / "skyflat"
        rig.capture.calibration_context = ""
        rig.capture.set_output(save=True, directory=str(folder),
                               target="cal", object_name="", panel="")

    def _sky_flat_frame(self, rig, st: dict[str, Any], spec: dict[str, Any],
                        aim: float, accept: float, lowest: float, highest: float,
                        wanted: int, half: str) -> None:
        """One sky flat: take it, judge it, and work out the next exposure."""
        exposure = st["exposure"]
        with self._lock:
            self._rig_state[rig.id] = (
                st["waiting"] or f"sky flat {len(st['paths']) + 1}/{wanted} "
                                 f"at {exposure:.2f}s")
        record = rig.capture.capture_blocking(exposure, frame_type="flat")
        # Signal, not signal plus the camera's offset: at the exposures a fast
        # telescope wants in a bright sky the two are comparable.
        level = max(0.0, float(record.stats.get("median") or 0.0)
                    - st.get("pedestal", 0.0))
        st["levels"].append(level)

        if abs(level - aim) <= aim * accept and record.path:
            st["paths"].append(Path(record.path))
            st.setdefault("exposures", []).append(exposure)
            # Only the frames that were kept: the first frame of a set is at
            # whatever exposure the recipe guessed and is usually thrown away,
            # and averaging that into the headline would describe the guess
            # rather than the flats.
            st.setdefault("kept", []).append(level)
            if record.ccd_temperature is not None:
                st["temps"].append(float(record.ccd_temperature))
            st["shape"] = (record.width, record.height)
        else:
            # Too dark is mostly read noise; too bright is off the linear part
            # of the sensor. Neither belongs in a flat, and keeping the file
            # would only invite it into a later restack.
            st["rejected"] += 1
            if record.path:
                with contextlib.suppress(OSError):
                    Path(record.path).unlink()

        if len(st["paths"]) >= wanted:
            st["done"] = True
            return

        # The sensor is linear, so what the last frame gave says what the next
        # exposure should be. It lags the sky by one frame, which is what the
        # acceptance band is for.
        following = highest if level <= 1.0 else exposure * aim / level
        following = min(highest, max(lowest, following))
        st["exposure"] = following

        # Out of range at one end or the other.  Whether that is fatal depends
        # entirely on which way the sky is going: in the evening it is darkening,
        # so "too bright" fixes itself in a minute or two and "too dark" never
        # will.  Before dawn it is exactly the other way round.  Giving up on
        # the recoverable one is how a fast telescope ends the night with no
        # flats while the sky outside was perfect.
        at_ceiling = following >= highest - 1e-6 and level < aim * (1 - accept)
        at_floor = following <= lowest + 1e-6 and level > aim * (1 + accept)
        darkening = half != "morning"
        st["waiting"] = ""

        if at_ceiling and darkening:
            st["done"] = True
            st["reason"] = (f"the sky is too dark for a flat even at {highest:g}s"
                            + (" — the twilight ran out" if st["paths"] else ""))
        elif at_ceiling:
            st["waiting"] = (f"too dark at {highest:g}s; waiting for dawn to "
                             "brighten")
        elif at_floor and not darkening:
            st["done"] = True
            st["reason"] = (f"the sky is already too bright at {lowest:g}s — "
                            "dawn got ahead of it")
        elif at_floor:
            st["waiting"] = (f"saturating at {lowest:g}s; waiting for the "
                             "twilight to fade")

    def _sky_filter_specs(self, spec: dict[str, Any]) -> list[dict[str, Any]]:
        """A sky-flat set, split into one per filter if it covers them all.

        The master's wheel decides the sequence: there is one mount pointed at
        one patch of sky, so every telescope has to be on the same filter at
        the same moment, and a telescope whose wheel has no such filter says so
        for itself.
        """
        if not spec.get("allFilters"):
            return [spec]
        names = filter_names(self.rigs.master, self.config)
        if not names:
            return [{**spec, "allFilters": False}]
        return [{**spec, "filter": name, "allFilters": False} for name in names]

    def filters_for(self, spec: dict[str, Any], rig) -> list[str]:
        """Which filters one set covers on one telescope.

        A set that says "every filter" is expanded here rather than written out
        as a row per filter, which keeps a seven-filter wheel to one line on
        screen instead of fourteen — and expands per *telescope*, so two scopes
        carrying different wheels are each covered correctly by the same set.
        """
        if not spec.get("allFilters"):
            return [spec["filter"]]
        names = filter_names(rig, self.config)
        return names or [spec["filter"] or ""]

    def _set_on_rig(self, rig, spec: dict[str, Any],
                    flat_exposures: dict[tuple[str, str], float],
                    stopped) -> list[dict[str, Any]]:
        kind = spec["frameType"]
        camera = rig.manager.require("camera")

        def check() -> None:
            if stopped():
                raise _Aborted("the calibration run was stopped")

        def note(text: str) -> None:
            with self._lock:
                self._rig_state[rig.id] = text

        camera.set_settings(binning=spec["binning"], gain=spec["gain"],
                            offset=spec["offset"])

        # Where the subs go: the library, not tonight's target folder.  The
        # target is set to "cal" rather than left blank so the frames are
        # numbered — the unnamed form falls back to a timestamp to the second,
        # and a run of bias frames goes faster than that.
        night = rig.capture.night_name()
        folder = self.library.subs_dir / night / clean_target(rig.name) / kind
        rig.capture.calibration_context = ""
        rig.capture.set_output(save=True, directory=str(folder),
                               target="cal", object_name="", panel="")

        wanted = (self.filters_for(spec, rig)
                  if kind in calibration.FILTERED_TYPES else [""])
        with self._lock:
            self._frames = int(spec["count"]) * len(wanted)

        produced: list[dict[str, Any]] = []
        for position, name in enumerate(wanted):
            check()
            one = {**spec, "filter": name, "allFilters": False}
            label = _describe_set(one)
            if len(wanted) > 1:
                label = f"{label} ({position + 1} of {len(wanted)})"
            note(label)
            if name:
                self._select_filter(rig, name, check)
            produced.append(self._one_filter(rig, one, label, camera,
                                             flat_exposures, stopped, check,
                                             note, position))
        note("")
        return produced

    def _one_filter(self, rig, spec: dict[str, Any], label: str, camera,
                    flat_exposures: dict[tuple[str, str], float], stopped,
                    check, note, position: int) -> dict[str, Any]:
        """One set, for one filter, on one telescope."""
        kind = spec["frameType"]
        exposure = float(spec["exposure"])
        measured = None
        if kind == "flat":
            self._prepare_light(rig, spec, check)
            if spec["autoExposure"]:
                exposure, measured = self._find_flat_exposure(rig, spec, check, note)
            flat_exposures[(rig.id, spec["filter"].lower())] = exposure
        elif kind == "darkflat" and spec["followsFlat"]:
            found = flat_exposures.get((rig.id, spec["filter"].lower()))
            if found is None:
                raise DeviceError(
                    f"there is no flat exposure to match: shoot the "
                    f"{spec['filter'] or 'unfiltered'} flats in the same run, "
                    "before this set")
            exposure = found
            self._darken(rig, check)
        else:
            self._darken(rig, check)

        if kind == "bias":
            exposure = 0.0

        paths: list[Path] = []
        temperatures: list[float] = []
        shape = (0, 0)
        base = position * int(spec["count"])
        for number in range(1, int(spec["count"]) + 1):
            check()
            with self._lock:
                self._frame = base + number
            note(f"{label}  {number}/{spec['count']}")
            record = rig.capture.capture_blocking(exposure, frame_type=kind)
            if record.path:
                paths.append(Path(record.path))
            if record.ccd_temperature is not None:
                temperatures.append(float(record.ccd_temperature))
            shape = (record.width, record.height)

        if not paths:
            raise DeviceError("no frames were saved; is saving switched off?")

        job = {
            "rig": rig, "kind": kind, "spec": spec, "label": label,
            "paths": paths, "exposure": exposure, "measured": measured,
            "shape": shape,
            "meta": {
                "exposure": exposure,
                "binning": spec["binning"],
                "gain": _resolve(spec["gain"], camera.gain),
                "offset": _resolve(spec["offset"], camera.offset),
                "temperature": (round(sum(temperatures) / len(temperatures), 2)
                                if temperatures else None),
                "filter": (spec["filter"] if kind in calibration.FILTERED_TYPES
                           else ""),
                "telescope": rig.name,
                "camera": camera.name or "",
            },
        }

        # Flats are stacked at the end of the run, not here.  A flat is only a
        # flat once its own dark is off it — and the dark for the flats cannot
        # be shot until the flats have settled on an exposure, so at this point
        # in the run it does not exist yet.  Everything else has no such
        # dependency and is built straight away, so an aborted run still leaves
        # the masters it had finished.
        if kind == "flat":
            return {"pending": job,
                    "telescope": rig.name, "rig": rig.id, "set": label,
                    "frameType": kind, "master": None, "exposure": exposure,
                    "measuredAdu": measured,
                    "detail": "waiting for the darks for these flats"}
        return self._build(job, stopped, note)

    def _build(self, job: dict[str, Any], stopped, note) -> dict[str, Any]:
        """Stack one shot set into a master and put it in the library."""
        rig, kind, spec = job["rig"], job["kind"], job["spec"]
        settings = self.library.settings()
        paths = job["paths"]

        note(f"{job['label']}: stacking {len(paths)} frames")
        self._set("stacking", f"{rig.name}: stacking {len(paths)} {kind} frames")

        subtract = None
        removed = None
        sky = bool(job.get("sky"))
        if kind == "flat":
            want = {**job["meta"], "width": job["shape"][0],
                    "height": job["shape"][1]}
            # A panel flat is one exposure, so the dark for it is the right
            # correction.  Sky flats are a different exposure every frame, so
            # no single dark describes them and the bias is what applies to all
            # of them at once — which is fine, because they are seconds long.
            candidates = ("bias",) if sky else ("darkflat", "bias")
            why = ""
            for candidate in candidates:
                found, why = self.library.match(candidate, want)
                if found is not None:
                    subtract = self.library.frame(found).astype("float32")
                    removed = candidate
                    break
            if subtract is None:
                self._say(f"{rig.name}: the flats are not dark-subtracted — {why}",
                          "warn")

        frame, info = calibration.stack(
            [str(p) for p in paths],
            method=str(settings.get("stackMethod") or "sigma"),
            sigma_low=float(settings.get("sigmaLow") or 3.0),
            sigma_high=float(settings.get("sigmaHigh") or 3.0),
            subtract=subtract,
            # Sky flats are taken through a fading sky, so they have to be
            # scaled to a common level before they can be combined at all —
            # and it is that common level which lets the clipping recognise a
            # star, which is in a different place in every frame.
            normalise=sky,
            should_abort=stopped)

        master = self.library.store(kind, frame, job["meta"], info)
        self._say(f"{rig.name}: built {Path(master['path']).name} from "
                  f"{info['frames']} frames ({info['method']})", "success")

        if not settings.get("keepSubs", True):
            for path in paths:
                with contextlib.suppress(OSError):
                    path.unlink()

        return {
            "telescope": rig.name, "rig": rig.id, "set": job["label"],
            "frameType": kind, "master": master, "info": info,
            "exposure": job["exposure"], "measuredAdu": job["measured"],
            "darkSubtracted": removed if kind == "flat" else None,
            "source": ("sky" if sky else "panel") if kind == "flat" else None,
        }

    # -- the light path ----------------------------------------------------
    def _select_filter(self, rig, name: str, check) -> None:
        wheel = rig.manager.get("filterwheel")
        if wheel is None or not wheel.connected:
            raise DeviceError(f"there is no filter wheel to put on {name}")
        names = list(wheel.names or [])
        if name not in names:
            raise DeviceError(f"the wheel has no filter called {name}")
        if names.index(name) == wheel.position:
            return
        wheel.set_position(names.index(name))
        deadline = time.monotonic() + 120
        while wheel.moving and time.monotonic() < deadline:
            check()
            time.sleep(0.2)

    def _prepare_light(self, rig, spec: dict[str, Any], check) -> None:
        """Shut the cover and light the panel.

        **Shut, not open.**  On every common device the light *is* the cover —
        an Alnitak Flip-Flat, a FlatMan on a flip mount, a Deep Sky Dad — and
        the illuminated face only points down the tube when the lid is closed.
        This used to open it, which put the panel face-away over an open
        aperture: no even illumination, and on the drivers that refuse
        `CalibratorOn` with the cover open — which the ASCOM specification
        explicitly permits, and Alnitak's do — no light at all.  The
        auto-exposure then chased a dark frame to the top of its range and gave
        up, so one wrong direction here looked like three separate faults.

        The cover is opened again for light frames by `Capture.open_light_path`,
        which is where that belongs.
        """
        panel = rig.manager.get("flatpanel")
        if panel is None or not panel.connected:
            self._say(f"{rig.name}: no flat panel is connected — the flats will be "
                      "of whatever the telescope is pointed at", "warn")
            return

        if panel.has_cover and panel.cover_state != "closed":
            self._say(f"{rig.name}: closing the cover for the flats")
            panel.close_cover()
            self._wait_for_cover(panel, "closed", check)

        settings = self.library.settings()
        # A starting point, not a decision: with automatic brightness on, the
        # exposure search moves the panel from here to wherever this filter
        # needs it.
        brightness = self._panel_percent(rig, spec, settings)
        self._set_panel(rig, brightness)

        # Believe the panel rather than the command. A driver that quietly
        # declined is the failure this whole routine has to be able to report:
        # the frames still arrive, they are just dark, and dark flats that call
        # themselves flats poison a calibration library for a season.
        # Given time to come on. A panel reports "not ready" while it warms
        # or ramps - the Platform simulator for a few seconds, an Alnitak for
        # longer - and one look after one second called every such panel
        # broken and refused the flats.
        lit = True
        deadline = time.monotonic() + 20.0
        while True:
            check()
            time.sleep(0.5)
            with contextlib.suppress(Exception):
                lit = bool(panel.light_on)
            if lit or time.monotonic() > deadline:
                break
        if not lit:
            raise DeviceError(
                f"{rig.name}: the flat panel did not come on. Some drivers "
                "refuse to light the panel unless the cover is shut — it reads "
                f"{panel.cover_state!r} here.")
        self._say(f"{rig.name}: flat panel on at {brightness}%")

    def _darken(self, rig, check) -> None:
        """Close the cover and put the light out before a dark or a bias."""
        panel = rig.manager.get("flatpanel")
        if panel is None or not panel.connected:
            self._say(f"{rig.name}: there is no cover to close — make sure the "
                      "telescope is capped before the darks", "warn")
            return
        with contextlib.suppress(Exception):
            panel.turn_off()
        if panel.has_cover and panel.cover_state != "closed":
            panel.close_cover()
            self._wait_for_cover(panel, "closed", check)
        time.sleep(1.0)

    def _park_light(self, rig) -> None:
        """Leave the panel out at the end of a run, however it ended.

        The cover is deliberately left as it is.  A run that finished on the
        darks ends with it shut, and shut is the right state for a telescope
        nobody is using — dust settles on the corrector either way.  It opens
        again by itself before the next light frame, so nothing is waiting on
        the operator to remember.
        """
        panel = rig.manager.get("flatpanel")
        if panel is None or not panel.connected:
            return
        with contextlib.suppress(Exception):
            panel.turn_off()
        with contextlib.suppress(Exception):
            if panel.has_cover and panel.cover_state == "closed":
                self._say(f"{rig.name}: the cover is closed; it will open again "
                          "before the next light frame")

    def _wait_for_cover(self, panel, wanted: str, check,
                        timeout: float = 180.0) -> None:
        deadline = time.monotonic() + timeout
        while panel.cover_state != wanted:
            check()
            if time.monotonic() > deadline:
                raise DeviceError(f"the cover did not {wanted} in time")
            time.sleep(0.5)

    def _pedestal(self, rig, spec: dict[str, Any]) -> float:
        """The camera's offset, so a flat's target level means what it says.

        A frame's median is signal *plus* the camera's electronic offset.  At
        the exposures a fast astrograph wants in daylight — a few hundred
        microseconds — that offset is a real fraction of the reading, and
        aiming at 25000 ADU without allowing for it aims at rather less signal
        than intended.  Taken from the master bias when there is one, and
        treated as zero when there is not, which is the old behaviour.
        """
        camera = rig.manager.get("camera")
        if camera is None:
            return 0.0
        binning = max(1, int(spec.get("binning") or 1))
        want = {
            "exposure": 0.0, "binning": binning,
            "gain": _resolve(spec.get("gain"), camera.gain),
            "offset": _resolve(spec.get("offset"), camera.offset),
            "temperature": camera.temperature,
            "telescope": rig.name, "camera": camera.name or "",
        }
        if camera.sensor_width:
            want["width"] = camera.sensor_width // binning
            want["height"] = camera.sensor_height // binning
        found, _ = self.library.match("bias", want)
        if found is None:
            return 0.0
        return float(np.median(self.library.frame(found)))

    def _find_flat_exposure(self, rig, spec: dict[str, Any], check,
                            note) -> tuple[float, float]:
        """Work out what panel brightness and exposure put the flat on target.

        Measured rather than guessed: what reaches the sensor depends on the
        panel, the filter and the optics, and it is different on every telescope
        on the mount.

        **Two knobs, not one.**  Searching the exposure alone fails whenever the
        panel is too bright for the shortest exposure the camera can take —
        which is not an exotic case, it is luminance.  L passes several times the
        light of any narrowband filter, so a panel set for Ha saturates L before
        the shutter can close, and the search then walks to its floor and gives
        up with nothing to show for it.  Dimming the panel is the move a person
        would make, so it is the move this makes.

        Both knobs are very nearly linear in signal, so one measurement gives
        the throughput of the whole path — panel, filter, optics, sensor — and
        the pair that lands on target follows from it.  The exposure is aimed at
        a comfortable few seconds and the brightness set to suit, rather than
        the other way round: a flat of a few milliseconds is at the mercy of
        panel flicker and shutter travel, and neither averages out in a frame
        that short.
        """
        settings = self.library.settings()
        target = float(settings.get("flatTargetAdu") or 25000.0)
        tolerance = max(0.005, float(settings.get("flatTolerancePercent") or 8.0) / 100.0)
        lowest = float(settings.get("flatMinExposure") or 0.000032)
        highest = float(settings.get("flatMaxExposure") or 30.0)
        prefer = min(highest, max(lowest,
                                  float(settings.get("flatPreferredExposure") or 3.0)))
        pedestal = self._pedestal(rig, spec)

        panel = rig.manager.get("flatpanel")
        dimmable = (panel is not None and panel.connected
                    and bool(settings.get("flatAutoBrightness", True))
                    and spec.get("brightness") is None)
        floor = max(1, int(settings.get("flatMinBrightness") or 5))
        brightness = self._panel_percent(rig, spec, settings)

        exposure = float(spec["exposure"]) or prefer
        exposure = min(highest, max(lowest, exposure))

        # These are test frames, not data: they must not land in the library.
        rig.capture.save_enabled = False
        best = (exposure, brightness, None, float("inf"))
        try:
            for attempt in range(1, FLAT_ATTEMPTS + 1):
                check()
                note(f"finding the flat exposure ({attempt}) at {exposure:g}s"
                     + (f", panel {brightness}%" if dimmable else ""))
                record = rig.capture.capture_blocking(exposure, frame_type="flat")
                stats = record.stats or {}
                # Signal, not signal plus the camera's offset.
                level = max(0.0, float(stats.get("median") or 0.0) - pedestal)
                clipped = float(stats.get("saturatedPercent") or 0.0) > 0.5

                gap = abs(level - target)
                if gap < best[3] and not clipped:
                    best = (exposure, brightness, level, gap)
                self._say(f"{rig.name}: {exposure:g}s"
                          + (f" at {brightness}%" if dimmable else "")
                          + f" gives {level:.0f} ADU (aiming for {target:.0f})"
                          + (" — saturated" if clipped else ""))
                if gap <= target * tolerance and not clipped:
                    return exposure, level

                # How much signal this path makes per percent of panel per
                # second. A clipped frame has no usable level in it — the true
                # one is somewhere above what was read — so the response is to
                # back off hard and measure again rather than to compute from a
                # number that is wrong.
                if clipped or level <= 1.0:
                    if clipped:
                        if dimmable and brightness > floor:
                            brightness = max(floor, int(brightness / 4) or floor)
                        else:
                            exposure = max(lowest, exposure / 8.0)
                    else:
                        if exposure < highest:
                            exposure = highest
                        elif dimmable and brightness < 100:
                            brightness = 100
                        else:
                            break
                    if dimmable:
                        self._set_panel(rig, brightness)
                    continue

                throughput = level / (max(1, brightness) * exposure)
                wanted = target / throughput          # percent x seconds
                if dimmable:
                    # Aim for a comfortable exposure and dim to suit; only leave
                    # that band when the panel runs out of range.
                    exposure = prefer
                    brightness = int(round(wanted / exposure))
                    if brightness > 100:
                        brightness = 100
                        exposure = wanted / brightness
                    elif brightness < floor:
                        brightness = floor
                        exposure = wanted / brightness
                else:
                    exposure = wanted / max(1, brightness)

                exposure = min(highest, max(lowest, exposure))
                if dimmable:
                    self._set_panel(rig, brightness)
        finally:
            rig.capture.save_enabled = True

        chosen, level_at, level, _ = best
        if dimmable and level_at != brightness:
            self._set_panel(rig, level_at)
        if level is None:
            self._say(f"{rig.name}: no exposure between {lowest:g}s and "
                      f"{highest:g}s reaches {target:.0f} ADU"
                      + (", even with the panel at both ends of its range"
                         if dimmable else " at this panel brightness"), "warn")
        else:
            self._say(f"{rig.name}: settling for {chosen:g}s"
                      + (f" at {level_at}%" if dimmable else "")
                      + f" giving {level:.0f} ADU; {target:.0f} is out of reach",
                      "warn")
        return chosen, (level or 0.0)

    def _panel_percent(self, rig, spec: dict[str, Any],
                       settings: dict[str, Any]) -> int:
        """What the panel is set to now, as a percentage."""
        asked = spec.get("brightness")
        if asked is not None:
            return int(asked)
        return int(settings.get("flatPanelBrightness") or 50)

    def _set_panel(self, rig, percent: int) -> None:
        """Move the panel to a percentage of its range, and let it settle."""
        panel = rig.manager.get("flatpanel")
        if panel is None or not panel.connected:
            return
        level = int(round(panel.max_brightness * max(1, percent) / 100.0))
        panel.turn_on(max(1, level))
        # LED panels are not instant and the driver returns before the light
        # has, so a frame taken immediately reads the old brightness.
        time.sleep(0.4)


def _resolve(asked: Any, actual: Any) -> Any:
    """What the camera ended up at when the recipe did not insist on a value."""
    return actual if asked is None else asked


def _describe_set(spec: dict[str, Any]) -> str:
    kind = spec["frameType"]
    bits = [f"{spec['count']}x"]
    if kind == "bias":
        bits.append("bias")
    elif kind == "flat" and spec.get("source") == "sky":
        bits.append("sky flat")
    else:
        bits.append(kind)
        if spec["frameType"] == "flat" and spec["autoExposure"]:
            bits.append("(auto)")
        elif spec.get("followsFlat") and kind == "darkflat":
            bits.append("(matching the flats)")
        elif spec["exposure"]:
            bits.append(f"{spec['exposure']:g}s")
    if spec.get("allFilters"):
        bits.append("every filter")
    elif spec["filter"]:
        bits.append(spec["filter"])
    if spec["binning"] > 1:
        bits.append(f"bin{spec['binning']}")
    return " ".join(bits)


def _parallel(jobs: list[tuple[Any, Any]], name: str) -> dict[str, Any]:
    """One thread per telescope, the way the sequencer shoots a slot."""
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    threads: list[threading.Thread] = []

    def worker(rig, job) -> None:
        try:
            results[rig.id] = job()
        except Exception as exc:                 # noqa: BLE001 - reported per rig
            errors[rig.id] = str(exc)

    for rig, job in jobs:
        thread = threading.Thread(target=worker, args=(rig, job), daemon=True,
                                  name=f"{name}-{rig.id}")
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    return {"results": results, "errors": errors}


def filter_names(rig, config: Config) -> list[str]:
    """What is in this telescope's wheel: asked of it, or as it was described.

    The wheel can only be asked when it is connected, and a calibration recipe
    is written in the afternoon with everything switched off — so the names
    typed into Equipment have to serve, and the observatory-wide list after
    those.  Deliberately the same order of preference the rest of the program
    uses, or a recipe would offer filters the plan does not.
    """
    wheel = rig.manager.get("filterwheel")
    if wheel is not None and wheel.connected:
        live = [str(name).strip() for name in (wheel.names or [])
                if str(name).strip()]
        if live:
            return live
    own = [str(name).strip()
           for name in (rig.config.get("camera", "filterNames", []) or [])
           if str(name).strip()]
    if own:
        return own
    # A RASA with one filter in the drawer and nothing else described: that one
    # filter is the whole list, and is more honest than the shared default.
    fitted = str(rig.config.get("camera", "fixedFilter", "") or "").strip()
    if fitted:
        return [fitted]
    return [str(name).strip()
            for name in (config.get("schedule", "filters", []) or [])
            if str(name).strip()]


def default_exposures(rig, config: Config) -> dict[str, float]:
    """The sub length each of this telescope's filters is shot at, by name.

    The one table that decides an exposure everywhere: Auto-arrange writes
    it onto the plan, the collaboration server is told it when a project is
    joined, and the darks in the library have to match it. One source, so
    that a light frame taken tonight always has a dark of its own length.
    Names are in the one spelling the rest of the program uses.
    """
    from . import autoplan, filters

    settings = config.section("autoplan")
    table: dict[str, float] = {}
    for name in filter_names(rig, config):
        key = filters.canonical(name)
        if not key or key in table:
            continue
        table[key] = float(autoplan._exposure_for(name, {}, settings))
    return table


def suggested_recipe(rigs, config: Config) -> dict[str, Any]:
    """A sensible starting recipe for the rig as it stands.

    Built from what is actually connected - the filters in the wheel, and
    the exposures those filters are shot at - because a blank form is the
    reason calibration does not get done. The darks are one set per distinct
    default exposure, so every light the plan takes has a dark of its own
    length; the flats measure themselves; bias frames cover the flats. No
    dark flats: a bias is what a flat of a few seconds is corrected by here,
    and a set the library does not need is a set nobody wants to sit through.
    """
    sets: list[dict[str, Any]] = []
    master = rigs.master
    camera_settings = config.section("camera")

    sets.append({"frameType": "bias", "count": 50, "exposure": 0.0, "binning": 1})

    exposures = sorted(set(default_exposures(master, config).values())) or [300.0]
    for exposure in exposures:
        sets.append({"frameType": "dark", "count": 25, "exposure": exposure,
                     "binning": 1})

    # One line for the whole wheel rather than a row per filter: the recipe
    # is something to read as well as to run.
    names = filter_names(master, config)
    sets.append({"frameType": "flat", "count": 25, "exposure": 0.0,
                 "binning": 1, "filter": "", "allFilters": bool(names)})

    return {
        "name": "Full library",
        "sets": [s for s in (_clean_set(entry, i) for i, entry in enumerate(sets))
                 if s is not None],
        # Not part of the recipe — the cameras keep their own setpoint — but
        # worth showing beside it, because a dark library is only valid at the
        # temperature it was taken at.
        "setpoint": camera_settings.get("setpoint"),
        "exposures": exposures,
    }
