"""Telling a focuser that has stopped answering from one that is merely at 0,
and bringing it back by reconnecting it (0.2.52d).

2026-11-03, FSQ106 and vsd100: the ZWO EAF's ASCOM driver said Connected, read
position 0 and -273 C, and threw "Move Failure" on every move. Starfront warned
"the focuser is at the end of its travel" - it was not; nothing was answering -
and autofocus failed and backed off for 10, then 30 minutes, all night.

-273 C (absolute zero, near enough) is what ZWO's driver reports with no live
focuser behind it, and position 0 with it is the uninitialised value. A Move
Failure, or a COM "Exception occurred", is the driver saying the same thing.

What is done about it is the least that can help: only the focuser is
disconnected and reconnected - never the camera, mount or anything else on the
telescope - up to three times, spaced out, and never while it says it is
moving. A focuser still dead after that has lost its power or its USB at the
scope, which no software can fix, and it is left alone for half an hour with a
warning saying so, rather than reconnected every time autofocus asks.
"""
from __future__ import annotations

import contextlib
import threading
import time
from typing import Any, Callable, TypeVar

from .devices.base import DeviceError

T = TypeVar("T")

#: A temperature at or below this is not a reading: ZWO's EAF driver gives
#: -273 when there is no focuser on the other end.
PHANTOM_TEMPERATURE_C = -270.0

#: Words in a driver's error that mean "there is nothing there", as opposed
#: to "you asked for a position past the end" (ours) or "it took too long".
_DEAD_MARKERS = (
    "move failure",            # ASCOM.EAF
    "exception occurred",      # COM DISP_E_EXCEPTION, the wrapper of most of them
    "-2147352567",
    "not connected",
    "no device",
    "device not found",
    "device removed",
    "invalid id",
    "not responding",
    "disconnected",
    "connection refused",
    "timed out talking",
)


def bogus_reading(focuser: Any) -> str | None:
    """Why this focuser's own readings say nobody is answering, or None.

    Both have to hold: a real focuser can sit at 0, and one with a broken
    probe can read nonsense temperatures; at absolute zero *and* at an end
    of travel it is the driver's placeholder values, not a focuser.
    """
    if focuser is None:
        return None
    try:
        temperature = focuser.temperature
    except Exception:                               # noqa: BLE001 - a read that throws
        temperature = None
    if temperature is None or float(temperature) > PHANTOM_TEMPERATURE_C:
        return None
    try:
        position = int(focuser.position)
    except Exception:                               # noqa: BLE001
        return f"it reads {float(temperature):g} C and no position"
    limit = int(getattr(focuser, "max_step", 0) or 0)
    if position <= 0 or (limit and position >= limit):
        return (f"it reads position {position} and {float(temperature):g} C, the "
                "values its driver gives when no focuser is answering")
    return None


def is_dead_error(exc: BaseException) -> bool:
    """Whether a failed focuser call means the focuser is not there."""
    text = str(exc).lower()
    if "must be 0.." in text or "did not stop moving" in text:
        return False                                # ours: a range check, a slow move
    return any(marker in text for marker in _DEAD_MARKERS)


def _moving(focuser: Any) -> bool:
    """Whether it says it is moving. A read that throws is a focuser that is
    not moving anything."""
    try:
        return bool(focuser.moving)
    except Exception:                               # noqa: BLE001
        return False


