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

        self._running = True
        # A focus frame is a measurement, not data. Left saved it lands in the
        # target's folder named exactly like a real sub — same prefix, same
        # filter, just a different exposure — and a nine-point sweep every hour
        # puts a hundred of them a night among the frames that matter. They
        # still reach the viewer; they just never reach the disk. The same
        # reasoning, and the same mechanism, as a centring frame.
        was_saving = self.capture.save_enabled
        self.capture.save_enabled = False
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
                with self._lock:
                    self._last = run
                if attempt < attempts:
                    say(f"Autofocus attempt {attempt} was no good ({last_error}); "
                        f"going back to {start_position} and trying again")
                    self._move(focuser, start_position, overshoot, from_below=True)

            self._move(focuser, start_position, overshoot, from_below=True)
            raise DeviceError(f"autofocus did not find a minimum: {last_error}")
        finally:
            self.capture.save_enabled = was_saving
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

        # Out by `offset x step`, then walk back inwards.
        for index in range(offset_steps, -1, -1):
            sample(start_position + index * step)

        # Keep going until both arms of the V have enough points to fit.
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

            if left >= offset_steps and right >= offset_steps:
                break
            if taken >= maximum_samples:
                run.detail = (f"gave up after {taken} points without bracketing "
                              "a minimum")
                return False
            if starless >= STARLESS_LIMIT:
                run.detail = (f"{starless} points in a row had no measurable "
                              "stars — too light, too cloudy, or the cover is on")
                return False

            if left < offset_steps:
                target = min(positions) - step
                say(f"More points needed below {lowest}")
            else:
                target = max(positions) + step
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
                run.detail = ("the focuser reached the end of its travel before "
                              "the curve turned round")
                return False
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
            self._settle(focuser, position + max(1, overshoot))
        self._settle(focuser, position)

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
