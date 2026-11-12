"""Telling a camera that has stopped answering from one that is merely busy,
and bringing it back by reconnecting it (0.2.52e).

2026-11-04, FSQ106: ZWO's ASCOM camera driver still said Connected after it
had lost the ASI6200, read its exposure limits as 0 to 0 s, and answered
StartExposure with a modal box - "Please input Exposure Duration between 0
and 0s, and restart exposure!" - that never returns until somebody clicks OK.
The sequence hung there and Abort did nothing.

The same shape as `focushealth`: a few cheap reads before relying on the
camera (`Camera.probe`); a camera that fails them is never sent
StartExposure. Only that telescope's camera is reconnected - never its
focuser, mount or wheel, and never another telescope's camera - up to three
times, spaced out (0, 10, 30 s), never while a frame is being read out, and
a Stop cuts it short. The settings it was running at (cooler and setpoint,
gain, offset, binning) go back on after a reconnect, and the camera has to
pass the same reads before it counts as back. A camera still gone after
that has lost its power or its USB at the scope, which no software can fix:
it is left alone for half an hour with one plain warning saying so.
"""
from __future__ import annotations

import contextlib
import threading
import time
from typing import Any, Callable

from .devices.base import CameraLostError
from .recovery import call_with_timeout

#: Capture states in which the camera is moving a frame: never reconnected then.
_READING_OUT = ("downloading", "saving")


