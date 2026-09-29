"""Measuring how far apart the filters focus.

Filters are not parfocal.  A 3 nm Ha and a clear luminance sit at different
focus positions because the glass is a different thickness and the light is a
different colour, and the difference is tens to hundreds of focuser steps —
enough to turn a night of narrowband into a night of soft narrowband.

The usual answer is to focus on every filter change, which costs a sweep each
time.  The better one is to measure the differences once, store them, and then
*move* by the difference on every change: a filter change becomes a focuser move
of known size instead of a five-minute sweep.  That is what
`sequencer.filterOffsets` is for; this is what fills it in.

**How it measures, and why it is not simply "sweep each filter once".**

Focus drifts with temperature all night, and a sweep takes minutes.  Measure L
at 22:00 and Ha at 22:12 and the difference between them contains twelve minutes
of cooling as well as the filters.

Two things are done about it.  Each pass is reduced against **its own** reading
of the reference, so only the drift *within* a pass can contaminate it — and
every pass after the first is swept in the **opposite direction**.  A filter
measured late in one pass is measured early in the next, so the drift error
enters with one sign and then the other and averages out.  Sweeping every pass
in the same order would not do this: the error would be the same every time and
averaging would preserve it exactly, which is what the first version of this
did.

That cancellation needs at least two passes, which is why the default is two.
More is better and costs a sweep per filter each, so it is also why the default
is not five.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .devices.base import DeviceError

#: How long to wait for the wheel to finish moving before a sweep.
WHEEL_TIMEOUT = 120.0


class OffsetRun:
    """One measurement of every filter's focus position, on one telescope."""

    def __init__(self, rigs: Any, config: Any) -> None:
        self.rigs = rigs
        self.config = config

        self._thread: threading.Thread | None = None
        self._abort = threading.Event()
        self._lock = threading.RLock()

        self._state = "idle"
        self._message = ""
        self._error: str | None = None
        self._rig_id: str | None = None
        self._reference: str = ""
        self._filters: list[str] = []
        self._passes = 0
        self._pass = 0
        self._done = 0
        self._total = 0
        # name -> list of focus positions, one per pass that measured it.
        self._positions: dict[str, list[int]] = {}
        # name -> list of offsets against the reference, one per pass.
        self._offsets: dict[str, list[float]] = {}
        self._skipped: dict[str, str] = {}
        self._result: dict[str, int] | None = None
        self._started: float | None = None
        self._finished: float | None = None

    # -- lifecycle ---------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, rig: Any, filters: list[str] | None = None,
              passes: int = 2, reference: str = "") -> None:
        if self.running:
            raise DeviceError("a filter offset run is already going")

        focuser = rig.manager.get("focuser")
        if focuser is None or not focuser.connected:
            raise DeviceError("measuring offsets needs a focuser")
        wheel = rig.manager.get("filterwheel")
        if wheel is None or not wheel.connected:
            raise DeviceError("measuring offsets needs a filter wheel")
        camera = rig.manager.get("camera")
        if camera is None or not camera.connected:
            raise DeviceError("measuring offsets needs a camera")

        from .filters import canonical
        available = [canonical(name) for name in (wheel.names or [])
                     if str(name).strip()]
        # Asked for in any spelling, matched in the one the wheel reports.
        asked = [canonical(name) for name in (filters or available)]
        wanted = [name for name in asked if name in available]
        if len(wanted) < 2:
            raise DeviceError(
                "at least two filters are needed to measure an offset between "
                "them — name the wheel's slots in Equipment first")

        # The reference is the filter everything else is measured against, so it
        # should be the one that focuses most reliably. The autofocus filter is
        # chosen for exactly that property, so it is the default.
        named = canonical(reference or rig.config.get("sequencer", "autofocusFilter", "")
                          or "")
        self._reference = named if named in wanted else wanted[0]

        with self._lock:
            self._abort.clear()
            self._rig_id = rig.id
            self._filters = wanted
            self._passes = max(1, min(5, int(passes)))
            self._pass = 0
            self._done = 0
            self._total = self._passes * len(wanted)
            self._positions = {}
            self._offsets = {}
            self._skipped = {}
            self._result = None
            self._error = None
            self._started = time.time()
            self._finished = None
            self._state = "starting"
            self._message = ""

        self._thread = threading.Thread(target=self._run, args=(rig,), daemon=True,
                                        name="filter-offsets")
        self._thread.start()

    def abort(self) -> None:
        if not self.running:
            return
        self._abort.set()
        self.rigs.log("Filter offset run: stopping", "warn")

    def _check(self) -> None:
        if self._abort.is_set():
            raise DeviceError("the offset run was stopped")

    def _say(self, message: str, level: str = "info") -> None:
        self.rigs.log(f"Filter offsets: {message}", level)
        with self._lock:
            self._message = message

    # -- the run -----------------------------------------------------------
    def _run(self, rig: Any) -> None:
        focuser = rig.manager.get("focuser")
        start_position = focuser.position
        try:
            self._say(f"measuring {', '.join(self._filters)} against "
                      f"{self._reference}, {self._passes} pass(es)")
            for index in range(self._passes):
                with self._lock:
                    self._pass = index + 1
                self._one_pass(rig)
            self._finish()
        except DeviceError as exc:
            with self._lock:
                self._error = str(exc)
                self._state = "idle"
                self._finished = time.time()
            self._say(str(exc), "error")
            # Put the focuser back where it started rather than leaving it
            # wherever the last filter happened to want it.
            with _ignore():
                focuser.move_to(int(start_position))
        except Exception as exc:                  # noqa: BLE001 - never fatal
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
                self._state = "idle"
                self._finished = time.time()
            self._say(f"failed: {exc}", "error")

    def _one_pass(self, rig: Any) -> None:
        """Sweep every filter once, and work the offsets out within this pass.

        Within the pass, because that is what makes the numbers mean the filters
        rather than the temperature: the reference is measured in the same pass
        as everything it is compared against, so the drift between them is
        minutes rather than the whole run.

        Alternate passes run in the opposite direction. Within a pass the drift
        error grows with how far down the order a filter sits, so sweeping the
        same way every time gives every pass the *same* error and averaging
        keeps it. Reversed, a filter measured last is measured first next time
        and the two errors cancel.
        """
        order = (list(self._filters) if self._pass % 2
                 else list(reversed(self._filters)))
        measured: dict[str, int] = {}
        for name in order:
            self._check()
            position = self._measure(rig, name)
            if position is None:
                continue
            measured[name] = position
            with self._lock:
                self._positions.setdefault(name, []).append(position)

        reference = measured.get(self._reference)
        if reference is None:
            self._say(f"pass {self._pass}: {self._reference} could not be "
                      "focused, so this pass cannot be used", "warn")
            return
        with self._lock:
            for name, position in measured.items():
                self._offsets.setdefault(name, []).append(float(position - reference))

    def _measure(self, rig: Any, name: str) -> int | None:
        """Put the wheel on one filter and sweep it. None when it would not focus."""
        with self._lock:
            self._state = "measuring"
        self._say(f"pass {self._pass}/{self._passes}: focusing {name}")
        try:
            self._select(rig, name)
        except DeviceError as exc:
            self._note_skip(name, f"could not select it — {exc}")
            return None

        try:
            run = rig.focuser.run(should_abort=lambda: self._abort.is_set())
        except DeviceError as exc:
            if self._abort.is_set():
                raise
            self._note_skip(name, str(exc))
            with self._lock:
                self._done += 1
            return None

        with self._lock:
            self._done += 1
        if run.best_position is None:
            self._note_skip(name, run.detail or "no focus position was found")
            return None
        self._say(f"{name} focuses at {run.best_position}"
                  + (f" (HFD {run.best_hfd:.2f})" if run.best_hfd else ""))
        return int(run.best_position)

    def _note_skip(self, name: str, reason: str) -> None:
        with self._lock:
            self._skipped[name] = reason
        self._say(f"{name}: {reason}", "warn")

    def _select(self, rig: Any, name: str) -> None:
        wheel = rig.manager.get("filterwheel")
        names = list(wheel.names or [])
        if name not in names:
            raise DeviceError(f"the wheel has no filter called {name}")
        index = names.index(name)
        if wheel.position == index:
            return
        wheel.set_position(index)
        deadline = time.monotonic() + WHEEL_TIMEOUT
        while wheel.moving:
            self._check()
            if time.monotonic() > deadline:
                raise DeviceError("the wheel did not finish moving in time")
            time.sleep(0.3)

    def _finish(self) -> None:
        """Average the passes and write the answer into the telescope's settings."""
        with self._lock:
            offsets = {name: values[:] for name, values in self._offsets.items()}
            reference = self._reference
            rig_id = self._rig_id

        if not offsets:
            with self._lock:
                self._error = "nothing could be focused"
                self._state = "idle"
                self._finished = time.time()
            self._say("nothing could be focused", "error")
            return

        final = {name: int(round(sum(values) / len(values)))
                 for name, values in offsets.items()}
        # The reference is the zero by definition, whatever rounding says.
        final[reference] = 0

        rig = self.rigs.get(rig_id)
        # Merged rather than replaced: a filter that could not be measured keeps
        # whatever was already known about it instead of silently becoming zero.
        stored = dict(rig.config.get("sequencer", "filterOffsets", {}) or {})
        stored.update(final)
        rig.config.update("sequencer", {"filterOffsets": stored})

        with self._lock:
            self._result = final
            self._state = "idle"
            self._finished = time.time()
        self._say("measured " + ", ".join(f"{name} {value:+d}"
                                          for name, value in sorted(final.items()))
                  + f" (steps from {reference}), saved for {rig.name}", "success")

    # -- reporting ---------------------------------------------------------
    def spread(self) -> dict[str, float]:
        """How far apart the passes were, per filter.

        The number that says whether to believe the answer: two passes agreeing
        to five steps is a measurement, two passes ninety steps apart is a pair
        of guesses and the run should be repeated.
        """
        with self._lock:
            return {name: round(max(values) - min(values), 1)
                    for name, values in self._offsets.items() if len(values) > 1}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "state": self._state,
                "message": self._message,
                "error": self._error,
                "rig": self._rig_id,
                "reference": self._reference,
                "filters": list(self._filters),
                "pass": self._pass,
                "passes": self._passes,
                "done": self._done,
                "total": self._total,
                "positions": {name: list(values)
                              for name, values in self._positions.items()},
                "offsets": {name: [round(v) for v in values]
                            for name, values in self._offsets.items()},
                "spread": self.spread(),
                "skipped": dict(self._skipped),
                "result": dict(self._result) if self._result else None,
                "started": self._started,
                "finished": self._finished,
            }


class _ignore:
    """A driver refusing to move the focuser back is not worth a second error."""

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return kind is not None
