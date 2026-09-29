"""Running the plan: the thing that actually moves the telescope all night.

The sequencer walks the plan in order and, for each target, points the rig,
focuses, and takes the frames that were asked for.  Mosaics are walked tile by
tile in the order the scheduler worked out, so neighbouring tiles are shot next
to each other in time.

**More than one telescope.**  One rig is the master: it carries the mount and
the guider, so it decides where everything points and when everything dithers.
Every other rig is bolted to the same mount and cannot point itself, so the
sequencer runs the night in *slots*.  A slot starts an exposure on every
telescope at once, waits for all of them, and only then dithers — so a slave is
never mid-frame while the master nudges the mount, which is the whole reason to
do it this way rather than letting each scope run its own loop.  Autofocus works
the same way: when any telescope is due a run, every telescope with a focuser
sweeps at the same time, because the mount is not usable for imaging by anyone
while one of them is defocused.

Each telescope keeps its own allocation, so the master can be taking five-minute
luminance while the faster scope alongside it takes ten-minute Ha of the same
field.  Both write the same OBJECT — including the panel, for a mosaic — so the
frames stack together whichever telescope they came off.

Two events interrupt imaging and are handled rather than avoided:

  * **the meridian** - a German equatorial mount cannot track through it.
    Imaging stops before the target gets there, the mount is sent back to the
    same coordinates (which makes the driver flip), the new pointing is plate
    solved and synced, and imaging resumes.
  * **focus drift** - temperature moves, filters are not parfocal, and hours
    pass.  A focus run is triggered on whichever of those comes first.

Everything checks for an abort between steps.  Stopping is always allowed and
always leaves the mount tracking where it is rather than parking it, because a
surprise slew in the dark is worse than a stopped sequence.
"""

from __future__ import annotations

import contextlib
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import allsky, astro, capture, filters, plans, schedule
from .devices.base import DeviceError
from .imaging import stars

# States the UI shows. "waiting" means the next target is not up yet.
IDLE = "idle"

# How many measured subs to keep for the trend chart. A long night at short
# subs is a few hundred frames, so this holds the whole run.  With several
# telescopes it holds proportionally fewer slots, which is the right trade: the
# chart is about the last few hours, not the whole week.
FRAME_HISTORY = 900


def panel_labels(target_name: str, panel: dict[str, Any], mosaic: bool
                 ) -> tuple[str, str, str]:
    """The object, the filename panel tag and the on-screen label for a panel.

    A mosaic panel is its own object as far as a stacker is concerned — it is a
    different piece of sky — so OBJECT has to name the panel and not just the
    mosaic.  Every telescope shooting that panel writes the same string, which
    is what lets frames off two scopes be sorted into one set of panels.

    ASCII only: FITS cards are ASCII, so an em dash would be written as a
    question mark.
    """
    if not mosaic:
        return target_name, "", target_name
    index = int(panel.get("index", 1) or 1)
    return (f"{target_name} - Panel {index}", f"P{index}",
            f"{target_name} panel {index}")


def _flatten(allocation: list[dict[str, Any]],
             order: str = "grouped") -> list[dict[str, Any]]:
    """An allocation as the flat list of frames it actually means.

    `[{L, 300, 3}, {Ha, 600, 2}]` becomes three L frames then two Ha frames.
    Slot *n* is then simply frame *n* of each telescope's list, which is what
    makes "expose together" a one-line idea rather than a scheduling problem.

    `rotate` deals them round instead — L, Ha, L, Ha, L — which costs a filter
    change per frame and buys the thing a filter change cannot: a session cut
    short by cloud at forty per cent is forty per cent of *every* channel rather
    than all of the luminance and none of the red.
    """
    rows: list[list[dict[str, Any]]] = []
    for item in allocation:
        count = int(item.get("count", 0) or 0)
        exposure = float(item.get("exposure", 0) or 0)
        name = str(item.get("name") or "")
        if count <= 0 or exposure <= 0:
            continue
        rows.append([{"filter": name, "exposure": exposure} for _ in range(count)])

    if order != "rotate":
        return [frame for row in rows for frame in row]

    dealt: list[dict[str, Any]] = []
    for index in range(max((len(row) for row in rows), default=0)):
        for row in rows:
            if index < len(row):
                dealt.append(row[index])
    return dealt


