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
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import allsky, astro, capture, coverprep, filters, plans, roofguard, schedule
from .devices.base import DeviceError, read_slot
from .recovery import (FAILED as RIG_FAILED, OK as RIG_OK, RECOVERING as RIG_RECOVERING,
                       ProgressJournal, RigRecovery, backoff, call_with_timeout)
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
        #: Called alongside the camera aborts in a shutdown: anything else
        #: that drives the cameras (the focuser check) and must stop too.
        self.extra_aborts: list[Any] = []
        #: (rig id, asked filter, fitted filter) already warned about, so a
        #: drawer telescope following another's allocation says so once.
        self._said_fitted: set[tuple[str, str, str]] = set()
        #: True from the moment a person presses Stop until the next start.
        #: Stop means "leave the rig where it is"; park-at-end is for a night
        #: that ran out on its own, and used to fire after a Stop as well.
        self._stopped_by_hand = False

        self._thread: threading.Thread | None = None
        # The warm-down outlives the run that started it, so it gets its own.
        self._warm_thread: threading.Thread | None = None
        self._warm_stop = threading.Event()
        # A run that was stopped by hand, or ran out before the night did,
        # leaves the cameras cold (0.2.52d). They warm at dawn instead, unless
        # a new sequence starts first: a thread that waits, and its stop flag.
        self._warm_later_thread: threading.Thread | None = None
        self._warm_later_stop = threading.Event()
        self._warm_later_at: float | None = None
        #: Set when the run ended because the night itself was over (dawn,
        #: less the margin), which is the one ordinary end that warms at once.
        self._night_over = False
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
        # Rough-guiding bookkeeping: frames in a row over the limit, and when
        # a fresh star was last picked for it.
        self._rough_runs = 0
        self._rough_last = 0.0
        self._rough_restarts = 0
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

        # -- 0.2.51: recovery that does not end the night ------------------
        # One record per telescope: ok, recovering (attempt n of m, why,
        # what it is doing), or failed for the rest of the target.
        self._rig_recovery: dict[str, RigRecovery] = {}
        # Bumped on every start. A sequence thread that was given up on by
        # the watchdog finds its number out of date at its next `_check` and
        # leaves, instead of waking up inside the next night's run.
        self._generation = 0
        self._abandoned = False
        # The watchdog: when the sequence thread last showed signs of life,
        # and the latest a step it is waiting on is allowed to take.
        self._beat_at = time.monotonic()
        self._expect_by: float | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog: dict[str, Any] = {"state": "idle"}
        # Set by whatever decided the night cannot go on, so the way out
        # closes the observatory (closeOutOnFailure) and says why.
        self._close_out: str | None = None
        # Guiding that could not be restarted is tried again later.
        self._guide_retry_at: float | None = None
        self._guide_retries = 0
        # The roof at the start of a target (0.2.53b). A failed or timed-out
        # open is the site's technician lock (Bray, Hakos): latched here, and
        # nothing more is sent to the roof until a person starts a new run.
        self._roof_locked: str | None = None
        # Whether this run sent the OpenShutter - so the end of the run knows
        # the roof it is leaving open is one it opened.
        self._roof_opened_by_run = False
        # Frames shot so far tonight by entry, panel, telescope and filter,
        # written to disk after each one so a restart resumes there.
        try:
            from .config import data_root
            journal_path = data_root() / "sequence-progress.json"
        except Exception:                          # noqa: BLE001 - a bare build
            journal_path = Path("sequence-progress.json")
        self.journal = ProgressJournal(journal_path)
        self._progress: dict[str, int] = {}
        self._finished_entries: set[str] = set()
        self._resume: dict[str, Any] | None = None

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
        # A thread the watchdog has given up on may still be alive inside a
        # driver call, but the run is over: never "running but dead".
        return (self._thread is not None and self._thread.is_alive()
                and not self._abandoned)

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

    @property
    def warm_pending(self) -> bool:
        """Whether cameras left cold by the last run are waiting for dawn to warm."""
        thread = self._warm_later_thread
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
                # When cameras left cold by a Stop are due to warm (0.2.52d).
                "warmAt": self._warm_later_at if self.warm_pending else None,
                # What the UI should gate on, rather than `running`: a paused or
                # stopping sequence leaves the cameras free.
                "imaging": self.owns_the_cameras,
                "state": self._state,
                "stateSince": self._state_since,
                "message": self._message,
                "error": self._error,
                # What went wrong and what is being done about it.
                "recovery": dict(self._recovery) if self._recovery else None,
                # Per telescope (0.2.51): ok / recovering / failed, attempt
                # n of m, why, and what the recovery is doing right now.
                "rigRecovery": {rig_id: rec.as_dict()
                                for rig_id, rec in self._rig_recovery.items()},
                "watchdog": dict(self._watchdog),
                "resumed": bool(self._resume),
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
                "telescopes": {rig_id: (
                    {**state, "recovery": self._rig_recovery[rig_id].as_dict()}
                    if rig_id in self._rig_recovery else dict(state))
                    for rig_id, state in self._rig_state.items()},
                # The master's own progress, kept at the top level so a
                # single-telescope setup reads exactly as it always did.
                "filter": self._rig_state.get(self.master.id, {}).get("filter", ""),
                "frame": self._rig_state.get(self.master.id, {}).get("frame", 0),
                "frameCount": self._rig_state.get(self.master.id, {}).get("frames", 0),
                # The technician-lock text, for the roof badge. Display only.
                "roofLocked": self._roof_locked or "",
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
        self._beat()

    def _alive(self) -> bool:
        """For a long job's abort poll: it is still polling, so it is alive."""
        self._beat_at = time.monotonic()
        return False

    def _beat(self) -> None:
        """The sequence thread is alive and getting somewhere."""
        if getattr(threading.current_thread(), "sf_generation", None) == self._generation:
            self._beat_at = time.monotonic()

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

    def _unwarn(self, key: str) -> None:
        """Take a run warning down: the thing it was about has come right.

        A sticky warning is cleared by name or not at all - the watchdog cannot
        see a focus run or a plate solve for itself - and until these were
        cleared on success a bad patch of sky at midnight was still "failing"
        on the board the next afternoon.
        """
        if self.warnings is None:
            return
        with contextlib.suppress(Exception):
            self.warnings.clear(key)

    def _set_rig(self, rig, **values: Any) -> None:
        with self._lock:
            state = self._rig_state.setdefault(
                rig.id, {"name": rig.name, "role": rig.role, "filter": "",
                         "frame": 0, "frames": 0, "state": IDLE})
            state["name"] = rig.name
            state["role"] = rig.role
            state.update(values)
        # A focus sweep reporting its points, a cooler its temperature: the
        # run is getting somewhere (the watchdog).
        self._beat()

    # -- control -----------------------------------------------------------
    def start(self, loop: bool = False, resume: dict[str, Any] | None = None) -> None:
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
        self._night_over = False
        # Last night's event warnings belong to last night.
        if self.warnings is not None:
            with contextlib.suppress(Exception):
                self.warnings.clear_sticky()
        self._solve_failures = 0

        self._abort.clear()
        self._paused.clear()
        self._skip.clear()
        self._stopped_by_hand = False
        # A new night may have new drawers; the fitted-filter warning is
        # worth hearing once more.
        self._said_fitted.clear()
        with self._lock:
            self._loop = bool(loop)
            self._loop_next = None
            self._nights = 0
        self._reset_night()
        if resume:
            self._seed_resume(resume)
        else:
            # A person starting a new run is the only thing that lifts the
            # technician-lock latch (0.2.53b); an auto-restart never does.
            self._roof_locked = None
            self._roof_opened_by_run = False
        with self._lock:
            self._generation += 1
            self._abandoned = False
            self._close_out = None
            self._expect_by = None
            self._beat_at = time.monotonic()
        self._journal_write()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sequencer")
        self._thread.sf_generation = self._generation       # type: ignore[attr-defined]
        self._thread.start()
        self._start_watchdog()

    # -- the watchdog (0.2.51) ------------------------------------------------
    #
    # 2026-10-14 02:24: three exposures started and then nothing, for twelve
    # and a half hours, until somebody restarted the program in the afternoon.
    # The sequence thread was inside a driver call that never returned and
    # nothing was watching it.  The watchdog is a thread of its own that knows
    # when the run last showed signs of life and how long the step it is in
    # may take.  Past that it saves every thread's stack, aborts the captures
    # (which frees most hangs), and if the run is still stuck one period
    # later it gives the thread up and closes the night out itself.  It does
    # the same for a run still going past dawn.

    def _start_watchdog(self) -> None:
        generation = self._generation
        thread = threading.Thread(target=self._watch, args=(generation,), daemon=True,
                                  name="sequencer-watchdog")
        self._watchdog_thread = thread
        thread.start()

    def _watch(self, generation: int) -> None:
        stage = 0
        stage_at = 0.0
        reason = ""
        while generation == self._generation:
            time.sleep(0.5)
            thread = self._thread
            if thread is None or not thread.is_alive() or self._abandoned:
                break
            settings = self._recovery_settings()
            limit = float(settings.get("watchdogMinutes") or 0.0) * 60.0
            if limit <= 0 or not settings.get("enabled", True):
                with self._lock:
                    self._watchdog = {"state": "off"}
                continue
            now = time.monotonic()
            with self._lock:
                beat = self._beat_at
                expect = self._expect_by
            quiet = now - beat
            due = max(beat + limit, expect or 0.0)
            late_dawn = None
            with contextlib.suppress(Exception):
                dawn = self._dawn_at or None
                after = float(settings.get("dawnCloseOutMinutes") or 0.0) * 60.0
                if dawn and not self._loop and self._settings().get("stopAtDawn", True) \
                        and time.time() > dawn + after:
                    late_dawn = f"still running {(time.time() - dawn) / 60.0:.0f} min after dawn"
            with self._lock:
                self._watchdog = {"state": "stalled" if stage else "watching",
                                  "quietSeconds": round(quiet, 1),
                                  "limitSeconds": limit,
                                  "expectBy": None if expect is None
                                  else time.time() + (expect - now),
                                  "stage": stage, "reason": reason}
            stalled = now > due or late_dawn is not None
            if not stalled:
                if stage:
                    stage = 0
                    reason = ""
                    self.manager.log("Watchdog: the sequence is moving again", "success")
                continue
            if stage == 0:
                stage, stage_at = 1, now
                reason = late_dawn or f"no progress for {quiet / 60.0:.1f} min"
                self.manager.log(f"Watchdog: {reason} — saving the thread stacks and "
                                 "aborting the captures", "error")
                self._dump_stacks(reason)
                with self._lock:
                    self._recovery = {"kind": "watchdog", "reason": reason,
                                      "attempt": 1, "attempts": 2, "since": time.time()}
                if late_dawn:
                    self._abort.set()
                for rig in self.rigs.all:
                    with contextlib.suppress(Exception):
                        call_with_timeout(rig.capture.abort, 15.0, "aborting a capture")
                    with contextlib.suppress(Exception):
                        rig.capture.release_stuck()
                continue
            # Stage 1 did not free it: give the thread up and close out here.
            if now - stage_at > min(limit, 300.0):
                self._abandon(reason)
                break

    def _dump_stacks(self, reason: str) -> None:
        """Every thread's stack, for the morning: where the run was stuck."""
        import sys as _sys
        import traceback as _tb
        with contextlib.suppress(Exception):
            from .config import data_root
            folder = data_root() / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / time.strftime("sequence-stacks-%Y%m%d-%H%M%S.txt")
            names = {t.ident: t.name for t in threading.enumerate()}
            lines = [f"Watchdog: {reason}", ""]
            for ident, frame in _sys._current_frames().items():
                lines.append(f"--- thread {names.get(ident, ident)}")
                lines.extend(line.rstrip("\n") for line in _tb.format_stack(frame))
                lines.append("")
            path.write_text("\n".join(lines), encoding="utf-8")
            self.manager.log(f"Watchdog: thread stacks saved to {path}", "warn")

    def _abandon(self, reason: str) -> None:
        """The run is stuck for good: close the night out from here."""
        message = f"the sequence stopped responding ({reason})"
        with self._lock:
            self._abandoned = True
            self._error = message
            self._close_out = message
            self._recovery = {"kind": "watchdog", "reason": f"{reason}; closing out",
                              "attempt": 2, "attempts": 2, "since": time.time()}
        self._abort.set()
        self.manager.log(f"Watchdog: {message} — closing the night out", "error")
        self._tell("failure", "The sequence stopped responding",
                   f"{reason}. Closing the observatory out.\n\n{self._summary()}")
        action = str(self._recovery_settings().get("closeOutOnFailure") or "park")
        with contextlib.suppress(Exception):
            call_with_timeout(self._stop_guiding, 30.0, "stopping guiding")
        mount = self.manager.get("mount")
        if mount is not None and action != "none":
            try:
                if action == "park":
                    call_with_timeout(self._park_and_check, 600.0, "parking the mount")
                else:
                    call_with_timeout(lambda: mount.set_tracking(False), 30.0,
                                      "stopping tracking")
            except Exception as exc:              # noqa: BLE001 - reported
                self.manager.log(f"Watchdog: could not make the mount safe: {exc}", "error")
                with contextlib.suppress(Exception):
                    call_with_timeout(lambda: mount.set_tracking(False), 30.0,
                                      "stopping tracking")
                self._tell("failure", "The mount may not be safe",
                           f"{exc}. The telescope may still be pointing at the sky.")
        with self._lock:
            self._entry_id = None
            self._rig_state = {}
            self._recovery = {"kind": "watchdog", "reason": message,
                              "attempt": 2, "attempts": 2, "since": time.time()}
        self._set(IDLE, "error")
        self._journal_close("failed")
        with contextlib.suppress(Exception):
            self._start_warming()

    # -- the progress journal (0.2.51) ----------------------------------------
    #
    # FSQ106, September and October: eleven runs ended with the program killed
    # or the PC off, and every one of them started again from frame one of the
    # first target - or, mostly, not at all.  The run now writes what it has
    # shot after every frame, and a program that comes back up the same night
    # picks up at the frame it was on.

    @staticmethod
    def _progress_key(entry_id: str, panel_index: int, rig_id: str, filter_name: str) -> str:
        return f"{entry_id}:{int(panel_index)}:{rig_id}:{filter_name}"

    def _journal_write(self, active: bool = True) -> None:
        with contextlib.suppress(Exception):
            entries = [e.get("id") for e in (self.plan.raw().get("entries") or [])]
            with self._lock:
                data = {
                    "version": 1, "active": active, "loop": self._loop,
                    "night": self._night_now(), "started": self._started,
                    "entryId": self._entry_id, "panelIndex": self._panel_index,
                    "progress": dict(self._progress),
                    "finished": sorted(self._finished_entries),
                    "plan": entries,
                }
            self.journal.write(data)

    def _journal_close(self, reason: str) -> None:
        with contextlib.suppress(Exception):
            self._journal_write(active=False)
            self.journal.close(reason)

    def resumable(self) -> dict[str, Any] | None:
        """The run a restart should pick up again, or None.

        Only a run that was still going when the program went away - not one
        that finished, failed or was stopped by hand - and only on the same
        night, with the same plan still loaded.
        """
        settings = self._recovery_settings()
        if not settings.get("enabled", True) or not settings.get("resumeAfterRestart", True):
            return None
        data = self.journal.load()
        if not data or not data.get("active"):
            return None
        if time.time() - float(data.get("updated") or 0) > 16 * 3600:
            return None
        night = self._night_now()
        if not night or data.get("night") != night:
            return None
        entries = [e.get("id") for e in (self.plan.raw().get("entries") or [])]
        if not entries or not set(entries) & set(data.get("plan") or []):
            return None
        return data

    def _seed_resume(self, data: dict[str, Any]) -> None:
        progress = {str(k): int(v) for k, v in (data.get("progress") or {}).items()
                    if int(v or 0) > 0}
        with self._lock:
            self._resume = dict(data)
            self._progress = dict(progress)
            self._finished_entries = set(data.get("finished") or [])
            for key, count in progress.items():
                parts = key.split(":", 3)
                if len(parts) != 4:
                    continue
                done_key = f"{parts[0]}:{parts[2]}:{parts[3]}"
                self._done[done_key] = self._done.get(done_key, 0) + count
        shot = sum(progress.values())
        self._say(f"Resuming tonight's run after a restart: {shot} frame(s) already "
                  f"shot, {len(self._finished_entries)} target(s) finished", "warn")

    def _without_done(self, queue: list[dict[str, Any]], entry_id: str,
                      panel_index: int, rig_id: str) -> list[dict[str, Any]]:
        """A telescope's queue for a panel less the frames already shot tonight."""
        if not self._progress:
            return queue
        left = {}
        for item in queue:
            name = str(item.get("filter") or "")
            if name not in left:
                left[name] = self._progress.get(
                    self._progress_key(entry_id, panel_index, rig_id, name), 0)
        out = []
        for item in queue:
            name = str(item.get("filter") or "")
            if left.get(name, 0) > 0:
                left[name] -= 1
                continue
            out.append(item)
        return out

    def _credit(self, entry_id: str, panel_index: int, rig_id: str, filter_name: str) -> None:
        key = self._progress_key(entry_id, panel_index, rig_id, filter_name)
        with self._lock:
            self._progress[key] = self._progress.get(key, 0) + 1
        self._journal_write()

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
            self._rig_recovery = {}
            self._progress = {}
            self._finished_entries = set()
            self._resume = None
            self._guide_retry_at = None
            self._guide_retries = 0
            self._recovery = None
            self._recoveries = []
            self._rescues = 0
            self._target_rescues = 0
            self._guiding_wanted = False
            self._guide_lost_since = None
            self._guide_lost_in_slot = False
            self._rough_runs = 0
            self._rough_last = 0.0
            self._rough_restarts = 0
            self._recentre_request = None
            self._cooling_wanted = set()
            self._homed = False
            self._weather_hold = False
            self._dawn_at = None
            self._night_over = False

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
        self._stopped_by_hand = True
        self._abort.set()
        # Stopped by a person (or the program closed by one) is not a run to
        # pick up after a restart; written now, not when the thread gets there.
        if self.running:
            self._journal_close("stopped")
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
        # What was running, taken before the shutdown closes the journal, so
        # the auto-restart (0.2.53a) can pick it up at the same frame.
        hook = getattr(self, "on_weather_shutdown", None)
        if hook is not None:
            with contextlib.suppress(Exception):
                hook(self.weather_snapshot())
        self._weather_hold = True
        self._say(f"The sky is {reason}: shutting down", "error")
        self.shut_down()

    # -- auto-restart after a weather shutdown (0.2.53a) -------------------
    def weather_snapshot(self) -> dict[str, Any] | None:
        """The run a weather shutdown is interrupting, as the journal has it."""
        if not self.running:
            return None
        entries = [e.get("id") for e in (self.plan.raw().get("entries") or [])]
        with self._lock:
            return {
                "version": 1, "active": True, "loop": self._loop,
                "night": self._night_now(), "started": self._started,
                "entryId": self._entry_id, "panelIndex": self._panel_index,
                "progress": dict(self._progress),
                "finished": sorted(self._finished_entries),
                "plan": entries, "takenAt": time.time(),
            }

    def _restart_entry(self, snapshot: dict[str, Any]):
        """(entry, target, panel) the interrupted run was on, or Nones."""
        entries = list(self.plan.raw().get("entries") or [])
        done = set(snapshot.get("finished") or [])
        entry = next((e for e in entries if e.get("id") == snapshot.get("entryId")), None)
        if entry is None:
            entry = next((e for e in entries if e.get("id") not in done
                          and not plans.is_calibration(e)), None)
        if entry is None or plans.is_calibration(entry):
            return entry, None, None
        target = next((t for t in self.targets.listing()
                       if t.get("id") == entry.get("targetId")), None)
        if target is None or target.get("type") == "allsky" or target.get("ra") is None:
            return entry, target, None
        index = int(snapshot.get("panelIndex") or 0)
        panel = next((p for p in (target.get("panels") or [])
                      if int(p.get("index", 0) or 0) == index), None)
        if panel is None:
            panel = {"index": 0, "ra": target["ra"], "dec": target["dec"]}
        return entry, target, panel

    def weather_deadlines(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Tonight's key, dawn (less the margin), the sequence's end, one sub."""
        self._dawn_at = None
        dawn = None
        with contextlib.suppress(Exception):
            dawn = self._dawn()
        if dawn:
            dawn -= float(self._settings().get("dawnMarginMinutes") or 0.0) * 60.0
        entries = list(self.plan.raw().get("entries") or [])
        done = set(snapshot.get("finished") or [])
        left = [e for e in entries if e.get("id") not in done]
        ends = [e.get("endAt") for e in left]
        end = (max(float(x) for x in ends)
               if ends and all(ends) and not snapshot.get("loop") else None)
        entry, _, _ = self._restart_entry(snapshot)
        subs = [float(f.get("exposure") or 0) for f in ((entry or {}).get("filters") or [])
                if int(f.get("count") or 0) > 0 and float(f.get("exposure") or 0) > 0]
        if not subs:
            subs = [float(f.get("exposure") or 0) for e in left
                    for f in (e.get("filters") or []) if float(f.get("exposure") or 0) > 0]
        return {"night": self._night_now(), "dawn": dawn, "end": end,
                "sub": min(subs) if subs else 60.0}

    def restart_begin(self) -> None:
        if self.running:
            raise DeviceError("a sequence is already running")
        self._abort.clear()
        self._paused.clear()
        self._skip.clear()
        # The shutdown's warm-down is still ramping the setpoint up.
        self._stop_warming()

    def restart_abort(self) -> None:
        self._abort.set()

    def restart_recool(self, timeout: float) -> list[str]:
        """Cool every imaging camera to its setpoint; the names still warm at the timeout."""
        wanted = []
        every = bool(self.config.get("camera", "coolAllAtStart", True))
        for rig in self.rigs.imaging():
            settings = rig.config.section("camera")
            camera = rig.manager.get("camera")
            if camera is None or not camera.connected or not camera.can_cool:
                continue
            if not (every or settings.get("coolAtStart", True)):
                continue
            setpoint = float(settings.get("setpoint", -10.0))
            camera.set_setpoint(setpoint)
            camera.set_cooler(True)
            if hasattr(rig, "note_cooler"):
                rig.note_cooler(on=True, setpoint=setpoint)
            wanted.append((rig, camera, setpoint,
                           float(settings.get("coolToleranceC") or 1.0)))
        if wanted:
            self._say("Auto-restart: recooling " + ", ".join(r.name for r, *_ in wanted))
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            late = [rig.name for rig, camera, setpoint, tol in wanted
                    if camera.temperature is None or abs(camera.temperature - setpoint) > tol]
            if not late or time.monotonic() >= deadline:
                return late
            self._set("cooling", "auto-restart: recooling")
            self._sleep(min(2.0, max(0.05, deadline - time.monotonic())))

    def restart_unpark(self) -> None:
        dome = self.manager.get("dome")
        if dome is not None and dome.connected and getattr(dome, "can_shutter", False) \
                and self._roof_state(dome) != "open":
            # 0.2.53b: the auto-restart still opens nothing by default (as in
            # 0.2.53a). With 'reopenRoofOnAutoRestart' on (and 'Open roof at
            # sequence start') it opens the way a target does - the safety
            # monitor is asked again first - and never against a latched lock.
            if not (self._roof_open_wanted()
                    and self._settings().get("reopenRoofOnAutoRestart", False)) \
                    or self._roof_locked:
                raise DeviceError(f"the roof/shutter is {self._roof_state(dome)}; it is not "
                                  "reopened automatically"
                                  + (f" ({self._roof_locked})" if self._roof_locked else ""))
            if not self._ensure_roof_open("the auto-restart"):
                raise DeviceError("the night ran out before the roof could be opened")
        mount = self._mount_ready()
        self._release_mount(mount)
        if getattr(mount, "at_park", False):
            raise DeviceError("the mount is still parked after the unpark")

    def restart_centre(self, snapshot: dict[str, Any]) -> str:
        entry, target, panel = self._restart_entry(snapshot)
        if panel is None:
            return "nothing to centre on (the run carries on with what it has)"
        label = f"{(entry or {}).get('name') or 'the target'}"
        self._ensure_sky_cover(label)
        ra, dec = astro.normalise_ra_hours(panel["ra"]), float(panel["dec"])
        self._set("slewing", f"auto-restart: slewing to {label}")
        mount = self._slew(self._mount_ready(), ra, dec, label)
        self._wait_for_mount(mount)
        if self.solver.executable() is None:
            return f"slewed to {label} (no plate solver set up, so not solved)"
        self._set("centring", f"auto-restart: plate solving {label}")
        try:
            self.solver.start("center", None, ra, dec, None)
            self._wait_for_solver()
        except DeviceError as exc:
            raise DeviceError(f"plate solving {label} failed: {exc}") from exc
        return f"centred on {label}"

    def restart_focus(self) -> str:
        rigs = [rig for rig in self._focusable() if not self._focus_muted(rig)]
        due = {rig.name: rig.focuser.due(False) for rig in rigs}
        due = {name: why for name, why in due.items() if why}
        if not due:
            return ""
        self._maybe_focus()
        return "; ".join(f"{name}: {why}" for name, why in due.items())

    def restart_resume(self, snapshot: dict[str, Any]) -> None:
        self.start(loop=bool(snapshot.get("loop")), resume=snapshot)

    def restart_park(self) -> None:
        self._abort.set()
        with contextlib.suppress(Exception):
            self._stop_guiding()
        try:
            self._park_and_check()
        finally:
            with contextlib.suppress(Exception):
                self._start_warming()
            self._set(IDLE, "auto-restart failed: parked")

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
        self._beat()
        mine = getattr(threading.current_thread(), "sf_generation", None)
        if mine is not None and mine != self._generation:
            raise _Stopped()
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
    def _parallel(jobs: list[tuple[Any, Any]], name: str,
                  timeout: float | None = None) -> dict[str, Any]:
        """Run one job per telescope at the same time and collect the outcomes.

        A thread each rather than a pool: there are at most a handful of
        telescopes, and each job spends its life blocked on a driver.

        With a `timeout`, a telescope whose job has not come back by then is
        reported as hung (in `errors` and in `hung`) and its thread is left
        behind: one wedged driver must not hold every other telescope - and
        the night - with it, which is what an unbounded join did.
        """
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        threads: list[tuple[Any, threading.Thread]] = []
        lock = threading.Lock()
        late: set[str] = set()

        def worker(rig, job) -> None:
            try:
                value = job()
                with lock:
                    if rig.id not in late:
                        results[rig.id] = value
            except Exception as exc:              # noqa: BLE001 - reported per rig
                with lock:
                    if rig.id not in late:
                        errors[rig.id] = str(exc)

        generation = getattr(threading.current_thread(), "sf_generation", None)
        for rig, job in jobs:
            thread = threading.Thread(target=worker, args=(rig, job), daemon=True,
                                      name=f"{name}-{rig.id}")
            # A job of the run's counts as the run being alive (watchdog).
            thread.sf_generation = generation          # type: ignore[attr-defined]
            thread.start()
            threads.append((rig, thread))
        deadline = None if not timeout else time.monotonic() + float(timeout)
        hung: list[str] = []
        for rig, thread in threads:
            if deadline is None:
                thread.join()
            else:
                thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                with lock:
                    late.add(rig.id)
                    errors[rig.id] = (f"no answer from the driver in {float(timeout):.0f}s "
                                      f"({name} hung)")
                    results.pop(rig.id, None)
                hung.append(rig.id)
        return {"results": results, "errors": errors, "hung": hung}

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

        # One switch for the whole mount outranks each telescope's own box: a
        # follower imported with cooling off (rasa2 on 2026-10-08 spent the
        # night at +18 C beside two at -5 C) is the thing it exists to catch.
        every = bool(self.config.get("camera", "coolAllAtStart", True))
        if not camera.can_cool or not (every or settings.get("coolAtStart", True)):
            return
        setpoint = float(settings.get("setpoint", -10.0))
        camera.set_setpoint(setpoint)
        camera.set_cooler(True)
        # Remembered on the telescope, so a camera brought back after a
        # failed frame is cooled again rather than left at ambient.
        if hasattr(rig, "note_cooler"):
            rig.note_cooler(on=True, setpoint=setpoint)
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
        # A cooler that is not getting anywhere (2026-10-14 20:45: a camera
        # reconnected mid-focus came back at 28.7 C and stayed there) is asked
        # once more, and then given up on - instead of the full timeout with
        # every telescope waiting on it.
        stall = float(self._recovery_settings().get("coolStallSeconds") or 0.0)
        best: float | None = None
        best_at = time.monotonic()
        nudged = False
        poll = 5.0 if not stall else max(0.2, min(5.0, stall / 4.0))
        while True:
            self._check()
            now = camera.temperature
            if now is not None and abs(now - setpoint) <= tolerance:
                self._say(f"{label}: camera at {now:.1f} C", "success")
                break
            if now is not None and (best is None or now < best - 0.5):
                best, best_at = now, time.monotonic()
            if stall and time.monotonic() - best_at > stall:
                if not nudged:
                    nudged = True
                    best_at = time.monotonic()
                    self._say(f"{label}: the camera is not cooling ({now} C); "
                              "switching the cooler on again", "warn")
                    with contextlib.suppress(Exception):
                        camera.set_setpoint(setpoint)
                    with contextlib.suppress(Exception):
                        camera.set_cooler(True)
                else:
                    self._say(f"{label}: the cooler is not bringing the camera down "
                              f"({now} C, wanted {setpoint:g} C); starting anyway "
                              "rather than holding the night", "warn")
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
            time.sleep(poll)
        with self._lock:
            self._cooling_wanted.discard(rig.id)

    def _warm_cameras(self, only: list[Any] | None = None) -> None:
        rigs = [rig for rig in (self.rigs.all if only is None else only)
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
        # Whatever happens to the camera from here on, nobody wants it cold.
        if hasattr(rig, "note_cooler"):
            rig.note_cooler(on=False)
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
            # Given up on by the watchdog, which has closed the night out
            # already; a run started since then is not this thread's to tidy.
            if self._abandoned or getattr(threading.current_thread(), "sf_generation",
                                          self._generation) != self._generation:
                return
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
            close_out = self._close_out
            failure_action = str(self._recovery_settings().get("closeOutOnFailure")
                                 or "park")
            if close_out and not self._stopped_by_hand and failure_action != "none":
                # Unrecoverable, or the night ran out while recovering: the
                # observatory is made safe whatever parkAtEnd says.
                self._say(f"Closing the night out: {close_out}", "error")
                with contextlib.suppress(Exception):
                    self._make_safe(
                        park=failure_action == "park" or bool(settings.get("parkAtEnd")),
                        stop_tracking=failure_action == "stopTracking"
                        or bool(settings.get("stopTrackingAtEnd")))
            elif self._stopped_by_hand:
                # Stop, pressed by a person, means exactly what the button
                # says: the run ends and the rig is left where it is, still
                # tracking. Park-at-end is for a night that ran out on its
                # own; it used to fire here after a Stop too, and the mount
                # went to park under somebody who wanted to carry on by hand.
                if settings.get("parkAtEnd") or settings.get("stopTrackingAtEnd"):
                    self._say("Stopped by hand: the mount is left where it is, "
                              "tracking (park at end applies to a night that ends on its own)")
            elif settings.get("parkAtEnd") or settings.get("stopTrackingAtEnd"):
                with contextlib.suppress(Exception):
                    self._make_safe(park=bool(settings.get("parkAtEnd")),
                                    stop_tracking=bool(settings.get("stopTrackingAtEnd")))
            # 0.2.53b: a roof this run opened is shut again at the end of a run
            # that ended on its own or failed (after the park), never after a
            # Stop by hand - Stop leaves the rig exactly where it is.
            if not self._stopped_by_hand:
                try:
                    self._close_roof_after_run()
                except Exception as exc:          # noqa: BLE001 - reported
                    self._say(f"Could not close the roof at the end of the run: {exc}",
                              "error")
                    self._tell("failure", "The roof did not close at the end of the run",
                               f"{exc}. " + self._summary())

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
            self._warm_after_run(close_out=bool(
                close_out and not self._stopped_by_hand and failure_action != "none"))
            # An orderly end - finished, failed, stopped - is not resumed after
            # a restart; only a program that went away mid-run is.
            if not self._abandoned and getattr(threading.current_thread(),
                                               "sf_generation", None) == self._generation:
                self._journal_close("stopped" if self._stopped_by_hand
                                    else ("failed" if self._error else "finished"))

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
            # The cover is not opened here. A plan that starts with darks,
            # bias or panel flats must keep it shut; a light target opens it
            # in _goto, before the slew and before any centring or autofocus
            # frame. Opening here used to run whenever any later entry was a
            # light, including in front of the darks, and it trusted a
            # has_cover flag latched at connect so an Unknown Alnitak never
            # moved.
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
                # A run that failed is closed out (closeOutOnFailure), not
                # left with the mount tracking into the pier.
                if not self._close_out:
                    self._close_out = f"the sequence failed: {exc}"
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
            self._beat()                       # waiting on purpose (watchdog)
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

    def _ensure_sky_cover(self, label: str) -> None:
        """Open and verify every connected cover before `label` looks at the sky.

        Re-reads the driver. Does nothing during a dark, bias or panel-flat
        run (the roof guard is held for those). Does not command the roof.
        """
        if coverprep.guard_blocks_open():
            return
        rigs = self.rigs.imaging() if hasattr(self.rigs, "imaging") else [self.master]
        for rig in rigs:
            panel = None
            with contextlib.suppress(Exception):
                panel = rig.manager.get("flatpanel")
            if panel is None or not getattr(panel, "connected", False):
                continue
            self._open_cover(panel, label)

    def _open_cover(self, panel: Any = None, label: str = "the first slew") -> None:
        """Open one flat panel's cover and wait until it reports Open.

        A cover closed at the end of one night stays closed until something
        opens it. `has_cover` latched at connect is not consulted: CoverState
        is read now, and Unknown on a connected CoverCalibrator is opened.
        A cover that does not report Open holds the run instead of letting
        a centring frame or an autofocus frame shoot through the lid.
        """
        if coverprep.guard_blocks_open():
            return
        if panel is None:
            panel = self.manager.get("flatpanel")
        if panel is None or not getattr(panel, "connected", False):
            return
        self._set("homing", "opening the cover")

        def log(message: str, level: str = "info") -> None:
            text = message
            if message.startswith("Opening the cover"):
                text = f"Opening the cover before {label} — {message}"
            self._say(text, level)

        try:
            coverprep.ensure_open(
                panel, timeout=coverprep.timeout_seconds(self.config),
                log=log, check=self._check,
                command=coverprep.auto_cover(self.config))
        except coverprep.CoverHeld:
            self._set("waiting", f"holding: the cover is not open before {label}")
            raise

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
            # Picked up after a restart: what was finished stays finished.
            if entry.get("id") in self._finished_entries:
                self._say(f"{entry['name']}: already finished tonight")
                continue
            self._begin_entry(entry)

            if plans.is_calibration(entry):
                try:
                    self._run_calibration(entry)
                    self._finished(entry)
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

            # Started over when the ground moves under it: the target laid
            # out again at the camera's real angle, or the entry dealt again
            # by the collaboration server. A few times at most, so a bad
            # solve or a server that cannot make its mind up cannot keep a
            # night circling.
            restarts = 0
            reframed = False
            while True:
                try:
                    if target.get("type") == "allsky":
                        self._run_allsky(entry, target)
                    else:
                        self._run_entry(entry, target, reframed=reframed)
                except (_Reframed, _Redealt) as why:
                    restarts += 1
                    if restarts > 3:
                        self._say(f"{entry['name']}: changed under the run too many "
                                  "times tonight; leaving it", "warn")
                        break
                    # The target and the entry both, fresh from disk: a re-lay
                    # re-mapped the share onto new panels and wrote the new
                    # picks on the plan, and a re-deal wrote new filters and
                    # counts. Starting over with the old copy sent the
                    # telescope to the wrong panel in the wrong filter.
                    target = self.targets.get(target["id"])
                    entry = next((e for e in self.plan.raw().get("entries") or []
                                  if e.get("id") == entry.get("id")), entry)
                    if isinstance(why, _Reframed):
                        reframed = True
                        self._say(f"{entry['name']}: starting over on the panels as "
                                  "they now are", "warn")
                    else:
                        self._say(f"{entry['name']}: the deal changed under the run - "
                                  "starting over on what the plan says now", "warn")
                    continue
                except _Skipped:
                    self._say(f"Skipped {entry['name']}", "warn")
                break

    def _finished(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._finished_entries.add(str(entry.get("id")))
        self._journal_write()

    def _panel_left(self, entry: dict[str, Any], panel: dict[str, Any]) -> int:
        """Frames this panel still needs tonight, on every telescope."""
        rigs = self.rigs.imaging()
        master_id = self.master.id
        filter_order = str(plans.options_for(entry).get("filterOrder") or "grouped")
        fitted = plans.fitted_filters(rigs)
        index = int(panel.get("index", 0) or 0)
        left = 0
        for rig in rigs:
            with contextlib.suppress(Exception):
                queue = _flatten(plans.allocation_for(entry, rig.id, master_id, fitted),
                                 filter_order)
                left += len(self._without_done(queue, entry["id"], index, rig.id))
        return left

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
            # ...and a telescope that ran out of attempts on the last target
            # gets one more full set on this one.
            for rec in self._rig_recovery.values():
                if rec.state == RIG_FAILED:
                    rec.state = RIG_OK
                    rec.attempt = 0
                    rec.reason = ""
        self._journal_write()

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
                self._night_over = True
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
            should_abort=lambda: (self._alive() or self._abort.is_set()
                                  or self._skip.is_set()
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
        self._ensure_sky_cover(entry.get("name") or "the all-sky survey")
        if not self._ensure_roof_open(entry.get("name") or "the all-sky survey", entry):
            return
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
            # 0.2.53b: never a light under a roof that does not read open.
            self._roof_gate_lights()
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
        # The cover first, and whether or not the roof is already open. The
        # Twin FSQ left the lid shut through centring and autofocus because
        # nothing re-read CoverState until a later light frame.
        self._ensure_sky_cover(entry.get("name") or "the target")
        # 0.2.53b: the roof opened before anything moves or exposes - at the
        # start of the sequence (its first target) and before every target
        # after it, if it is not open by then.
        if not self._ensure_roof_open(entry.get("name") or "the target", entry):
            return
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

        resumed = any(key.startswith(f"{entry['id']}:") for key in self._progress)
        for position, panel in enumerate(queue, start=1):
            self._check()
            if self._skip.is_set():
                raise _Skipped()
            # After a restart, a panel that already has everything is not
            # slewed to again.
            if resumed and not self._panel_left(entry, panel):
                continue
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
                # save=True, always: a sequence's lights are the point of the
                # night, and whatever the Image tab or an interrupted focus
                # sweep left saving set to is not a reason to drop them.
                rig.capture.set_output(save=True, target=entry["name"],
                                       object_name=object_name, panel=panel_tag)
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

            self._goto(panel, label, rotate=self._rotate_for(entry, target))
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
            # guider it had just settled. And through the filter the first
            # frame will use: a sweep through whatever the wheel was left on
            # was followed by a filter change, and a filter change is a
            # reason to sweep again - two runs, nine minutes, before the
            # first sub of the night.
            self._select_first_filters(entry)
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
        self._finished(entry)

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
        # A sensor is a rectangle, and a rectangle turned half a circle
        # covers the same sky: 95 degrees and 275 degrees are the same
        # footprint, and a solver that reports the one when the settings say
        # the other has not found the camera turned. The angle is brought to
        # the same half-turn as the layout before the two are compared, so
        # that only a real turn re-lays the panels.
        measured = astro.same_half_turn(float(measured), float(expected))
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

    def _rotate_for(self, entry: dict[str, Any] | None, target: dict[str, Any] | None) -> bool:
        """Whether this entry's panels may turn the rotator.

        A collaboration chunk joined with "turn the rotator to the project's
        angle" switched off was laid at the camera's own angle precisely so
        that the rotator stays where it is - and then every slew turned it
        anyway, to the angle the panels were laid at, which the rotator's
        own calibration put somewhere else. Everything else may turn.
        """
        if not entry or not target or not (target.get("collab") or {}).get("task"):
            return True
        return bool(plans.options_for(entry).get("collabMatchRotation", True))

    def _goto(self, panel: dict[str, Any], label: str, rotate: bool = True) -> None:
        """Point the mount.  The slaves are bolted to it and come along.

        Guiding stops first: a guider still pulsing the mount through a slew
        and a centring has lost its star, and its corrections fight the sync
        and the nudges. `_run_entry` starts it again once the panel is
        centred and focused.

        The cover is verified before the home and the slew, so a centring
        frame cannot be the first thing that asks the lid to move.
        """
        self._ensure_sky_cover(label)
        self._home_mount()
        guider = self.manager.get("guider")
        if guider is not None and guider.connected and guider.guiding:
            self._say("Stopping guiding for the slew and the centring")
            self._stop_guiding()
        self._set("slewing", f"slewing to {label}")
        self._say(f"Slewing to {label}")
        started = time.monotonic()
        mount = self._mount_ready()
        self._ensure_tracking(mount)

        rotator = self.manager.get("rotator")
        rotation = panel.get("rotation")
        rotating = (rotate and rotation is not None and rotator is not None
                    and rotator.connected)
        if rotating:
            with contextlib.suppress(Exception):
                self._say(f"Rotator: {float(rotator.position):.1f}° -> {float(rotation):.1f}° "
                          f"for {label}")
            rotator.move_absolute(float(rotation))
        elif rotation is not None and rotator is not None and rotator.connected:
            self._say(f"Rotator left where it is ({float(rotator.position):.1f}°): "
                      "this collaboration is shot at the camera's own angle")

        mount = self._slew(mount, astro.normalise_ra_hours(panel["ra"]),
                           float(panel["dec"]), label)
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
                self._unwarn("solver.failing")
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

    #: How long between tries when the mount refuses a slew.
    SLEW_RETRY_SECONDS = 3.0

    def _mount_ready(self):
        """The mount, reconnected first if it has dropped off (0.2.51).

        FSQ106 2026-10-20 23:22 ended "Sequence failed: Mount is not
        connected". A mount that has gone is reconnected on its remembered
        driver a few times, with a backoff, before the run gives up on it.
        """
        mount = self.manager.get("mount")
        if mount is not None and mount.connected:
            return mount
        settings = self._recovery_settings()
        attempts = int(settings.get("mountReconnectAttempts") or 0) \
            if settings.get("enabled", True) and settings.get("reconnectDevices", True) else 0
        base = float(settings.get("rigRecoveryBackoffSeconds") or 20.0)
        cap = float(settings.get("rigRecoveryBackoffMaxSeconds") or 300.0)
        step = float(settings.get("recoveryStepSeconds") or 180.0)
        for attempt in range(1, attempts + 1):
            self._recovering("mount", "the mount is not connected; reconnecting it "
                             f"(attempt {attempt} of {attempts})", attempt)
            with contextlib.suppress(Exception):
                self.manager.disconnect("mount")
            try:
                outcome = call_with_timeout(
                    lambda: self.rigs.connect_remembered(self.master, ("mount",)),
                    step, "the mount reconnect")
                failed = (outcome or {}).get("failed") or []
            except Exception as exc:              # noqa: BLE001 - counted
                failed = [str(exc)]
            mount = self.manager.get("mount")
            if mount is not None and mount.connected:
                self._recovered("mount", "The mount is back", True)
                return mount
            self._say("Mount reconnect failed: " + ("; ".join(failed) or "no answer"),
                      "warn")
            if attempt < attempts:
                self._sleep(backoff(attempt, base, cap))
        if attempts:
            self._recovered("mount", "The mount did not come back", False)
        return self.manager.require("mount")

    def _slew(self, mount, ra_hours: float, dec_deg: float, label: str):
        """Ask for a slew until the mount takes it; reconnect it if it will not.

        Returns the mount the slew went to, which is a fresh one after a
        reconnect.
        """
        try:
            self._slew_once(mount, ra_hours, dec_deg, label)
            return mount
        except DeviceError as exc:
            if not (self._recovery_on("reconnectDevices")
                    and int(self._recovery_settings().get("mountReconnectAttempts") or 0)):
                raise
            self._check()
            self._say(f"{exc}; reconnecting the mount and trying once more", "warn")
            with contextlib.suppress(Exception):
                self.manager.disconnect("mount")
            mount = self._mount_ready()
            self._ensure_tracking(mount)
            self._slew_once(mount, ra_hours, dec_deg, label)
            return mount

    def _slew_once(self, mount, ra_hours: float, dec_deg: float, label: str) -> None:
        """Ask for a slew until the mount takes it.

        OnStep answers a slew sent in the first second or two after guiding
        stops with "SlewError: Already in motion": the last guide pulse is
        still being worked off.  On 2026-10-19 PHD2's pulse went out 38 ms
        before the slew to panel 6, and that one refusal was taken as fatal -
        the mount was parked and the night ended at 20:47.  So: wait for the
        pulse to clear where the driver can report it, a moment's grace where
        it cannot, and retry every refusal for a while before giving up, the
        way `_park_and_check` already does for Park.
        """
        # A guide pulse the driver can report is waited out; one it cannot
        # report gets a beat of grace, which is what the retries are for.
        settle_until = time.monotonic() + 10.0
        while getattr(mount, "pulse_guiding", False) and time.monotonic() < settle_until:
            time.sleep(0.2)
        time.sleep(0.5)
        timeout = float(self._settings().get("slewRetrySeconds") or 60.0)
        deadline = time.monotonic() + timeout
        attempts = 0
        while True:
            attempts += 1
            try:
                mount.slew_to(ra_hours, dec_deg)
                return
            except Exception as exc:              # noqa: BLE001 - the driver's word
                self._check()
                if time.monotonic() + self.SLEW_RETRY_SECONDS >= deadline:
                    raise DeviceError(
                        f"the mount refused to slew to {label} {attempts} time(s) in "
                        f"{timeout:.0f}s — last answer: {exc}") from exc
                if attempts == 1 or attempts % 5 == 0:
                    self._say(f"Slew refused ({exc}); trying again", "warn")
                time.sleep(self.SLEW_RETRY_SECONDS)

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

        The cover is verified before the first autofocus frame. A sweep
        through a closed lid measures nothing and used to run anyway.

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

        # Before the first autofocus exposure, not merely before a light.
        self._ensure_sky_cover(label or "autofocus")
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
                    should_abort=lambda: (self._alive() or self._abort.is_set()
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
                self._unwarn(f"focus.failed.{rig.id}")
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

    @staticmethod
    def _flip_minutes(settings: dict[str, Any], key: str, default: float) -> float:
        """A flip timing setting in minutes; a blank is the default, a 0 is 0."""
        value = settings.get(key)
        try:
            return max(0.0, float(default if value is None or value == "" else value))
        except (TypeError, ValueError):
            return default

    def _flip_room(self, ahead: float, settings: dict[str, Any]) -> float:
        """Hours of imaging left before the mount has to stop for the flip.

        The mount can track *flip limit* minutes past the meridian before its
        own limit stops it, and imaging has to be over *pause* minutes before
        that. With the limit at 0 - a mount that cannot pass the meridian, or
        one nobody has set a limit for - imaging stops before transit, which
        with ten-minute subs can mean a quarter of an hour of clear sky spent
        waiting at every flip. A mount that can track a few minutes past is
        let run the last frame through transit and flipped the moment it ends.
        """
        pause = self._flip_minutes(settings, "flipPauseMinutes", 5.0) / 60.0
        limit = self._flip_minutes(settings, "flipLimitMinutes", 0.0) / 60.0
        return ahead + limit - pause

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
        limit = self._flip_minutes(settings, "flipLimitMinutes", 0.0) / 60.0
        # Flip if this frame would still be running when the mount must stop.
        # A panel already an hour or more past where the mount would have
        # stopped was slewed to on the other side and is not flipped again.
        return (self._flip_room(ahead, settings) < next_frame_seconds / 3600.0
                and ahead > -(1.0 + limit))

    #: How many times a flip slew that left the mount on the same side of the
    #: pier is tried again, and how long between tries.  OnStep (and others)
    #: keep tracking past the meridian for as long as their own limit allows
    #: and only flip on a goto once the target is beyond it - two minutes past,
    #: which is the default flip wait, is not beyond it.  2026-10-19 20:23: the
    #: flip slew on panel 16 came back on the west side and the run carried on
    #: towards the limit with a warning and nothing else.
    FLIP_RETRIES = 4

    def _flip(self, panel: dict[str, Any], label: str, attempt: int = 1) -> bool:
        """Flip the mount for a panel crossing the meridian.

        True when the mount changed side (or cannot say), False when the slew
        left it where it was and the caller should try again later.
        """
        settings = self._settings()
        mount = self._mount_ready()
        before = mount.side_of_pier

        after = self._flip_minutes(settings, "flipAfterMinutes", 2.0)
        limit = self._flip_minutes(settings, "flipLimitMinutes", 0.0)
        if limit > 0:
            # Never later than the mount's own limit allows: imaging ran up to
            # the pause before it, and the mount stops tracking there.
            after = min(after, max(0.0, limit - self._flip_minutes(
                settings, "flipPauseMinutes", 5.0)))
        ahead = self._hours_to_meridian(panel["ra"]) or 0.0
        delay = max(0.0, (ahead * 60.0 + after) * 60.0)
        if delay >= 30.0:
            self._set("flipping", f"waiting {delay / 60:.1f} min for the meridian")
            self._say(f"Meridian flip for {label}: waiting {delay / 60:.1f} minutes"
                      + ("" if limit > 0 else " — the mount is set to track 0 min past "
                         "the meridian (Settings › Mount); give it the minutes its own "
                         "limit allows and the last frame runs through transit instead"),
                      "warn")
            self._sleep(delay)
        elif delay > 0:
            self._set("flipping", "flipping at the meridian")
            self._say(f"Meridian flip for {label}")
            self._sleep(delay)
        elif attempt > 1:
            self._say(f"Meridian flip for {label}: trying again "
                      f"({-ahead * 60:.0f} min past the meridian)", "warn")

        guider = self.manager.get("guider")
        guiding = guider is not None and guider.connected and guider.guiding
        if guiding:
            self._say("Stopping guiding for the flip")
            with contextlib.suppress(Exception):
                guider.stop_guiding()

        # Where the rotator stands before the flip: the angle the centring
        # solve corrected it to, which is the panel's angle plus this rig's
        # mechanical-to-sky offset - not the panel's angle itself.
        rotator = self.manager.get("rotator")
        rotation = panel.get("rotation")
        rotator_before = None
        if rotation is not None and rotator is not None and rotator.connected:
            with contextlib.suppress(Exception):
                rotator_before = float(rotator.position)

        self._set("flipping", "flipping")
        mount = self._slew(mount, astro.normalise_ra_hours(panel["ra"]),
                           float(panel["dec"]), f"{label} (flip)")
        self._wait_for_mount(mount)

        after = mount.side_of_pier
        unflipped = before is not None and after == before
        if unflipped:
            retry_minutes = float(settings.get("flipRetryMinutes") or 5.0)
            again = attempt < self.FLIP_RETRIES
            self._say(f"The mount did not change side of pier ({after}); "
                      "it may not have flipped"
                      + (f" — trying again in {retry_minutes:g} min" if again
                         else " — giving up on the flip for this panel"), "warn")
            self._warn("mount.flip", "warning", "The meridian flip may not have happened",
                       f"After the flip slew the mount still reports the {after} side "
                       f"of the pier on {label}.",
                       "The mount is probably still inside its own past-meridian limit "
                       "and will flip on a later try. If it never does, check the "
                       "driver's flip settings before it runs into its limit.")
        else:
            self._say(f"Flipped from {before} to {after}", "success")
        self._tell("activity", f"Meridian flip on {label}",
                   (f"The mount did not change side of pier ({after})"
                    if unflipped else f"Flipped from {before} to {after}")
                   + "; re-centring and restarting guiding.")
        with self._lock:
            self._flips += 1

        rotating = rotation is not None and rotator is not None and rotator.connected
        if rotating and rotator_before is not None and not unflipped:
            # 2026-11-04, EAGLE (NiteCrawler on a CEM GEM): the flip used to
            # re-command the panel's own angle (270.09), throwing away the
            # +47 deg the centring solve had found (it stood at 223.05), and
            # re-centred without an angle - every sub after the flip came out
            # at PA 137 instead of 270. The sky angle does not change on a flip;
            # the camera turns over with the tube, so the rotator goes to its
            # corrected position plus `rotatorFlipDegrees` (180 on a plain
            # rotator; 0 for one whose driver compensates for the side of
            # pier), and the solve below checks and corrects what is left.
            turn = float(settings.get("rotatorFlipDegrees", 180.0) or 0.0)
            goal = (rotator_before + turn) % 360.0
            self._set("flipping", "turning the camera for the new side of the pier")
            self._say(f"Rotator: {rotator_before:.1f}° -> {goal:.1f}° for the flip "
                      f"(keeps {float(rotation):.1f}° on the sky)")
            rotator.move_absolute(goal)
            deadline = time.monotonic() + 300
            while rotator.moving:
                self._check()
                if time.monotonic() > deadline:
                    break
                time.sleep(0.3)

        if settings.get("flipSolve", True) and self.solver.executable() is not None:
            # A full sequencer re-checks the cover before the post-flip solve.
            # A rotator-only stand-in used by the flip check has no cover.
            ensure = getattr(self, "_ensure_sky_cover", None)
            if ensure is not None:
                ensure("the re-centre after the flip")
            self._set("flipping", "re-centring after the flip")
            try:
                # With the panel's angle, so the solve measures the frame's
                # position angle and turns the rotator onto it if it is out.
                self.solver.start("center", None, astro.normalise_ra_hours(panel["ra"]),
                                  float(panel["dec"]),
                                  float(rotation) if rotating else None)
                self._wait_for_solver()
            except DeviceError as exc:
                self._say(f"Could not re-centre after the flip: {exc}", "warn")

        if guiding:
            self._say("Restarting guiding")
            with contextlib.suppress(Exception):
                guider.start_guiding(recalibrate=False)

        # Focus shifts across a flip on most rigs — on all of them.
        self._maybe_focus()
        return not unflipped

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
                camera = rig.manager.get("camera")
                instrument = str(getattr(camera, "name", "") or "") if camera is not None else ""
                bayer = str(getattr(camera, "bayer_pattern", "") or "") if camera is not None else ""
                focal = rig.config.get("optics", "focalLength", None)
                colour = bool(bayer) or bool(rig.config.get("camera", "colour", False))
                self.targets.add_panel_integration(
                    entry["targetId"], rig.capture.night_name(),
                    int(panel.get("index", 0) or 0), filter_name, record.exposure,
                    hfr=entry_row["hfr"], guide_rms=entry_row["guideRms"],
                    telescope=rig.name, instrument=instrument,
                    focal_length=float(focal) if focal else None,
                    role=getattr(rig, "role", "") or "",
                    colour=colour, bayer=bayer)

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
            if not self._calibration_sound(guider):
                return
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
        if getattr(mount, "at_park", False):
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
        # Park is asked for until the mount takes it. OnStep refuses Park
        # with "Mount in motion" for a second or two after guiding stops -
        # the last guide pulse is still being worked off - and one refusal
        # used to end the attempt, with the telescope left tracking into the
        # morning. So: a moment's grace after guiding, then every refusal is
        # retried a few seconds later until the park timeout runs out.
        deadline = time.monotonic() + timeout
        time.sleep(min(2.0, timeout))
        attempts = 0
        while True:
            attempts += 1
            try:
                mount.park()
                break
            except Exception as exc:              # noqa: BLE001 - the driver's word
                if time.monotonic() + 3.0 >= deadline:
                    raise DeviceError(
                        f"the mount refused to park {attempts} time(s) in "
                        f"{timeout:.0f}s — last answer: {exc}") from exc
                if attempts == 1 or attempts % 5 == 0:
                    self._say(f"Park refused ({exc}); trying again", "warn")
                time.sleep(3.0)
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
            if getattr(mount, "at_park", False):
                self._say("Mount parked", "success")
                return
            now = time.monotonic()
            if now >= deadline:
                break
            if getattr(mount, "slewing", False):
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

    # -- the roof opened at the start of a target (0.2.53b) ----------------
    #
    # Hakos, 2026-11-04: a sequence started at 21:50 and imaged a closed roof
    # all night - ASCOM.HakosRoof.Dome was connected but nothing ever sent it
    # OpenShutter, because the sequencer only ever closed a roof. Bray: "you
    # should make it so that the roof opens when a sequence starts".
    #
    # Only the lead telescope (the master, which owns the mount) drives the
    # roof: a follower's manager is never asked for its dome, and every
    # telescope waits on the one roof state read from the lead's.
    #
    # An open that fails, or does not read open in time, is the site's
    # technician lock (Bray's standing rule for Hakos): latched, counted as
    # closed, never retried, no close-back, no lights, an alert and a warning
    # that stays up, and the run stopped the ordinary way (park, warm).

    ROOF_POLL_SECONDS = 2.0
    #: How long a filter change may take before the lights are held.
    FILTER_SETTLE_SECONDS = 120.0
    #: Consecutive "error" reads during an open before it counts as failed;
    #: one bad read over COM is a glitch, not the driver's "Error".
    ROOF_ERROR_READS = 3

    def _roof_settings(self) -> dict[str, Any]:
        s = self._settings()
        return {
            "open": bool(s.get("openRoofAtStart", True)),
            "requireSafety": bool(s.get("requireSafetyToOpen", False)),
            "timeout": max(10.0, float(s.get("roofOpenTimeoutSeconds") or 180.0)),
            "gate": bool(s.get("pauseLightsUnlessRoofOpen", True)),
            "closeAtEnd": bool(s.get("closeRoofAtEnd", True)),
        }

    def _roof_open_wanted(self) -> bool:
        return self._roof_settings()["open"]

    def _lead_dome(self):
        """The lead telescope's roof, if it has one that can be opened."""
        try:
            dome = self.master.manager.get("dome")
        except Exception:                         # noqa: BLE001 - no master yet
            return None
        if dome is None or not getattr(dome, "connected", False) \
                or not getattr(dome, "can_shutter", False):
            return None
        return dome

    @staticmethod
    def _roof_state(dome) -> str:
        """open, closed, opening, closing, error - a read that fails is error."""
        try:
            return str(dome.shutter_state or "error").lower()
        except Exception:                         # noqa: BLE001 - a fault is not open
            return "error"

    def _safety_watching(self) -> bool:
        watcher = getattr(self, "safety", None)
        if watcher is None:
            return False
        if not self.config.section("safety").get("enabled", True):
            return False
        with contextlib.suppress(Exception):
            return bool(watcher.watching)
        return False

    def _safety_says_safe(self) -> bool:
        watcher = getattr(self, "safety", None)
        try:
            return watcher.read() is True
        except Exception:                         # noqa: BLE001 - a fault is unsafe
            return False

    def _roof_alert(self, subject: str, body: str) -> None:
        self._tell("safety", subject, body)

    def _roof_lock(self, dome, why: str) -> None:
        """Latch the technician lock and stop the run. Raises."""
        name = getattr(dome, "name", "the roof")
        self._roof_locked = why
        title = "The roof did not open - treated as LOCKED"
        detail = (f"{name}: {why}. Per the site rule a failed or timed-out open is "
                  "the technician's lock: the roof counts as closed, nothing more "
                  "is sent to it (no retry, no close), and no lights are taken.")
        fix = ("Check with the site. If the roof really is open, close it from the "
               "roof's own control. Start the sequence again by hand once it is "
               "unlocked - an auto-restart never opens against a lock.")
        self._warn("roof.locked", "critical", title, detail, fix)
        self._say(f"{title}: {why}", "error")
        self._roof_alert(title, f"{detail}\n\n{fix}\n\n{self._summary()}")
        raise DeviceError(f"the roof did not open ({why}) - treated as the "
                          "technician's lock; the sequence is stopped")

    def _ensure_roof_open(self, label: str, entry: dict[str, Any] | None = None) -> bool:
        """Open the lead's roof before `label` moves or exposes anything.

        True to go on (open, or nothing to open, or the setting is off).
        False when the night ran out while waiting for a safe sky. Raises
        (stopping the run) on a failed or timed-out open, a latched lock,
        or 'Require safety monitor to open' with no monitor.
        """
        settings = self._roof_settings()
        if not settings["open"]:
            return True                             # old behaviour: never opened
        dome = self._lead_dome()
        if dome is None:
            return True
        if self._roof_state(dome) == "open":
            return True
        if self._roof_locked:
            raise DeviceError(f"the roof is treated as locked ({self._roof_locked}); "
                              "nothing more is sent to it tonight")

        # The safety monitor first: an unsafe sky is waited out, never opened into.
        if self._safety_watching():
            self._unwarn("roof.nosafety")
            told = False
            while not self._safety_says_safe():
                if not told:
                    self._say(f"The roof stays shut before {label}: the safety "
                              "monitor says it is not safe - waiting", "warn")
                    told = True
                self._set("waiting", "roof shut: waiting for the safety monitor")
                reason = self._past_deadline(entry, (entry or {}).get("endAt"))
                if reason:
                    self._say(f"{reason} - the roof was never opened for {label}", "warn")
                    return False
                self._sleep(10.0)
                if self._roof_state(dome) == "open":
                    return True
        elif settings["requireSafety"]:
            title = "The roof was not opened: no safety monitor"
            detail = ("'Require safety monitor to open' is on and no safety monitor "
                      "is connected or configured, so the roof stays shut.")
            self._warn("roof.nosafety", "critical", title, detail,
                       "Connect the safety monitor, or turn the requirement off "
                       "under Equipment → Sequencer.")
            self._roof_alert(title, detail)
            raise DeviceError("the roof was not opened: 'Require safety monitor to "
                              "open' is on and there is no safety monitor")
        else:
            title = "Roof opened with no safety monitor - nothing will close it for weather"
            detail = (f"{getattr(dome, 'name', 'The roof')} is being opened for {label} "
                      "with no safety monitor connected or configured. Starfront "
                      "closes it at the end of the run, but nothing will close it "
                      "if the weather turns.")
            self._warn("roof.nosafety", "warning", title, detail,
                       "Connect a safety monitor (Equipment → Safety), or watch the sky.")
            self._roof_alert(title, detail)
            self._say(title, "warn")

        timeout = settings["timeout"]
        self._set("homing", "opening the roof")
        self._say(f"Opening the roof ({getattr(dome, 'name', 'dome')}) before {label}", "warn")
        try:
            roofguard.request_open(dome)
        except Exception as exc:                  # noqa: BLE001 - a refused open
            self._roof_lock(dome, f"OpenShutter failed: {exc}")
        self._roof_opened_by_run = True
        deadline = time.monotonic() + timeout
        errors = 0
        state = self._roof_state(dome)
        while True:
            if state == "open":
                break
            errors = errors + 1 if state == "error" else 0
            if errors >= self.ROOF_ERROR_READS:
                self._roof_lock(dome, "the roof reported an error while opening")
            if time.monotonic() >= deadline:
                # "opening" is never proof of progress: HakosRoof 6.2 reads a
                # closing roof and every text it does not know as opening.
                self._roof_lock(dome, f"not open after {timeout:.0f} s (it reads {state})")
            self._sleep(self.ROOF_POLL_SECONDS)
            state = self._roof_state(dome)
        self._say("Roof open", "success")
        self._unwarn("roof.closed")
        return True

    def _roof_gate_lights(self) -> None:
        """Hold the next light until the lead's roof reads open.

        Closed, closing, opening, error: none of them is open. The run is held
        (re-checked every few seconds), alerted once it has stayed shut for a
        minute, and carries on by itself once it reads open. Nothing is sent to
        the roof from here: a roof shut part way through the night was shut by
        somebody or something, and is not opened against. Calibration frames
        never come through here.
        """
        if not self._roof_settings()["gate"]:
            return
        dome = self._lead_dome()
        if dome is None:
            return
        state = self._roof_state(dome)
        if state == "open":
            return
        started = time.monotonic()
        alerted = False
        self._say(f"Holding the lights: the roof reads {state}", "warn")
        while state != "open":
            self._set("waiting", f"lights held: the roof reads {state}")
            if not alerted and time.monotonic() - started >= 60.0:
                alerted = True
                title = "Lights held: the roof is not open"
                detail = (f"{getattr(dome, 'name', 'The roof')} has read {state} for a "
                          "minute. No light is taken until it reads open"
                          + (f" (treated as locked: {self._roof_locked})"
                             if self._roof_locked else "") + ".")
                self._warn("roof.closed", "critical", title, detail,
                           "Open the roof, or stop the sequence.")
                self._roof_alert(title, detail)
            reason = self._past_deadline(None, None)
            if reason:
                self._say(f"{reason} while the lights were held for the roof", "warn")
                raise _Skipped()
            self._sleep(5.0)
            state = self._roof_state(dome)
        self._unwarn("roof.closed")
        self._say("The roof reads open again: carrying on", "success")

    def _close_roof_after_run(self) -> None:
        """Shut the roof this run opened, once the mount is parked (0.2.53b)."""
        if not (self._roof_opened_by_run and self._roof_settings()["closeAtEnd"]):
            return
        mount = self.manager.get("mount")
        if mount is not None and getattr(mount, "connected", False) \
                and not getattr(mount, "at_park", False):
            self._say("The roof this run opened is left open: the mount is not "
                      "parked", "error")
            self._tell("failure", "The roof was left open",
                       "The mount did not report parked, so the roof was not closed "
                       "over it. " + self._summary())
            return
        self._close_roof()
        self._roof_opened_by_run = False

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
        if self._roof_locked and self._roof_state(dome) != "open":
            # 0.2.53b: the technician's lock counts as closed and is never
            # fought - no close-back is sent. A roof that reads open is closed
            # as before: the lock is evidently not on.
            self._say(f"Roof treated as locked and closed ({self._roof_locked}): "
                      "nothing sent to it", "warn")
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
        self._unwarn("roof.nosafety")
        if getattr(dome, "can_park", False) and not dome.at_park:
            with contextlib.suppress(Exception):
                dome.park()

    def _abort_cameras(self) -> None:
        for stop in list(self.extra_aborts):
            with contextlib.suppress(Exception):
                stop()
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
        self._cancel_warm_later()
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

    def _start_warming(self, only: list[Any] | None = None) -> None:
        """Ramp the coolers off on a thread of their own.

        Detached on purpose. Warming is several minutes of sleeping that has
        nothing to do with the sequence any more, and doing it on the sequencer
        thread meant Stop did not take effect until it finished.
        """
        if self.warming:
            return
        self._warm_stop.clear()
        rigs = [rig for rig in (self.rigs.all if only is None else only)
                if (camera := rig.manager.get("camera")) is not None
                and camera.connected and camera.can_cool
                and rig.config.section("camera").get("warmAtEnd", True)]
        if not rigs:
            return
        self._warm_thread = threading.Thread(
            target=lambda: self._warm_cameras(rigs), daemon=True, name="warm-down")
        self._warm_thread.start()

    # -- warming after a run (0.2.52d) ---------------------------------------
    #
    # 2026-11-04 00:16: "when i stop a sequence i dont need the camera to warm
    # up". Every end of a run used to ramp the coolers off - a Stop pressed to
    # change one setting, and a run that ended in its first second because the
    # target was still under its altitude floor, both cost a ten-minute warm
    # and another cool-down. Now only the end of the night warms at once.

    #: How close to dawn (on top of the dawn margin) a plan that runs out
    #: counts as having run to the end of the night.
    END_OF_NIGHT_SLACK_SECONDS = 30 * 60

    def _warm_decision(self, close_out: bool) -> tuple[bool, str]:
        """Whether this run's end warms the cameras now, and why (for the log)."""
        if close_out:
            return True, "the night is being closed out"
        if self._stopped_by_hand:
            return False, "stopped by hand"
        if self._error:
            return False, "the run failed"
        if self._night_over:
            return True, "the night is over"
        with self._lock:
            frames = sum(self._done.values())
        dawn = None
        with contextlib.suppress(Exception):
            dawn = self._dawn()
        if dawn:
            margin = float(self._settings().get("dawnMarginMinutes") or 0.0) * 60.0
            if time.time() >= dawn - margin - self.END_OF_NIGHT_SLACK_SECONDS:
                return True, "the night is over"
        if not frames:
            return False, "nothing was shot"
        if not dawn:
            # No site, so no dawn to tell the end of the night by: a plan
            # that was shot through to its end is the end, as it always was.
            return True, "the plan is finished"
        return False, "the plan ran out before the end of the night"

    def _warm_after_run(self, close_out: bool = False) -> None:
        """Warm the cameras at the end of a run - or, after a Stop, keep them cold.

        * warmAtEnd off (per telescope): never warmed by a run's end at all.
        * The night over (dawn), a loop's night put to bed, a failure being
          closed out, Shut down: warmed now, as before.
        * Stopped by hand, failed without a close-out, or ended before the
          night did (the plan ran out, nothing was up): the cooler stays on at
          its setpoint. warmOnStop brings the old warm-at-once back; otherwise,
          with warmAtDawnIfCold (on by default), the camera is warmed at dawn
          if no sequence has started by then, so it is not left cold all day.
        """
        if self.warming:
            return                       # a loop's put-to-bed is warming them already
        now, why = self._warm_decision(close_out)
        rigs = [rig for rig in self.rigs.all
                if (camera := rig.manager.get("camera")) is not None
                and camera.connected and getattr(camera, "can_cool", False)
                and rig.config.section("camera").get("warmAtEnd", True)]
        if not rigs:
            return
        at_once = [rig for rig in rigs if now
                   or rig.config.section("camera").get("warmOnStop", False)]
        if at_once:
            self._start_warming(at_once)
        # Only a cooler that is on is being kept cold; one already off (a
        # loop's night put to bed, then Stop) is left alone and not booked.
        cold = [rig for rig in rigs if rig not in at_once
                and _cooler_on(rig.manager.get("camera"))]
        if not cold:
            return
        dawn = None
        with contextlib.suppress(Exception):
            dawn = self._next_dawn()
        later = [rig for rig in cold
                 if rig.config.section("camera").get("warmAtDawnIfCold", True)]
        for rig in cold:
            camera = rig.manager.get("camera")
            reading = None
            with contextlib.suppress(Exception):
                reading = camera.temperature
            where = f" at {reading:.1f} C" if isinstance(reading, (int, float)) else ""
            when = (f"; it warms at {_clock(dawn)} unless a sequence starts first"
                    if dawn and rig in later else "")
            self._say(f"{rig.name}: the camera stays cooled{where} ({why}){when}")
        if dawn and later:
            self._warm_later(later, dawn)

    #: A run stopped in daylight - after dawn, or before dusk - has no dawn
    #: coming to wait for; its cameras warm this long after it ended instead.
    WARM_LATER_DAYTIME_SECONDS = 60 * 60

    def _next_dawn(self) -> float | None:
        """When cameras a stopped run left cold should warm.

        The end of the night it was stopped in; or, stopped in daylight, an
        hour from now. None without a site, when there is nothing to go by.
        """
        site = self._site()
        if site is None:
            return None
        now = time.time()
        night = schedule.night(site[0], site[1])
        dusk = (night.get("duskAstronomical") or night.get("duskNautical")
                or night.get("sunset"))
        dawn = (night.get("dawnAstronomical") or night.get("windowEnd")
                or night.get("sunrise"))
        if dusk and dawn and float(dusk) <= now < float(dawn):
            return float(dawn)
        return now + self.WARM_LATER_DAYTIME_SECONDS

    def _warm_later(self, rigs: list[Any], at: float) -> None:
        """Warm these cameras at `at`, unless a sequence starts before then."""
        self._cancel_warm_later()
        self._warm_later_stop.clear()
        self._warm_later_at = float(at)

        def wait_then_warm() -> None:
            if self._warm_later_stop.wait(max(0.0, at - time.time())):
                return
            # A run going by then owns the cameras and warms them itself.
            if self.running:
                return
            self._say("Dawn: warming the camera(s) a stopped run left cold")
            self._start_warming(rigs)

        self._warm_later_thread = threading.Thread(
            target=wait_then_warm, daemon=True, name="warm-at-dawn")
        self._warm_later_thread.start()

    def _cancel_warm_later(self) -> None:
        self._warm_later_stop.set()
        thread = self._warm_later_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._warm_later_thread = None
        self._warm_later_at = None

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
                # The same patient, verified park the shutdown uses: asked
                # for until the mount takes it, then confirmed by AtPark. A
                # single Park that the mount refused - OnStep says "in
                # motion" for a moment after guiding stops - once left the
                # telescope tracking into the morning after a normal night.
                self._park_and_check()
            elif stop_tracking:
                mount.set_tracking(False)
                self._say("Tracking stopped")
        except Exception as exc:                  # noqa: BLE001 - reported, not fatal
            self._say(f"Could not make the mount safe: {exc}", "error")
            if park:
                # Not parked is bad; tracking on into the pier is worse
                # (2026-10-08 06:02, 10-13 06:07: park refused, left tracking).
                with contextlib.suppress(Exception):
                    mount.set_tracking(False)
                    self._say("Parking failed, so tracking is stopped instead", "warn")
                # Somebody not in the room needs to hear this one.
                self._tell("failure", "The mount did not park at the end of the sequence",
                           f"{exc}. The telescope may still be pointing at the sky.")

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

    def _guiding_rough(self) -> str | None:
        """Guiding that is running but trailing every sub, or None.

        A guider at 5" RMS is still "guiding" by every state it reports, and
        the ten subs before anyone looks are all trailed.  Past the limit for
        the set number of frames in a row this says so, and the caller drops
        the star for a fresh one; between times it is only noted, and a fresh
        star is not asked for again until the spacing has passed.
        """
        settings = self._recovery_settings()
        limit = float(settings.get("roughGuidingArcsec") or 0)
        if limit <= 0:
            return None
        guider = self.manager.get("guider")
        if guider is None or not guider.connected or not guider.guiding:
            return None
        try:
            status = guider.status()
            rms = status.get("rmsTotal")
            samples = int(status.get("samples") or 0)
        except Exception:                        # noqa: BLE001 - informational only
            return None
        # Ten guide steps is the least that makes an RMS worth acting on; a
        # guider straight off a dither has a window of two.
        if rms is None or samples < 10 or float(rms) <= limit:
            self._rough_runs = 0
            return None
        self._rough_runs += 1
        needed = max(1, int(settings.get("roughGuidingFrames") or 1))
        if self._rough_runs < needed:
            self._say(f'Guiding is rough: {float(rms):.1f}" RMS against {limit:g}" '
                      f"({self._rough_runs} of {needed} frames before a fresh star)",
                      "warn")
            return None
        spacing = float(settings.get("roughGuidingRetryMinutes") or 0) * 60.0
        if self._rough_last and time.monotonic() - self._rough_last < spacing:
            return None                 # a fresh star was tried not long ago
        self._rough_runs = 0
        self._rough_last = time.monotonic()
        return f'guiding is rough — {float(rms):.1f}" RMS against a limit of {limit:g}"'

    #: PHD2's RA and Dec axes should make a right angle. A calibration further
    #: off than this was made on a star PHD2 could not really see, and guiding
    #: on it pushes the star the wrong way with every correction - PHD2 chases
    #: it to the edge of the frame saying "Guiding" all the way. A real mount
    #: on a real star comes out within a few degrees.
    CALIBRATION_ORTHO_LIMIT_DEG = 10.0

    #: The star PHD2 must be seeing before a calibration is asked for. Below
    #: this it is a smudge through cloud, and the calibration will be too.
    RECALIBRATE_MIN_SNR = 10.0

    def _star_well_seen(self, guider) -> bool:
        """Is PHD2 holding a star bright enough to calibrate on?"""
        try:
            status = guider.status()
        except Exception:                        # noqa: BLE001 - informational only
            return False
        snr = status.get("snr")
        return (bool(status.get("guiding")) and not status.get("starLost")
                and snr is not None and float(snr) >= self.RECALIBRATE_MIN_SNR)

    def _calibration_sound(self, guider) -> bool:
        """Is the calibration PHD2 has just started guiding on worth guiding on?

        False means guiding has been stopped and the night goes on unguided,
        with a warning that says what to do: a bad calibration makes trailed
        subs that an unguided mount would not, so refusing is the kinder
        choice. A guider that cannot be asked, or that has no calibration to
        report, is given the benefit of the doubt.
        """
        read = getattr(guider, "calibration_data", None)
        if read is None:
            return True
        try:
            data = read()
        except Exception:                        # noqa: BLE001 - an older PHD2
            return True
        error = (data or {}).get("orthoError")
        if error is None or float(error) <= self.CALIBRATION_ORTHO_LIMIT_DEG:
            self._unwarn("guider.calibration")
            return True
        detail = (f"PHD2's RA and Dec axes are {float(error):.0f}° off a right angle "
                  f"(RA {data['xAngle']:.1f}°, Dec {data['yAngle']:.1f}°)")
        self._say(f"Not guiding on this calibration: {detail}. Every correction "
                  "would push the star the wrong way. Carrying on unguided.", "error")
        self._warn("guider.calibration", "critical", "PHD2's calibration is bad",
                   detail + ".",
                   "Calibrate PHD2 by hand on a bright star under clear sky, near the "
                   "meridian and the celestial equator, then start guiding again.")
        with contextlib.suppress(Exception):
            guider.stop_guiding()
        self._guiding_wanted = False
        return False

    def _check_guiding(self) -> None:
        """Between slots: is the guider still doing its job, and if not, fix it.

        Called between frames rather than during one, because everything the fix
        does — stopping, re-selecting a star, calibrating — would ruin the sub in
        flight anyway.
        """
        if not self._guiding_wanted and self._guide_retry_at is not None \
                and time.monotonic() >= self._guide_retry_at:
            self._retry_guiding()
        if not self._guiding_wanted or not self._recovery_on("guidingRecovery"):
            return
        fault = self._guiding_fault()
        rough = False
        if fault is None:
            self._guide_lost_since = None
            fault = self._guiding_rough()
            if fault is None:
                return
            rough = True
        else:
            self._guide_lost_in_slot = True

        settings = self._recovery_settings()
        # PHD2 usually finds the star again by itself within an exposure or two.
        # A cloud is over before a re-acquisition would have finished, so the
        # grace period is not politeness — it is the faster of the two options.
        # Rough guiding gets no grace: it has already had its frames' worth.
        grace = 0.0 if rough else float(settings.get("guideGraceSeconds") or 0)
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

        # A fresh calibration is slow and is the only thing that fixes a mount
        # that has been nudged by hand - but it is also the one thing that
        # cannot be done through cloud. A lost star means PHD2 cannot see the
        # star, and a calibration made on a star it cannot see is not a bad
        # try, it is a bad calibration: PHD2 writes it over the good one and
        # every night after guides on it. So a lost star is only ever given a
        # fresh star. Rough guiding on a star PHD2 is holding well is the real
        # signature of a bad calibration, and that is the only thing that
        # recalibrates. A restart for rough guiding looks successful the moment
        # PHD2 is guiding again - the verdict takes another window of frames -
        # so the count that matters is rough restarts across slots: a fresh
        # star first, and only when that was not it, the calibration.
        if rough:
            self._rough_restarts += 1
        recalibrate = (bool(recalibrate_after) and rough
                       and self._rough_restarts >= recalibrate_after)
        if recalibrate and not self._star_well_seen(guider):
            self._say("Guiding is rough but the guide star is too faint to "
                      "calibrate on; restarting on a fresh star instead", "warn")
            recalibrate = False

        for attempt in range(1, attempts + 1):
            if self._budget_spent():
                self._give_up("The guider keeps losing its star")
            self._recovering(
                "guiding",
                f"{fault}; restarting guiding"
                + (" on a fresh star" if rough else "")
                + (" with a fresh calibration" if recalibrate else "")
                + f" (attempt {attempt} of {attempts})", attempt)
            with contextlib.suppress(Exception):
                guider.stop_guiding()
            self._sleep(2.0)
            try:
                kwargs: dict[str, Any] = {}
                if rough:
                    # The same faint star again is the same rough guiding
                    # again: drop it and let PHD2 pick afresh.
                    kwargs["fresh_star"] = True
                guider.start_guiding(
                    settle_pixels=float(guiding.get("settlePixels") or 1.5),
                    settle_time=float(guiding.get("settleTime") or 8.0),
                    settle_timeout=float(guiding.get("settleTimeout") or 60.0),
                    recalibrate=recalibrate, **kwargs)
            except DeviceError as exc:
                self._say(f"Could not start guiding: {exc}", "warn")
                self._cooldown()
                continue
            if self._wait_for_guiding(
                    float(guiding.get("settleTimeoutSeconds") or 180.0)):
                if not self._calibration_sound(guider):
                    self._recovered("guiding", "Guiding is off for the night: PHD2's "
                                    "calibration cannot be guided on", False)
                    self._guide_lost_since = None
                    return
                self._recovered("guiding", "Guiding again on a fresh star" if rough
                                else "Guiding again", True)
                self._guide_lost_since = None
                return
            self._cooldown()

        # Out of attempts, but an unguided sub of a bright target is still a
        # sub: the night carries on and the log says loudly why it is worse.
        self._guide_lost_since = None
        self._guiding_wanted = False
        # ...for now. 2026-10-14 01:52: a cloud took the star, three restarts
        # inside it failed, and the rest of the night went unguided under a
        # clear sky. Tried again later, spaced out further each time.
        minutes = float(settings.get("guideRetryMinutes") or 0.0)
        later = ""
        if minutes > 0:
            wait = minutes * 60.0 * (2 ** min(self._guide_retries, 3))
            self._guide_retry_at = time.monotonic() + wait
            later = f"; trying again in {wait / 60.0:.0f} min"
        self._recovered(
            "guiding",
            "Could not get the guider going again; carrying on unguided — "
            f"these frames will not be as tight{later}", False)

    def _retry_guiding(self) -> None:
        """Guiding that was given up on earlier in the night, tried again."""
        self._guide_retry_at = None
        self._guide_retries += 1
        guider = self.manager.get("guider")
        if guider is None or not guider.connected:
            return
        self._recovering("guiding", "trying the guider again "
                         f"(retry {self._guide_retries} since it was given up on)",
                         self._guide_retries)
        with contextlib.suppress(Exception):
            guider.stop_guiding()
        self._start_guiding()
        if self._guiding_wanted:
            self._guide_retries = 0
            self._recovered("guiding", "Guiding again", True)
            return
        minutes = float(self._recovery_settings().get("guideRetryMinutes") or 0.0)
        if minutes > 0:
            wait = minutes * 60.0 * (2 ** min(self._guide_retries, 3))
            self._guide_retry_at = time.monotonic() + wait
        self._recovered("guiding", "The guider still will not guide; carrying on "
                        "unguided", False)

    def _wait_for_guiding(self, timeout: float) -> bool:
        """Wait for the guider to settle onto a star.  True if it did."""
        deadline = time.monotonic() + timeout
        # PHD2 still says Stopped for a few seconds after `guide` is accepted:
        # it has a guide exposure to take before it reports anything else.
        # Read one second in, that was "the guider is Stopped rather than
        # guiding; carrying on unguided" (2026-10-19 20:14:51) and a panel shot
        # unguided while PHD2 was in fact guiding. So Stopped only counts once
        # it has had long enough to be a real stop.
        grace = time.monotonic() + 20.0
        time.sleep(1.0)
        guider = self.manager.get("guider")
        while time.monotonic() < deadline:
            self._check()
            if guider is None or not guider.connected:
                return False
            if guider.guiding and not guider.settling:
                return True
            if guider.state == "LostLock":
                return False
            if guider.state == "Stopped" and time.monotonic() > grace:
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
        # 0.2.53b: never a light under a roof that does not read open.
        self._roof_gate_lights()

        # The first light frame is where the sensor temperature finally matters.
        # By now the mount has homed, slewed, centred and focused, so this is
        # usually already satisfied and returns at once.
        cool_limit = 60.0 + max(
            [float(rig.config.section("camera").get("coolTimeoutMinutes") or 20.0) * 60.0
             for rig in active] or [0.0])
        self._expect(cool_limit)
        self._parallel([(rig, lambda rig=rig: self._await_cooling(rig))
                        for rig in active], "cool", timeout=cool_limit)

        self._check()
        limit = self._frame_timeout(max(float(frames[rig.id]["exposure"]) for rig in active))
        self._expect(limit + 5.0)
        outcome = self._parallel([(rig, job(rig)) for rig in active], "expose",
                                 timeout=limit)
        self._expect(None)
        self._free_hung(active, outcome)
        if not outcome["errors"] or not self._recovery_on("enabled"):
            return outcome

        settings = self._recovery_settings()
        retries = int(settings.get("frameRetryAttempts") or 0)
        wait = float(settings.get("frameRetrySeconds") or 0)
        # When the other telescopes' frames landed, the one that failed gets a
        # single quick retry here; anything more happens in the background
        # (`_bench`) so the others are not kept waiting on it.
        if len(active) > 1 and len(outcome["errors"]) < len(active):
            retries = min(retries, 1)
        # A camera that hung is not asked again in this slot: it has already
        # cost a whole frame timeout, and its recovery starts with a reconnect.
        hung = set(outcome.get("hung") or [])
        # So is one just back from a background recovery: it has had its
        # quick retries already, and failing again sends it straight back.
        hung |= {rig.id for rig in active
                 if (rec := self._rig_recovery.get(rig.id)) is not None and rec.attempt > 0}
        # And one the camera check has already reconnected three times and
        # called gone (0.2.52e): asking it again here only delays the others.
        hung |= {rig_id for rig_id, message in outcome["errors"].items()
                 if "power-cycle or replug it at the scope" in str(message)}

        for attempt in range(1, retries + 1):
            if not outcome["errors"]:
                break
            if self._budget_spent():
                self._give_up("The cameras keep failing")
            failed = [rig for rig in active
                      if rig.id in outcome["errors"] and rig.id not in hung]
            if not failed:
                break
            first = outcome["errors"][failed[0].id]
            self._recovering(
                "camera",
                ", ".join(rig.name for rig in failed) + f": {first}; trying that "
                f"frame again in {wait:g}s (attempt {attempt} of {retries})",
                attempt)
            self._sleep(wait)
            if settings.get("reconnectDevices", True) and attempt > 1:
                self._reconnect_cameras(failed)
            self._expect(limit + 5.0)
            retry = self._parallel([(rig, job(rig)) for rig in failed], "re-expose",
                                   timeout=limit)
            self._expect(None)
            self._free_hung(failed, retry)
            hung |= set(retry.get("hung") or [])
            outcome["results"].update(retry["results"])
            outcome["errors"] = {**{rig_id: message for rig_id, message
                                    in outcome["errors"].items()
                                    if rig_id in hung and rig_id not in retry["results"]},
                                 **retry["errors"]}
            if not outcome["errors"]:
                self._recovered("camera", "The camera answered again", True)
                return outcome

        if retries:
            self._recovered("camera", "The camera did not answer again", False)
        return outcome

    # -- one telescope's recovery (0.2.51) ------------------------------------
    #
    # Detect (a frame that failed or hung) -> safe state (abort the exposure,
    # free the capture) -> reconnect, with the power box on a repeat ->
    # verify (the camera answers) -> rejoin at the frame it missed, refocused
    # if it was away long enough.  On its own thread, with a backoff between
    # attempts and a deadline on every step, so the telescopes that are fine
    # are never kept waiting on the one that is not.

    def _rec(self, rig) -> RigRecovery:
        with self._lock:
            rec = self._rig_recovery.get(rig.id)
            if rec is None:
                rec = self._rig_recovery[rig.id] = RigRecovery(rig.id, rig.name)
            return rec

    def _rig_usable(self, rig) -> bool:
        rec = self._rig_recovery.get(rig.id)
        if rec is not None and rec.state != RIG_OK:
            return False
        camera = rig.manager.get("camera")
        if camera is None or not camera.connected:
            # Dropped without a failed frame to say so: recover it.
            self._bench(rig, "the camera is not connected")
            return False
        return True

    def _rig_recovering(self, rig) -> bool:
        rec = self._rig_recovery.get(rig.id)
        return rec is not None and rec.state == RIG_RECOVERING

    def _expect(self, seconds: float | None) -> None:
        """Tell the watchdog how long the step about to block may take."""
        with self._lock:
            self._expect_by = None if seconds is None else time.monotonic() + float(seconds)
        self._beat()

    def _frame_timeout(self, exposure: float) -> float:
        """The longest one frame may take before its telescope is given up on.

        The exposure, the camera's own hang allowance, the download deadline
        - twice, because a failed frame gets a reconnect and one more try
        inside the capture - and some slack.
        """
        settings = self._recovery_settings()
        hung = max(30.0, float(settings.get("hungSeconds") or 120.0))
        download = float(settings.get("downloadTimeoutSeconds") or 120.0)
        grace = float(settings.get("slotGraceSeconds") or 60.0)
        return 2.0 * (float(exposure) + hung + download) + grace

    def _free_hung(self, rigs: list[Any], outcome: dict[str, Any]) -> None:
        """A capture thread stuck in a driver: abort it and free the camera."""
        for rig_id in outcome.get("hung") or []:
            rig = next((r for r in rigs if r.id == rig_id), None)
            if rig is None:
                continue
            self._say(f"{rig.name}: the camera has stopped answering (hung in the "
                      "driver); giving up on that frame", "error")
            with contextlib.suppress(Exception):
                call_with_timeout(rig.capture.abort, 15.0, "aborting the exposure")
            with contextlib.suppress(Exception):
                rig.capture.release_stuck()

    def _bench(self, rig, reason: str) -> None:
        """Take a telescope out of the slots and start recovering it."""
        settings = self._recovery_settings()
        rec = self._rec(rig)
        if rec.state != RIG_OK:
            return
        attempts = int(settings.get("rigRecoveryAttempts") or 0)
        with self._lock:
            rec.name = rig.name
            rec.reason = str(reason)
            rec.attempts = attempts
            rec.failures += 1
            rec.since = time.time()
            if rec.down_since is None:
                rec.down_since = time.time()
            if not settings.get("enabled", True) or attempts <= 0 \
                    or rec.attempt >= attempts:
                rec.state = RIG_FAILED
                rec.step = ""
            else:
                rec.state = RIG_RECOVERING
                rec.step = "starting"
        rec.note(f"failed: {reason}", False)
        self._set_rig(rig, state=rec.state)
        if rec.state == RIG_FAILED:
            self._say(f"{rig.name}: out of recovery attempts — {reason}; it sits out "
                      "the rest of this target", "error")
            self._tell("recovery", f"Could not recover {rig.name}", reason)
            self._recovery_banner()
            return
        self._say(f"{rig.name}: recovering in the background, the other telescopes "
                  f"carry on — {reason}", "warn")
        self._tell("recovery", f"{rig.name} is recovering", reason)
        thread = threading.Thread(target=self._recover_rig,
                                  args=(rig, rec, self._generation),
                                  daemon=True, name=f"recover-{rig.id}")
        rec.thread = thread
        thread.start()
        self._recovery_banner()

    def _recovery_banner(self) -> None:
        """The run's recovery banner: every telescope that is not ok."""
        with self._lock:
            troubled = [rec for rec in self._rig_recovery.values() if rec.state != RIG_OK]
            if not troubled:
                if self._recovery and self._recovery.get("kind") == "telescope":
                    self._recovery = None
                return
            first = troubled[0]
            self._recovery = {
                "kind": "telescope",
                "reason": "; ".join(f"{rec.name}: {rec.label()}" for rec in troubled),
                "attempt": first.attempt, "attempts": first.attempts,
                "since": first.since,
                "rigs": [rec.as_dict() for rec in troubled],
            }

    def _recover_rig(self, rig, rec: RigRecovery, generation: int) -> None:
        settings = self._recovery_settings()
        base = float(settings.get("rigRecoveryBackoffSeconds") or 20.0)
        cap = float(settings.get("rigRecoveryBackoffMaxSeconds") or 300.0)
        step = float(settings.get("recoveryStepSeconds") or 180.0)

        def gone() -> bool:
            return self._abort.is_set() or generation != self._generation

        def doing(text: str) -> None:
            with self._lock:
                rec.step = text
            self._recovery_banner()

        try:
            while rec.attempt < rec.attempts:
                with self._lock:
                    rec.attempt += 1
                    delay = backoff(rec.attempt, base, cap)
                    rec.next_at = time.time() + delay
                doing(f"waiting {delay:g}s before attempt {rec.attempt} of {rec.attempts}")
                until = time.monotonic() + delay
                while time.monotonic() < until:
                    if gone():
                        return
                    time.sleep(min(0.25, max(0.0, until - time.monotonic())))
                with self._lock:
                    rec.next_at = None

                # Safe state: nothing half-exposed, nothing holding the camera.
                doing("aborting the exposure")
                with contextlib.suppress(Exception):
                    call_with_timeout(rig.capture.abort, min(step, 30.0),
                                      "aborting the exposure")
                with contextlib.suppress(Exception):
                    rig.capture.release_stuck()
                if gone():
                    return

                # Reconnect: the remembered driver, the power box on a repeat.
                doing("reconnecting the camera")
                failed: list[str] = []
                with contextlib.suppress(Exception):
                    call_with_timeout(lambda: rig.manager.disconnect("camera"),
                                      min(step, 30.0), "disconnecting the camera")
                try:
                    reconnect = getattr(self.rigs, "reconnect_camera", None)
                    if reconnect is not None:
                        outcome = call_with_timeout(
                            lambda: reconnect(rig, stop=gone, progress=doing),
                            step, "the camera reconnect")
                    else:
                        outcome = call_with_timeout(
                            lambda: self.rigs.connect_remembered(rig, ("camera",)),
                            step, "the camera reconnect")
                    failed = list((outcome or {}).get("failed") or [])
                except Exception as exc:          # noqa: BLE001 - counted as a failure
                    failed = [str(exc)]
                if gone():
                    return

                # Verify: connected and answering.
                doing("checking the camera answers")
                camera = rig.manager.get("camera")
                ok = camera is not None and camera.connected
                if ok:
                    try:
                        call_with_timeout(lambda: getattr(camera, "temperature", None),
                                          min(step, 30.0), "reading the camera")
                    except Exception as exc:      # noqa: BLE001 - counted as a failure
                        ok = False
                        failed.append(str(exc))
                if ok:
                    # Connected is not answering (0.2.52e): a driver that has
                    # lost its camera reconnects "fine" and reads 0 to 0 s.
                    probe = getattr(camera, "probe", None)
                    if probe is not None:
                        try:
                            bad = call_with_timeout(probe, min(step, 60.0),
                                                    "checking the camera")
                        except Exception as exc:  # noqa: BLE001 - counted as a failure
                            bad = str(exc)
                        if bad:
                            ok = False
                            failed.append(f"it reconnected but {bad}")
                if ok:
                    self._rig_back(rig, rec)
                    return
                with self._lock:
                    rec.reason = "; ".join(failed) or "the camera did not come back"
                rec.note(f"attempt {rec.attempt}: {rec.reason}", False)
                self._say(f"{rig.name}: recovery attempt {rec.attempt} of {rec.attempts} "
                          f"failed — {rec.reason}", "warn")

            with self._lock:
                rec.state = RIG_FAILED
                rec.step = ""
            self._set_rig(rig, state=RIG_FAILED)
            self._say(f"{rig.name}: out of recovery attempts ({rec.attempts}); it sits "
                      "out the rest of this target", "error")
            self._tell("recovery", f"Could not recover {rig.name}", rec.reason)
        finally:
            self._recovery_banner()

    def _rig_back(self, rig, rec: RigRecovery) -> None:
        away = time.time() - (rec.down_since or time.time())
        with self._lock:
            rec.state = RIG_OK
            rec.step = "rejoining"
            rec.rejoined = True
            rec.recoveries += 1
        rec.note("camera back", True)
        self._set_rig(rig, state="")
        self._say(f"{rig.name}: the camera is back (attempt {rec.attempt} of "
                  f"{rec.attempts}); it rejoins at the frame it missed", "success")
        self._tell("recovery", f"Recovered {rig.name}", f"after {away:.0f}s")
        # Away long enough for the focus to have moved with the temperature.
        if away > 600.0:
            focuser = rig.manager.get("focuser")
            if focuser is not None and focuser.connected:
                self._refocus_reason[rig.id] = "back after a camera recovery"

    def _rig_ok(self, rig) -> None:
        """A frame landed: this telescope's recovery count starts again."""
        rec = self._rig_recovery.get(rig.id)
        if rec is None or (rec.attempt == 0 and rec.down_since is None):
            return
        with self._lock:
            rec.attempt = 0
            rec.down_since = None
            rec.reason = ""
            rec.step = ""
            rec.rejoined = False

    def _wait_for_rigs(self, pending: list[Any], entry: dict[str, Any],
                       end_at: float | None, label: str) -> bool:
        """No telescope can shoot: wait for one to come back, or for the end.

        False when the night (or the target) ran out first - the run then
        ends and is closed out like any other end of night.
        """
        while True:
            self._check()
            if self._skip.is_set():
                raise _Skipped()
            if any(self._rig_recovery.get(r.id) is None
                   or self._rig_recovery[r.id].state != RIG_RECOVERING for r in pending):
                return True
            reason = self._past_deadline(entry, end_at)
            if reason:
                with self._lock:
                    self._close_out = f"{reason} while recovering " + ", ".join(
                        r.name for r in pending)
                self._say(f"{label}: {reason} — still recovering "
                          + ", ".join(r.name for r in pending), "error")
                return False
            self._set("recovering", "; ".join(
                f"{r.name}: {self._rec(r).label()}"
                + (f" ({self._rec(r).step})" if self._rec(r).step else "")
                for r in pending))
            time.sleep(0.5)

    def _reverify(self, active: list[Any], panel: dict[str, Any], label: str) -> None:
        """Before a recovered telescope shoots again: is the rig as it was?"""
        back = [rig for rig in active
                if (rec := self._rig_recovery.get(rig.id)) is not None and rec.rejoined]
        if not back:
            return
        for rig in back:
            self._rig_recovery[rig.id].rejoined = False
            # Gain, offset and the cooler came back with the reconnect
            # (`restore_camera`); the filter is selected by the slot.
        # The mount may have been left alone for minutes: still tracking, on
        # target? A master that was away is also the camera that solves.
        if any(rig.id == self.master.id for rig in back):
            self._check_tracking(panel, label, force_recentre=True)

    def _check_tracking(self, panel: dict[str, Any], label: str,
                        force_recentre: bool = False) -> None:
        """Between frames: a mount that stopped tracking is put right.

        2026-10-20 21:5x: the mount stopped tracking and the night carried on
        shooting trails for an hour. Tracking is switched back on and the
        target centred again, since it will have drifted.
        """
        mount = self.manager.get("mount")
        if mount is None or not getattr(mount, "connected", False):
            return
        try:
            tracking = call_with_timeout(lambda: mount.tracking, 30.0, "reading the mount")
            parked = call_with_timeout(lambda: getattr(mount, "at_park", False), 30.0,
                                       "reading the mount")
        except Exception as exc:                  # noqa: BLE001 - said, not raised
            self._say(f"Could not read the mount: {exc}", "warn")
            return
        if parked:
            return
        if tracking and not force_recentre:
            return
        if not tracking:
            self._recovering("mount", "the mount has stopped tracking; switching "
                                      "tracking back on and centring again")
            try:
                call_with_timeout(lambda: mount.set_tracking(True), 30.0,
                                  "switching tracking on")
            except Exception as exc:              # noqa: BLE001 - reported
                self._recovered("mount", f"Could not switch tracking back on: {exc}",
                                False)
                return
        if self.solver is not None and self.solver.executable() is not None:
            with contextlib.suppress(Exception):
                self._goto(panel, label)
        if not tracking:
            self._recovered("mount", "Tracking again", True)

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
                # Back with its gain, offset and cooler as they were: a
                # reopened body comes up warm with the driver's defaults.
                # The set may power-cycle the camera's outlets on the way,
                # so it is told how to notice a Stop and where to say what
                # it is waiting for.
                reconnect = getattr(self.rigs, "reconnect_camera", None)
                outcome = (reconnect(rig, stop=self._abort.is_set,
                                     progress=lambda text: self._set("recovering", text))
                           if reconnect is not None
                           else self.rigs.connect_remembered(rig, ("camera",)))
                if outcome["failed"]:
                    self._say(f"{rig.name}: " + "; ".join(outcome["failed"]), "warn")
                elif outcome["connected"]:
                    self._say(f"{rig.name}: the camera is back", "success")
                    # The lid may have dropped with the USB. Re-read it before
                    # the next centring, autofocus or light frame.
                    self._ensure_sky_cover(f"{rig.name} after the camera reconnect")
            except Exception as exc:              # noqa: BLE001 - reported, not fatal
                self._say(f"{rig.name}: could not reconnect the camera — {exc}",
                          "warn")

    # -- imaging -----------------------------------------------------------
    def _select_first_filters(self, entry: dict[str, Any]) -> None:
        """Put every telescope's wheel on the filter its first frame will use.

        Done before the start-of-target focus run so the sweep is through
        the imaging filter, and the slot's own selection afterwards finds
        the wheel already there and triggers nothing. A wheel that will not
        move says so and the slot selection tries again.
        """
        rigs = self.rigs.imaging()
        master_id = self.master.id
        options = plans.options_for(entry)
        filter_order = str(options.get("filterOrder") or "grouped")
        fitted = plans.fitted_filters(rigs)
        for rig in rigs:
            try:
                queue = _flatten(
                    plans.allocation_for(entry, rig.id, master_id, fitted), filter_order)
            except Exception:                    # noqa: BLE001 - the slot loop will say
                continue
            name = str((queue[0] if queue else {}).get("filter") or "")
            if not name:
                continue
            try:
                self._select_filter(rig, name)
            except DeviceError as exc:
                self._say(f"{rig.name}: could not select {name} before the focus "
                          f"run — {exc}", "warn")

    def _hold_unknown_filter(self, rig, name: str, why: str) -> None:
        """Do not take a light through a filter the wheel has not confirmed.

        Raises. The slot loop holds and tries again. FILTER is not guessed
        and a blank card is not written, because the exposure never starts.
        """
        title = f"{rig.name}: the filter is not known"
        detail = (f"Asked for {name or 'the filter'}. {why}. No light is taken "
                  "until the wheel reports that slot. Nothing is guessed.")
        self._warn("filter.unknown", "critical", title, detail,
                   "Check the filter wheel. The run holds; it does not shoot "
                   "a blank FILTER.")
        self._say(f"{title} — {why}", "error")
        raise DeviceError(f"{rig.name}: filter {name or '?'} did not settle ({why})")

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
                # Once per telescope and filter, not once per frame: on a
                # three-scope mount following one allocation this is the
                # normal state of two of them all night, and a log that says
                # so every two minutes buries everything else.
                key = (rig.id, name.lower(), fitted.lower())
                if key not in self._said_fitted:
                    self._said_fitted.add(key)
                    self._say(f"{rig.name}: the plan asks for {name} but {fitted} is "
                              f"in the light path, and there is no wheel to change "
                              f"it — shooting {fitted} (said once)", "warn")
            return False
        # Matched in the one spelling on both sides: a wheel that reports its
        # own names through a driver that never folded them still lines up.
        names = [filters.canonical(n) or str(n) for n in (wheel.names or [])]
        index = names.index(name) if name in names else None
        if index is None:
            slot, _said = read_slot(wheel)
            if slot is None:
                self._hold_unknown_filter(rig, name, "the wheel has no filter by that name "
                                          "and is not reporting a slot")
            self._say(f"{rig.name}: the wheel has no filter called {name}; "
                      "shooting with what is loaded", "warn")
            return False
        self._set_rig(rig, state=f"selecting {name}")
        started = time.monotonic()
        slot, said_moving = read_slot(wheel)
        if slot != index:
            wheel.set_position(index)
        deadline = time.monotonic() + float(self.FILTER_SETTLE_SECONDS)
        while True:
            self._check()
            slot, said_moving = read_slot(wheel)
            if slot == index:
                if said_moving:
                    self._say(f"{rig.name}: the wheel reported moving, but the "
                              f"position read is {name}; using that", "warn")
                break
            if time.monotonic() >= deadline:
                self._hold_unknown_filter(
                    rig, name,
                    f"it did not report slot {index} ({name}) within "
                    f"{self.FILTER_SETTLE_SECONDS:.0f}s")
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
            health = getattr(rig, "focus_health", None)
            if health is not None:
                # A focuser that has stopped answering is reconnected and the
                # move made once more (0.2.52d).
                health.call(lambda f: f.move_relative(delta),
                            f"the {name} focus offset", self._abort.is_set)
                focuser = rig.manager.get("focuser") or focuser
            else:
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

    def _deal_key(self, entry: dict[str, Any], rigs: list[Any], master_id: str) -> str:
        """What the entry asks for, as one string: filters, counts and panels."""
        options = plans.options_for(entry)
        fitted = plans.fitted_filters(rigs)
        return json.dumps({
            "filters": {rig.id: plans.allocation_for(entry, rig.id, master_id, fitted)
                        for rig in rigs},
            "panels": sorted(int(i) for i in (options.get("panels") or [])),
        }, sort_keys=True)

    def _deal_changed(self, entry: dict[str, Any], rigs: list[Any],
                      master_id: str, dealt: str) -> bool:
        """Whether the plan now asks something else of this entry."""
        if not (self.targets.get(entry.get("targetId") or "") or {}).get("collab"):
            # Only a collaboration entry is dealt by anybody but the operator,
            # and an operator editing the plan mid-run gets what the run
            # started with, as always.
            return False
        with contextlib.suppress(Exception):
            fresh = next((e for e in self.plan.raw().get("entries") or []
                          if e.get("id") == entry.get("id")), None)
            if fresh is None:
                return False
            return self._deal_key(fresh, rigs, master_id) != dealt
        return False

    def _shoot(self, entry: dict[str, Any], panel: dict[str, Any], label: str,
               end_at: float | None) -> bool:
        """Work through one panel, one slot at a time, on every telescope.

        False when it stopped early — out of night, or the field has set.

        Each telescope keeps its own place in its queue (0.2.51). A telescope
        whose camera fails is taken out of the slot and recovered in the
        background while the others carry on; it rejoins at the frame it
        missed. Only when no telescope can shoot does the run wait - in a
        "recovering" state the status names - and only when every one has
        run out of attempts does the night end, closed out safely.
        """
        imaging = self.rigs.imaging()
        rigs = imaging + [rig for rig in self.rigs.all
                          if rig not in imaging and self._rig_recovering(rig)]
        if not rigs:
            raise DeviceError("no camera is connected")
        master_id = self.master.id
        overheads = float(self.config.get("schedule", "perFrameSeconds", 15.0))
        options = plans.options_for(entry)
        filter_order = str(options.get("filterOrder") or "grouped")
        panel_index = int(panel.get("index", 0) or 0)

        # The labels are already set by `_run_entry`, before the slew.
        queues: dict[str, list[dict[str, Any]]] = {}
        # A follower with a filter in its drawer shoots the master's frames
        # through that filter, and its queue has to say so.
        fitted = plans.fitted_filters(rigs)
        for rig in rigs:
            full = _flatten(
                plans.allocation_for(entry, rig.id, master_id, fitted), filter_order)
            # After a restart: less what this panel already has tonight.
            queues[rig.id] = self._without_done(full, entry["id"], panel_index, rig.id)
            self._set_rig(rig, frame=len(full) - len(queues[rig.id]),
                          frames=len(full), filter="")

        slots = max((len(queue) for queue in queues.values()), default=0)
        with self._lock:
            self._slots = slots
        if not slots:
            return True
        dealt = self._deal_key(entry, rigs, master_id)
        position = {rig.id: 0 for rig in rigs}
        offset = {rig.id: int(self._rig_state.get(rig.id, {}).get("frame", 0) or 0)
                  for rig in rigs}

        # A panel slewed to after it crossed the meridian is already on the
        # side the driver chose for it, and "flipping" it would be a second
        # slew to the same place, a re-centre and a warning that it did not
        # change side - once per panel, on every panel of a mosaic shot late.
        ahead = self._hours_to_meridian(panel["ra"])
        flipped = ahead is not None and ahead < 0.0
        # A flip slew that left the mount on the same side is tried again a
        # few minutes later, a few times, while the flip is still due.
        flip_attempts = 0
        flip_retry_at = 0.0
        while True:
            self._check()
            if self._skip.is_set():
                raise _Skipped()
            # Between frames, the plan is read again: a collaboration entry
            # can be dealt afresh by the server while the run is on it - the
            # join dealt it blind to the Moon, the first poll with the Moon
            # dealt it again - and the night must follow the deal, not the
            # copy the run started with.
            if self._deal_changed(entry, rigs, master_id, dealt):
                raise _Redealt()

            # Whoever still has a frame left and a camera that is not being
            # recovered takes part; a telescope that ran out of allocation
            # simply sits the rest of the panel out.
            left = [rig for rig in rigs if position[rig.id] < len(queues[rig.id])]
            if not left:
                break
            active = [rig for rig in left if self._rig_usable(rig)]
            if not active:
                pending = [rig for rig in left if self._rig_recovering(rig)]
                if not pending:
                    names = ", ".join(f"{rig.name} ({self._rec(rig).reason})"
                                      for rig in left)
                    message = (f"every telescope with frames left on {label} has "
                               f"run out of recovery attempts: {names}")
                    if len(left) == len(rigs):
                        with self._lock:
                            self._close_out = message
                        raise DeviceError(message)
                    # The others have finished this panel; the run goes on
                    # without the one that failed.
                    self._say(message, "error")
                    break
                if not self._wait_for_rigs(pending, entry, end_at, label):
                    return False
                continue
            with self._lock:
                self._slot = max(position[rig.id] for rig in active) + 1
            frames = {rig.id: queues[rig.id][position[rig.id]] for rig in active}
            # A telescope back from a recovery is checked before it shoots.
            self._reverify(active, panel, label)

            changed = self._parallel(
                [(rig, lambda rig=rig: self._select_filter(rig, frames[rig.id]["filter"]))
                 for rig in active], "filter",
                timeout=float(self._recovery_settings().get("recoveryStepSeconds") or 180.0))
            unsettled = [rig for rig in active if rig.id in changed["errors"]]
            if unsettled:
                # A wheel that has not settled holds the slot. The frame is not
                # taken, and the queue is not advanced, so the same slot is
                # tried again. Stop still ends it.
                names = ", ".join(rig.name for rig in unsettled)
                self._set("waiting", f"holding the lights: filter not settled ({names})")
                self._say(f"Holding the lights: {names} — the filter wheel has not "
                          "settled, so nothing is exposed", "warn")
                self._sleep(5.0)
                continue
            self._maybe_focus(filter_changed=changed["results"])

            # Between frames is the only safe place to put any of this right: a
            # slew or a guider restart during an exposure is a ruined sub.
            self._check_recentre(panel, label)
            self._check_guiding()
            self._check_tracking(panel, label)

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

            if (not flipped and time.monotonic() >= flip_retry_at
                    and self._flip_due(panel, longest + overheads)):
                flip_attempts += 1
                flipped = self._flip(panel, label, flip_attempts)
                if not flipped:
                    if flip_attempts >= self.FLIP_RETRIES:
                        flipped = True
                    else:
                        minutes = float(self._settings().get("flipRetryMinutes") or 5.0)
                        flip_retry_at = time.monotonic() + minutes * 60.0

            for rig in active:
                self._set_rig(rig, frame=offset[rig.id] + position[rig.id] + 1,
                              filter=frames[rig.id]["filter"], state="exposing")
            self._set("imaging", f"{label}: slot {self._slot}/{slots} — " + ", ".join(
                f"{rig.name} {frames[rig.id]['filter']} "
                f"{frames[rig.id]['exposure']:g}s" for rig in active))

            # Anything the guider does during this exposure marks the frame.
            self._guide_lost_in_slot = False
            outcome = self._expose_slot(active, frames)
            if self._guiding_wanted and not self._guiding_healthy():
                self._guide_lost_in_slot = True

            # Any camera - the master's included - that is still failing after
            # the quick retries is recovered on its own, in the background. The
            # mount, the guider and the solver are not the master camera, and
            # one camera off the USB bus used to end the night for all three
            # telescopes (2026-10-12 21:05, 10-14 20:42, 10-20 00:16 and 00:35).
            for rig in active:
                self._set_rig(rig, state="")
                message = outcome["errors"].get(rig.id)
                if message is None:
                    continue
                self._say(f"{rig.name}: frame failed — {message}", "warn")
                self._bench(rig, message)

            for rig in active:
                record = outcome["results"].get(rig.id)
                if record is None:
                    continue
                position[rig.id] += 1
                self._rig_ok(rig)
                filter_name = frames[rig.id]["filter"]
                key = f"{entry['id']}:{rig.id}:{filter_name}"
                with self._lock:
                    self._done[key] = self._done.get(key, 0) + 1
                self._credit(entry["id"], panel_index, rig.id, filter_name)

                # Measured first, then credited: the night's report wants the
                # star size and the guiding this frame was taken under, and
                # those are not known until the frame has been measured.
                self._record_frame(rig, record, panel, entry, filter_name,
                                   guide_lost=self._guide_lost_in_slot)

            if any(position[rig.id] < len(queues[rig.id]) for rig in rigs):
                self._dither(entry)

        return True


class _Stopped(Exception):
    """The operator stopped the sequence."""


class _Skipped(Exception):
    """The operator skipped this target."""


class _Reframed(Exception):
    """The target's panels were laid out again under the run; start it over."""


class _Redealt(Exception):
    """The entry's deal changed under the run - other filters, other panels.

    A collaboration's list for the night is dealt by the server and can be
    dealt again after the run has started: the join dealt it blind to the
    Moon, the first poll with the Moon dealt it afresh, and the plan said
    OIII while the telescope went on shooting the Ha it had started with.
    The run reads the plan again and starts the entry over on what it says.
    """


def _cooler_on(camera: Any) -> bool:
    """Whether a camera's cooler is on; True when it cannot say. A camera
    already warm (a loop that put the night to bed, then Stop) has nothing
    to keep cold and nothing to warm."""
    try:
        value = camera.cooler_on
    except Exception:                               # noqa: BLE001
        return True
    return True if value is None else bool(value)


def _clock(timestamp: float) -> str:
    return time.strftime("%H:%M", time.localtime(timestamp))