class FocuserHealth:
    """One telescope's focuser: noticing it has gone, and reconnecting it.

    `spec` returns the remembered driver - {"backend", "driverId", "name",
    "options"} - the same record Connect everything uses, or None.
    """

    #: Reconnects tried, one after another, before giving up.
    ATTEMPTS = 3
    #: Wait before each attempt: the first at once, then longer, so a
    #: focuser whose hub is re-enumerating gets time to come back.
    SPACING_SECONDS = (0.0, 10.0, 30.0)
    #: After giving up, how long before trying again on its own.
    COOL_OFF_SECONDS = 30 * 60

    def __init__(self, name: str, manager: Any,
                 spec: Callable[[], dict[str, Any] | None],
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self.name = name
        self.manager = manager
        self._spec = spec
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._given_up_until = 0.0
        self._said_cool_off = False
        #: For the warnings board and the status panel.
        self.state: dict[str, Any] = {"state": "ok"}

    # -- reporting ---------------------------------------------------------
    def _log(self, message: str, level: str = "info") -> None:
        with contextlib.suppress(Exception):
            self.manager.log(f"Focuser: {message}", level)

    @property
    def gave_up(self) -> bool:
        return self._clock() < self._given_up_until

    # -- the cure ----------------------------------------------------------
    def heal(self, reason: str, stop: Callable[[], bool] | None = None) -> bool:
        """Reconnect the focuser - only the focuser - until it answers.

        True when it came back answering. False when it would not, when it
        is in its cool-off after failing to, when it says it is moving, or
        when another reconnect is already under way.
        """
        if not self._lock.acquire(blocking=False):
            self._log("already being reconnected", "warn")
            return False
        try:
            if self.gave_up:
                if not self._said_cool_off:
                    self._said_cool_off = True
                    self._log(f"not responding ({reason}); not reconnecting it again "
                              "until "
                              + time.strftime("%H:%M", time.localtime(self._given_up_until))
                              + " - power-cycle or replug it at the scope", "error")
                return False
            focuser = self.manager.get("focuser")
            if focuser is not None and _moving(focuser):
                self._log(f"not reconnecting while it is moving ({reason})", "warn")
                return False
            spec = None
            with contextlib.suppress(Exception):
                spec = self._spec()
            if not spec:
                self._log(f"not responding ({reason}), and there is no remembered "
                          "driver to reconnect it with", "error")
                return False

            last = reason
            for attempt in range(1, self.ATTEMPTS + 1):
                wait = self.SPACING_SECONDS[min(attempt - 1, len(self.SPACING_SECONDS) - 1)]
                if wait:
                    self._sleep(wait)
                if stop is not None and stop():
                    self._log("reconnect abandoned: stopped", "warn")
                    return False
                current = self.manager.get("focuser")
                if current is not None and _moving(current):
                    self._log("reconnect abandoned: it started moving", "warn")
                    return False
                self._log(f"not responding ({last}); reconnecting the focuser only, "
                          f"attempt {attempt} of {self.ATTEMPTS} - camera and mount "
                          "are not touched", "warn")
                self.state = {"state": "reconnecting", "attempt": attempt,
                              "reason": last, "at": self._clock()}
                with contextlib.suppress(Exception):
                    self.manager.disconnect("focuser")
                try:
                    self.manager.connect("focuser", spec["backend"], spec["driverId"],
                                         spec.get("name"), spec.get("options"))
                except Exception as exc:              # noqa: BLE001 - reported
                    last = f"reconnect failed: {exc}"
                    continue
                fresh = self.manager.get("focuser")
                bad = bogus_reading(fresh)
                if bad is None:
                    where = ""
                    with contextlib.suppress(Exception):
                        where = f" at {int(fresh.position)}"
                    self._log(f"reconnected and answering{where}", "success")
                    self.state = {"state": "reconnected", "at": self._clock(),
                                  "reason": reason, "attempts": attempt}
                    self._said_cool_off = False
                    return True
                last = f"still not answering after reconnecting: {bad}"

            self._given_up_until = self._clock() + self.COOL_OFF_SECONDS
            self._said_cool_off = False
            self.state = {"state": "dead", "at": self._clock(), "reason": last,
                          "retryAfter": self._given_up_until}
            self._log(f"still not responding after {self.ATTEMPTS} reconnects ({last}). "
                      "Power-cycle or replug the focuser (or its USB hub) at the "
                      "scope, then reconnect it", "error")
            return False
        finally:
            self._lock.release()

    def call(self, action: Callable[[Any], T], what: str,
             stop: Callable[[], bool] | None = None) -> T:
        """Do something with the focuser; if that shows it has gone, reconnect
        it and do it once more."""
        focuser = self.manager.require("focuser")
        try:
            return action(focuser)
        except Exception as exc:
            bogus = bogus_reading(focuser)
            if not (is_dead_error(exc) or bogus):
                raise
            if not self.heal(f"{what}: {exc}", stop):
                raise DeviceError(
                    f"the focuser is not responding ({exc}). Starfront tried "
                    "reconnecting it; power-cycle or replug it at the scope") from exc
        return action(self.manager.require("focuser"))

    def check(self, what: str, stop: Callable[[], bool] | None = None) -> bool:
        """Before relying on it: reconnect a focuser whose readings are the
        driver's placeholders. True when it is (now) answering."""
        bad = bogus_reading(self.manager.get("focuser"))
        if bad is None:
            return True
        return self.heal(f"{what}: {bad}", stop)
