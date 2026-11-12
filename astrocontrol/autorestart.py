"""Starting the night again after the weather shut it down (0.2.53a).

Bray, 2026-11-04: "shut down, but occasionally there is a roof status
glitch! and the scope should restart if conditions become good again".

`onUnsafe` stays "shutdown" - stop, park, close, warm - and this is what
comes after it.  A weather shutdown of a *running* sequence arms a restart.
From then on every safety poll ticks it, and once the sky has read safe
**continuously** for the safe window it starts the night again, in order:

  1. recool the cameras to their setpoint (with a timeout; a camera that
     does not get there is reported and the restart goes on, as a fresh
     start does);
  2. unpark the mount (a dome/roof shutter this program closed is never
     reopened by itself: the restart stays parked and says so);
  3. slew to the interrupted target and plate-solve onto it - a solve that
     fails parks again, warms, alerts and stays parked for the night;
  4. refocus if the autofocus triggers say so (temperature moved past
     autofocusTemperatureDelta, the interval, ...);
  5. resume the interrupted sequence from the progress journal: the same
     target, and only the frames not shot yet.

The safe window is `resumeAfterSeconds` (600 s) doubled after each restart
in the same night (600, 1200, 2400 s), and there are at most
`autoRestartMaxPerNight` (3) restarts a night.  No restart once
astronomical dawn (less dawnMarginMinutes) or the sequence's end time has
passed, or when what is left before then is shorter than one sub.  Any
user action on the telescope - Stop, Start, Shut down, a slew, a park, a
frame by hand - cancels it.  Every arm, restart, skip, cancel and failure is
logged and sent through the notifier's "safety" alerts.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

DEFAULTS = {
    "autoRestart": True,
    "autoRestartMaxPerNight": 3,
    "autoRestartCoolTimeoutMinutes": 20.0,
}

STEPS = ("recool", "unpark", "centre", "refocus", "resume")


class AutoRestart:
    """Thread-safe. `tick()` is cheap and never raises; the restart runs on a
    thread of its own (`spawn`), so a slow cooler never holds the watcher."""

    def __init__(self, settings: Callable[[], dict[str, Any]], host: Any,
                 notify: Callable[[str, str], Any] | None = None,
                 log: Callable[[str, str], Any] | None = None,
                 clock: Callable[[], float] = time.time,
                 spawn: Callable[[Callable[[], None]], Any] | None = None) -> None:
        self._settings = settings
        self.host = host
        self._notify = notify
        self._log = log
        self._clock = clock
        self._spawn = spawn or (lambda fn: threading.Thread(
            target=fn, daemon=True, name="auto-restart").start())
        self._lock = threading.RLock()
        self.state = "idle"     # idle armed restarting resumed skipped cancelled failed
        self.reason = ""
        self.step = ""
        self.snapshot: dict[str, Any] | None = None
        self.night: str | None = None
        self._count: dict[str, int] = {}
        self._safe_since: float | None = None
        self._token = 0
        self.events: list[dict[str, Any]] = []

    # -- settings ----------------------------------------------------------
    def _cfg(self) -> dict[str, Any]:
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in (self._settings() or {}).items() if v is not None})
        return merged

    def enabled(self) -> bool:
        s = self._cfg()
        return (bool(s.get("enabled", True)) and bool(s.get("autoRestart", True))
                and str(s.get("onUnsafe") or "shutdown").lower() == "shutdown")

    def cap(self) -> int:
        try:
            return max(0, int(self._cfg().get("autoRestartMaxPerNight")))
        except (TypeError, ValueError):
            return int(DEFAULTS["autoRestartMaxPerNight"])

    def restarts(self, night: str | None = None) -> int:
        return self._count.get(str(night if night is not None else self.night), 0)

    def window(self) -> float:
        """Continuous safe needed before the next restart: doubled per restart."""
        base = float(self._cfg().get("resumeAfterSeconds") or 600.0)
        return base * (2 ** self.restarts())

    # -- telling -----------------------------------------------------------
    def _emit(self, kind: str, subject: str, body: str = "", level: str = "warn") -> None:
        with self._lock:
            self.events.append({"t": self._clock(), "kind": kind,
                                "message": subject + (f" - {body}" if body else "")})
            del self.events[:-20]
        text = f"Auto-restart: {subject}" + (f" - {body}" if body else "")
        if self._log is not None:
            try:
                self._log(text, level)
            except Exception:                      # noqa: BLE001 - never fatal
                pass
        if self._notify is not None:
            try:
                self._notify(f"Auto-restart: {subject}", body or subject)
            except Exception:                      # noqa: BLE001
                pass

    # -- the edges ---------------------------------------------------------
    def arm(self, snapshot: dict[str, Any] | None) -> None:
        """A weather shutdown is about to happen. `snapshot` is the run it
        interrupts (None when nothing was running - then a restart already
        under way is the run, and its snapshot is kept)."""
        with self._lock:
            under_way = self.state in ("restarting", "armed")
            self._token += 1                       # whatever restart was going is over
            if snapshot is None and under_way:
                snapshot = self.snapshot
            if snapshot is None:
                return
            if not self.enabled():
                self.state, self.reason, self.snapshot = "idle", "", None
                return
            self.snapshot = dict(snapshot)
            self.night = str(snapshot.get("night") or "")
            self._safe_since = None
            self.step = ""
            done = self.restarts()
            if done >= self.cap():
                self.state = "skipped"
                self.reason = (f"{done} of {self.cap()} restarts used tonight; "
                               "staying shut down")
                skip = True
            else:
                self.state = "armed"
                self.reason = (f"restarts after {self.window():.0f} s of continuous safe "
                               f"(restart {done + 1} of {self.cap()} tonight)")
                skip = False
        if skip:
            self._emit("skipped", "not restarting tonight", self.reason, "error")
        else:
            self._emit("armed", "armed after the weather shutdown", self.reason)

    def cancel(self, why: str) -> bool:
        """A person did something: they are in charge now."""
        with self._lock:
            if self.state not in ("armed", "restarting"):
                return False
            was = self.state
            self._token += 1
            self.state, self.reason, self.step = "cancelled", why, ""
        if was == "restarting":
            try:
                self.host.restart_abort()
            except Exception:                      # noqa: BLE001
                pass
        self._emit("cancelled", "cancelled", why)
        return True

    def tick(self, safe: bool | None) -> None:
        """One safety poll. Starts the restart once it is due and allowed."""
        try:
            self._tick(safe)
        except Exception as exc:                   # noqa: BLE001 - never fatal
            if self._log is not None:
                self._log(f"Auto-restart: {exc}", "warn")

    def _tick(self, safe: bool | None) -> None:
        now = self._clock()
        with self._lock:
            if self.state != "armed":
                return
            if safe is not True:
                self._safe_since = None            # continuous means continuous
                return
            if self._safe_since is None:
                self._safe_since = now
                return
            window = self.window()
            if now - self._safe_since < window:
                return
            why = self._why_not(now)
            if why:
                self.state, self.reason = "skipped", why
            else:
                self._count[str(self.night)] = self.restarts() + 1
                self.state = "restarting"
                self.reason = f"restart {self.restarts()} of {self.cap()} tonight"
                self._token += 1
                token = self._token
                snapshot = dict(self.snapshot or {})
        if why:
            self._emit("skipped", "not restarting", why, "error")
            return
        self._emit("restarting", f"safe for {window:.0f} s; restarting",
                   self.reason, "success")
        self._spawn(lambda: self._restart(token, snapshot))

    def _why_not(self, now: float) -> str:
        if getattr(self.host, "running", False):
            return "a sequence is already running"
        if self.restarts() >= self.cap():
            return f"{self.restarts()} of {self.cap()} restarts used tonight"
        d = self.host.weather_deadlines(self.snapshot or {}) or {}
        if d.get("night") and self.night and d["night"] != self.night:
            return "the night it was shut down in is over"
        dawn, end = d.get("dawn"), d.get("end")
        sub = float(d.get("sub") or 0.0)
        if dawn and now >= dawn:
            return "astronomical dawn has passed"
        if end and now >= end:
            return "the sequence's end time has passed"
        limits = [(t, w) for t, w in ((dawn, "dawn"), (end, "the sequence's end"))
                  if t]
        if limits:
            limit, what = min(limits)
            if now + sub > limit:
                return (f"{limit - now:.0f} s left before {what}, less than one "
                        f"{sub:.0f} s sub")
        return ""

    # -- the restart -------------------------------------------------------
    def _live(self, token: int) -> bool:
        with self._lock:
            return token == self._token and self.state == "restarting"

    def _restart(self, token: int, snapshot: dict[str, Any]) -> None:
        host = self.host
        step = "begin"
        try:
            host.restart_begin()
            for step in STEPS:
                if not self._live(token):
                    return
                with self._lock:
                    self.step = step
                if step == "recool":
                    minutes = float(self._cfg().get("autoRestartCoolTimeoutMinutes") or 20.0)
                    late = host.restart_recool(minutes * 60.0) or []
                    if late and self._live(token):
                        self._emit("cool", "the cameras did not reach their setpoint",
                                   f"{', '.join(late)} after {minutes:g} min; carrying on, "
                                   "as a fresh start does")
                elif step == "unpark":
                    host.restart_unpark()
                elif step == "centre":
                    said = host.restart_centre(snapshot)
                    if said and self._log is not None:
                        self._log(f"Auto-restart: {said}", "info")
                elif step == "refocus":
                    why = host.restart_focus()
                    if why and self._log is not None:
                        self._log(f"Auto-restart: refocused ({why})", "info")
                elif step == "resume":
                    host.restart_resume(snapshot)
            with self._lock:
                if token != self._token:
                    return
                self.state, self.step = "resumed", ""
            self._emit("resumed", "the sequence is running again",
                       f"{self.reason}; picked up where it stopped", "success")
        except Exception as exc:                   # noqa: BLE001 - reported below
            if not self._live(token):
                return                             # cancelled, or the weather turned again
            with self._lock:
                self.state = "failed"
                self.reason = f"{step}: {exc}"
                self.step = ""
            try:
                host.restart_park()
                parked = "parked again and staying parked"
            except Exception as park_exc:          # noqa: BLE001
                parked = f"and could not park again ({park_exc}) - check the mount"
            self._emit("failed", f"could not {step}",
                       f"{exc}; {parked}. No more restarts tonight.", "error")

    # -- reporting ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            waited = None if self._safe_since is None else now - self._safe_since
            window = self.window() if self.night is not None else None
            return {
                "enabled": self.enabled(),
                "state": self.state,
                "reason": self.reason,
                "step": self.step,
                "restarts": self.restarts(),
                "max": self.cap(),
                "windowSeconds": window,
                "safeForSeconds": None if waited is None else round(waited, 1),
                "restartIn": (None if waited is None or window is None
                              or self.state != "armed"
                              else round(max(0.0, window - waited), 1)),
                "events": list(self.events[-5:]),
            }