class Sequencer:
    def __init__(self, rigs, config, targets, plan, coverage=None,
                 recipes=None, calibrator=None, progress=None) -> None:
        self.rigs = rigs
        self.config = config
        self.targets = targets
        self.plan = plan
        # Where survey fields are ticked off as they are shot, so the revisit
        # filter has something to work from next time.
        self.coverage = coverage
        # Calibration tasks in the plan: the recipes and the thing that shoots
        # them.  Both optional, so a Sequencer can still be built without them.
        self.recipes = recipes
        self.calibrator = calibrator
        # Where all-sky frames are counted. Optional, so a Sequencer can still
        # be built without one.
        self.progress = progress
        # The weather watcher, set by whoever builds both. Optional, so a
        # Sequencer can still be constructed without one.
        self.safety: Any = None
        # Where a night's notable moments are told to somebody who is not in the
        # room. Optional for the same reason.
        self.notifier: Any = None
        # The warnings board, for the things only the run can see happen: a
        # flip that did not flip, a solver that keeps failing. Optional.
        self.warnings: Any = None
        self._solve_failures = 0
        # Told when the first plate solve on a collaboration target measures
        # the camera at an angle other than the one its panels were laid at.
        # Set by whoever builds both; it lays the target out again, and this
        # run then starts the target over on the new panels.
        self.on_camera_angle: Any = None

        self._thread: threading.Thread | None = None
        # The warm-down outlives the run that started it, so it gets its own.
        self._warm_thread: threading.Thread | None = None
        self._warm_stop = threading.Event()
        self._abort = threading.Event()
        self._paused = threading.Event()
        self._skip = threading.Event()
        self._lock = threading.RLock()

        self._state = IDLE
        self._state_since = time.time()
        self._message = ""
        self._error: str | None = None
        # What is being rescued right now, and what has been rescued so far.
        # Both are shown, because "it recovered" is only reassuring when you can
        # see how often it has had to.
        self._recovery: dict[str, Any] | None = None
        self._recoveries: list[dict[str, Any]] = []
        self._rescues = 0
        self._target_rescues = 0
        # Rigs whose cooler has been started but whose temperature has not been
        # waited for yet. Emptied as each one settles before its first frame.
        self._cooling_wanted: set[str] = set()
        # Whether the mount has been sent home this run; once is enough.
        self._homed = False
        # Whether the run is stopped or held because the sky is not safe, as
        # opposed to because somebody pressed Pause.
        self._weather_hold = False
        # When the sky gets light tonight, worked out once per run. 0.0 means
        # "asked and there was no answer", which is not the same as "not asked".
        self._dawn_at: float | None = None
        # Which step of a shutdown is in progress, so the button can say.
        self._shutdown_step = ""
        # Night after night: when the plan runs out the observatory is put to
        # bed and the run waits for the next dusk rather than ending. `_loop_next`
        # is when the next night starts, for the UI; `_nights` how many so far.
        self._loop = False
        self._loop_next: float | None = None
        self._nights = 0
        # Guiding the sequencer itself asked for. Nothing is recovered that was
        # never wanted: a run deliberately going unguided must not have the
        # guider started under it every slot.
        self._guiding_wanted = False
        self._guide_lost_since: float | None = None
        self._guide_lost_in_slot = False
        self._recentre_request: str | None = None
        self._entry_id: str | None = None
        self._panel: int = 0
        self._panels: int = 0
        # Which panel of the target that is, as the target numbers them.
        # `_panel` is a position in tonight's queue - "3 of 6" - and on a
        # mosaic shooting panels 7, 12 and 20 only, position 3 is panel 20.
        # The framing picture needs the panel's own number to light it up.
        self._panel_index: int = 0
        self._slot: int = 0
        self._slots: int = 0
        self._done: dict[str, int] = {}
        self._started: float | None = None
        self._flips = 0
        self._focus_runs = 0
        self._frames_log: list[dict[str, Any]] = []
        self._since_dither = 0
        # Everything below is per telescope: two scopes drift out of focus and
        # off target independently, so one shared number would be meaningless.
        self._reference_hfr: dict[str, float] = {}
        self._hfr_over_limit: dict[str, int] = {}
        self._refocus_reason: dict[str, str] = {}
        # A telescope whose sweep just failed is not asked again immediately.
        # Every refocus stops and sweeps the whole mount, so one slave that
        # cannot focus — clouded over, no stars, a focuser that will not move —
        # must not drag the other two into a sweep after every single sub.
        self._focus_failures: dict[str, int] = {}
        self._focus_blocked_until: dict[str, float] = {}
        self._since_solve: dict[str, int] = {}
        # The filter offset currently standing in each focuser's position, so a
        # change only ever moves by the difference between two filters.
        self._filter_offset: dict[str, int] = {}
        self._rig_state: dict[str, dict[str, Any]] = {}
        self._checking = threading.Lock()

    # -- the telescopes ----------------------------------------------------
    @property
    def master(self):
        return self.rigs.master

    @property
    def manager(self):
        """The master's devices: the mount, the guider and the event log."""
        return self.rigs.master.manager

    @property
    def capture(self):
        return self.rigs.master.capture

    @property
    def solver(self):
        return self.rigs.master.solver

    @property
    def focuser(self):
        return self.rigs.master.focuser

    # -- state -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def stopping(self) -> bool:
        return self._abort.is_set()

    @property
    def owns_the_cameras(self) -> bool:
        """Whether the sequencer is actually using the cameras right now.

        Not the same as `running`.  A paused sequence is sitting in `_check`
        holding nothing — the thread is alive, but it is not taking frames, so
        it should not stand in the way of focusing by hand.
        """
        return self.running and not self.paused and not self.stopping

    @property
    def warming(self) -> bool:
        """Whether a warm-down is still ramping the coolers off in the background."""
        thread = self._warm_thread
        return thread is not None and thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "paused": self._paused.is_set(),
                "stopping": self._abort.is_set(),
                # Paused by the weather rather than by a person, which reads
                # very differently in the banner at three in the morning.
                "weatherHold": self._weather_hold,
                "shutdownStep": self._shutdown_step,
                # Running night after night, and when the next one begins.
                "loop": self._loop,
                "loopNext": self._loop_next,
                "nights": self._nights,
                # Warming runs on after the sequence has ended, so it is
                # reported separately rather than holding the run open.
                "warming": self.warming,
                # What the UI should gate on, rather than `running`: a paused or
                # stopping sequence leaves the cameras free.
                "imaging": self.owns_the_cameras,
                "state": self._state,
                "stateSince": self._state_since,
                "message": self._message,
                "error": self._error,
                # What went wrong and what is being done about it.
                "recovery": dict(self._recovery) if self._recovery else None,
                "recoveries": list(self._recoveries),
                "rescues": self._rescues,
                "entryId": self._entry_id,
                "panel": self._panel,
                "panelIndex": self._panel_index,
                "panels": self._panels,
                "slot": self._slot,
                "slots": self._slots,
                "framesDone": dict(self._done),
                "started": self._started,
                "flips": self._flips,
                "focusRuns": self._focus_runs,
                "focus": self.focuser.status(),
                "frames": list(self._frames_log),
                "referenceHfr": self._reference_hfr.get(self.master.id),
                "referenceHfrByRig": dict(self._reference_hfr),
                "telescopes": {rig_id: dict(state)
                               for rig_id, state in self._rig_state.items()},
                # The master's own progress, kept at the top level so a
                # single-telescope setup reads exactly as it always did.
                "filter": self._rig_state.get(self.master.id, {}).get("filter", ""),
                "frame": self._rig_state.get(self.master.id, {}).get("frame", 0),
                "frameCount": self._rig_state.get(self.master.id, {}).get("frames", 0),
            }

    def _set(self, state: str, message: str = "") -> None:
        with self._lock:
            # Timed from when the stage *changed*, so the indicator can say
            # "slewing for 40s" rather than restating the same stage's age every
            # time the message inside it is reworded.
            if state != self._state:
                self._state_since = time.time()
            self._state = state
            self._message = message

    def _say(self, message: str, level: str = "info") -> None:
        self.manager.log(message, level)
        self._set(self._state, message)

    def _summary(self) -> str:
        """The night in a few lines, for a message read on a phone."""
        with self._lock:
            done = dict(self._done)
            started = self._started
            rescues = self._rescues
            flips = self._flips
            focus_runs = self._focus_runs
        frames = sum(done.values())
        hours = (time.time() - started) / 3600.0 if started else 0.0
        lines = [f"{frames} frame(s) over {hours:.1f}h"]
        if flips:
            lines.append(f"{flips} meridian flip(s)")
        if focus_runs:
            lines.append(f"{focus_runs} focus run(s)")
        if rescues:
            lines.append(f"{rescues} recovery/recoveries")
        return "  ·  ".join(lines)

    def _tell(self, event: str, subject: str, body: str = "") -> None:
        """Tell somebody who is not in the room. Never fails a night."""
        if self.notifier is None:
            return
        with contextlib.suppress(Exception):
            self.notifier.send(event, subject, body)

    def _warn(self, key: str, level: str, title: str, detail: str = "",
              fix: str = "") -> None:
        """Put something only the run can see onto the warnings board."""
        if self.warnings is None:
            return
        with contextlib.suppress(Exception):
            self.warnings.raise_(key, level, title, detail, fix, sticky=True)

    def _set_rig(self, rig, **values: Any) -> None:
        with self._lock:
            state = self._rig_state.setdefault(
                rig.id, {"name": rig.name, "role": rig.role, "filter": "",
                         "frame": 0, "frames": 0, "state": IDLE})
            state["name"] = rig.name
            state["role"] = rig.role
            state.update(values)

    # -- control -----------------------------------------------------------
    def start(self, loop: bool = False) -> None:
        """Run the plan tonight; with `loop`, every night until told to stop.

        On loop the night is bracketed by the whole of what an unattended
        observatory needs: the mount released, homed and the cover opened
        before the first slew, and at the end the cover closed, the mount
        homed and parked - verified, not assumed - the roof shut and the
        cameras warmed; then a wait for the next dusk and the same again. The
        mount parking itself is the part that matters most, so a mount that
        cannot park is refused the loop rather than left pointing up.
        """
        if self.running:
            raise DeviceError("the sequence is already running")
        stored = self.plan.raw()
        if not stored["entries"]:
            raise DeviceError("the plan is empty")
        self.manager.require("camera")
        # A plan of nothing but calibration tasks is a perfectly good afternoon's
        # work with the dome shut, and it has no use for the mount.
        if any(not plans.is_calibration(e) for e in stored["entries"]):
            self.manager.require("mount")
        if loop:
            mount = self.manager.get("mount")
            if mount is not None and mount.connected \
                    and not getattr(mount, "can_park", True):
                raise DeviceError(
                    "this mount cannot park itself, so it cannot be left to "
                    "run night after night - use Run sequence instead")
            if self._site() is None:
                raise DeviceError(
                    "the observing site is not set, so dusk and dawn cannot be "
                    "worked out - set it in Site & Optics")

        # Refuse to open the night at all while the monitor says otherwise.
        # Everything else here protects the data; this protects the telescope.
        safety = self.config.section("safety")
        if safety.get("enabled", True) and safety.get("blockStart", True):
            watcher = getattr(self, "safety", None)
            if watcher is not None and watcher.read() is False:
                raise DeviceError(
                    "the safety monitor says it is not safe to open — "
                    "start is blocked. Turn the block off in Equipment → "
                    "Safety if that is wrong.")

        # A warm-down from the previous run would be raising the setpoint while
        # this one tries to lower it.
        self._stop_warming()
        # Last night's event warnings belong to last night.
        if self.warnings is not None:
            with contextlib.suppress(Exception):
                self.warnings.clear_sticky()
        self._solve_failures = 0

        self._abort.clear()
        self._paused.clear()
        self._skip.clear()
        with self._lock:
            self._loop = bool(loop)
            self._loop_next = None
            self._nights = 0
        self._reset_night()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sequencer")
        self._thread.start()

    def _reset_night(self) -> None:
        """Forget the previous night: counters, references, what was homed."""
        with self._lock:
            self._error = None
            self._done = {}
            self._started = time.time()
            self._flips = 0
            self._focus_runs = 0
            self._frames_log = []
            self._since_dither = 0
            self._reference_hfr = {}
            self._hfr_over_limit = {}
            self._refocus_reason = {}
            self._focus_failures = {}
            self._focus_blocked_until = {}
            self._since_solve = {}
            self._filter_offset = {}
            self._rig_state = {}
            self._recovery = None
            self._recoveries = []
            self._rescues = 0
            self._target_rescues = 0
            self._guiding_wanted = False
            self._guide_lost_since = None
            self._guide_lost_in_slot = False
            self._recentre_request = None
            self._cooling_wanted = set()
            self._homed = False
            self._weather_hold = False
            self._dawn_at = None

    def pause(self) -> None:
        self._paused.set()
        self._say("Sequence paused after the current frame", "warn")

    def resume(self) -> None:
        self._paused.clear()
        self._say("Sequence resumed")

    def skip(self) -> None:
        self._skip.set()
        self._say("Skipping to the next target", "warn")

    def stop(self) -> None:
        self._abort.set()
        self._paused.clear()
        for rig in self.rigs.all:
            with contextlib.suppress(Exception):
                rig.capture.abort()
        self._say("Sequence stopping", "warn")

    # -- weather -----------------------------------------------------------
    def held_for_weather(self) -> bool:
        """Whether the run is stopped because the sky is not safe."""
        return self._weather_hold

    def weather_unsafe(self, detail: str | None = None) -> None:
        """Called by the safety watcher once it has made up its mind.

        Two responses, and which one is right depends on the building. A
        roll-off that closes itself wants the whole shutdown; a dome under a
        roof somebody else controls wants the run held where it is so it can
        pick up again in twenty minutes without a slew.
        """
        action = str(self._settings_section("safety").get("onUnsafe")
                     or "shutdown").lower()
        reason = f"unsafe{f' — {detail}' if detail else ''}"
        if action == "pause" and self.running:
            self._weather_hold = True
            self._paused.set()
            self._say(f"The sky is {reason}: holding the run until it clears",
                      "error")
            return
        self._weather_hold = True
        self._say(f"The sky is {reason}: shutting down", "error")
        self.shut_down()

    def weather_safe(self) -> None:
        """Safe again, and settled long enough to be believed."""
        if not self._weather_hold:
            return
        self._weather_hold = False
        if self.running and self._paused.is_set():
            self._paused.clear()
            self._say("The sky is safe again: carrying on", "success")

    def _settings_section(self, name: str) -> dict[str, Any]:
        return self.config.section(name)

    # -- helpers -----------------------------------------------------------
    def _check(self) -> None:
        """Raise if stopping; block while paused."""
        if self._abort.is_set():
            raise _Stopped()
        while self._paused.is_set() and not self._abort.is_set():
            self._set("paused", "waiting for the sky to clear"
                      if self._weather_hold else "paused")
            time.sleep(0.4)
        if self._abort.is_set():
            raise _Stopped()

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check()
            time.sleep(min(0.5, deadline - time.monotonic()))

    def _settings(self) -> dict[str, Any]:
        """Settings that describe the observatory rather than one telescope."""
        return self.config.section("sequencer")

    def _site(self) -> tuple[float, float] | None:
        from .config import effective_site
        site = effective_site(self.config, self.manager)
        if site.get("latitude") is None:
            return None
        return float(site["latitude"]), float(site["longitude"])

    @staticmethod
    def _parallel(jobs: list[tuple[Any, Any]], name: str) -> dict[str, Any]:
        """Run one job per telescope at the same time and collect the outcomes.

        A thread each rather than a pool: there are at most a handful of
        telescopes, and each job spends its life blocked on a driver.
        """
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        threads: list[threading.Thread] = []

        def worker(rig, job) -> None:
            try:
                results[rig.id] = job()
            except Exception as exc:              # noqa: BLE001 - reported per rig
                errors[rig.id] = str(exc)

        for rig, job in jobs:
            thread = threading.Thread(target=worker, args=(rig, job), daemon=True,
                                      name=f"{name}-{rig.id}")
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        return {"results": results, "errors": errors}

    # -- the cameras -------------------------------------------------------
    def _prepare_cameras(self) -> None:
        """Apply each telescope's gain and offset, then start the coolers.

        This no longer waits for temperature.  Switching the cooler on takes a
        moment; reaching -10 C takes ten minutes, and standing still for them
        wastes darkness on a rig that could have been homing, slewing, centring
        and focusing throughout.  The wait moves to `_await_cooling`, just
        before the first light frame, where by then there is usually nothing
        left to wait for.
        """
        rigs = self.rigs.imaging()
        outcome = self._parallel([(rig, lambda rig=rig: self._prepare_camera(rig))
                                  for rig in rigs], "cool")
        for rig_id, message in outcome["errors"].items():
            self._say(f"Could not prepare {self._name(rig_id)}: {message}", "warn")

        waiting = [rig for rig in rigs
                   if rig.config.section("camera").get("waitForCooling", False)]
        if waiting:
            self._parallel([(rig, lambda rig=rig: self._await_cooling(rig))
                            for rig in waiting], "cool")

    def _name(self, rig_id: str) -> str:
        with contextlib.suppress(Exception):
            return self.rigs.get(rig_id).name
        return rig_id

    def _prepare_camera(self, rig) -> None:
        settings = rig.config.section("camera")
        camera = rig.manager.require("camera")
        label = rig.name

        gain = settings.get("gain")
        offset = settings.get("offset")
        if gain is not None or offset is not None:
            camera.set_settings(gain=int(gain) if gain is not None else None,
                                offset=int(offset) if offset is not None else None)
            self._say(f"{label}: gain {camera.gain}, offset {camera.offset}")

        if not settings.get("coolAtStart", True) or not camera.can_cool:
            return
        setpoint = float(settings.get("setpoint", -10.0))
        camera.set_setpoint(setpoint)
        camera.set_cooler(True)
        self._say(f"{label}: cooling the camera to {setpoint:g} C")
        with self._lock:
            self._cooling_wanted.add(rig.id)

    def _await_cooling(self, rig) -> None:
        """Block until this camera is at its setpoint, or gives up trying.

        Called before the first light frame rather than before the first slew,
        so the minutes spent coming down to temperature are the same minutes
        spent homing, slewing, centring and focusing.  Runs once per rig per
        sequence: a cooler that has settled does not need proving again between
        every target.
        """
        with self._lock:
            if rig.id not in self._cooling_wanted:
                return
        settings = rig.config.section("camera")
        camera = rig.manager.get("camera")
        if camera is None or not camera.connected or not camera.can_cool:
            return
        setpoint = float(settings.get("setpoint", -10.0))
        tolerance = float(settings.get("coolToleranceC") or 1.0)
        timeout = float(settings.get("coolTimeoutMinutes") or 20.0) * 60.0
        label = rig.name

        deadline = time.monotonic() + timeout
        announced = False
        while True:
            self._check()
            now = camera.temperature
            if now is not None and abs(now - setpoint) <= tolerance:
                self._say(f"{label}: camera at {now:.1f} C", "success")
                break
            if time.monotonic() > deadline:
                self._say(f"{label}: camera reached "
                          f"{now if now is None else round(now, 1)} C, not "
                          f"{setpoint:g} C, within {timeout / 60:.0f} minutes; "
                          "starting anyway", "warn")
                break
            if not announced:
                self._set("cooling", f"cooling to {setpoint:g} C")
                announced = True
            self._set_rig(rig, state=f"cooling {now:.1f} C" if now is not None
                          else "cooling")
            time.sleep(5.0)
        with self._lock:
            self._cooling_wanted.discard(rig.id)

    def _warm_cameras(self) -> None:
        rigs = [rig for rig in self.rigs.all
                if (camera := rig.manager.get("camera")) is not None and camera.connected]
        self._parallel([(rig, lambda rig=rig: self._warm_camera(rig))
                        for rig in rigs], "warm")

    def _warm_camera(self, rig) -> None:
        """Ramp the cooler off rather than switching it off.

        A sensor taken from -10 C to ambient in one step can pull moisture out
        of the air and onto the window.  Raising the setpoint in steps takes a
        few minutes and avoids that entirely.
        """
        settings = rig.config.section("camera")
        if not settings.get("warmAtEnd", True):
            return
        camera = rig.manager.get("camera")
        if camera is None or not camera.connected or not camera.can_cool:
            return

        rate = float(settings.get("warmRateCPerMinute") or 3.0)
        start = camera.temperature
        if start is None:
            camera.set_cooler(False)
            return

        self._say(f"{rig.name}: warming the camera from {start:.1f} C at {rate:g} C/min")
        target = 15.0
        current = start
        # The sequence's own abort flag is deliberately ignored: stopping a run
        # should still leave the camera warmed safely. `_warm_stop` is the one
        # thing that cuts it short, and only a new sequence sets that — which is
        # going to cool the sensor again anyway.
        while current < target:
            current = min(target, current + rate)
            try:
                camera.set_setpoint(current)
            except DeviceError:
                break
            self._set_rig(rig, state=f"warming {current:.0f} C")
            if self._warm_stop.wait(60.0):
                self._say(f"{rig.name}: warming cut short by a new sequence", "warn")
                return
        with contextlib.suppress(Exception):
            camera.set_cooler(False)
        self._set_rig(rig, state="")
        self._say(f"{rig.name}: camera cooler off", "success")

    # -- the run -----------------------------------------------------------
    def _run(self) -> None:
        try:
            # The night just run ends at its dawn; the next one is the first
            # whose dusk comes after that. A plan that ran out at one in the
            # morning is not run again at ten past.
            after: float | None = None
            while True:
                if self._loop:
                    # Never before dusk: a loop started at three in the
                    # afternoon would otherwise cool the cameras and slew at
                    # a target in daylight. Returns at once in the dark.
                    if not self._wait_for_dusk(after):
                        break
                    if self._nights:
                        self._stop_warming()
                        self._reset_night()
                self._night()
                if not self._loop or self._abort.is_set():
                    break
                after = self._dawn() or time.time()
                with self._lock:
                    self._nights += 1
                self._put_to_bed()
        finally:
            with self._lock:
                self._entry_id = None
                self._panel = self._panels = self._slot = self._slots = 0
                self._panel_index = 0
                self._rig_state = {}
                self._recovery = None
                self._cooling_wanted = set()
                self._loop = False
                self._loop_next = None
            # A frame taken by hand afterwards must not claim to be panel 5
            # of a mosaic that finished at dawn.
            for rig in self.rigs.all:
                with contextlib.suppress(Exception):
                    rig.capture.clear_context()

            # Order matters, and this is the order a night has to end in.
            #
            # Guiding stops first, always — not only when the mount is being
            # parked. PHD2 left guiding after the plan runs out chases a star
            # through dawn, pushes the mount around for hours, and is what
            # "the system was still trying to guide in the morning" looks like.
            with contextlib.suppress(Exception):
                self._stop_guiding()

            # Then the mount, while there is still a thread paying attention to
            # it. Parking is deliberately opt-in: a surprise slew in the dark is
            # worse than a mount left tracking, unless there is a roof that has
            # to close over it — which is what the setting is for.
            settings = self._settings()
            if settings.get("parkAtEnd") or settings.get("stopTrackingAtEnd"):
                with contextlib.suppress(Exception):
                    self._make_safe(park=bool(settings.get("parkAtEnd")),
                                    stop_tracking=bool(settings.get("stopTrackingAtEnd")))

            # A copy of the log where the data is, so a night run at a remote
            # site can be read into afterwards without getting onto that machine.
            with contextlib.suppress(Exception):
                self._save_log_copy()

            # Warming last, and not on this thread. Ramping a sensor from -10 C
            # to ambient is about ten minutes of sleeping, and doing it here is
            # what made Stop appear to hang: the run was over, but the sequencer
            # thread was still alive and everything gated on it stayed gated.
            # It is safety work, not sequence work, so it outlives the run.
            self._set(IDLE, self._message)
            self._start_warming()

    def _night(self) -> None:
        """One night of the plan, from the coolers coming on to the plan running out.

        Reports how it ended - finished, stopped, failed - and returns either
        way; what happens to the rig afterwards is the caller's, because it
        differs between a run that is over and a loop that has a night to
        put to bed and another to wait for.
        """
        try:
            stored = self.plan.raw()
            known = {t["id"]: t for t in self.targets.listing()}
            rigs = self.rigs.imaging()
            self._roll_plan_times()
            self._say(
                f"Sequence started: {len(stored['entries'])} target(s) on "
                f"{len(rigs)} telescope(s) — "
                + ", ".join(rig.name for rig in rigs), "success")
            self._tell("activity",
                       ("Night opened" if self._loop else "Sequence started")
                       + (f" (night {self._nights + 1} on loop)" if self._loop else ""),
                       f"{len(stored['entries'])} target(s): "
                       + ", ".join(e.get("name", "?") for e in stored["entries"][:8])
                       + ("…" if len(stored["entries"]) > 8 else ""))
            self._prepare_cameras()
            if any(not plans.is_calibration(e) for e in stored["entries"]):
                self._open_cover()
            self._walk_plan(stored, known)

            self._set(IDLE, "sequence finished")
            self._say("Sequence finished", "success")
            self._tell("sequenceEnd", "Sequence finished", self._summary())
        except _Stopped:
            self._set(IDLE, "stopped")
            self._say("Sequence stopped", "warn")
            # Only when the weather stopped it: a person pressing Stop knows
            # they pressed Stop.
            if self._weather_hold:
                self._tell("safety", "Sequence stopped: the sky is not safe",
                           self._summary())
        except Exception as exc:                # noqa: BLE001 - shown to the operator
            with self._lock:
                self._error = str(exc)
            self._set(IDLE, "error")
            self._say(f"Sequence failed: {exc}", "error")
            self._tell("failure", f"Sequence failed: {exc}", self._summary())

    def _roll_plan_times(self) -> None:
        """Carry the plan's slot times forward to tonight.

        Reading the plan on the Plan tab does this; a loop that runs for a
        week with nobody opening the tab would otherwise walk a plan whose
        every start and end time is days in the past.
        """
        site = self._site()
        if site is None:
            return
        with contextlib.suppress(Exception):
            night = schedule.night(site[0], site[1])
            moved = self.plan.roll_times(night.get("windowStart"),
                                         night.get("windowEnd"))
            if moved:
                self._say(f"A new night: carried {moved} slot time(s) over "
                          "from the previous plan")

    # -- night after night -------------------------------------------------
    def _next_dusk(self, now: float | None = None,
                   after: float | None = None) -> tuple[float, float] | None:
        """When the next night's imaging starts, and when that night ends.

        The start is astronomical dusk less a lead for cooling, homing and
        focusing - or nautical dusk, or sunset, where the sky never gets
        properly dark. `after` rules out a night already run: its dusk has
        to come later than that. None when the sun never sets at all.
        """
        site = self._site()
        if site is None:
            return None
        now = time.time() if now is None else now
        lead = float(self._settings().get("loopLeadMinutes") or 20.0) * 60.0
        import datetime as _dt
        # Today by the site's sun, so a telescope on another continent
        # counts its nights where it stands.
        today = _dt.datetime.fromtimestamp(now, schedule._site_zone(site[1])).date()
        for days in range(-1, 3):
            night = schedule.night(site[0], site[1], today + _dt.timedelta(days=days))
            dusk = (night.get("duskAstronomical") or night.get("duskNautical")
                    or night.get("sunset"))
            dawn = (night.get("dawnAstronomical") or night.get("dawnNautical")
                    or night.get("sunrise") or night.get("windowEnd"))
            if not dusk or not dawn or dawn <= now:
                continue
            if after is not None and float(dusk) <= after:
                continue
            return max(now, float(dusk) - lead), float(dawn)
        return None

    def _wait_for_dusk(self, after: float | None = None) -> bool:
        """Sit out the day. False if stopped while waiting.

        Returns straight away in the dark. The safety monitor is asked
        before the night opens, the same as when a sequence is started by
        hand, and an unsafe sky is waited out rather than refused.
        """
        found = self._next_dusk(after=after)
        if found is None:
            self._say("The sun does not set here at this time of year; the "
                      "loop is over", "warn")
            return False
        start_at, _dawn = found
        with self._lock:
            self._loop_next = start_at
        if start_at > time.time():
            self._set("waiting", f"next night starts at {_clock(start_at)}")
            self._say(f"On loop: the next night starts at {_clock(start_at)}")
            self._tell("activity", f"Next night opens at {_clock(start_at)}",
                       "The observatory is parked and waiting for dusk.")
        # Woken the instant Stop is pressed, rather than at the next tick.
        while time.time() < start_at:
            if self._abort.wait(min(5.0, max(0.1, start_at - time.time()))):
                return False
        while not self._sky_safe():
            self._set("waiting", "waiting for the sky to be safe")
            if self._abort.wait(30.0):
                return False
        with self._lock:
            self._loop_next = None
        return not self._abort.is_set()

    def _sky_safe(self) -> bool:
        safety = self.config.section("safety")
        if not (safety.get("enabled", True) and safety.get("blockStart", True)):
            return True
        watcher = getattr(self, "safety", None)
        if watcher is None:
            return True
        with contextlib.suppress(Exception):
            return watcher.read() is not False
        return True

    def _put_to_bed(self) -> dict[str, Any]:
        """The end of a looped night: everything parked, closed and warm.

        The same steps as Abort & park, in the same order and each attempted
        whatever the one before it did, plus the mount is sent home before it
        parks - a mount that has found its switches parks from a known place,
        and starts the next night from one. A park that cannot be verified is
        the one thing here that is sent as a failure: an observatory left
        pointing at the sky until the next evening is what this exists to
        prevent, and nobody is watching.
        """
        done: list[str] = []
        failed: dict[str, str] = {}

        def step(name: str, action) -> None:
            with self._lock:
                self._shutdown_step = name
            try:
                action()
                done.append(name)
            except Exception as exc:              # noqa: BLE001 - report, continue
                failed[name] = str(exc)
                self._say(f"Putting the observatory to bed: could not {name} "
                          f"— {exc}", "error")

        self._say("Night over: putting the observatory to bed", "warn")
        step("stop guiding", self._stop_guiding)
        step("stop the cameras", self._abort_cameras)
        step("close the cover", self._close_cover)
        step("home the mount", self._home_for_bed)
        step("park the mount", self._park_and_check)
        step("close the roof", self._close_roof)
        step("copy the log", self._save_log_copy)
        step("warm the cameras", self._start_warming)
        with self._lock:
            self._shutdown_step = ""

        summary = ", ".join(done) or "nothing to do"
        if failed:
            summary += f" — but could not {', '.join(failed)}"
        self._say(f"In bed: {summary}", "error" if failed else "success")
        if "park the mount" in failed:
            self._tell("failure", "The mount did not park at the end of the night",
                       f"{failed['park the mount']}. The telescope may still be "
                       "pointing at the sky. " + self._summary())
        else:
            self._tell("sequenceEnd", "Night finished; observatory parked",
                       self._summary())
        return {"done": done, "failed": failed}

    def _home_for_bed(self) -> None:
        """Send the mount to its home switches before it parks.

        Skipped by a mount that cannot home, or is home already. Given the
        same time as homing at the start of the night.
        """
        mount = self.manager.get("mount")
        if mount is None or not mount.connected:
            return
        if not getattr(mount, "can_find_home", False):
            return
        if mount.at_park or mount.at_home:
            return
        timeout = float(self._settings().get("homeTimeoutMinutes") or 5.0) * 60.0
        self._set("parking", "homing the mount")
        mount.find_home()
        deadline = time.monotonic() + timeout
        time.sleep(1.0)
        while mount.slewing and time.monotonic() < deadline:
            time.sleep(0.4)
        if mount.slewing:
            raise DeviceError("the mount did not finish homing in time")
        self._say("Mount homed" if mount.at_home
                  else "Mount stopped moving but does not report being at home",
                  "success" if mount.at_home else "warn")

    def _open_cover(self) -> None:
        """Open the flat panel's cover before the first slew, light off.

        A cover closed at the end of one night stays closed until something
        opens it, and nothing did: the next night's frames were of the inside
        of the lid. A cover that will not open ends the night here rather than
        after an hour of those.
        """
        panel = self.manager.get("flatpanel")
        if panel is None or not panel.connected:
            return
        with contextlib.suppress(Exception):
            panel.turn_off()
        if not getattr(panel, "has_cover", False):
            return
        if panel.cover_state in ("open", "notpresent"):
            return
        self._set("homing", "opening the cover")
        self._say("Opening the flat panel's cover")
        panel.open_cover()
        deadline = time.monotonic() + float(
            self._settings().get("coverTimeoutSeconds") or 120.0)
        while panel.cover_state == "moving" and time.monotonic() < deadline:
            self._check()
            time.sleep(0.5)
        if panel.cover_state != "open":
            raise DeviceError(f"the flat panel's cover is {panel.cover_state}, "
                              "not open")
        self._say("Cover open", "success")

    def _walk_plan(self, stored: dict[str, Any], known: dict[str, Any]) -> None:
        """Work through the plan in order, finishing each target before the next.

        The order is the plan's own — the order the arranger worked out or the
        operator dragged the boxes into — and it is already in time order, so
        walking it is walking the night.  Nothing here reshuffles it: a plan you
        can read down the page and a night that happens in a different order
        would be two different plans.

        Interleaving several targets is the mosaic's job, not this loop's.  A
        mosaic is already a set of panels visited in a worked-out order inside
        one entry, which is the same idea done where it belongs.
        """
        for entry in stored["entries"]:
            if self._abort.is_set():
                break
            self._begin_entry(entry)

            if plans.is_calibration(entry):
                try:
                    self._run_calibration(entry)
                except _Skipped:
                    self._say(f"Skipped {entry['name']}", "warn")
                continue

            target = known.get(entry.get("targetId"))
            skip = self._why_not_now(entry, target)
            if skip:
                self._say(f"{entry['name']}: {skip}")
                continue
            self._tell("activity", f"Starting {entry['name']}",
                       self._entry_summary(entry, target))

            try:
                if target.get("type") == "allsky":
                    self._run_allsky(entry, target)
                else:
                    self._run_entry(entry, target)
            except _Reframed:
                # The target was laid out again under the run - the camera
                # measured at a different angle than its panels assumed. Once:
                # the fresh panels are read back and the target started over;
                # a second disagreement is not chased, or a bad solve could
                # keep a night circling.
                try:
                    target = self.targets.get(target["id"])
                    self._say(f"{entry['name']}: starting over on the panels as "
                              "they now are", "warn")
                    self._run_entry(entry, target, reframed=True)
                except _Skipped:
                    self._say(f"Skipped {entry['name']}", "warn")
            except _Skipped:
                self._say(f"Skipped {entry['name']}", "warn")

    def _entry_summary(self, entry: dict[str, Any],
                       target: dict[str, Any] | None) -> str:
        """A target in a line, for a message: panels and what is planned."""
        bits = []
        panels = len((target or {}).get("panels") or [])
        if panels > 1:
            share = plans.options_for(entry).get("panels") or []
            bits.append(f"{len(share) or panels} of {panels} panels")
        planned = [f"{int(f.get('count') or 0)}×{f.get('name')} "
                   f"{float(f.get('exposure') or 0):g}s"
                   for f in (entry.get("filters") or []) if int(f.get("count") or 0) > 0]
        if planned:
            bits.append(", ".join(planned))
        return "  ·  ".join(bits)

    def _begin_entry(self, entry: dict[str, Any]) -> None:
        """Make this entry the current one, and give it a fresh rescue budget."""
        self._skip.clear()
        with self._lock:
            self._entry_id = entry["id"]
            # Every target starts with a full recovery budget: a night that
            # needed three rescues on M31 should still be willing to rescue
            # NGC 7000.
            self._target_rescues = 0
            self._recentre_request = None

    def _why_not_now(self, entry: dict[str, Any],
                     target: dict[str, Any] | None) -> str:
        """Why this target should be passed over, or "" if it should be shot.

        A start time is deliberately not a reason: the plan is walked in time
        order, so a target whose turn has not come yet is one to *wait* for, and
        `_wait_for_start` does the waiting.  Everything here is a reason the
        target is not worth pointing at at all tonight.
        """
        if target is None:
            return "it is no longer in the target list"

        options = plans.options_for(entry)
        if not options.get("enabled", True):
            return "it is switched off in the plan"

        if (target.get("type") != "allsky"
                and not self._anything_planned(entry)):
            return "it has no frames planned"

        goal = float(options.get("goalHours") or 0.0)
        if goal > 0:
            done = float((target.get("integration") or {}).get("seconds") or 0.0)
            if done >= goal * 3600.0:
                return (f"it has reached its {goal:g}-hour goal "
                        f"({done / 3600.0:.1f}h shot)")

        end_at = entry.get("endAt")
        if end_at and time.time() >= end_at:
            return "its end time has passed"

        site = self._site()
        if site is None or target.get("type") == "allsky":
            return ""

        # Judged at the moment this target's turn actually begins, not now: a
        # target due at two in the morning is below the horizon at dusk, and
        # refusing it for that would throw away half the plan at the first
        # target.
        when = max(time.time(), float(entry.get("startAt") or 0.0))
        floor = float(options.get("minAltitude") or 0.0)
        if floor <= 0:
            floor = float(self.config.get("schedule", "minAltitude", 0.0) or 0.0)
        ra = astro.normalise_ra_hours(float(target.get("ra") or 0.0))
        dec = float(target.get("dec") or 0.0)
        if floor > 0:
            altitude = astro.altitude_at(ra, dec, when, site[0], site[1])
            if altitude < floor:
                return (f"it is at {altitude:.0f}° when its turn comes, below "
                        f"its {floor:g}° floor")

        moon = float(options.get("moonAvoidance") or 0.0)
        if moon > 0:
            distance = self._moon_distance(ra, dec)
            if distance is not None and distance < moon:
                return (f"the Moon is {distance:.0f}° away, inside its "
                        f"{moon:g}° limit")
        return ""

    @staticmethod
    def _moon_distance(ra_hours: float, dec_deg: float) -> float | None:
        """Degrees from the Moon, or None when that cannot be worked out.

        The Moon is where it is regardless of where you stand — a degree of
        parallax does not matter to an avoidance radius — so this needs no site.
        """
        try:
            jd = astro.julian_from_timestamp(time.time())
            moon_ra, moon_dec = astro.moon_position(jd)
            return astro.separation_degrees(ra_hours, dec_deg, moon_ra, moon_dec)
        except Exception:                        # noqa: BLE001 - never stop a run for this
            return None

    def _anything_planned(self, entry: dict[str, Any]) -> bool:
        master_id = self.master.id
        return any(_flatten(plans.allocation_for(entry, rig.id, master_id))
                   for rig in self.rigs.imaging())

    def _focus_seconds_estimate(self) -> float:
        """Roughly what a focus sweep costs, for deciding whether to start one.

        From the measured overheads where there are any, so a rig that has been
        watched knows its own number rather than a guess.
        """
        settings = self._settings()
        points = int(settings.get("focusPoints") or 9)
        exposure = float(settings.get("focusExposure") or 6.0)
        frames = max(1, int(settings.get("focusFramesPerPoint") or 1))
        store = getattr(self.rigs, "overheads", None)
        if store is not None:
            with contextlib.suppress(Exception):
                return float(store.focus_seconds(points, exposure, frames))
        return points * frames * (exposure + 6.0) + 45.0

    def _setup_seconds(self) -> float:
        """What a slew, a centre, a focus sweep and settling the guider cost.

        The point of the number is not precision — it is that starting all of
        that with four minutes of night left is never right.
        """
        slew = 90.0
        store = getattr(self.rigs, "overheads", None)
        if store is not None:
            with contextlib.suppress(Exception):
                slew = float(store.value("slew", 90.0))
        focus = (self._focus_seconds_estimate()
                 if self._settings().get("autofocusOnStart", True) else 0.0)
        return slew + focus

    def _dawn(self) -> float | None:
        """When the sky gets light tonight, whatever the plan says.

        The backstop the plan does not provide. A target with no end time set
        has no end time at all, and `_too_low` only stops the one field that has
        set — so a plan of several targets would go on slewing, focusing and
        exposing into full daylight, one target at a time, until it ran out.
        Which is what happened.

        Worked out once per run and cached: it does not move during a night, and
        it is consulted between every frame.
        """
        if self._dawn_at is not None:
            return self._dawn_at or None
        site = self._site()
        if site is None:
            self._dawn_at = 0.0                  # nothing to work it out from
            return None
        try:
            night = schedule.night(site[0], site[1])
        except Exception:                         # noqa: BLE001 - never fatal
            self._dawn_at = 0.0
            return None
        value = (night.get("dawnAstronomical") or night.get("windowEnd")
                 or night.get("sunrise"))
        self._dawn_at = float(value) if value else 0.0
        return self._dawn_at or None

    def _past_deadline(self, entry: dict[str, Any] | None, end_at: float | None,
                       need: float = 0.0) -> str | None:
        """Why there is no point starting the next thing, or None to carry on.

        `need` is how long the thing about to be started will take, so a target
        with four minutes left does not begin a twelve-minute focus sweep.

        Checked *before* slewing, focusing and guiding rather than only before
        the exposure. All three are expensive — a slew and centre is minutes, a
        focus sweep can be ten — and doing them first meant the end time was
        noticed only after the telescope had already been driven to a field that
        had set and swept the focuser through it. Which is how a scope ends up
        autofocusing on the ground at dawn.
        """
        now = time.time()
        if end_at and now + need >= float(end_at):
            name = (entry or {}).get("name") or "this target"
            return f"{name} has reached its end time"
        dawn = self._dawn()
        # On loop, dawn is always the end: the night has to close so the next
        # can open, whatever the setting says about a run by hand.
        if dawn and (self._settings().get("stopAtDawn", True) or self._loop):
            margin = float(self._settings().get("dawnMarginMinutes") or 0.0) * 60.0
            if now + need >= dawn - margin:
                return "the night is over"
        return None

    def _wait_for_start(self, entry: dict[str, Any]) -> None:
        """A start time set on the graph means "not before this"."""
        start_at = entry.get("startAt")
        if not start_at or time.time() >= start_at:
            return
        self._set("waiting", f"waiting for {entry['name']}")
        self._say(f"Waiting until {_clock(start_at)} for {entry['name']}")
        while time.time() < start_at:
            self._check()
            if self._skip.is_set():
                raise _Skipped()
            time.sleep(1.0)

    def _run_calibration(self, entry: dict[str, Any]) -> None:
        """Run a calibration recipe as a task in the plan.

        Darks at the end of the night are worth having and nobody wants to sit
        up for them, which is the whole reason this is a plan task rather than
        only a button on the Calibrate tab.
        """
        if self.calibrator is None or self.recipes is None:
            self._say(f"{entry['name']}: calibration is not available in this "
                      "session; skipping", "warn")
            return
        try:
            recipe = self.recipes.get(entry.get("recipeId") or "")
        except DeviceError as exc:
            self._say(f"{entry['name']}: {exc}", "warn")
            return

        self._wait_for_start(entry)
        end_at = entry.get("endAt")
        if end_at and time.time() >= end_at:
            self._say(f"{entry['name']} reached its end time before it started",
                      "warn")
            return

        # The cover is about to close and the panel is about to come on; a
        # guider left running would spend the whole set hunting for a star it
        # cannot see.  The next target starts it again.
        guider = self.manager.get("guider")
        if guider is not None and guider.connected and guider.guiding:
            self._say("Stopping guiding for the calibration task")
            with contextlib.suppress(Exception):
                guider.stop_guiding()

        self._set("calibrating", entry["name"])
        self._say(f"Calibration task: {entry['name']}")
        self.calibrator.run(
            recipe, entry.get("rigId") or None,
            should_abort=lambda: (self._abort.is_set() or self._skip.is_set()
                                  or bool(end_at and time.time() >= end_at)),
            report=lambda text: self._set("calibrating", f"{entry['name']}: {text}"))
        if self._skip.is_set():
            raise _Skipped()

    # -- the all-sky survey ------------------------------------------------
    def _run_allsky(self, entry: dict[str, Any], target: dict[str, Any]) -> None:
        """Work through the all-sky grid for as long as this entry is given.

        Different from every other target in one way that matters: it is never
        finished, so it does not run until it has taken what was asked for — it
        runs until its scheduled end and then stops, having moved the survey on
        by however many fields fitted.  Which fields those are is decided once,
        at the start, by following the meridian; see `allsky.night_plan`.
        """
        site = self._site()
        if site is None:
            raise DeviceError("the observing site is not set")
        latitude, longitude = site
        parameters = target.get("allsky") or {}
        goal = [dict(item) for item in (parameters.get("goal") or [])]
        if not goal:
            self._say(f"{entry['name']} has no filters set for a field; skipping",
                      "warn")
            return

        self._wait_for_start(entry)
        settings = {**self.config.section("allsky"), **(parameters.get("plan") or {})}
        overheads = {
            "perFrame": float(self.config.get("schedule", "perFrameSeconds", 15.0)),
            "filterChange": float(self.config.get("schedule",
                                                  "filterChangeSeconds", 20.0)),
            "perPanel": float(self.config.get("schedule", "perPanelSeconds", 90.0)),
        }

        start_at = max(time.time(), float(entry.get("startAt") or 0.0))
        end_at = entry.get("endAt")
        if not end_at:
            # No end time means "until the sky runs out", which for a survey is
            # the end of astronomical night rather than for ever.
            night = schedule.night(latitude, longitude)
            end_at = (night.get("dawnAstronomical") or night.get("windowEnd")
                      or start_at + 8 * 3600.0)
        end_at = float(end_at)
        if end_at <= start_at:
            self._say(f"{entry['name']} has no time left in its window", "warn")
            return

        fields = allsky.grid(parameters["fieldWidth"], parameters["fieldHeight"],
                             parameters.get("overlap", 0.2),
                             parameters.get("decMin", -90.0),
                             parameters.get("decMax", 90.0),
                             parameters.get("stagger", True))
        # Every telescope shoots the same field at the same time, so a field
        # costs what one telescope's share of the goal costs — capped, so a long
        # goal is built up over several passes instead of forcing the night into
        # a scatter of unconnected fields.
        per_field = allsky.visit_seconds(
            goal, overheads, float(settings.get("visitMinutes") or 0.0),
            max(1, len(self.rigs.imaging())))
        # The angle the grid was laid out for. With a rotator the camera is put
        # there for every field; without one it is what the operator set, and
        # the grid was sized around it.
        rotation = parameters.get("rotation")

        forecast = allsky.night_plan(
            fields, self.progress.survey(target["id"]) if self.progress else {},
            goal, latitude, longitude, start_at, end_at, per_field, settings)
        if not forecast["fields"]:
            self._say(f"{entry['name']}: "
                      f"{forecast.get('detail') or 'nothing to shoot'}", "warn")
            return
        self._say(f"{entry['name']}: about {len(forecast['fields'])} field(s) in "
                  f"this slot, following the meridian — no flips", "success")
        with self._lock:
            self._panels = len(forecast["fields"])

        # Chosen one at a time rather than as a list settled in advance.  A
        # field can take longer than it was budgeted, the clouds can take an
        # hour out of the middle, a slave camera can drop out and halve the
        # rate — and after any of those the list made at dusk is describing a
        # night that is no longer happening.  Asking again before each field
        # costs milliseconds and is always right.
        attempted: set[str] = set()
        position = 0
        # The shortest frame in the goal: with less than this left there is no
        # point choosing another field, only time to slew to it.
        shortest = min((float(g.get("exposure") or 0) for g in goal
                        if float(g.get("exposure") or 0) > 0), default=0.0)
        while time.time() + shortest < end_at:
            self._check()
            if self._skip.is_set():
                raise _Skipped()

            mount = self.manager.get("mount")
            where = None
            if mount is not None and mount.connected:
                with contextlib.suppress(Exception):
                    where = (mount.ra, mount.dec)

            plan = allsky.night_plan(
                fields, self.progress.survey(target["id"]) if self.progress else {},
                goal, latitude, longitude, time.time(), end_at, per_field,
                settings, start_at_position=where, exclude=attempted,
                width_hint=parameters["fieldWidth"],
                height_hint=parameters["fieldHeight"])
            if not plan["fields"]:
                reason = plan.get("detail") or "nothing left to shoot in this window"
                self._say(f"{entry['name']}: {reason}")
                return

            field = plan["fields"][0]
            attempted.add(field["id"])
            position += 1
            with self._lock:
                self._panel = position
                self._panels = max(self._panels, position)

            label = f"{entry['name']} {field['id']}"
            self._goto({"ra": field["ra"], "dec": field["dec"],
                        "rotation": rotation},
                       f"{field['id']} ({field['altitude']:.0f}° up, "
                       f"{field['slew']:.1f}° away)")
            self._maybe_focus()
            self._start_guiding()
            # The output is pointed at the field's folder only once there is
            # something to put in it: slewing and focusing can eat the rest of
            # the window, and a survey folder full of empty directories for
            # fields that were never shot is a record that lies.
            # The visit ends at whichever comes first: the goal met, the visit
            # budget spent, or the window closing.
            self._shoot_allsky(entry, target, field, goal, label,
                               min(end_at, time.time() + field["seconds"]),
                               end_at)

        self._say(f"{entry['name']} reached its end time after {position} "
                  f"field(s)", "warn")

    def _prepare_allsky_output(self, entry: dict[str, Any],
                               target: dict[str, Any],
                               field: dict[str, Any]) -> None:
        """Point every camera at this field's own folder.

        One folder per field, not per night: a survey field is shot in pieces
        across months, and everything that belongs to it belongs together.  The
        night is in the filename, so nothing is lost by not being in the path.
        """
        root = self.rigs.master.capture.root_dir / capture.clean_target(entry["name"])
        for rig in self.rigs.imaging():
            rig.capture.set_output(
                save=True, directory=str(root / field["id"]),
                target=field["id"],
                object_name=f"{entry['name']} {field['id']}")
            rig.capture.set_context(target=entry["name"], targetId=target.get("id"),
                                    entryId=entry.get("id"), mosaic=False,
                                    panel=int(field.get("index", 0) or 0),
                                    panelRa=field.get("ra"), panelDec=field.get("dec"))
            rig.capture.calibration_context = "survey"

    def _shoot_allsky(self, entry: dict[str, Any], target: dict[str, Any],
                      field: dict[str, Any], goal: list[dict[str, Any]],
                      label: str, visit_end: float, end_at: float) -> None:
        """Shoot one field until its goal is met, or this visit's time is up.

        The goal counts frames, not frames per telescope: three telescopes on
        one mount finish a field in a third of the time and leave three times
        the depth behind them, which is the whole reason for having three.

        A visit need not finish the goal.  Leaving before it is done is what
        lets the night walk a run of touching fields rather than sitting on one
        while the sky turns its neighbours past the meridian; the record is per
        frame, so the next visit carries on from exactly where this one stopped.
        """
        rigs = self.rigs.imaging()
        if not rigs:
            raise DeviceError("no camera is connected")

        recorded = self.progress.field(target["id"], field["id"]) if self.progress else {}
        outstanding: dict[str, dict[str, Any]] = {}
        for item in goal:
            name = str(item.get("name") or "") or "unfiltered"
            already = int((recorded.get("filters", {}).get(name) or {})
                          .get("frames", 0))
            wanted = max(0, int(item.get("count") or 0) - already)
            if wanted > 0 and float(item.get("exposure") or 0) > 0:
                outstanding[name] = {"left": wanted,
                                     "exposure": float(item["exposure"])}
        if not outstanding:
            return
        # Is there time for even one frame? If not, nothing about this field
        # should be touched — including its folder.
        soonest = min(v["exposure"] for v in outstanding.values())
        if time.time() + soonest > end_at:
            self._say(f"{label}: no time left in the window for it", "warn")
            return
        self._prepare_allsky_output(entry, target, field)

        slots = 0
        while outstanding:
            self._check()
            if self._skip.is_set():
                raise _Skipped()
            # Whichever filter is furthest from done, so a field that runs out
            # of time is short of everything evenly rather than missing one
            # colour entirely.
            name = max(outstanding, key=lambda key: outstanding[key]["left"])
            exposure = outstanding[name]["exposure"]
            if time.time() + exposure > end_at:
                self._say(f"{label}: out of time part way through "
                          f"({outstanding[name]['left']} {name} frame(s) short)",
                          "warn")
                return
            if slots and time.time() + exposure > visit_end:
                left = sum(v["left"] for v in outstanding.values())
                self._say(f"{label}: {slots} frame(s) this visit, {left} still "
                          "wanted — moving on while its neighbours are up")
                return

            changed = self._parallel(
                [(rig, lambda rig=rig: self._select_filter(rig, name))
                 for rig in rigs], "filter")
            self._maybe_focus(filter_changed=changed["results"])

            slots += 1
            with self._lock:
                self._slot = slots
                self._slots = slots + max(v["left"] for v in outstanding.values())
            for rig in rigs:
                self._set_rig(rig, frame=slots, filter=name, state="exposing")
            self._set("imaging", f"{label}: {name} {exposure:g}s "
                                 f"({outstanding[name]['left']} to go)")

            self._parallel([(rig, lambda rig=rig: self._await_cooling(rig))
                            for rig in rigs], "cool")
            outcome = self._parallel(
                [(rig, lambda rig=rig: rig.capture.capture_blocking(
                    exposure, frame_type="light")) for rig in rigs], "expose")

            for rig in rigs:
                self._set_rig(rig, state="")
                message = outcome["errors"].get(rig.id)
                if message is None:
                    continue
                if rig.id == self.master.id:
                    raise DeviceError(message)
                self._say(f"{rig.name}: frame failed — {message}", "warn")

            for rig in rigs:
                record = outcome["results"].get(rig.id)
                if record is None:
                    continue
                # Written to the log before it is counted anywhere else: this is
                # the record the survey is measured by, months from now.
                if self.progress is not None:
                    with contextlib.suppress(Exception):
                        self.progress.record(
                            target["id"], field["id"], name, record.exposure,
                            rig.capture.night_name(), rig.name,
                            record.path or "")
                if name in outstanding:
                    outstanding[name]["left"] -= 1
                    if outstanding[name]["left"] <= 0:
                        del outstanding[name]
                # `_record_frame` measures the sub and then credits it, so the
                # night's report carries the star size it was taken at.
                self._record_frame(rig, record, {"index": field["index"],
                                                 "ra": field["ra"],
                                                 "dec": field["dec"]},
                                   entry, name)

            if outstanding:
                self._dither()

        self._say(f"{label}: field complete", "success")

    def _run_entry(self, entry: dict[str, Any], target: dict[str, Any],
                   reframed: bool = False) -> None:
        """Shoot a target: every panel, every frame of its allocation."""
        site = self._site()
        if site is None:
            raise DeviceError("the observing site is not set")
        latitude, longitude = site

        self._wait_for_start(entry)
        end_at = entry.get("endAt")
        panels = target.get("panels") or []
        if panels and target.get("type") == "survey":
            # A survey sweep already knows its order: the planner sorted it by
            # what is about to set, inside a twilight window minutes long.
            # Re-sorting it as though it were a mosaic would throw that away.
            queue = sorted(panels, key=lambda p: p.get("index", 0))
        elif panels:
            order = schedule.tile_order(
                panels, latitude, longitude,
                schedule.night(latitude, longitude),
                float(self.config.get("schedule", "minAltitude", 30.0)))
            by_index = {p["index"]: p for p in panels}
            queue = [by_index[i] for i in order["order"] if i in by_index]
        else:
            queue = [{"index": 0, "ra": target["ra"], "dec": target["dec"],
                      "rotation": target.get("rotation", 0.0)}]

        options = plans.options_for(entry)
        # Going back over a mosaic: shoot only the panels that were picked. The
        # order they are shot in is still the one worked out above — this is a
        # filter on the queue, not a replacement for it, so a re-shoot of three
        # panels still visits them in the order the sky wants.
        wanted = [int(index) for index in (options.get("panels") or [])]
        if wanted and len(queue) > 1:
            chosen = [panel for panel in queue if panel.get("index") in wanted]
            if chosen:
                missing = sorted(set(wanted) - {p.get("index") for p in chosen})
                self._say(f"{entry['name']}: shooting "
                          f"{len(chosen)} of {len(queue)} panel(s) — "
                          + ", ".join(str(p["index"]) for p in chosen)
                          + (f" (no panel {missing} on this target)" if missing
                             else ""), "warn")
                queue = chosen
            else:
                self._say(f"{entry['name']}: none of the chosen panels "
                          f"({', '.join(str(i) for i in wanted)}) exist on this "
                          "target — shooting all of them", "warn")

        with self._lock:
            self._panels = len(queue)
        # A long slew across the sky is exactly when focus has moved, so an
        # operator who asked for a sweep at the start of a target gets one.
        if options.get("focusOnStart"):
            for rig in self._focusable():
                self._refocus_reason[rig.id] = f"{entry['name']}: focus on start"

        for position, panel in enumerate(queue, start=1):
            self._check()
            if self._skip.is_set():
                raise _Skipped()
            # Before the slew, and allowing for what a slew, a centre and a
            # focus sweep are about to cost. Starting all that with four minutes
            # left is how the night runs past its end.
            reason = self._past_deadline(entry, end_at, self._setup_seconds())
            if reason:
                self._say(f"{reason} — not starting {entry['name']}", "warn")
                return
            with self._lock:
                self._panel = position
                self._panel_index = int(panel.get("index", 0) or 0)

            mosaic = len(queue) > 1
            object_name, panel_tag, label = panel_labels(entry["name"], panel, mosaic)

            # Named before the slew, not after it: centring and focusing both
            # take frames, and a frame taken while the labels still said the
            # last panel would be filed under the wrong one.  Every telescope
            # writes the same OBJECT, panel included, so frames off two scopes
            # sort into the same set of stacks.  The filename carries the panel
            # too, while the folder stays keyed on the target so one mosaic
            # remains one folder.
            stamp = target.get("collab") or {}
            for rig in self.rigs.imaging():
                rig.capture.set_output(target=entry["name"], object_name=object_name,
                                       panel=panel_tag)
                # What only the planner knows, for the header of every frame:
                # which target and panel, the panel's own framing, and the
                # collaboration it is shot for.
                rig.capture.set_context(
                    target=entry["name"], targetId=target.get("id"),
                    entryId=entry.get("id"), mosaic=mosaic,
                    panel=int(panel.get("index", 1) or 1), panels=len(queue),
                    panelRa=panel.get("ra"), panelDec=panel.get("dec"),
                    panelAngle=panel.get("rotation"),
                    collabProject=stamp.get("project") or None,
                    collabProjectName=stamp.get("projectName") or None,
                    collabTask=stamp.get("task") or None)
                # Which lights get a calibrated copy written beside them is a
                # setting, but it can only be acted on by something that knows
                # what is being shot — and only the sequencer does.
                rig.capture.calibration_context = (
                    "survey" if target.get("type") == "survey" else "light")

            self._goto(panel, label)
            if position == 1 and not reframed:
                self._check_camera_angle(entry, target, panel)
            # Checked again after the slew: a slew across the sky plus a centre
            # is minutes, and the field may have set while the mount was moving.
            # This is the check that stops the focuser sweeping through a
            # telescope that is now pointing at the ground.
            reason = self._past_deadline(entry, end_at)
            if reason or self._too_low(panel, 0.0, label, entry):
                if reason:
                    self._say(f"{reason} — stopping before the focus run", "warn")
                return
            # Focus first, then guide: a focus run would only have to pause the
            # guider it had just settled.
            self._maybe_focus(panel=panel, label=label, entry=entry)
            self._start_guiding()
            if not self._shoot(entry, panel, label, end_at):
                # Out of night, or the field has set. Later panels are lower
                # still, so there is nothing to be gained by trying them.
                return

            # A survey field counts as covered once it has actually been shot,
            # in the Sun's frame rather than in RA and Dec.
            if self.coverage is not None and panel.get("cell"):
                with contextlib.suppress(Exception):
                    self.coverage.record_many([panel])

        self._say(f"{entry['name']}: the whole allocation is shot", "success")

    #: How far the camera may measure from the angle a fixed-angle mosaic was
    #: laid at before the mosaic is laid again. The same figure the program
    #: uses when it lays one out.
    ANGLE_TOLERANCE = 2.0

    def _check_camera_angle(self, entry: dict[str, Any], target: dict[str, Any],
                            panel: dict[str, Any]) -> None:
        """After the first slew of a collaboration: is the camera where the
        panels assume it is?

        On a rig with no rotator the angle the panels were laid at came out of
        the settings - a number somebody typed, or a solve from months ago -
        and the camera may sit somewhere else now. The plate solve that just
        centred the first panel measured it. If it disagrees by more than the
        tolerance, whoever built this is told, lays the mosaic out again at
        the real angle, and the target is started over on the new panels.
        A rig with a rotator commands its angle and has nothing to check.
        """
        if self.on_camera_angle is None:
            return
        if not (target.get("collab") or {}).get("task"):
            return
        if target.get("align") != "fixed":
            return
        rotator = self.manager.get("rotator")
        if rotator is not None and rotator.connected:
            return
        solved = (self.solver.status() or {}).get("result") or {}
        measured = solved.get("rotation")
        if measured is None:
            return
        expected = panel.get("rotation")
        if expected is None:
            expected = target.get("rotation") or 0.0
        gap = abs(((float(measured) - float(expected) + 180.0) % 360.0) - 180.0)
        if gap <= self.ANGLE_TOLERANCE:
            return
        self._say(f"{entry['name']}: the camera measures {float(measured):.1f}° "
                  f"on the sky, {gap:.1f}° from the {float(expected):.1f}° its "
                  "panels were laid at - laying the mosaic out again", "warn")
        try:
            self.on_camera_angle(target["id"], float(measured))
        except Exception as exc:                  # noqa: BLE001 - reported, not fatal
            self._say(f"{entry['name']}: could not lay the mosaic out again - "
                      f"{exc}", "error")
            return
        raise _Reframed()

    # -- pointing ----------------------------------------------------------
    def _record_overhead(self, kind: str, seconds: float) -> None:
        """Note what something really took, for the planner to cost the next one."""
        store = getattr(self.rigs, "overheads", None)
        if store is not None:
            with contextlib.suppress(Exception):
                store.record(kind, seconds)

    def _home_mount(self) -> None:
        """Send the mount to its home switches, once, before the first slew.

        A mount that has been power-cycled, hand-slewed, or left somewhere by
        another program does not know where it is pointing, and every slew after
        that is wrong by the same amount — which shows up as a plate solve that
        cannot find the field and a night that never starts.  Homing first costs
        a minute and removes the whole class of problem.

        Failure here is reported and stepped over rather than thrown: a mount
        that will not home is still a mount that will usually slew, and a plate
        solve will catch the pointing anyway.
        """
        with self._lock:
            if self._homed:
                return
            self._homed = True                  # one attempt per run, good or bad

        settings = self._settings()
        mount = self.manager.get("mount")
        if mount is None or not mount.connected:
            return
        self._release_mount(mount)
        if not settings.get("homeAtStart", True):
            return
        if not getattr(mount, "can_find_home", False):
            self._say("The mount cannot home itself; going straight to the "
                      "first target")
            return
        if mount.at_home:
            self._say("Mount is already at home")
            return

        timeout = float(settings.get("homeTimeoutMinutes") or 5.0) * 60.0
        self._set("homing", "homing the mount")
        self._say("Homing the mount before the first slew")
        started = time.monotonic()
        try:
            mount.find_home()
            self._wait_for_mount(mount, timeout=timeout)
        except DeviceError as exc:
            self._say(f"Could not home the mount: {exc} — carrying on", "warn")
            return
        if mount.at_home:
            self._say(f"Mount homed in {time.monotonic() - started:.0f}s", "success")
        else:
            self._say("The mount finished moving but does not report being at "
                      "home; carrying on", "warn")

    def _release_mount(self, mount) -> None:
        """Make a mount that was put away able to move again.

        A parked mount will not move for anything until it is released, and
        it is released whether or not it is about to be homed, because a
        night that parked itself yesterday has to be able to start today. A
        mount still on its way to park - a park is a slew, and a slow one on
        some drivers - is waited for first, since unparking one mid-slew is
        undefined. Tracking is switched on because most drivers refuse a slew
        without it: the ASCOM simulator's exact words are "SlewToCoordinates
        is not allowed when tracking is False", and that was the first slew
        of a night that had parked itself.
        """
        timeout = float(self._settings().get("parkTimeoutSeconds") or 300.0)
        if getattr(mount, "slewing", False):
            self._set("homing", "waiting for the mount to finish moving")
            deadline = time.monotonic() + timeout
            while getattr(mount, "slewing", False) and time.monotonic() < deadline:
                self._check()
                time.sleep(0.5)
        if getattr(mount, "at_park", False):
            self._set("homing", "releasing the mount from park")
            try:
                mount.unpark()
                self._say("Mount released from park")
            except Exception as exc:              # noqa: BLE001 - the slew will say
                self._say(f"Could not release the mount from park: {exc}", "warn")
        self._ensure_tracking(mount)

    def _ensure_tracking(self, mount) -> None:
        """Tracking on, if the mount says it is off. Never fatal."""
        try:
            if getattr(mount, "tracking", True) is False:
                mount.set_tracking(True)
                self._say("Tracking switched on")
        except Exception as exc:                  # noqa: BLE001 - the slew will say
            self._say(f"Could not switch tracking on: {exc}", "warn")

    def _goto(self, panel: dict[str, Any], label: str) -> None:
        """Point the mount.  The slaves are bolted to it and come along."""
        self._home_mount()
        self._set("slewing", f"slewing to {label}")
        self._say(f"Slewing to {label}")
        started = time.monotonic()
        mount = self.manager.require("mount")
        self._ensure_tracking(mount)

        rotator = self.manager.get("rotator")
        rotation = panel.get("rotation")
        rotating = (rotation is not None and rotator is not None
                    and rotator.connected)
        if rotating:
            rotator.move_absolute(float(rotation))

        mount.slew_to(astro.normalise_ra_hours(panel["ra"]), float(panel["dec"]))
        self._wait_for_mount(mount)

        if rotating:
            self._set("rotating", "waiting for the rotator")
            deadline = time.monotonic() + 300
            while rotator.moving:
                self._check()
                if time.monotonic() > deadline:
                    raise DeviceError("the rotator did not finish in time")
                time.sleep(0.3)

        if self.solver.executable() is not None:
            self._set("centring", f"plate solving {label}")
            try:
                # The framing angle goes in as well, so the same solve that
                # centres the panel also corrects the rotator onto the sky
                # angle rather than trusting its own calibration.
                self.solver.start("center", None, astro.normalise_ra_hours(panel["ra"]),
                                  float(panel["dec"]),
                                  rotation if rotating else None)
                self._wait_for_solver()
                self._solve_failures = 0
            except DeviceError as exc:
                # A failed centre is not a reason to abandon the night; the frame
                # is still usable, just not perfectly framed.
                self._say(f"Could not centre on {label}: {exc}", "warn")
                self._solve_failures += 1
                if self._solve_failures >= 3:
                    self._warn("solver.failing", "warning", "Plate solving keeps failing",
                               f"{self._solve_failures} centring solves in a row have failed; "
                               f"last: {exc}",
                               "Cloud, focus, or a wrong focal length in Site & Optics. "
                               "Targets are being shot where the mount put them.")

        # Slew, settle and centre together: what one panel costs before a single
        # frame of it is taken, which on a mosaic is paid once per panel.
        self._record_overhead("slew", time.monotonic() - started)

    def _wait_for_mount(self, mount, timeout: float = 600.0) -> None:
        time.sleep(1.0)
        deadline = time.monotonic() + timeout
        while mount.slewing:
            self._check()
            if time.monotonic() > deadline:
                raise DeviceError("the mount did not finish slewing in time")
            time.sleep(0.4)
        self._sleep(float(self._settings().get("settleSeconds") or 10.0))

    def _wait_for_solver(self, timeout: float = 900.0) -> None:
        deadline = time.monotonic() + timeout
        time.sleep(0.5)
        while self.solver.busy:
            self._check()
            if time.monotonic() > deadline:
                self.solver.abort()
                raise DeviceError("plate solving took too long")
            time.sleep(0.4)
        status = self.solver.status()
        if status.get("error"):
            raise DeviceError(status["error"])

    # -- focus -------------------------------------------------------------
    def _focusable(self) -> list[Any]:
        rigs = []
        for rig in self.rigs.imaging():
            focuser = rig.manager.get("focuser")
            if focuser is not None and focuser.connected:
                rigs.append(rig)
        return rigs

    #: How long a telescope's drift trigger is muted after a failed sweep, by
    #: how many times in a row it has now failed.  A cloud passes in minutes; a
    #: focuser that has lost its grub screw does not fix itself, and after the
    #: last of these the telescope stops asking for the night.
    FOCUS_BACKOFF_MINUTES = (10.0, 30.0, 90.0)

    def _focus_failed(self, rig, message: str) -> None:
        """Note that a telescope could not focus, and stop it asking again yet.

        Refocusing is a whole-mount event: every telescope stops imaging and
        sweeps.  So a telescope that cannot focus is not merely failing to
        improve itself, it is spending everything else's night as well — which
        is why a failure buys silence rather than an immediate retry.
        """
        failures = self._focus_failures.get(rig.id, 0) + 1
        self._focus_failures[rig.id] = failures
        # A fresh two-in-a-row is required before the drift trigger fires again,
        # on top of the wait below.
        self._hfr_over_limit.pop(rig.id, None)
        self._refocus_reason.pop(rig.id, None)

        if failures > len(self.FOCUS_BACKOFF_MINUTES):
            self._focus_blocked_until[rig.id] = float("inf")
            self._say(f"{rig.name}: autofocus failed {failures} times "
                      f"({message}) — leaving it alone for the rest of the run. "
                      "Focus it by hand from the Focuser panel once it is "
                      "sorted.", "error")
            return

        minutes = self.FOCUS_BACKOFF_MINUTES[failures - 1]
        self._focus_blocked_until[rig.id] = time.time() + minutes * 60.0
        self._say(f"{rig.name}: autofocus failed — {message}. Not asking again "
                  f"for {minutes:g} minutes, so the other telescopes keep "
                  "imaging.", "warn")
        self._warn(f"focus.failed.{rig.id}", "warning" if failures < 2 else "critical",
                   f"Autofocus is failing on {rig.name}",
                   f"{failures} run(s) in a row: {message}.",
                   "Cloud or no stars in the field, a focuser that is not moving, or a "
                   "sweep that needs a bigger step. Frames are being taken at the last "
                   "good focus.")

    def _focus_muted(self, rig) -> bool:
        """Is this telescope's drift trigger still in its post-failure wait?"""
        until = self._focus_blocked_until.get(rig.id)
        return until is not None and time.time() < until

    def _maybe_focus(self, filter_changed: dict[str, bool] | None = None,
                     panel: dict[str, Any] | None = None, label: str = "",
                     entry: dict[str, Any] | None = None) -> None:
        """Focus every telescope at once, if any of them is due.

        They go together because a focus sweep defocuses the stars on purpose:
        while one telescope is sweeping, nothing else on the mount can be taking
        a usable frame anyway.  Doing them in series would double the time the
        mount spends not imaging for no gain at all.

        Refused outright when the field has set or the night has ended.  A focus
        sweep is the longest thing the sequencer does — nine points, several
        frames each, and it retries — so it is the worst possible thing to start
        on a telescope pointing at the ground, and on a starless frame it will
        fail slowly rather than quickly.  There is a check before this is called
        as well; this one is here because that check is the kind that gets
        forgotten when somebody adds another caller.
        """
        if panel is not None:
            if self._too_low(panel, 0.0, label or "this field", entry):
                self._say("Not focusing: the field is below the altitude limit",
                          "warn")
                return
            if self._past_deadline(entry, (entry or {}).get("endAt"),
                                   self._focus_seconds_estimate()):
                self._say("Not focusing: there is not enough night left for a "
                          "sweep", "warn")
                return
        # A telescope still in its post-failure wait is left out entirely: it
        # neither asks for a sweep nor gets one. Sweeping a focuser that has
        # already failed twice only spends the other telescopes' night again.
        rigs = [rig for rig in self._focusable() if not self._focus_muted(rig)]
        if not rigs:
            return

        changed = filter_changed or {}
        reasons: dict[str, str] = {}
        for rig in rigs:
            reason = (self._refocus_reason.get(rig.id)
                      or rig.focuser.due(bool(changed.get(rig.id))))
            if reason:
                reasons[rig.id] = reason
        if not reasons:
            return

        first = next(iter(reasons.values()))
        for rig in rigs:
            self._refocus_reason.pop(rig.id, None)
        self._set("focusing", f"autofocus: {first}")
        self._say("Autofocus on " + ", ".join(rig.name for rig in rigs)
                  + f" ({first})")

        # The guider must not chase stars that are deliberately being defocused,
        # and it should not lose its lock while the focuser sweeps either.
        guider = self.manager.get("guider")
        paused = False
        if guider is not None and guider.connected and guider.guiding:
            try:
                guider.set_paused(True)
                paused = True
                self._say("Guiding paused for the focus run")
            except DeviceError as exc:
                self._say(f"Could not pause guiding: {exc}", "warn")

        # Sweeping through one named filter keeps every run comparable and
        # keeps the offsets below meaningful — and finds stars far faster than a
        # 3 nm narrowband filter does. The imaging filter goes back afterwards.
        restore = self._use_focus_filter(rigs)

        try:
            # Skip ends the sweep as well as Stop: the sweep is for the target
            # being skipped, and a skip that waited two minutes for it to
            # finish read as a button that did nothing.
            outcome = self._parallel(
                [(rig, lambda rig=rig: rig.focuser.run(
                    should_abort=lambda: (self._abort.is_set()
                                          or self._skip.is_set()),
                    report=lambda text, rig=rig: self._set_rig(
                        rig, state=f"focus: {text}")))
                 for rig in rigs], "focus")
            if self._skip.is_set():
                # Cut short on purpose - not a focuser that failed, so no
                # back-off is charged against it.
                raise _Skipped()
            for rig in rigs:
                if rig.id in outcome["errors"]:
                    self._focus_failed(rig, outcome["errors"][rig.id])
                    continue
                # Star size after focusing is the yardstick the drift trigger
                # measures against, so it is reset here rather than at start.
                self._reference_hfr.pop(rig.id, None)
                self._hfr_over_limit.pop(rig.id, None)
                self._focus_failures.pop(rig.id, None)
                self._focus_blocked_until.pop(rig.id, None)
                # Focus now stands where this filter wants it, so that is the
                # offset the next filter change measures its move from.
                self._filter_offset[rig.id] = self._offset_for(
                    rig, self._filter_in_path(rig))
            with self._lock:
                self._focus_runs += 1
        finally:
            for rig, filter_name in restore.items():
                with contextlib.suppress(Exception):
                    self._select_filter(rig, filter_name)
            for rig in rigs:
                self._set_rig(rig, state="")
            if paused:
                try:
                    guider.set_paused(False)
                    self._say("Guiding resumed")
                except DeviceError as exc:
                    self._say(f"Could not resume guiding: {exc}", "warn")

    def _filter_in_path(self, rig) -> str:
        """The filter this telescope is shooting through right now."""
        wheel = rig.manager.get("filterwheel")
        if wheel is not None and wheel.connected:
            names = [filters.canonical(n) or str(n) for n in (wheel.names or [])]
            index = wheel.position
            if 0 <= index < len(names):
                return names[index]
        return filters.canonical(rig.config.get("camera", "fixedFilter", "") or "")

    def _offset_for(self, rig, name: str) -> int:
        offsets = rig.config.get("sequencer", "filterOffsets", {}) or {}
        if not isinstance(offsets, dict):
            return 0
        return int(filters.canonical_keys(offsets).get(filters.canonical(name), 0) or 0)

    def _use_focus_filter(self, rigs: list[Any]) -> dict[Any, str]:
        """Put each telescope on its autofocus filter, and say what to put back.

        Returns the filter that was in the path per telescope, for the caller to
        restore once the sweep is done.  A telescope with no named autofocus
        filter, or one already on it, is left alone and contributes nothing.
        """
        restore: dict[Any, str] = {}
        for rig in rigs:
            wanted = str(rig.config.get("sequencer", "autofocusFilter", "") or "").strip()
            if not wanted:
                continue
            wheel = rig.manager.get("filterwheel")
            if wheel is None or not wheel.connected:
                continue
            current = self._filter_in_path(rig)
            if not current or current == wanted:
                continue
            self._say(f"{rig.name}: focusing through {wanted}")
            try:
                self._select_filter(rig, wanted)
                # Put the imaging filter back either way: a wheel that would not
                # move has said so in the log, and re-selecting what is already
                # in the path costs nothing.
                restore[rig] = current
            except DeviceError as exc:
                self._say(f"{rig.name}: could not select {wanted} to focus "
                          f"through — {exc}", "warn")
        return restore

    # -- the meridian ------------------------------------------------------
    def _hours_to_meridian(self, ra_hours: float) -> float | None:
        site = self._site()
        if site is None:
            return None
        lst = astro.local_sidereal_hours(site[1])
        hour_angle = ((lst - ra_hours + 12.0) % 24.0) - 12.0
        return -hour_angle          # positive means the meridian is still ahead

    def _flip_due(self, panel: dict[str, Any], next_frame_seconds: float) -> bool:
        settings = self._settings()
        if not settings.get("meridianFlipEnabled", True):
            return False
        mount = self.manager.get("mount")
        if mount is None or not mount.connected:
            return False
        # Not conditional on the mount reporting a side of pier. A German
        # equatorial whose driver leaves SideOfPier unknown is still a mount
        # that will run into its limit, and the flip - a slew to the same
        # coordinates - is harmless on a fork, which turning the setting off
        # spares altogether.
        ahead = self._hours_to_meridian(panel["ra"])
        if ahead is None:
            return False
        pause = float(settings.get("flipPauseMinutes") or 5.0) / 60.0
        # Flip if this frame would still be running when the mount must stop.
        return ahead - (next_frame_seconds / 3600.0) < pause and ahead > -1.0

    def _flip(self, panel: dict[str, Any], label: str) -> None:
        settings = self._settings()
        mount = self.manager.require("mount")
        before = mount.side_of_pier

        wait_minutes = float(settings.get("flipAfterMinutes") or 2.0)
        ahead = self._hours_to_meridian(panel["ra"]) or 0.0
        delay = max(0.0, (ahead * 60.0 + wait_minutes) * 60.0)
        self._set("flipping", f"waiting {delay / 60:.1f} min for the meridian")
        self._say(f"Meridian flip for {label}: waiting {delay / 60:.1f} minutes", "warn")
        self._sleep(delay)

        guider = self.manager.get("guider")
        guiding = guider is not None and guider.connected and guider.guiding
        if guiding:
            self._say("Stopping guiding for the flip")
            with contextlib.suppress(Exception):
                guider.stop_guiding()

        self._set("flipping", "flipping")
        mount.slew_to(astro.normalise_ra_hours(panel["ra"]), float(panel["dec"]))
        self._wait_for_mount(mount)

        after = mount.side_of_pier
        if before is not None and after == before:
            self._say(f"The mount did not change side of pier ({after}); "
                      "it may not have flipped", "warn")
            self._warn("mount.flip", "warning", "The meridian flip may not have happened",
                       f"After the flip slew the mount still reports the {after} side "
                       f"of the pier on {label}.",
                       "Watch the mount: if it is really past the meridian on the wrong "
                       "side it will hit its limit. Check the driver's flip settings.")
        else:
            self._say(f"Flipped from {before} to {after}", "success")
        self._tell("activity", f"Meridian flip on {label}",
                   (f"The mount did not change side of pier ({after})"
                    if before is not None and after == before
                    else f"Flipped from {before} to {after}")
                   + "; re-centring and restarting guiding.")
        with self._lock:
            self._flips += 1

        rotator = self.manager.get("rotator")
        rotation = panel.get("rotation")
        if rotation is not None and rotator is not None and rotator.connected:
            # The rotator works in sky position angle, so re-commanding the same
            # angle puts the framing back where it was despite the flip.
            self._set("flipping", "restoring the camera angle")
            rotator.move_absolute(float(rotation))
            deadline = time.monotonic() + 300
            while rotator.moving:
                self._check()
                if time.monotonic() > deadline:
                    break
                time.sleep(0.3)

        if settings.get("flipSolve", True) and self.solver.executable() is not None:
            self._set("flipping", "re-centring after the flip")
            try:
                self.solver.start("center", None, astro.normalise_ra_hours(panel["ra"]),
                                  float(panel["dec"]))
                self._wait_for_solver()
            except DeviceError as exc:
                self._say(f"Could not re-centre after the flip: {exc}", "warn")

        if guiding:
            self._say("Restarting guiding")
            with contextlib.suppress(Exception):
                guider.start_guiding(recalibrate=False)

        # Focus shifts across a flip on most rigs — on all of them.
        self._maybe_focus()

    # -- watching the run --------------------------------------------------
    def _record_frame(self, rig, record: Any, panel: dict[str, Any],
                      entry: dict[str, Any], filter_name: str,
                      guide_lost: bool = False) -> None:
        """Measure the sub just taken, and every so often check the pointing.

        Star size catches a telescope drifting out of focus; a plate solve
        catches the mount drifting off target.  Both are what you want to find
        out during a run, not the following morning.  Star size is per
        telescope: two optical trains lose focus independently.
        """
        settings = self._settings()
        entry_row: dict[str, Any] = {
            "t": record.timestamp,
            "id": record.id,
            "filename": record.filename,
            "entryId": entry["id"],
            "rig": rig.id,
            "telescope": rig.name,
            "filter": filter_name,
            "exposure": record.exposure,
            "panel": panel.get("index", 0),
            "hfr": None,
            "stars": 0,
            "errorArcmin": None,
            # Taken while the guider had no lock, so very likely trailed. Kept
            # on the row rather than only in the log, because the chart is where
            # a run of bad subs is actually noticed.
            "guideLost": bool(guide_lost),
        }
        if guide_lost:
            self._set_aside(rig, record)

        if settings.get("measureFrames", True):
            try:
                measured = stars.measure(rig.capture.frame(record.id))
                if measured["hfd"] is not None:
                    # HFR is the radius; the measurement is a diameter.
                    entry_row["hfr"] = round(measured["hfd"] / 2.0, 3)
                    entry_row["stars"] = measured["stars"]
            except Exception as exc:             # noqa: BLE001 - never stop a run for this
                entry_row["detail"] = str(exc)

        hfr = entry_row["hfr"]
        if hfr is not None:
            with self._lock:
                reference = self._reference_hfr.setdefault(rig.id, hfr)
            drift = (hfr - reference) / reference * 100.0 if reference else 0.0
            entry_row["driftPercent"] = round(drift, 1)

            # Refocusing on measured drift is better than refocusing on a clock:
            # it acts when the optics have actually moved, and leaves them alone
            # when they have not. One sub can be a passing cloud, so it takes
            # two in a row over the line to trigger.
            trigger = float(rig.config.get("sequencer", "autofocusHfrPercent", 0) or 0)
            # A telescope that has just failed to focus stays quiet for a while:
            # its stars are still big, so without this it would queue a fresh
            # whole-mount sweep on every sub from here to dawn.
            if trigger and self._focus_muted(rig):
                trigger = 0.0
            if trigger and drift > trigger:
                over = self._hfr_over_limit.get(rig.id, 0) + 1
                self._hfr_over_limit[rig.id] = over
                if over >= 2 and not self._refocus_reason.get(rig.id):
                    self._refocus_reason[rig.id] = (
                        f"{rig.name}: stars grew {drift:.0f}% "
                        f"(HFR {hfr:.2f} against {reference:.2f})")
                    self._say(f"Refocus queued: {self._refocus_reason[rig.id]}", "warn")
            else:
                self._hfr_over_limit[rig.id] = 0

            limit = float(settings.get("focusWarnPercent") or 0)
            if limit and drift > limit and not trigger:
                self._say(f"{rig.name}: stars have grown {drift:.0f}% since the "
                          f"last focus (HFR {hfr:.2f} against {reference:.2f})", "warn")

        every = int(settings.get("solveEveryFrames") or 0)
        if every > 0:
            count = self._since_solve.get(rig.id, 0) + 1
            if count >= every:
                count = 0
                self._check_pointing(rig, record.id, panel, entry_row)
            self._since_solve[rig.id] = count

        # Credit the target as each sub lands, not at the end, so a run that is
        # stopped half way still counts what it actually shot — and credit it
        # with how the frame came out, which is what turns a running total into
        # a report of the night.
        entry_row["guideRms"] = self._guide_rms()
        with contextlib.suppress(Exception):
            self.targets.add_integration(
                entry["targetId"], filter_name, record.exposure,
                rig.capture.night_name(), hfr=entry_row["hfr"],
                guide_rms=entry_row["guideRms"], guide_lost=bool(guide_lost),
                telescope=rig.name)
        # And to the panel it landed on. A mosaic's panels are different sky,
        # and a collaboration counts depth per point of sky - so this is what
        # gets reported, panel by panel, rather than the target's total.
        if not guide_lost:
            with contextlib.suppress(Exception):
                self.targets.add_panel_integration(
                    entry["targetId"], rig.capture.night_name(),
                    int(panel.get("index", 0) or 0), filter_name, record.exposure,
                    hfr=entry_row["hfr"], guide_rms=entry_row["guideRms"])

        with self._lock:
            self._frames_log.append(entry_row)
            del self._frames_log[:-FRAME_HISTORY]

    def _guide_rms(self) -> float | None:
        """Total guide RMS in arcseconds right now, or None when not guiding."""
        guider = self.manager.get("guider")
        if guider is None or not guider.connected or not guider.guiding:
            return None
        try:
            value = guider.status().get("rmsTotal")
            return None if value is None else round(float(value), 3)
        except Exception:                        # noqa: BLE001 - informational only
            return None

    def _night_now(self) -> str:
        """The night the frames being taken belong to."""
        with contextlib.suppress(Exception):
            return self.master.capture.night_name()
        return ""

    def _set_aside(self, rig, record: Any) -> None:
        """Move a sub taken without a guide lock into a `suspect` folder.

        Nothing is ever deleted — the frame may be perfectly usable, and that is
        a judgement for the morning with the stack open.  It is moved out of the
        way so it is not stacked by accident, and the log says where it went.
        """
        self._say(f"{rig.name}: that frame was taken without a guide lock",
                  "warn")
        if not self._recovery_settings().get("discardLostFrames", False):
            return
        path = getattr(record, "path", None)
        if not path:
            return
        try:
            source = Path(path)
            destination = source.parent / "suspect" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
            self._say(f"{rig.name}: moved {source.name} to suspect/", "warn")
        except OSError as exc:
            self._say(f"{rig.name}: could not set that frame aside — {exc}", "warn")

    def _check_pointing(self, rig, image_id: str, panel: dict[str, Any],
                        row: dict[str, Any]) -> None:
        """Plate solve a sub in the background to confirm where we are pointed.

        In the background because a solve takes seconds and the next exposure
        should not wait for it.  If one is still running the check is skipped
        rather than queued: a backlog of stale pointing checks helps nobody, and
        with several telescopes that backlog would build several times faster.
        """
        if rig.solver.executable() is None:
            return
        if not self._checking.acquire(blocking=False):
            return

        def job() -> None:
            try:
                result = rig.solver.measure(image_id)
                separation = astro.separation_degrees(
                    result.ra, result.dec,
                    astro.normalise_ra_hours(panel["ra"]), float(panel["dec"])) * 60.0
                row["errorArcmin"] = round(separation, 2)
                row["solvedRa"] = result.ra
                row["solvedDec"] = result.dec
                limit = float(self._settings().get("pointingWarnArcmin") or 0)
                if limit and separation > limit:
                    self._say(f"Pointing check ({rig.name}): {separation:.1f}' from "
                              "where this target should be", "warn")
                # Past the recovery threshold the object is on its way out of
                # frame, so ask for a re-centre. Only the master's solve counts:
                # it is the telescope carrying the mount, and a slave measuring
                # its own small offset is not a reason to slew the whole rig.
                recentre = float(self._recovery_settings().get("recentreArcmin") or 0)
                if (rig.id == self.master.id and recentre and separation > recentre
                        and self._recovery_on("pointingRecovery")):
                    self._recentre_request = f"{separation:.1f}' off target"
            except Exception as exc:             # noqa: BLE001 - informational only
                row["solveDetail"] = str(exc)
            finally:
                self._checking.release()

        threading.Thread(target=job, daemon=True, name="pointing-check").start()

    def _start_guiding(self) -> None:
        """Make sure the guider is actually guiding before frames are taken.

        Nothing else does this.  Dithering between subs checks that guiding is
        running and quietly does nothing when it is not, and the meridian flip
        only restarts guiding it had stopped itself — so a night could run from
        end to end unguided, with every frame trailed and nothing in the log to
        say why.
        """
        settings = self.config.section("guiding")
        if not settings.get("startWithSequence", True):
            return
        guider = self.manager.get("guider")
        if guider is None or not guider.connected:
            return
        if guider.guiding:
            self._guiding_wanted = True
            return

        self._set("guiding", "starting the guider")
        self._say("Starting guiding")
        try:
            guider.start_guiding(
                settle_pixels=float(settings.get("settlePixels") or 1.5),
                settle_time=float(settings.get("settleTime") or 8.0),
                settle_timeout=float(settings.get("settleTimeout") or 60.0),
                recalibrate=False)
        except DeviceError as exc:
            # Worth a loud line and not worth abandoning the night for: an
            # unguided sub of a bright target is still a sub.
            self._say(f"Could not start guiding: {exc}. Carrying on unguided.",
                      "error")
            return

        # Wait for it to settle, so the first frame is not taken mid-slew.
        if self._wait_for_guiding(float(settings.get("settleTimeoutSeconds") or 180.0)):
            # Only now is guiding something worth watching and recovering: a
            # guider that never started is not one that has lost its star.
            self._guiding_wanted = True
            self._guide_lost_since = None
            self._say("Guiding", "success")
            return
        self._say(f"The guider is {guider.state} rather than guiding; carrying "
                  "on unguided", "warn")

    def _dither(self, entry: dict[str, Any] | None = None) -> None:
        """Nudge the guider between slots so hot pixels do not stack up.

        Called only when every telescope has finished its frame, which is the
        point of the slot: a dither during an exposure is a trailed sub, and on
        a slave nobody would notice until the morning.

        A target may set its own interval: a hundred thirty-second subs want
        dithering far less often than ten ten-minute ones, and the cost of a
        dither is the same settle either way.
        """
        settings = self.config.section("guiding")
        if not settings.get("ditherEnabled", True):
            return
        every = 0
        if entry is not None:
            every = int(plans.options_for(entry).get("ditherEveryFrames") or 0)
        if every <= 0:
            every = int(settings.get("ditherEveryFrames") or 1)
        every = max(1, every)
        self._since_dither += 1
        if self._since_dither < every:
            return
        self._since_dither = 0

        guider = self.manager.get("guider")
        if guider is None or not guider.connected or not guider.guiding:
            return

        self._set("dithering", "dithering")
        started = time.monotonic()
        try:
            guider.dither(
                pixels=float(settings.get("ditherPixels") or 3.0),
                ra_only=bool(settings.get("ditherRaOnly", False)),
                settle_pixels=float(settings.get("settlePixels") or 1.5),
                settle_time=float(settings.get("settleTime") or 8.0),
                settle_timeout=float(settings.get("settleTimeout") or 60.0))
        except DeviceError as exc:
            self._say(f"Dither failed: {exc}", "warn")
            return

        timeout = float(settings.get("settleTimeout") or 60.0) + 15.0
        deadline = time.monotonic() + timeout
        time.sleep(1.0)
        while guider.settling:
            self._check()
            if time.monotonic() > deadline:
                self._say("The guider did not settle in time; carrying on", "warn")
                return
            time.sleep(0.5)
        # A dither and its settle, which is part of what every frame costs.
        self._record_overhead("dither", time.monotonic() - started)
        # The next frame on every telescope is the first after a dither.
        for rig in self.rigs.imaging():
            rig.capture.mark_dithered()

    # -- recovery ----------------------------------------------------------
    #
    # The things that go wrong at three in the morning are not exotic.  The
    # guide star goes behind a cloud; the mount drifts until the object is
    # halfway out of the frame; a camera misses a download.  Each of those used
    # to end the night — either loudly, by killing the run, or worse, quietly,
    # by filling the disk with trailed subs of not-quite-the-right sky.
    #
    # So each is *noticed* between frames, *named* in the status so the screen
    # says what is being rescued, and *retried* a bounded number of times.  The
    # bound is the important half: a rig that needs rescuing every five minutes
    # has a fault that another retry will not fix, and going round that loop
    # until dawn wastes the night as thoroughly as stopping would.

    def _recovery_settings(self) -> dict[str, Any]:
        return self.config.section("recovery")

    def _recovery_on(self, key: str) -> bool:
        settings = self._recovery_settings()
        return bool(settings.get("enabled", True)) and bool(settings.get(key, True))

    def _recovering(self, kind: str, reason: str, attempt: int = 0) -> None:
        """Say what is being rescued.  Shown in the banner, not just the log."""
        with self._lock:
            self._recovery = {"kind": kind, "reason": reason, "attempt": attempt,
                              "since": time.time()}
        self._set("recovering", reason)
        self._say(f"Recovery ({kind}): {reason}", "warn")

    def _recovered(self, kind: str, detail: str, ok: bool) -> None:
        with self._lock:
            self._recovery = None
            self._recoveries.append({"t": time.time(), "kind": kind,
                                     "detail": detail, "ok": ok,
                                     "entryId": self._entry_id})
            del self._recoveries[:-60]
            self._rescues += 1
            self._target_rescues += 1
            entry_id = self._entry_id
        self._say(detail, "success" if ok else "error")
        self._tell("recovery", f"Recovered ({kind})" if ok
                   else f"Could not recover ({kind})", detail)
        # Counted against the night as well as the session, because the session
        # log is gone by morning and "was last Tuesday a fight?" is asked later.
        with contextlib.suppress(Exception):
            target_id = self._target_id_for(entry_id)
            if target_id:
                self.targets.note_recovery(target_id, self._night_now())

    def _target_id_for(self, entry_id: str | None) -> str:
        if not entry_id:
            return ""
        for entry in self.plan.raw()["entries"]:
            if entry["id"] == entry_id:
                return str(entry.get("targetId") or "")
        return ""

    def _budget_spent(self) -> bool:
        """Has this target been rescued more often than it is worth?"""
        limit = int(self._recovery_settings().get("maxPerTarget") or 0)
        return bool(limit) and self._target_rescues >= limit

    def _cooldown(self) -> None:
        self._sleep(float(self._recovery_settings().get("cooldownSeconds") or 0))

    def _give_up(self, reason: str) -> None:
        """Out of attempts.  Do whatever the operator asked for, and say so.

        Never returns: either this target is abandoned or the run is.
        """
        action = str(self._recovery_settings().get("onGiveUp") or "next")
        self._tell("giveUp", "Recovery gave up", f"{reason}\n\n{self._summary()}")
        if action == "next":
            self._say(f"{reason} — giving up on this target and moving on", "error")
            raise _Skipped()
        if action == "park":
            self._say(f"{reason} — stopping the sequence and parking the mount",
                      "error")
            self._make_safe(park=True)
        else:
            self._say(f"{reason} — stopping the sequence", "error")
        self._abort.set()
        raise _Stopped()

    def _stop_guiding(self) -> None:
        """Take PHD2 off the sky.

        Deliberately not conditional on `guider.guiding`. PHD2 that is looping,
        settling, calibrating or sitting in LostLock is still driving the guide
        camera and still sending pulses to the mount, and all of those states
        will happily run until morning. `stop_capture` is what actually ends it,
        and asking a stopped PHD2 to stop again costs nothing.
        """
        guider = self.manager.get("guider")
        if guider is None or not guider.connected:
            return
        with self._lock:
            self._guiding_wanted = False
        try:
            guider.stop_guiding()
        except Exception as exc:                  # noqa: BLE001 - reported, not fatal
            self._say(f"Could not stop guiding: {exc}", "warn")
            return
        self._say("Guiding stopped")

    def _save_log_copy(self) -> None:
        """Leave a copy of the session log beside the night's frames.

        The real log lives under the data directory of whichever machine ran the
        night. At a remote site that is exactly where you cannot get at it, and
        "it did something odd at 3am" is unanswerable without it. The capture
        folder is already synced — it is where the data goes — so a copy there
        arrives with the frames.

        Best effort throughout: a night's data is not worth failing over a log.
        """
        if not self.config.get("capture", "logWithData", True):
            return
        from . import logs

        source = logs.log_dir() / "astrocontrol.log"
        if not source.is_file():
            return
        folder = Path(self.rigs.master.capture.session_dir)
        folder.mkdir(parents=True, exist_ok=True)
        night = self.rigs.master.capture.night_name()
        destination = folder / f"astrocontrol-{night}.log"
        shutil.copy2(source, destination)
        self._say(f"Session log copied to {destination}")

    def shut_down(self) -> dict[str, Any]:
        """Stop everything and put the observatory to bed.

        Stop ends the run and leaves the rig exactly where it is, which is right
        for "that is enough for tonight" — you may well want to look at
        something by hand afterwards. This is the other one: something is wrong,
        or the night is over, and the rig should be safe without anybody else
        touching it.

        Ordered by what goes wrong if it is left undone, and each step is
        attempted whatever the one before it did: a mount that will not park is
        not a reason to leave the cover open and the cameras at -10 C.

        Returns what it managed, because "abort" that quietly half-worked is
        worse than one that says which half.
        """
        done: list[str] = []
        failed: dict[str, str] = {}

        def step(name: str, action) -> None:
            # Published as it goes: a shutdown can take ten minutes between a
            # slow park and a slow roof, and a button that says nothing for ten
            # minutes gets pressed again.
            with self._lock:
                self._shutdown_step = name
            try:
                action()
                done.append(name)
            except Exception as exc:              # noqa: BLE001 - report, continue
                failed[name] = str(exc)
                self._say(f"Shutting down: could not {name} — {exc}", "error")

        # The very first thing, before any reporting, any waiting and any
        # `self.running` check: latch the abort. From this instant nothing can
        # start another exposure, whatever else below succeeds or fails or
        # races. It used to be set inside `if self.running:`, so a shutdown that
        # arrived in the wrong moment left the flag clear and the run carried on
        # taking frames around a mount that had just been parked.
        self._abort.set()
        self._paused.clear()
        self._abort_cameras()

        self._say("Shutting down: stopping the sequence, parking, closing the "
                  "cover and warming the cameras", "warn")

        # Then wait for the run to actually notice. Most of what it could be
        # doing checks the flag within a second; the slow ones are a guider
        # settling and a focus sweep, so the wait has to allow for those.
        if self.running:
            step("stop the sequence", self.stop)
            deadline = time.monotonic() + float(
                self._settings().get("abortTimeoutSeconds") or 300.0)
            while self.running and time.monotonic() < deadline:
                time.sleep(0.5)
            if self.running:
                # Parking under a run that is still going is not safe, but
                # leaving it unparked is worse, so it still happens — and this
                # is recorded as a failure rather than quietly succeeding.
                # "Abort worked" with the telescope still slewing is how you end
                # up on the ground.
                failed["stop the sequence"] = (
                    "the sequence did not stop in time — it is still running, "
                    "so anything below may be undone by it")
                self._say("Shutting down: the sequence has not stopped. Parking "
                          "anyway, but check the mount by hand.", "error")
                self._tell("failure", "Abort could not stop the sequence",
                           "The observatory was parked, but the run was still "
                           "going. Check the mount.")

        step("stop guiding", self._stop_guiding)
        step("stop the cameras", self._abort_cameras)
        # The cover before the mount: closing it while the telescope is still
        # pointing up is what it is for, and a parked scope may have the cover
        # somewhere awkward.
        step("close the cover", self._close_cover)
        # Not `_make_safe`, which reports a failed park and carries on — right
        # for the end of an ordinary run, wrong here. An abort that says it
        # parked when it did not is worse than one that says it could not: you
        # read "shut down" and go to bed with the telescope still pointing up.
        step("park the mount", self._park_and_check)
        # The roof last of the moving parts: a dome that closes over a telescope
        # still pointing at the sky is how a shutter meets an OTA.
        step("close the roof", self._close_roof)
        # Warming outlives this call, as it does at the end of any run.
        step("warm the cameras", self._start_warming)

        with self._lock:
            self._shutdown_step = ""
        self._say("Shut down: " + (", ".join(done) or "nothing to do")
                  + (f" — but could not {', '.join(failed)}" if failed else ""),
                  "error" if failed else "success")
        return {"done": done, "failed": failed}

    def _park_and_check(self, timeout: float | None = None) -> None:
        """Park, then confirm the mount agrees that it is parked.

        Asking is not the same as it having happened: a driver can accept `Park`
        and do nothing, and some take minutes because parking is a slew. What
        settles it is `AtPark`.
        """
        if timeout is None:
            timeout = float(self._settings().get("parkTimeoutSeconds") or 300.0)
        mount = self.manager.get("mount")
        if mount is None or not mount.connected:
            return
        if mount.at_park:
            return
        self._set("parking", "parking the mount")
        # A slew still in progress - the one the run was in the middle of, or
        # a park somebody pressed a moment ago - is ended before Park is sent.
        # Park on top of a slew is undefined in the ASCOM spec, and one
        # simulator answered it by reporting Slewing for ever.
        if getattr(mount, "slewing", False):
            with contextlib.suppress(Exception):
                mount.abort_slew()
            settle_until = time.monotonic() + min(15.0, timeout)
            while getattr(mount, "slewing", False) and time.monotonic() < settle_until:
                time.sleep(0.25)
        mount.park()
        # Waits for `AtPark` to come true, not merely for `Slewing` to go false.
        # Plenty of drivers clear Slewing the moment the command is accepted —
        # the slew code has a settle at the top for exactly that reason — and
        # some set AtPark a beat after the motion stops. Watching only Slewing
        # meant a mount that had parked perfectly well was reported as failing,
        # which is its own kind of bad: an abort that cries wolf gets ignored.
        deadline = time.monotonic() + timeout
        # How long to keep asking after the mount has stopped moving. A driver
        # that parks instantly answers on the first pass and never reaches this.
        settle = min(5.0, timeout)
        stopped_at: float | None = None
        while True:
            if mount.at_park:
                self._say("Mount parked", "success")
                return
            now = time.monotonic()
            if now >= deadline:
                break
            if mount.slewing:
                stopped_at = None
            else:
                # Stopped moving but not parked yet. Some drivers set AtPark a
                # beat after the motion ends, so it is worth a few more asks —
                # but not the whole timeout, or a mount that will never park
                # holds the shutdown open for five minutes.
                if stopped_at is None:
                    stopped_at = now
                elif now - stopped_at >= settle:
                    break
            time.sleep(0.25)
        raise DeviceError("the mount does not report being parked")

    def _close_roof(self, timeout: float | None = None) -> None:
        """Shut the dome or roll-off, and stop it following the mount.

        Slaving is turned off first: a dome still chasing a parked mount will
        drive its shutter back round, and on a roll-off the slave logic has
        nothing sensible to do at all once the telescope has stopped.
        """
        if timeout is None:
            timeout = float(self._settings().get("roofTimeoutSeconds") or 300.0)
        dome = self.manager.get("dome")
        if dome is None or not dome.connected:
            return
        if getattr(dome, "can_slave", False) and dome.slaved:
            with contextlib.suppress(Exception):
                dome.set_slaved(False)
        if not getattr(dome, "can_shutter", False):
            # No shutter to close, but parking a dome is still worth doing.
            if getattr(dome, "can_park", False) and not dome.at_park:
                with contextlib.suppress(Exception):
                    dome.park()
            return
        if dome.shutter_state == "closed":
            return

        self._set("parking", "closing the roof")
        dome.close_shutter()
        deadline = time.monotonic() + timeout
        while dome.shutter_state in ("closing", "opening") \
                and time.monotonic() < deadline:
            time.sleep(1.0)
        if dome.shutter_state != "closed":
            raise DeviceError(f"the roof is {dome.shutter_state}")
        self._say("Roof closed", "success")
        if getattr(dome, "can_park", False) and not dome.at_park:
            with contextlib.suppress(Exception):
                dome.park()

    def _abort_cameras(self) -> None:
        for rig in self.rigs.all:
            with contextlib.suppress(Exception):
                rig.capture.abort()

    def _close_cover(self) -> None:
        """Shut the flat panel's cover, and put its light out.

        A panel left lit behind a closed cover is not dangerous, but it is a
        bulb burning all night for nothing.
        """
        panel = self.manager.get("flatpanel")
        if panel is None or not panel.connected:
            return
        with contextlib.suppress(Exception):
            panel.turn_off()
        if not getattr(panel, "has_cover", False):
            return
        if panel.cover_state == "closed":
            return
        self._set("parking", "closing the cover")
        panel.close_cover()
        deadline = time.monotonic() + float(
            self._settings().get("coverTimeoutSeconds") or 120.0)
        while panel.cover_state == "moving" and time.monotonic() < deadline:
            time.sleep(0.5)
        if panel.cover_state != "closed":
            raise DeviceError(f"the cover is {panel.cover_state}")
        self._say("Cover closed", "success")

    def _stop_warming(self) -> None:
        """Cut a warm-down short, for a new sequence that is about to cool anyway."""
        if not self.warming:
            return
        self._warm_stop.set()
        thread = self._warm_thread
        if thread is not None:
            thread.join(timeout=10.0)

    def cancel_warming(self, reason: str = "") -> bool:
        """Stop a warm-down and put the cameras back on their setpoint.

        Anything that is about to take a frame on purpose calls this. A warm-down
        runs for ten minutes after a sequence ends, raising the setpoint a few
        degrees a minute — so a calibration run started during one shoots its
        darks up a temperature ramp, and a dark library is indexed by
        temperature. Frames taken at "somewhere between -10 and +4" match
        nothing and are quietly worthless.

        Returns whether there was anything to stop, so the caller can say so.
        """
        if not self.warming:
            return False
        self._stop_warming()
        self._say(f"Warm-down cancelled{f': {reason}' if reason else ''} — "
                  "putting the cameras back on their setpoint", "warn")
        for rig in self.rigs.all:
            settings = rig.config.section("camera")
            if not settings.get("coolAtStart", True):
                continue
            camera = rig.manager.get("camera")
            if camera is None or not camera.connected or not camera.can_cool:
                continue
            with contextlib.suppress(Exception):
                camera.set_setpoint(float(settings.get("setpoint", -10.0)))
                camera.set_cooler(True)
                with self._lock:
                    # Owed again: whatever is about to be shot should wait for
                    # the sensor to come back down before the first frame.
                    self._cooling_wanted.add(rig.id)
        return True

    def warm_down(self) -> None:
        """Ramp the coolers off, for something other than a sequence that has
        finished with the cameras - a calibration run from the Calibrate tab."""
        self._start_warming()

    def _start_warming(self) -> None:
        """Ramp the coolers off on a thread of their own.

        Detached on purpose. Warming is several minutes of sleeping that has
        nothing to do with the sequence any more, and doing it on the sequencer
        thread meant Stop did not take effect until it finished.
        """
        if self.warming:
            return
        self._warm_stop.clear()
        rigs = [rig for rig in self.rigs.all
                if (camera := rig.manager.get("camera")) is not None
                and camera.connected and camera.can_cool
                and rig.config.section("camera").get("warmAtEnd", True)]
        if not rigs:
            return
        self._warm_thread = threading.Thread(
            target=self._warm_cameras, daemon=True, name="warm-down")
        self._warm_thread.start()

    def _make_safe(self, park: bool = False, stop_tracking: bool = False) -> None:
        """Put the mount somewhere it can be left.

        Guiding is stopped first: parking a mount out from under PHD2 leaves it
        chasing a star that is no longer in the sky.
        """
        self._stop_guiding()
        mount = self.manager.get("mount")
        if mount is None or not mount.connected:
            return
        try:
            if park:
                self._set("parking", "parking the mount")
                mount.park()
                self._say("Mount parked", "success")
            elif stop_tracking:
                mount.set_tracking(False)
                self._say("Tracking stopped")
        except Exception as exc:                  # noqa: BLE001 - reported, not fatal
            self._say(f"Could not make the mount safe: {exc}", "error")

    # -- the guide star ----------------------------------------------------
    def _guiding_fault(self) -> str | None:
        """Why the guider is not guiding, or None while it is."""
        guider = self.manager.get("guider")
        if guider is None or not guider.connected:
            return None                 # there is no guiding to lose
        if guider.guiding or guider.settling:
            return None
        state = guider.state
        if state == "LostLock":
            with contextlib.suppress(Exception):
                detail = guider.status().get("starLost")
                if detail:
                    return f"the guide star was lost — {detail}"
            return "the guide star was lost"
        if state in ("Stopped", "Looping", "Paused", "Unknown"):
            return f"the guider is {state.lower()} rather than guiding"
        return None                     # calibrating, or something else in hand

    def _guiding_healthy(self) -> bool:
        return self._guiding_fault() is None

    def _check_guiding(self) -> None:
        """Between slots: is the guider still doing its job, and if not, fix it.

        Called between frames rather than during one, because everything the fix
        does — stopping, re-selecting a star, calibrating — would ruin the sub in
        flight anyway.
        """
        if not self._guiding_wanted or not self._recovery_on("guidingRecovery"):
            return
        fault = self._guiding_fault()
        if fault is None:
            self._guide_lost_since = None
            return
        self._guide_lost_in_slot = True

        settings = self._recovery_settings()
        # PHD2 usually finds the star again by itself within an exposure or two.
        # A cloud is over before a re-acquisition would have finished, so the
        # grace period is not politeness — it is the faster of the two options.
        grace = float(settings.get("guideGraceSeconds") or 0)
        if self._guide_lost_since is None:
            self._guide_lost_since = time.monotonic()
        waited = time.monotonic() - self._guide_lost_since
        if grace and waited < grace:
            self._recovering("guiding", f"{fault}; waiting {grace:g}s to see if "
                                        "it comes back on its own")
            deadline = self._guide_lost_since + grace
            while time.monotonic() < deadline:
                self._check()
                if self._guiding_healthy():
                    self._recovered("guiding", "The guide star came back", True)
                    self._guide_lost_since = None
                    return
                time.sleep(1.0)

        attempts = int(settings.get("guideRestartAttempts") or 0)
        recalibrate_after = int(settings.get("recalibrateAfterAttempts") or 0)
        guiding = self.config.section("guiding")
        guider = self.manager.get("guider")
        if guider is None or not guider.connected:
            return

        for attempt in range(1, attempts + 1):
            if self._budget_spent():
                self._give_up("The guider keeps losing its star")
            # A fresh calibration is slow and is the only thing that fixes a
            # mount that has been nudged by hand, so it is what the later
            # attempts do rather than what every attempt does.
            recalibrate = bool(recalibrate_after) and attempt > recalibrate_after
            self._recovering(
                "guiding",
                f"{fault}; restarting guiding"
                + (" with a fresh calibration" if recalibrate else "")
                + f" (attempt {attempt} of {attempts})", attempt)
            with contextlib.suppress(Exception):
                guider.stop_guiding()
            self._sleep(2.0)
            try:
                guider.start_guiding(
                    settle_pixels=float(guiding.get("settlePixels") or 1.5),
                    settle_time=float(guiding.get("settleTime") or 8.0),
                    settle_timeout=float(guiding.get("settleTimeout") or 60.0),
                    recalibrate=recalibrate)
            except DeviceError as exc:
                self._say(f"Could not start guiding: {exc}", "warn")
                self._cooldown()
                continue
            if self._wait_for_guiding(
                    float(guiding.get("settleTimeoutSeconds") or 180.0)):
                self._recovered("guiding", "Guiding again", True)
                self._guide_lost_since = None
                return
            self._cooldown()

        # Out of attempts, but an unguided sub of a bright target is still a
        # sub: the night carries on and the log says loudly why it is worse.
        self._recovered(
            "guiding",
            "Could not get the guider going again; carrying on unguided — "
            "these frames will not be as tight", False)
        self._guide_lost_since = None
        self._guiding_wanted = False

    def _wait_for_guiding(self, timeout: float) -> bool:
        """Wait for the guider to settle onto a star.  True if it did."""
        deadline = time.monotonic() + timeout
        time.sleep(1.0)
        guider = self.manager.get("guider")
        while time.monotonic() < deadline:
            self._check()
            if guider is None or not guider.connected:
                return False
            if guider.guiding and not guider.settling:
                return True
            if guider.state in ("Stopped", "LostLock"):
                return False
            time.sleep(0.5)
        return False

    # -- the pointing ------------------------------------------------------
    def _check_recentre(self, panel: dict[str, Any], label: str) -> None:
        """Act on a pointing check that found the target drifting out of frame.

        The check itself runs in the background off a sub that has already been
        taken; acting on it has to happen here, between frames, where a slew is
        safe to make.
        """
        reason = self._recentre_request
        if not reason or not self._recovery_on("pointingRecovery"):
            self._recentre_request = None
            return
        self._recentre_request = None

        if self._budget_spent():
            self._give_up("The mount keeps drifting off target")
        attempts = max(1, int(self._recovery_settings().get("recentreAttempts") or 1))
        self._recovering("pointing", f"{label} has drifted {reason}; going back "
                                     "to the target and solving again")

        guider = self.manager.get("guider")
        was_guiding = guider is not None and guider.connected and guider.guiding
        if was_guiding:
            with contextlib.suppress(Exception):
                guider.stop_guiding()

        recovered = False
        for attempt in range(1, attempts + 1):
            try:
                self._goto(panel, label)
                recovered = True
                break
            except DeviceError as exc:
                self._say(f"Could not get back on target (attempt {attempt} of "
                          f"{attempts}): {exc}", "warn")
                self._cooldown()

        self._recovered("pointing",
                        "Back on target" if recovered
                        else "Could not get back on target; carrying on where "
                             "the mount is pointing", recovered)
        if was_guiding:
            self._start_guiding()

    # -- the camera --------------------------------------------------------
    def _expose_slot(self, active: list[Any],
                     frames: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """One slot's frames on every telescope, retrying whatever fails.

        A camera that misses one download is a camera that is fine; one that has
        fallen off the USB bus is not, and asking it again is the only thing
        that tells the two apart.  Only the telescopes that failed are retried,
        so a working camera is never made to shoot the same slot twice.
        """
        def job(rig):
            return lambda: rig.capture.capture_blocking(
                frames[rig.id]["exposure"], frame_type="light")

        # The last gate before the shutter opens. Everything between the top of
        # the slot and here — a filter change, a focus sweep, a guider settling,
        # a re-centre — can take minutes, and an abort arriving during any of
        # them must not be followed by one more frame.
        self._check()

        # The first light frame is where the sensor temperature finally matters.
        # By now the mount has homed, slewed, centred and focused, so this is
        # usually already satisfied and returns at once.
        self._parallel([(rig, lambda rig=rig: self._await_cooling(rig))
                        for rig in active], "cool")

        self._check()
        outcome = self._parallel([(rig, job(rig)) for rig in active], "expose")
        if not outcome["errors"] or not self._recovery_on("enabled"):
            return outcome

        settings = self._recovery_settings()
        retries = int(settings.get("frameRetryAttempts") or 0)
        wait = float(settings.get("frameRetrySeconds") or 0)

        for attempt in range(1, retries + 1):
            if not outcome["errors"]:
                break
            if self._budget_spent():
                self._give_up("The cameras keep failing")
            failed = [rig for rig in active if rig.id in outcome["errors"]]
            first = outcome["errors"][failed[0].id]
            self._recovering(
                "camera",
                ", ".join(rig.name for rig in failed) + f": {first}; trying that "
                f"frame again in {wait:g}s (attempt {attempt} of {retries})",
                attempt)
            self._sleep(wait)
            if settings.get("reconnectDevices", True) and attempt > 1:
                self._reconnect_cameras(failed)
            retry = self._parallel([(rig, job(rig)) for rig in failed], "re-expose")
            outcome["results"].update(retry["results"])
            outcome["errors"] = retry["errors"]
            if not outcome["errors"]:
                self._recovered("camera", "The camera answered again", True)
                return outcome

        if retries:
            self._recovered("camera", "The camera did not answer again", False)
        return outcome

    def _reconnect_cameras(self, rigs: list[Any]) -> None:
        """Take a camera down and bring it back up on its remembered driver.

        This is what fixes a driver that has wedged rather than a cable that has
        fallen out — and it costs a few seconds to find out which it was.
        """
        for rig in rigs:
            self._set("recovering", f"reconnecting {rig.name}'s camera")
            self._say(f"{rig.name}: reconnecting the camera")
            with contextlib.suppress(Exception):
                rig.manager.disconnect("camera")
            try:
                outcome = self.rigs.connect_remembered(rig, ("camera",))
                if outcome["failed"]:
                    self._say(f"{rig.name}: " + "; ".join(outcome["failed"]), "warn")
                elif outcome["connected"]:
                    self._say(f"{rig.name}: the camera is back", "success")
            except Exception as exc:              # noqa: BLE001 - reported, not fatal
                self._say(f"{rig.name}: could not reconnect the camera — {exc}",
                          "warn")

    # -- imaging -----------------------------------------------------------
    def _select_filter(self, rig, name: str) -> bool:
        """Put a telescope's wheel on the named filter.  True if it moved."""
        # In the one spelling, whatever asked: the wheel's names already are.
        name = filters.canonical(name) or name
        wheel = rig.manager.get("filterwheel")
        if wheel is None or not wheel.connected:
            # Nothing to move. On a scope whose filter sits in a drawer that is
            # the normal state of affairs — but if the plan is asking for one
            # that is not the one fitted, that is worth saying before a night's
            # frames go in the wrong stack.
            fitted = filters.canonical(rig.config.get("camera", "fixedFilter", "") or "")
            if name and fitted and name.lower() != fitted.lower():
                self._say(f"{rig.name}: the plan asks for {name} but {fitted} is "
                          f"in the light path, and there is no wheel to change "
                          f"it — shooting {fitted}", "warn")
            return False
        # Matched in the one spelling on both sides: a wheel that reports its
        # own names through a driver that never folded them still lines up.
        names = [filters.canonical(n) or str(n) for n in (wheel.names or [])]
        if name not in names:
            self._say(f"{rig.name}: the wheel has no filter called {name}; "
                      "shooting with what is loaded", "warn")
            return False
        if names.index(name) == wheel.position:
            return False
        self._set_rig(rig, state=f"selecting {name}")
        started = time.monotonic()
        wheel.set_position(names.index(name))
        deadline = time.monotonic() + 120
        while wheel.moving and time.monotonic() < deadline:
            self._check()
            time.sleep(0.2)
        moved_focus = self._apply_filter_offset(rig, name)
        # What a change really costs on this wheel, focus offset included.
        self._record_overhead("filterChange", time.monotonic() - started)
        self._set_rig(rig, state="")
        # A filter change that focus was already corrected for by a known offset
        # is not a reason to sweep as well, which is the whole point of having
        # measured the offsets.
        return not moved_focus

    def _apply_filter_offset(self, rig, name: str) -> bool:
        """Nudge the focuser by this filter's offset from the last one.

        Filters are not parfocal — a 3 nm Ha and a luminance filter focus a few
        hundred steps apart on most trains — and a measured offset corrects that
        in a second, where a sweep on every filter change costs minutes of every
        hour.  Offsets are stored relative to each other, so what is applied is
        always the *difference* from the filter that is coming out; the absolute
        position keeps whatever the last real focus run found.
        """
        if not rig.config.get("sequencer", "useFilterOffsets", True):
            return False
        offsets = rig.config.get("sequencer", "filterOffsets", {}) or {}
        if not isinstance(offsets, dict) or not offsets:
            return False
        focuser = rig.manager.get("focuser")
        if focuser is None or not focuser.connected:
            return False

        wanted = self._offset_for(rig, name)
        delta = wanted - int(self._filter_offset.get(rig.id, 0))
        if not delta:
            return False
        try:
            self._set_rig(rig, state=f"filter offset {delta:+d}")
            focuser.move_relative(delta)
            deadline = time.monotonic() + 120
            while focuser.moving and time.monotonic() < deadline:
                self._check()
                time.sleep(0.2)
        except DeviceError as exc:
            self._say(f"{rig.name}: could not apply the {name} focus offset — "
                      f"{exc}", "warn")
            return False
        self._filter_offset[rig.id] = wanted
        self._say(f"{rig.name}: {name} focus offset {delta:+d} steps "
                  f"(now at {focuser.position})")
        return True

    def _too_low(self, panel: dict[str, Any], exposure: float,
                 label: str, entry: dict[str, Any] | None = None) -> bool:
        """Has this field sunk below the elevation worth shooting at?

        Checked at the *end* of the frame about to be started, not the start of
        it: a five-minute sub begun at the limit finishes below it, and the
        point of a limit is that nothing is taken under it.

        A target with a floor of its own overrides the observatory's, because a
        bright planetary nebula is worth shooting through the murk at 25 degrees
        and a faint galaxy is not.
        """
        floor = 0.0
        if entry is not None:
            floor = float(plans.options_for(entry).get("minAltitude") or 0.0)
        if floor <= 0:
            floor = float(self.config.get("schedule", "minAltitude", 0.0) or 0.0)
        if floor <= 0:
            return False
        site = self._site()
        if site is None:
            return False
        altitude = astro.altitude_at(
            astro.normalise_ra_hours(panel["ra"]), float(panel["dec"]),
            time.time() + exposure, site[0], site[1])
        if altitude >= floor:
            return False
        self._say(f"{label} is down to {altitude:.0f}°, below the {floor:g}° "
                  "limit — stopping here", "warn")
        return True

    def _shoot(self, entry: dict[str, Any], panel: dict[str, Any], label: str,
               end_at: float | None) -> bool:
        """Work through one panel, one slot at a time, on every telescope.

        False when it stopped early — out of night, or the field has set.
        """
        rigs = self.rigs.imaging()
        if not rigs:
            raise DeviceError("no camera is connected")
        master_id = self.master.id
        overheads = float(self.config.get("schedule", "perFrameSeconds", 15.0))
        options = plans.options_for(entry)
        filter_order = str(options.get("filterOrder") or "grouped")

        # The labels are already set by `_run_entry`, before the slew.
        queues: dict[str, list[dict[str, Any]]] = {}
        for rig in rigs:
            queues[rig.id] = _flatten(
                plans.allocation_for(entry, rig.id, master_id), filter_order)
            self._set_rig(rig, frame=0, frames=len(queues[rig.id]), filter="")

        slots = max((len(queue) for queue in queues.values()), default=0)
        with self._lock:
            self._slots = slots
        if not slots:
            return True

        # A panel slewed to after it crossed the meridian is already on the
        # side the driver chose for it, and "flipping" it would be a second
        # slew to the same place, a re-centre and a warning that it did not
        # change side - once per panel, on every panel of a mosaic shot late.
        ahead = self._hours_to_meridian(panel["ra"])
        flipped = ahead is not None and ahead < 0.0
        for slot in range(slots):
            self._check()
            if self._skip.is_set():
                raise _Skipped()
            with self._lock:
                self._slot = slot + 1

            # Whoever still has a frame at this slot takes part; a telescope
            # that ran out of allocation simply sits the rest of the panel out.
            active = [rig for rig in rigs if slot < len(queues[rig.id])]
            if not active:
                break
            frames = {rig.id: queues[rig.id][slot] for rig in active}

            changed = self._parallel(
                [(rig, lambda rig=rig: self._select_filter(rig, frames[rig.id]["filter"]))
                 for rig in active], "filter")
            for rig_id, message in changed["errors"].items():
                self._say(f"{self._name(rig_id)}: could not select a filter — "
                          f"{message}", "warn")
            self._maybe_focus(filter_changed=changed["results"])

            # Between frames is the only safe place to put any of this right: a
            # slew or a guider restart during an exposure is a ruined sub.
            self._check_recentre(panel, label)
            self._check_guiding()

            longest = max(frame["exposure"] for frame in frames.values())
            # The clock, the dawn and the horizon, before every frame. `end_at`
            # alone is not enough: a target with no end time set has no end time
            # at all, and the plan is allowed not to set one.
            reason = self._past_deadline(entry, end_at, longest)
            if reason:
                self._say(f"{label}: {reason}", "warn")
                return False
            if self._too_low(panel, longest, label, entry):
                return False

            if not flipped and self._flip_due(panel, longest + overheads):
                self._flip(panel, label)
                flipped = True

            for rig in active:
                self._set_rig(rig, frame=slot + 1, filter=frames[rig.id]["filter"],
                              state="exposing")
            self._set("imaging", f"{label}: slot {slot + 1}/{slots} — " + ", ".join(
                f"{rig.name} {frames[rig.id]['filter']} "
                f"{frames[rig.id]['exposure']:g}s" for rig in active))

            # Anything the guider does during this exposure marks the frame.
            self._guide_lost_in_slot = False
            outcome = self._expose_slot(active, frames)
            if self._guiding_wanted and not self._guiding_healthy():
                self._guide_lost_in_slot = True

            # A slave camera dropping out at two in the morning must not end the
            # night for the rest of the rig; the master failing does.
            for rig in active:
                self._set_rig(rig, state="")
                message = outcome["errors"].get(rig.id)
                if message is None:
                    continue
                if rig.id == master_id:
                    raise DeviceError(message)
                self._say(f"{rig.name}: frame failed — {message}", "warn")

            for rig in active:
                record = outcome["results"].get(rig.id)
                if record is None:
                    continue
                filter_name = frames[rig.id]["filter"]
                key = f"{entry['id']}:{rig.id}:{filter_name}"
                with self._lock:
                    self._done[key] = self._done.get(key, 0) + 1

                # Measured first, then credited: the night's report wants the
                # star size and the guiding this frame was taken under, and
                # those are not known until the frame has been measured.
                self._record_frame(rig, record, panel, entry, filter_name,
                                   guide_lost=self._guide_lost_in_slot)

            if slot + 1 < slots:
                self._dither(entry)

        return True


class _Stopped(Exception):
    """The operator stopped the sequence."""


class _Skipped(Exception):
    """The operator skipped this target."""


class _Reframed(Exception):
    """The target's panels were laid out again under the run; start it over."""


def _clock(timestamp: float) -> str:
    return time.strftime("%H:%M", time.localtime(timestamp))
