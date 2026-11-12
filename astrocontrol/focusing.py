"""Autofocus: find the focuser position where stars are smallest.

The routine follows NINA's, because the hard part of autofocus is not fitting a
curve — it is what you do when the curve you got is not the curve you wanted.

  1. Measure the stars where the focuser already is, and remember it.  That is
     the number the run will be judged against at the end.
  2. Move out by `offset x step` and walk back inwards, measuring as it goes, so
     every point is approached from the same direction and backlash is taken up
     the same way each time.
  3. **Keep going until the minimum is properly bracketed.**  This is the step
     that matters.  A sweep centred on the current position only finds focus if
     focus happened to be near the middle of it; when it is not, the whole sweep
     sits on one rising arm of the V.  Rather than give up, extend: whichever
     side of the lowest point has too few measurements gets another one, one
     step at a time, until both arms have enough to fit a line to.  A sweep that
     started 200 steps the wrong side of focus simply walks until it finds it.
  4. Fit the curve — trend lines, parabola, hyperbola — and take the position
     the chosen method asks for.
  5. **Check the answer.**  Move there, measure again, and only accept it if the
     stars really are no worse than they were at the start.  If they are worse,
     put the focuser back and try the whole thing again.

Stopping is always allowed, and always puts the focuser back where it started
rather than leaving it somewhere deliberately out of focus.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import focusfit
from .devices.base import DeviceError
from .imaging import stars

#: How many points in a row may have nothing measurable in them before the run
#: is called off. A real sweep goes out of focus, not out of stars: even badly
#: defocused, a field has something. A run of blanks means the sky has gone —
#: dawn, cloud, or a cover still on — and no number of further points will fix
#: it, because every one of them is discarded before the curve is fitted.
STARLESS_LIMIT = 4


@dataclass
class FocusPoint:
    position: int
    hfd: float | None
    stars: int


@dataclass
class FocusRun:
    started: float
    points: list[FocusPoint] = field(default_factory=list)
    best_position: int | None = None
    best_hfd: float | None = None
    start_position: int | None = None
    start_hfd: float | None = None
    temperature: float | None = None
    filter_name: str | None = None
    detail: str = ""
    attempt: int = 1
    attempts: int = 1
    fits: dict[str, Any] = field(default_factory=dict)
    method: str = ""
    #: False when another attempt cannot help: focus is past the end of the
    #: travel, and a second sweep from the same place walks into the same end.
    retry: bool = True

    def payload(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "points": [{"position": p.position, "hfd": p.hfd, "stars": p.stars}
                       for p in self.points],
            "bestPosition": self.best_position,
            "bestHfd": self.best_hfd,
            "startPosition": self.start_position,
            "startHfd": self.start_hfd,
            "temperature": self.temperature,
            "filter": self.filter_name,
            "detail": self.detail,
            "attempt": self.attempt,
            "attempts": self.attempts,
            "fits": self.fits,
            "method": self.method,
        }


class _Stopped(Exception):
    """The operator aborted the run."""


class AutoFocuser:
    """Runs a focus sweep with the connected focuser and camera."""

    def __init__(self, manager, capture, config) -> None:
        self.manager = manager
        self.capture = capture
        self.config = config
        self._lock = threading.RLock()
        self._last: FocusRun | None = None
        # The sweep in progress, published point by point so the curve can be
        # watched as it is measured rather than only after it finishes.
        self._current: FocusRun | None = None
        self._message = ""
        self._last_finished: float | None = None
        self._last_temperature: float | None = None
        self._running = False
        self._abort = threading.Event()
        # Where a finished run's timing is recorded, so the planner can cost a
        # sweep it has never seen. Set by the rig that owns this focuser.
        self.overheads: Any = None
        # The rig's FocuserHealth (0.2.52d): a focuser that has stopped
        # answering is reconnected and the sweep tried once more.
        self.health: Any = None

    # -- state -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    def abort(self) -> None:
        """Stop the sweep at the next point it checks.

        The focuser is put back where it started rather than left wherever the
        sweep had got to, which would be somewhere deliberately out of focus.
        """
        self._abort.set()
        with self._lock:
            self._message = "aborting"

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "message": self._message,
                "current": self._current.payload() if self._current else None,
                "last": self._last.payload() if self._last else None,
                "lastFinished": self._last_finished,
                "lastTemperature": self._last_temperature,
            }

    def settings(self) -> dict[str, Any]:
        return self.config.section("sequencer")

    # -- when a refocus is due --------------------------------------------
    def due(self, filter_changed: bool = False) -> str:
        """Why a focus run is needed now, or "" if it is not."""
        settings = self.settings()
        if self._last_finished is None:
            return "no focus run yet tonight" if settings.get("autofocusOnStart", True) else ""
        if filter_changed and settings.get("autofocusOnFilterChange", True):
            return "filter changed"

        interval = float(settings.get("autofocusIntervalMinutes") or 0)
        if interval > 0 and (time.time() - self._last_finished) > interval * 60:
            return f"{interval:g} minutes since the last focus run"

        delta = float(settings.get("autofocusTemperatureDelta") or 0)
        focuser = self.manager.get("focuser")
        if delta > 0 and focuser is not None and focuser.connected:
            now = focuser.temperature
            if (now is not None and self._last_temperature is not None
                    and abs(now - self._last_temperature) >= delta):
                return f"temperature moved {abs(now - self._last_temperature):.1f} C"
        return ""

    # -- the run -----------------------------------------------------------
    def run(self, should_abort: Callable[[], bool] | None = None,
            report: Callable[[str], None] | None = None) -> FocusRun:
        """A focus sweep - reconnecting a focuser that is not answering first.

        Before it starts, a focuser reading the driver's placeholders (0 steps
        at -273 C) is reconnected. A sweep that fails because a move failed
        the same way is tried once more after a reconnect. Only the focuser
        is reconnected, never while it is moving; see `focushealth`.
        """
        from .focushealth import bogus_reading, is_dead_error
        health = self.health
        say = report or (lambda message: None)
        if health is not None and self.manager.get("focuser") is not None:
            if bogus_reading(self.manager.get("focuser")) is not None:
                say("The focuser is not answering; reconnecting it")
                health.check("before autofocus", should_abort)
        try:
            return self._run_once(should_abort, report)
        except Exception as exc:
            focuser = self.manager.get("focuser")
            if health is None or "aborted" in str(exc) \
                    or not (is_dead_error(exc) or bogus_reading(focuser)):
                raise
            say(f"The focuser stopped answering ({exc}); reconnecting it")
            if not health.heal(f"autofocus: {exc}", should_abort):
                raise DeviceError(
                    f"the focuser is not responding ({exc}). Reconnecting it did "
                    "not help - power-cycle or replug it at the scope") from exc
            say("Focuser reconnected; running the focus sweep once more")
            return self._run_once(should_abort, report)

    def _run_once(self, should_abort: Callable[[], bool] | None = None,
                  report: Callable[[str], None] | None = None) -> FocusRun:
        focuser = self.manager.require("focuser")
        self.manager.require("camera")
        if not focuser.is_absolute:
            raise DeviceError("autofocus needs a focuser with absolute positioning")

        settings = self.settings()
        step = max(1, int(settings.get("focusStepSize") or 100))
        count = int(settings.get("focusPoints") or 9)
        if count < 5:
            raise DeviceError("a focus sweep needs at least 5 points")
        offset_steps = max(2, count // 2)
        # How far past a position to go before coming back down onto it. The
        # overshoot has to be *larger* than the focuser's real backlash: if it
        # is not, the return move is entirely swallowed taking up slack and the
        # optics never reach the commanded place at all. Overshooting further
        # than necessary is harmless, so the default is generous, and a run that
        # detects the shortfall triples it and tries again.
        overshoot = max(int(settings.get("focusBacklash") or 0), 5 * step)
        attempts = max(1, int(settings.get("focusAttempts") or 2))
        method = str(settings.get("focusMethod") or "trendhyperbolic")
        # Kept for the timing below: what the sweep was asked to cost in shutter
        # time, so the rest of the run can be attributed to moving and measuring.
        points = count
        exposure = float(settings.get("focusExposure") or 6.0)
        frames = max(1, int(settings.get("focusFramesPerPoint") or 1))

        self._abort.clear()
        self._stopping = (lambda: self._abort.is_set()
                          or bool(should_abort and should_abort()))
        say = report or (lambda message: None)
        start_position = focuser.position
        # Every measured point of every attempt, for choosing where to leave
        # the focuser if none of them succeeds.
        self._tried: list[FocusPoint] = []
        self._start_hfds: list[float] = []

        self._running = True
        # A focus frame is a measurement, not data. Left saved it lands in the
        # target's folder named exactly like a real sub — same prefix, same
        # filter, just a different exposure — and a nine-point sweep every hour
        # puts a hundred of them a night among the frames that matter. They
        # still reach the viewer; they just never reach the disk. The same
        # reasoning, and the same mechanism, as a centring frame.
        # Counted, not remembered: see CaptureService.suspend_saving.
        self.capture.suspend_saving()
        try:
            last_error = ""
            for attempt in range(1, attempts + 1):
                run = FocusRun(started=time.time(), start_position=start_position,
                               temperature=focuser.temperature, attempt=attempt,
                               attempts=attempts, method=method)
                self._name_the_filter(run)
                with self._lock:
                    self._current = run
                    self._message = (f"attempt {attempt} of {attempts}"
                                     if attempts > 1 else "starting")
                # Each retry assumes the last one was defeated by a stiffer
                # focuser than the overshoot allowed for.
                this_overshoot = overshoot * (3 ** (attempt - 1))
                try:
                    result = self._attempt(focuser, run, say, start_position, step,
                                           offset_steps, this_overshoot, method)
                except _Stopped:
                    run.detail = "aborted"
                    say(f"Focus run stopped; returning to {start_position}")
                    self._settle(focuser, start_position)
                    with self._lock:
                        self._last = run
                        self._current = None
                    raise DeviceError("focus run aborted")

                if result:
                    with self._lock:
                        self._last = run
                        self._last_finished = time.time()
                        self._last_temperature = focuser.temperature
                        self._message = f"focused at {run.best_position}"
                        self._current = None
                    # Time one run and you can predict every run: the exposures
                    # are known, so what is left is the cost of moving and
                    # measuring, which does not depend on the filter or the
                    # sub length. Only a run that succeeded is recorded — an
                    # abandoned sweep is not what a future one will cost.
                    if self.overheads is not None and attempt == 1:
                        self.overheads.record_focus_run(
                            time.time() - run.started, len(run.points) or points,
                            exposure, frames)
                    return run

                last_error = run.detail
                self._tried.extend(p for p in run.points if p.hfd)
                with self._lock:
                    self._last = run
                if not run.retry:
                    say(f"Autofocus stopped: {last_error}. Not retrying - another "
                        "sweep would end at the same place")
                    break
                if attempt < attempts:
                    say(f"Autofocus attempt {attempt} was no good ({last_error}); "
                        f"going back to {start_position} and trying again")
                    self._move(focuser, start_position, overshoot, from_below=True)

            # Every attempt failed. Going back to the start is right when the
            # start was the best place seen - but when the sweep measured
            # somewhere clearly sharper (EAGLE: HFD 3.3 at 307 against 9.2 at
            # the 1807 it started from), going back guarantees a night of soft
            # subs. Leave it at the sharpest point actually measured instead.
            parked = self._best_measured(start_position)
            self._move(focuser, parked, overshoot, from_below=True)
            if parked != start_position:
                say(f"Autofocus failed; left the focuser at {parked}, the "
                    f"sharpest point the sweep measured, not back at {start_position}")
                last_error += (f"; left at {parked}, the sharpest point measured, "
                               f"instead of {start_position}")
            raise DeviceError(f"autofocus did not find a minimum: {last_error}")
        finally:
            self.capture.resume_saving()
            self._running = False
            self._abort.clear()
            with self._lock:
                self._current = None

    # -- one attempt -------------------------------------------------------
    def _attempt(self, focuser, run: FocusRun, say, start_position: int, step: int,
                 offset_steps: int, overshoot: int, method: str) -> bool:
        settings = self.settings()
        exposure = float(settings.get("focusExposure") or 6.0)
        per_point = max(1, int(settings.get("focusFramesPerPoint") or 1))
        # How far a run may wander before it is called a failure, so a sweep in
        # the wrong direction cannot go on all night.
        #
        # Counted in *samples taken*, not in distinct positions measured. The
        # distinction is the whole bug: `measured` is keyed by position, so a
        # sweep that keeps re-sampling the same place does not grow it, and the
        # ceiling was never reached. One run took 915 frames over four and a
        # half hours that way and stopped only because somebody pressed Abort.
        maximum_samples = max(20, offset_steps * 10)
        taken = 0

        measured: dict[int, FocusPoint] = {}
        starless = 0

        def sample(position: int) -> FocusPoint | None:
            nonlocal taken, starless
            self._check()
            position = int(position)
            if focuser.max_step and not 0 <= position <= focuser.max_step:
                return None
            if position < 0:
                return None
            taken += 1
            self._move(focuser, position, overshoot, from_below=True)
            readings: list[float] = []
            total_stars = 0
            for _ in range(per_point):
                self._check()
                record = self.capture.capture_blocking(exposure, frame_type="light")
                result = stars.measure(self.capture.frame(record.id))
                if result["hfd"] is not None:
                    readings.append(float(result["hfd"]))
                    total_stars = max(total_stars, int(result["stars"]))
            hfd = (sum(readings) / len(readings)) if readings else None
            # A run of points with nothing measurable in them is what a sweep
            # into daylight, cloud or a closed cover looks like. Pressing on
            # costs an exposure a time and cannot converge, because every new
            # point is thrown away before the curve is fitted.
            starless = 0 if hfd else starless + 1
            point = FocusPoint(position, round(hfd, 3) if hfd else None, total_stars)
            measured[position] = point
            with self._lock:
                run.points = [measured[p] for p in sorted(measured)]
                self._message = f"{len(measured)} points measured"
            say(f"focus {position}: "
                + (f"HFD {hfd:.2f} from {total_stars} stars" if hfd
                   else "no stars measured"))
            return point

        # The stars where we are now: what the result is judged against.
        starting = sample(start_position)
        run.start_hfd = starting.hfd if starting else None
        if run.start_hfd:
            self._start_hfds.append(run.start_hfd)

        # Out by `offset x step`, then walk back inwards.
        for index in range(offset_steps, -1, -1):
            sample(start_position + index * step)

        # Keep going until both arms of the V have enough points to fit.
        #
        # A side the focuser physically cannot extend any further is "pinned":
        # it is measured right at the end of the travel, and then one point
        # beyond the lowest is enough for that side, because the curve has
        # visibly turned round between the end and the bottom. Insisting on
        # `offset_steps` points there made focus impossible anywhere within
        # `offset_steps x step` of either end - on EAGLE (375-step sweep, focus
        # near 300 of 94580) every run walked down to 307, found the next point
        # would be -68, and gave up with the bottom of the V already measured.
        pinned = {"low": False, "high": False}
        fillers = 0
        while True:
            self._check()
            usable = [(p.position, p.hfd) for p in run.points if p.hfd]
            if len(usable) < 3:
                run.detail = "too few positions had measurable stars"
                return False

            positions = [p for p, _ in usable]
            values = [v for _, v in usable]
            lowest = positions[values.index(min(values))]
            left = sum(1 for p in positions if p < lowest)
            right = sum(1 for p in positions if p > lowest)
            need_left = 1 if pinned["low"] else offset_steps
            need_right = 1 if pinned["high"] else offset_steps

            if left >= need_left and right >= need_right:
                # A pinned side with a single point is a thin arm to fit; put
                # one more between the end and the bottom when there is room.
                filler = self._filler(pinned, positions, lowest, left, right,
                                      focuser, measured)
                if filler is None or fillers >= 2 or taken >= maximum_samples:
                    break
                fillers += 1
                with self._lock:
                    self._message = f"filling in at {filler}"
                say(f"Adding a point at {filler}, between the end of travel and {lowest}")
                sample(filler)
                continue
            if taken >= maximum_samples:
                run.detail = (f"gave up after {taken} points without bracketing "
                              "a minimum")
                return False
            if starless >= STARLESS_LIMIT:
                run.detail = (f"{starless} points in a row had no measurable "
                              "stars — too light, too cloudy, or the cover is on")
                return False

            if left < need_left:
                side, target = "low", min(positions) - step
                say(f"More points needed below {lowest}")
            else:
                side, target = "high", max(positions) + step
                say(f"More points needed above {lowest}")

            # The target is worked out from the points that *measured*, so a
            # point with no stars in it leaves the edge where it was and the
            # same place is chosen again. That is not a sweep that needs more
            # time, it is one that cannot move, and it will ask for the same
            # frame until somebody notices in the morning.
            if target in measured:
                run.detail = (f"the sweep stopped making progress at {target}: "
                              "the points being added have no measurable stars")
                return False

            if target < 0 or (focuser.max_step and target > focuser.max_step):
                edge = 0 if side == "low" else int(focuser.max_step)
                at_edge = measured.get(edge)
                if at_edge is None:
                    # One step would leave the travel, but the end itself has
                    # not been looked at yet: measure there before deciding.
                    with self._lock:
                        self._message = f"measuring at the end of travel ({edge})"
                    say(f"The next point would be past the end of travel; "
                        f"measuring at {edge}")
                    if sample(edge) is None:
                        run.detail = "the focuser reached the end of its travel"
                        return False
                    continue
                if at_edge.hfd is None or lowest == edge:
                    best = min(usable, key=lambda item: item[1])
                    run.detail = (
                        f"best focus is past the {'low' if side == 'low' else 'high'} "
                        f"end of the focuser's travel: stars were still getting "
                        f"smaller at {edge} (HFD {best[1]:.2f} at {best[0]}). Move "
                        "the camera or drawtube, or re-zero the focuser, so focus "
                        "sits nearer the middle of its range")
                    run.retry = False
                    return False
                # The end was measured and is worse than the bottom: the curve
                # has turned round inside the travel. That side is done.
                pinned[side] = True
                say(f"Focus is close to the {side} end of travel; fitting with "
                    f"what fits between it and {lowest}")
                continue
            with self._lock:
                self._message = f"extending the sweep to {target}"
            if sample(target) is None:
                run.detail = "the focuser reached the end of its travel"
                return False

        # Fit it.
        solution = focusfit.solve([(p.position, p.hfd) for p in run.points if p.hfd],
                                  method)
        run.fits = solution["fits"]
        if solution["position"] is None:
            run.detail = solution["detail"]
            return False

        target = int(round(solution["position"]))
        with self._lock:
            self._message = f"moving to {target}"
        say(f"Curve fitted; focus at {target}")
        self._move(focuser, target, overshoot, from_below=True)

        # And prove it.
        confirm = sample(target)
        if confirm is None or confirm.hfd is None:
            run.detail = "no stars at the fitted position"
            return False

        # The sweep measured near here on its way through. If the confirming
        # frame disagrees badly with what the sweep saw at the same place, the
        # focuser is not going where it is told - almost always backlash that
        # the overshoot did not fully take up. Say so, because the number in the
        # log otherwise looks like a bad fit rather than a mechanical problem.
        neighbours = [p for p in run.points
                      if p.hfd and abs(p.position - target) <= step and
                      p.position != target]
        if neighbours:
            nearest = min(neighbours, key=lambda p: abs(p.position - target))
            if nearest.hfd and confirm.hfd > nearest.hfd * 1.6:
                run.detail = (
                    f"the sweep measured HFD {nearest.hfd:.2f} at {nearest.position} "
                    f"but {confirm.hfd:.2f} coming back to {target}: the focuser is "
                    f"not going where it is told. Backlash is more than the "
                    f"{overshoot} steps allowed for")
                say(f"Backlash: {run.detail}. Retrying with a bigger overshoot; "
                    f"set the focuser's backlash in Equipment to stop this "
                    f"costing a run.")
                return False

        limit = float(settings.get("focusMaxHfrRatio") or 1.15)
        if run.start_hfd and confirm.hfd > run.start_hfd * limit:
            run.detail = (f"stars at {target} are HFD {confirm.hfd:.2f}, worse than "
                          f"the {run.start_hfd:.2f} we started with")
            return False

        run.best_position = target
        run.best_hfd = confirm.hfd
        say(f"Focused at {target}, HFD {confirm.hfd:.2f}"
            + (f" (was {run.start_hfd:.2f})" if run.start_hfd else ""))
        return True

    # -- helpers -----------------------------------------------------------
    def _best_measured(self, start_position: int) -> int:
        """Where to leave the focuser after a run that found no minimum.

        The sharpest point measured, but only when it beats the starting stars
        by the same margin a successful run is held to; otherwise the start.
        """
        points = [p for p in getattr(self, "_tried", []) if p.hfd and p.stars]
        starts = getattr(self, "_start_hfds", [])
        if not points or not starts:
            return start_position
        best = min(points, key=lambda p: p.hfd)
        limit = float(self.settings().get("focusMaxHfrRatio") or 1.15)
        if best.hfd * limit < min(starts):
            return int(best.position)
        return start_position

    def _check(self) -> None:
        if self._stopping():
            raise _Stopped()

    def _name_the_filter(self, run: FocusRun) -> None:
        wheel = self.manager.get("filterwheel")
        if wheel is not None and wheel.connected:
            names, index = wheel.names, wheel.position
            run.filter_name = names[index] if 0 <= index < len(names) else None

    def _move(self, focuser, position: int, overshoot: int,
              from_below: bool = False) -> None:
        """Arrive at `position` always travelling the same way — downwards.

        This is the difference between a focus run that works and one that is
        merely close.  A focuser has backlash: the gears take up slack when the
        direction reverses, so the same commanded position reached from above
        and from below is not the same place optically.  A sweep walks steadily
        in one direction and is therefore consistent with itself, but the move
        to the fitted position at the end usually reverses — and the confirming
        frame is then taken somewhere the sweep never measured.

        So every arrival is made from above: if the focuser is already higher
        than the target it simply moves down, and if it is not, it steps past
        the target by `overshoot` first and comes back.  `from_below` is kept
        for callers that want the old spelling; the behaviour is the same.
        """
        position = int(position)
        if focuser.position <= position:
            above = position + max(1, overshoot)
            # Never ask for a place past the end of the travel: the driver
            # refuses it outright, so a point near the top would fail rather
            # than merely be approached with a little less overshoot.
            if focuser.max_step:
                above = min(above, int(focuser.max_step))
            if above > position:
                self._settle(focuser, above)
        self._settle(focuser, position)

    @staticmethod
    def _filler(pinned: dict[str, bool], positions: list[int], lowest: int,
                left: int, right: int, focuser, measured: dict) -> int | None:
        """A point between a pinned end of travel and the bottom of the curve.

        Only for a side that is pinned and has a single point on it, and only
        when there is room for a point that has not been measured already.
        """
        if pinned["low"] and left < 2:
            nearest = max(p for p in positions if p < lowest)
            middle = (nearest + lowest) // 2
            if nearest < middle < lowest and middle not in measured:
                return middle
        if pinned["high"] and right < 2 and focuser.max_step:
            nearest = min(p for p in positions if p > lowest)
            middle = (nearest + lowest + 1) // 2
            if lowest < middle < nearest and middle not in measured:
                return middle
        return None

    @staticmethod
    def _settle(focuser, position: int, timeout: float = 180.0) -> None:
        focuser.move_to(int(position))
        deadline = time.monotonic() + timeout
        time.sleep(0.4)
        while focuser.moving:
            if time.monotonic() > deadline:
                raise DeviceError("the focuser did not stop moving in time")
            time.sleep(0.2)
        time.sleep(0.3)
