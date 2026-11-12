"""Per-telescope recovery state, backoff, timeouts and the progress journal.

What the logs from the RASA and FSQ106 PCs (2026-09-16 .. 2026-11-01) showed
was not that recovery was missing but that it was all-or-nothing: three
retries twenty seconds apart, and then the whole night ended for every
telescope; or a driver call that never returned and a program that sat
"running" until the afternoon.  The pieces here are what the sequencer uses
to do better:

* `RigRecovery` - one telescope's state (ok, recovering, failed), the attempt
  it is on out of how many, why, and what it is doing now.  Shown in the
  status and on the page.
* `backoff` - how long to wait before each attempt: short first, then longer,
  capped, so a camera that is back in five seconds is not kept waiting and
  one that needs a minute is not hammered.
* `call_with_timeout` - every device step a recovery takes has a deadline.  A
  driver call cannot be cancelled from Python, so a call that overruns is
  abandoned on its own thread and reported as hung.
* `ProgressJournal` - where the run writes what it has shot, frame by frame,
  so a program that is killed or a PC that loses power picks the night up
  at the frame it was on.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .devices.base import DeviceError

OK = "ok"
RECOVERING = "recovering"
FAILED = "failed"


def backoff(attempt: int, base: float, cap: float) -> float:
    """Seconds to wait before `attempt` (1-based): base, 2x, 4x ... capped."""
    attempt = max(1, int(attempt))
    return float(min(max(0.0, cap), max(0.0, base) * (2 ** (attempt - 1))))


def call_with_timeout(fn: Callable[[], Any], seconds: float, what: str) -> Any:
    """Run `fn` and return what it returns, or raise if it takes too long.

    The call goes on a daemon thread of its own.  When it overruns it is left
    there - a wedged native driver call cannot be interrupted - and the
    caller gets a DeviceError naming the step, which is what lets the rest of
    the program carry on instead of waiting with it.
    """
    if seconds is None or seconds <= 0:
        return fn()
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:            # noqa: BLE001 - handed back below
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True, name=f"timed-{what[:24]}")
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise DeviceError(f"{what} did not answer within {seconds:g}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class RigRecovery:
    """One telescope's recovery, as the status and the page show it."""

    def __init__(self, rig_id: str, name: str) -> None:
        self.rig_id = rig_id
        self.name = name
        self.state = OK
        self.reason = ""
        self.step = ""
        self.attempt = 0
        self.attempts = 0
        self.next_at: float | None = None
        self.since: float | None = None
        self.down_since: float | None = None
        self.recoveries = 0
        self.failures = 0
        self.power_cycled = False
        self.rejoined = False
        self.thread: threading.Thread | None = None
        self.history: list[dict[str, Any]] = []

    @property
    def busy(self) -> bool:
        return self.state == RECOVERING

    def note(self, what: str, ok: bool | None = None) -> None:
        self.history.append({"t": time.time(), "what": what, "ok": ok,
                             "attempt": self.attempt})
        del self.history[:-20]

    def as_dict(self) -> dict[str, Any]:
        return {
            "rigId": self.rig_id, "name": self.name, "state": self.state,
            "reason": self.reason, "step": self.step,
            "attempt": self.attempt, "attempts": self.attempts,
            "nextAt": self.next_at, "since": self.since,
            "downSince": self.down_since, "recoveries": self.recoveries,
            "failures": self.failures, "history": list(self.history[-6:]),
        }

    def label(self) -> str:
        """"recovering, attempt 2/6: exposure failed", for a banner."""
        if self.state == OK:
            return "ok"
        text = self.state
        if self.attempts:
            text += f", attempt {self.attempt}/{self.attempts}"
        if self.reason:
            text += f": {self.reason}"
        return text


class ProgressJournal:
    """The run's progress on disk, rewritten after every frame.

    Small and written whole (to a temporary file, then renamed over the old
    one), so a power cut leaves either the last version or the one before -
    never half of one.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any] | None:
        with contextlib.suppress(Exception):
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        return None

    def write(self, data: dict[str, Any]) -> None:
        data = dict(data)
        data["updated"] = time.time()
        with self._lock:
            with contextlib.suppress(Exception):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
                os.replace(tmp, self.path)

    def close(self, reason: str) -> None:
        data = self.load() or {}
        data["active"] = False
        data["closed"] = reason
        self.write(data)
