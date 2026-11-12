"""Watching the weather, and stopping the night when it turns.

This is the only part of the program whose job is the equipment rather than the
data.  Everything else can fail and cost a night; this failing costs a mirror,
or a mount, or a roof.

It runs on its own thread from the moment the program starts — not from the
moment a sequence starts — because the dangerous state is a roof open over a
telescope, and that can be true with nothing running.

**Unsafe is the answer whenever the true answer is not known.**  A monitor that
has been unplugged, whose driver has thrown, or which has simply stopped
answering is not evidence of good weather.  The setting that relaxes this exists
because some monitors really are flaky, but it is off by default and turning it
on is a decision to trust the sky over the sensor.

**Both edges are deliberately slow, and by different amounts.**  A cloud sensor
that flickers unsafe for one poll is not weather, so there is a grace period
before anything happens.  Coming back is far slower still: starting up into a
gap in the cloud is how a rig ends up opening and closing its roof all night,
and the cost of waiting another ten minutes is ten minutes.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from . import netsafety

#: How often to ask. Cheap — one boolean off a driver — and the thing it guards
#: against arrives in minutes rather than hours.
POLL_SECONDS = 5.0

#: The network source's glitch handling (0.2.53a, Bray: "occasionally there is
#: a roof status glitch!"). An *unconfirmed* unsafe - an error, timeout, TLS or
#: HTTP failure, stale data, clock skew, or issafe=false the roof report does
#: not back up - must last this long, unbroken, before the shutdown. 75 s: the
#: source re-asks every 5 s while unsafe, so that is ~15 bad samples in a row,
#: and it spans two full 30 s polls plus 15 s, so a bad answer on one poll (or
#: on one poll and its retries) is always outlived by the next good one.
DEBOUNCE_SECONDS = 75.0
#: A *confirmed* unsafe (issafe=false that roof/state agrees with, or an
#: explicit closing/closed roof state) acts on the second such read in a row:
#: 5 s after the first, at the retry pace. Never later than this, and never
#: added to the grace or the debounce.
CONFIRM_READS = 2


class SafetyWatcher:
    """Polls the safety monitor and acts once it has made up its mind."""

    def __init__(self, rigs: Any, config: Any, on_unsafe: Any = None,
                 on_safe: Any = None, log: Any = None) -> None:
        self.rigs = rigs
        self.config = config
        self._on_unsafe = on_unsafe
        self._on_safe = on_safe
        self._log = log

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

        # None until the monitor has been asked once: "not known yet" is not the
        # same as "unsafe", and acting on it at start-up would shut down every
        # launch before the driver had answered.
        self._safe: bool | None = None
        self._raw: bool | None = None
        self._since = 0.0
        self._acted_on: bool | None = None
        self._error: str | None = None
        self._unsafe_at: float | None = None
        self._events: list[dict[str, Any]] = []
        self._mono = time.monotonic
        #: Set by the program (0.2.53a): ticked on every poll so a weather
        #: shutdown can be followed by a restart once it has been safe a while.
        self.auto_restart: Any = None
        self._fast = False
        # A network monitor configured in these settings (0.2.53a). Unlike an
        # Equipment slot it has no "not connected" state that reads safe.
        self._net = netsafety.AlpacaSafetySource(self.settings, log=log)

    # -- settings ----------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        return self.config.section("safety")

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="safety")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- the monitor -------------------------------------------------------
    def _monitor(self) -> Any | None:
        try:
            device = self.rigs.master.manager.get("safetymonitor")
        except Exception:                         # noqa: BLE001 - no master yet
            return None
        return device if device is not None and device.connected else None

    def _network(self) -> netsafety.AlpacaSafetySource | None:
        """The configured network source, or None when there is none."""
        return self._net if netsafety.configured(self.settings()) else None

    def read(self) -> bool | None:
        """What the monitor says now, or None when there is nothing to ask.

        Separated from the acting so the answer can be had without waiting for
        the grace period — `blockStart` needs it immediately.

        With a network source configured (0.2.53a) the answer is never None:
        it is True only when that source has a fresh, consistent "safe" (and
        an Equipment-slot monitor, if one is also connected, agrees). Every
        failure of it is unsafe whatever `unreachableIsUnsafe` says.
        """
        settings = self.settings()
        if not settings.get("enabled", True):
            return None
        network = self._network()
        if network is not None:
            safe, why = network.read()
            if not safe:
                self._error = why
                return False
            slot = self._monitor()
            if slot is not None:
                try:
                    if not bool(slot.is_safe):
                        self._error = f"{slot.name} says unsafe"
                        return False
                except Exception as exc:          # noqa: BLE001 - a fault is unsafe
                    self._error = str(exc)
                    return False
            self._error = None
            return True
        monitor = self._monitor()
        if monitor is None:
            return None
        try:
            answer = bool(monitor.is_safe)
            self._error = None
            return answer
        except Exception as exc:                  # noqa: BLE001 - a fault is unsafe
            self._error = str(exc)
            return False if settings.get("unreachableIsUnsafe", True) else None

    @property
    def safe(self) -> bool:
        """Whether it is safe to be pointing at the sky.

        True when there is no monitor: an observatory without one is not an
        observatory that is always unsafe, it is one that is not being watched.
        """
        with self._lock:
            return self._safe is not False

    @property
    def watching(self) -> bool:
        return ((self._monitor() is not None or self._network() is not None)
                and bool(self.settings().get("enabled", True)))

    # -- the loop ----------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll()
            except Exception as exc:              # noqa: BLE001 - never fatal
                self._error = str(exc)
            self.tick_restart()
            self._stop.wait(POLL_SECONDS)

    def _poll(self) -> None:
        settings = self.settings()
        answer = self.read()

        with self._lock:
            self._raw = answer
            if answer is None:
                # Nothing to watch. Forget any unsafe run so unplugging the
                # monitor does not leave a countdown ticking behind it.
                self._safe = None
                self._unsafe_at = None
                self._acted_on = None
                return
            if answer != self._safe:
                self._safe = answer
                self._since = time.time()
            if not answer and self._unsafe_at is None:
                self._unsafe_at = self._mono()
            if answer:
                self._unsafe_at = None
                self._fast = False

        if not answer:
            grace = float(settings.get("graceSeconds") or 0)
            held = self._mono() - (self._unsafe_at or self._mono())
            need, fast = self._hold(settings, grace)
            with self._lock:
                self._fast = fast
            if (fast or held >= need) and self._acted_on is not False:
                self._acted_on = False
                self._note("unsafe", f"The safety monitor says unsafe"
                                     f"{f' — {self._error}' if self._error else ''}")
                if self._on_unsafe is not None:
                    self._on_unsafe(self._error)
            return

        # Safe again. The wait before saying so is much longer than the one
        # before stopping, on purpose.
        if self._acted_on is False:
            settled = float(settings.get("resumeAfterSeconds") or 0)
            if time.time() - self._since >= settled:
                self._acted_on = True
                self._note("safe", "The safety monitor says safe again")
                if self._on_safe is not None:
                    self._on_safe()
        elif self._acted_on is None:
            self._acted_on = True

    def _hold(self, settings: dict[str, Any], grace: float) -> tuple[float, bool]:
        """(how long an unsafe must last, whether to act now regardless).

        Without a network source: the grace, as before. With one: a confirmed
        unsafe acts at once (CONFIRM_READS reads, ~5 s apart; the grace is not
        added on top of that), anything else must outlast max(grace, debounce)
        - the larger of the two, not the sum.
        """
        network = self._network()
        if network is None:
            return grace, False
        reads = int(settings.get("confirmReads") or CONFIRM_READS)
        if network.confirmed_reads() >= max(1, reads):
            return 0.0, True
        debounce = settings.get("debounceSeconds")
        debounce = DEBOUNCE_SECONDS if debounce is None else float(debounce)
        return max(grace, debounce), False

    def tick_restart(self) -> None:
        """Give the auto-restart this poll's answer (never raises)."""
        restart = self.auto_restart
        if restart is None:
            return
        try:
            restart.tick(self._raw if self.settings().get("enabled", True) else None)
        except Exception:                          # noqa: BLE001 - never fatal
            pass

    def _note(self, kind: str, message: str) -> None:
        with self._lock:
            self._events.append({"t": time.time(), "kind": kind, "message": message})
            del self._events[:-40]
        if self._log is not None:
            self._log(message, "error" if kind == "unsafe" else "success")

    # -- reporting ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        settings = self.settings()
        monitor = self._monitor()
        network = self._network()
        source = network.status() if network is not None else None
        names = [n for n in (source["name"] if source else None,
                             monitor.name if monitor is not None else None) if n]
        present = monitor is not None or network is not None
        need, _ = (self._hold(settings, float(settings.get("graceSeconds") or 0))
                   if network is not None else (float(settings.get("graceSeconds") or 0), False))
        restart = None
        if self.auto_restart is not None:
            try:
                restart = self.auto_restart.status()
            except Exception:                      # noqa: BLE001
                restart = None
        with self._lock:
            held = (None if self._unsafe_at is None
                    else round(self._mono() - self._unsafe_at, 1))
            return {
                # How long an unsafe has to last before the shutdown, and
                # whether the current one is confirmed (acts at once).
                "holdSeconds": need,
                "confirmed": self._fast,
                "autoRestart": restart,
                "enabled": bool(settings.get("enabled", True)),
                "connected": present,
                "name": " + ".join(names) if names else None,
                "watching": present and bool(settings.get("enabled", True)),
                # The network source's own line (0.2.53a): what it is, how old
                # its last good answer is, and the PC-vs-server clock skew.
                "source": source,
                # None when there is nothing to ask, which is not the same as
                # either answer.
                "safe": self._safe,
                "since": self._since or None,
                "unsafeFor": held,
                "graceSeconds": float(settings.get("graceSeconds") or 0),
                "onUnsafe": str(settings.get("onUnsafe") or "shutdown"),
                "acted": self._acted_on,
                "error": self._error,
                "events": list(self._events[-10:]),
            }