class CameraHealth:
    """One telescope's camera: noticing it has gone, and reconnecting it.

    `reconnect(stop)` brings the camera back on its remembered driver and puts
    its gain, offset and cooler back (`RigSet.reconnect_camera`); it returns
    {"connected": [...], "failed": [...]}.
    """

    ATTEMPTS = 3
    SPACING_SECONDS = (0.0, 10.0, 30.0)
    COOL_OFF_SECONDS = 30 * 60
    #: The longest one probe, disconnect or reconnect step is waited on.
    STEP_SECONDS = 90.0
    #: How long a frame waits for a reconnect somebody else started.
    WAIT_FOR_OTHER_SECONDS = 150.0

    def __init__(self, name: str, manager: Any,
                 reconnect: Callable[[Callable[[], bool] | None], dict[str, Any]] | None,
                 capture: Any = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self.name = name
        self.manager = manager
        self.reconnect = reconnect
        self.capture = capture
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._given_up_until = 0.0
        self._said_cool_off = False
        self.state: dict[str, Any] = {"state": "ok"}

    # -- reporting ---------------------------------------------------------
    def _log(self, message: str, level: str = "info") -> None:
        with contextlib.suppress(Exception):
            self.manager.log(f"Camera: {message}", level)

    @property
    def gave_up(self) -> bool:
        return self._clock() < self._given_up_until

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def plain(self, reason: str = "") -> str:
        """The one warning: what is wrong and what to do about it."""
        return (f"Camera {self.name} is not responding, power-cycle or replug it "
                "at the scope" + (f" ({reason})" if reason else ""))

    # -- the diagnosis -----------------------------------------------------
    def lost(self) -> str | None:
        """Why the connected camera is not really there, or None."""
        camera = self.manager.get("camera")
        if camera is None or not getattr(camera, "connected", False):
            return None
        probe = getattr(camera, "probe", None)
        if probe is None:
            return None
        try:
            return call_with_timeout(probe, self.STEP_SECONDS, "the camera check")
        except Exception as exc:                        # noqa: BLE001 - that is the answer
            return f"it does not answer ({exc})"

    def _reading_out(self) -> bool:
        state = str(getattr(self.capture, "_state", "") or "")
        return state in _READING_OUT

    # -- the cure ----------------------------------------------------------
    def heal(self, reason: str, stop: Callable[[], bool] | None = None) -> bool:
        """Reconnect this telescope's camera - it alone - until it answers."""
        if not self._lock.acquire(blocking=False):
            self._log("already being reconnected", "warn")
            return False
        try:
            if self.gave_up:
                if not self._said_cool_off:
                    self._said_cool_off = True
                    self._log(self.plain(reason) + "; not reconnecting it again until "
                              + time.strftime("%H:%M", time.localtime(self._given_up_until)),
                              "error")
                return False
            if self._reading_out():
                self._log(f"not reconnecting while a frame is being read out ({reason})",
                          "warn")
                return False
            if self.reconnect is None:
                self._log(self.plain(reason) + "; there is no remembered driver to "
                          "reconnect it with", "error")
                return False
            camera = self.manager.get("camera")
            binning = None
            with contextlib.suppress(Exception):
                binning = int(camera.binning) if camera is not None else None

            last = reason
            for attempt in range(1, self.ATTEMPTS + 1):
                wait = self.SPACING_SECONDS[min(attempt - 1, len(self.SPACING_SECONDS) - 1)]
                if wait:
                    self._sleep(wait)
                if stop is not None and stop():
                    self._log("reconnect abandoned: stopped", "warn")
                    self.state = {"state": "ok"}
                    return False
                if self._reading_out():
                    self._log("reconnect abandoned: a frame is being read out", "warn")
                    return False
                self._log(f"{self.name} is not responding ({last}); reconnecting its "
                          f"camera only, attempt {attempt} of {self.ATTEMPTS} - nothing "
                          "else is touched", "warn")
                self.state = {"state": "reconnecting", "attempt": attempt,
                              "reason": last, "at": self._clock()}
                with contextlib.suppress(Exception):
                    call_with_timeout(lambda: self.manager.disconnect("camera"),
                                      30.0, "disconnecting the camera")
                try:
                    outcome = call_with_timeout(lambda: self.reconnect(stop),
                                                self.STEP_SECONDS * 2, "the camera reconnect")
                except Exception as exc:                # noqa: BLE001 - counted
                    last = f"reconnect failed: {exc}"
                    continue
                failed = list((outcome or {}).get("failed") or [])
                fresh = self.manager.get("camera")
                if fresh is None or not getattr(fresh, "connected", False):
                    last = "reconnect failed: " + ("; ".join(failed) or "it did not come back")
                    continue
                if binning is not None:
                    with contextlib.suppress(Exception):
                        if int(fresh.binning) != binning:
                            fresh.set_settings(binning=binning)
                bad = self.lost()
                if bad is None:
                    self._log(f"{self.name} reconnected and answering"
                              + (f" (settings: {'; '.join(failed)})" if failed else ""),
                              "success")
                    self.state = {"state": "reconnected", "at": self._clock(),
                                  "reason": reason, "attempts": attempt}
                    self._said_cool_off = False
                    return True
                last = f"still not answering after reconnecting: {bad}"

            self._given_up_until = self._clock() + self.COOL_OFF_SECONDS
            self._said_cool_off = False
            self.state = {"state": "dead", "at": self._clock(), "reason": last,
                          "retryAfter": self._given_up_until}
            self._log(self.plain() + f". Reconnected {self.ATTEMPTS} times and it still "
                      f"does not answer ({last})", "error")
            return False
        finally:
            self._lock.release()

    def ensure(self, what: str, stop: Callable[[], bool] | None = None) -> None:
        """Before relying on the camera: if it has gone, bring it back or
        raise CameraLostError with the plain warning. Nothing is sent to the
        driver of a camera that fails the check."""
        reason = self.lost()
        if reason is None:
            return
        if self._lock.locked():
            # Somebody else (the idle watch) is already reconnecting it.
            deadline = time.monotonic() + self.WAIT_FOR_OTHER_SECONDS
            while self._lock.locked() and time.monotonic() < deadline:
                if stop is not None and stop():
                    raise CameraLostError(self.plain(reason))
                time.sleep(0.2)
            reason = self.lost()
            if reason is None:
                return
        if not self.heal(f"{what}: {reason}", stop):
            raise CameraLostError(self.plain(reason))

    def check(self, what: str, stop: Callable[[], bool] | None = None) -> bool:
        """`ensure` that answers True/False instead of raising."""
        try:
            self.ensure(what, stop)
            return True
        except CameraLostError:
            return False


class CameraWatch:
    """While a telescope is idle, ask its camera now and then whether it is
    still there, and reconnect it if not - so a camera that drops off between
    runs is back (or reported) before the next one needs it."""

    def __init__(self, rigs: Callable[[], list[Any]],
                 idle: Callable[[Any], bool],
                 interval: Callable[[], float] = lambda: 60.0) -> None:
        self._rigs = rigs
        self._idle = idle
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="camera-watch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> list[str]:
        """One look at every telescope. What was found, for the checks."""
        found: list[str] = []
        for rig in list(self._rigs()):
            health = getattr(rig, "camera_health", None)
            if health is None or health.busy or health.gave_up:
                continue
            with contextlib.suppress(Exception):
                if not self._idle(rig):
                    continue
                reason = health.lost()
                if reason is None:
                    continue
                found.append(f"{rig.name}: {reason}")
                health.heal(f"while idle: {reason}", self._stop.is_set)
        return found

    def _loop(self) -> None:
        while not self._stop.is_set():
            every = 60.0
            with contextlib.suppress(Exception):
                every = max(10.0, float(self._interval()))
            if self._stop.wait(every):
                return
            with contextlib.suppress(Exception):
                self.tick()
