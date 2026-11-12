"""One camera, one mutating driver call at a time.

A Python timeout cannot cancel a COM or SDK call that is already inside the
driver. This gate does not pretend otherwise. When a call overruns or the
operator cancels, the call is abandoned: its result is discarded, and nothing
else may start an exposure, download, abort, settings write, or reconnect on
that camera until the abandoned call has actually returned.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .devices.base import DeviceError


class CameraGate:
    """Serialises mutating camera calls and quarantines the ones that stick."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._inflight: dict[str, Any] | None = None

    def quarantined(self) -> str | None:
        """Why the camera must not be commanded, or None when it is free."""
        with self._lock:
            flight = self._inflight
            if flight is None or flight["done"].is_set() or not flight["abandoned"]:
                return None
            return str(flight.get("reason") or "a camera call is still inside the driver")

    def wait_clear(self, seconds: float) -> bool:
        """Wait until an abandoned call has returned. True when the camera is free."""
        with self._lock:
            flight = self._inflight
            done = None if flight is None else flight["done"]
        if done is not None:
            done.wait(max(0.0, float(seconds)))
        return self.quarantined() is None

    def begin(self, kind: str) -> int:
        with self._lock:
            flight = self._inflight
            if flight is not None and not flight["done"].is_set():
                why = flight.get("reason") or f"a {flight['kind']} call"
                raise DeviceError(
                    f"the camera is busy with {why} and that call cannot be "
                    "cancelled; no second camera command was sent")
            self._generation += 1
            token = self._generation
            self._inflight = {
                "generation": token,
                "kind": kind,
                "done": threading.Event(),
                "abandoned": False,
                "reason": "",
            }
            return token

    def finish(self, token: int) -> bool:
        """The driver call returned. False when the caller already gave up."""
        with self._lock:
            flight = self._inflight
            if flight is None or flight["generation"] != token:
                return False
            flight["done"].set()
            return not flight["abandoned"]

    def abandon(self, token: int, reason: str) -> None:
        with self._lock:
            flight = self._inflight
            if flight is None or flight["generation"] != token:
                return
            if flight["done"].is_set():
                return
            flight["abandoned"] = True
            flight["reason"] = reason

    def call(self, fn: Callable[[], Any], seconds: float, kind: str,
             cancel: Callable[[], bool] | None = None) -> Any:
        """Run `fn`. On timeout or cancel, leave it running and discard the result."""
        token = self.begin(kind)
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["value"] = fn()
            except BaseException as exc:            # noqa: BLE001 - handed back or dropped
                box["error"] = exc
            finally:
                box["kept"] = self.finish(token)

        thread = threading.Thread(target=run, daemon=True, name=f"camera-{kind[:16]}")
        thread.start()
        deadline = time.monotonic() + max(0.0, float(seconds))
        while thread.is_alive():
            if cancel is not None and cancel():
                self.abandon(token, f"{kind} cancelled")
                raise DeviceError(
                    f"{kind} cancelled. The driver call was not interrupted and "
                    "its result will be discarded.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.abandon(
                    token,
                    f"{kind} did not answer within {float(seconds):g}s")
                raise DeviceError(
                    f"{kind} did not answer within {float(seconds):g}s. "
                    "The driver call is still running and cannot be cancelled. "
                    "No further camera command will be sent until it finishes.")
            thread.join(min(0.25, remaining))
        if not box.get("kept", False):
            raise DeviceError(
                f"{kind} finished after it was given up; the result was discarded")
        if "error" in box:
            raise box["error"]
        return box.get("value")
