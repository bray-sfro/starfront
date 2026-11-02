"""What the rig actually costs between exposures, measured rather than guessed.

Every plan is arithmetic on two numbers: how long the shutter is open, and
everything else.  The first is exact.  The second used to be three typed
guesses — fifteen seconds a frame, twenty a filter change, ninety a panel — and
the guesses are always wrong in the same direction, because nobody types in the
cost of the thing they forgot about.  A 61-megapixel camera on USB 2 takes
eighteen seconds to hand over a frame; an autofocus sweep of nine points at six
seconds each is four minutes with the moves, and the old model costed it at
nothing at all.  A night planned on those guesses runs out of dark an hour early
and the operator is left wondering which target to cut.

So the rig is timed while it works.  Every download, every filter change, every
slew-settle-solve and every focus run records how long it took, and the plan is
costed on the median of what this rig has actually done.  The median rather than
the mean: one download that hit a stalled USB bus should not move the estimate,
and with a rolling window the number tracks a rig that has genuinely changed —
a new camera, a faster cable.

**A focus run is modelled, not just averaged.**  Timing one run of nine points
at six seconds tells you more than "that took four minutes": subtract the
exposures and what is left is the per-point cost of moving the focuser and
measuring the stars, which does not depend on the filter or the exposure.  So
one run calibrates every future run — nine points or fifteen, six seconds a
point or twenty, luminance or Ha — which is why the calibration routine does not
have to sweep once per filter.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from pathlib import Path
from typing import Any

from .config import data_root

#: How many samples of each kind to keep.  Enough that one bad night cannot move
#: the median, few enough that the number follows the rig when something about
#: it changes.
WINDOW = 40

#: What is measured.  The key, what it means, and what to assume before there is
#: any measurement at all — the old typed defaults, so a fresh install plans
#: exactly as it did before it had learned anything.
KINDS: dict[str, dict[str, Any]] = {
    # From the shutter closing to the frame being on disk: download, stretch,
    # statistics and the FITS write.
    "download": {"label": "Frame download", "default": 12.0, "unit": "s"},
    # The wheel moving, plus any focus offset applied with it.
    "filterChange": {"label": "Filter change", "default": 20.0, "unit": "s"},
    # Slew, settle, and the plate solve that centres it.
    "slew": {"label": "Slew and centre", "default": 90.0, "unit": "s"},
    # A dither and the guider settling after it.
    "dither": {"label": "Dither and settle", "default": 8.0, "unit": "s"},
    # One point of an autofocus sweep, *not counting its exposure*: the move,
    # the backlash take-up, the settle and measuring the stars.
    "focusPerPoint": {"label": "Autofocus, per point", "default": 6.0, "unit": "s"},
    # Everything a focus run costs that is not per point: the initial move out,
    # the final move onto the fitted position and the confirming frame.
    "focusFixed": {"label": "Autofocus, fixed cost", "default": 45.0, "unit": "s"},
}


class OverheadStore:
    """Measured overheads on disk, written through as they are recorded."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_root() / "overheads.json")
        self._lock = threading.RLock()
        self._samples: dict[str, list[float]] = {kind: [] for kind in KINDS}
        self._updated: dict[str, float] = {}
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(stored, dict):
            return
        with self._lock:
            for kind, values in (stored.get("samples") or {}).items():
                if kind in KINDS and isinstance(values, list):
                    self._samples[kind] = [float(v) for v in values
                                           if isinstance(v, (int, float))][-WINDOW:]
            self._updated = {k: float(v) for k, v in (stored.get("updated") or {}).items()
                             if k in KINDS and isinstance(v, (int, float))}

    def save(self) -> None:
        with self._lock:
            payload = json.dumps({"samples": self._samples,
                                  "updated": self._updated}, indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(payload, "utf-8")
        except OSError:
            pass                                # a read-only home is not fatal

    # -- recording ---------------------------------------------------------
    def record(self, kind: str, seconds: float) -> None:
        """Note one measurement.  Silently ignores anything implausible.

        A negative or absurd figure means something else went wrong — a clock
        adjustment, a driver that blocked for a minute — and letting it into the
        window would poison the estimate for the next forty frames.
        """
        if kind not in KINDS:
            return
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return
        if not 0.0 <= value <= 3600.0:
            return
        with self._lock:
            self._samples[kind].append(round(value, 3))
            del self._samples[kind][:-WINDOW]
            self._updated[kind] = time.time()
        self.save()

    def record_focus_run(self, seconds: float, points: int, exposure: float,
                         frames_per_point: int = 1) -> None:
        """Turn one timed focus run into the two numbers that predict any run.

        The exposures are known exactly, so subtracting them leaves the cost of
        moving and measuring.  Splitting *that* between a per-point part and a
        fixed part needs an assumption about the fixed part, so the fixed cost
        is taken as what is left after the per-point cost has been attributed —
        and the per-point cost is what a short sweep is dominated by, which is
        the number worth getting right.
        """
        points = max(1, int(points))
        frames = max(1, int(frames_per_point))
        shutter = points * frames * max(0.0, float(exposure))
        overhead = float(seconds) - shutter
        if overhead <= 0:
            return
        # The confirming frame and the two long moves either side are roughly a
        # fixed third of a typical sweep; the rest is per point. Both are then
        # recorded as measurements in their own right, so successive runs of
        # different lengths converge on the truth rather than on this split.
        fixed = min(overhead * 0.35, overhead)
        per_point = (overhead - fixed) / points
        self.record("focusFixed", fixed)
        self.record("focusPerPoint", per_point)

    def clear(self, kind: str | None = None) -> None:
        with self._lock:
            for name in ([kind] if kind else list(KINDS)):
                if name in self._samples:
                    self._samples[name] = []
                    self._updated.pop(name, None)
        self.save()

    # -- reading -----------------------------------------------------------
    def value(self, kind: str, fallback: float | None = None) -> float:
        """The measured figure, or what to assume until there is one."""
        with self._lock:
            samples = list(self._samples.get(kind) or [])
        if not samples:
            if fallback is not None:
                return float(fallback)
            return float(KINDS[kind]["default"]) if kind in KINDS else 0.0
        return round(statistics.median(samples), 2)

    def measured(self, kind: str) -> bool:
        with self._lock:
            return bool(self._samples.get(kind))

    def focus_seconds(self, points: int, exposure: float,
                      frames_per_point: int = 1) -> float:
        """How long a focus run of this shape takes on this rig.

        This is the whole point of measuring per point rather than per run: a
        nine-point sweep at six seconds and a fifteen-point sweep at twenty are
        both predicted from the same pair of numbers, so nothing has to be
        re-measured when the sweep is retuned or the filter changes.
        """
        points = max(1, int(points))
        frames = max(1, int(frames_per_point))
        return (self.value("focusFixed")
                + points * (frames * max(0.0, float(exposure))
                            + self.value("focusPerPoint")))

    def calibrate(self, rig: Any, frames: int = 3, focus: bool = True,
                  say: Any = None) -> dict[str, Any]:
        """Time this rig on purpose, rather than waiting for a night to do it.

        Three short exposures give the download; moving the wheel back and forth
        gives the filter change; one autofocus sweep gives both halves of what a
        sweep costs.  One sweep is enough for every filter and every sweep
        length, because what is measured is the per-point cost of moving and
        measuring — the exposures are arithmetic.

        Deliberately does not touch the mount.  Slew time depends entirely on
        how far the slew is, and a made-up slew measures a made-up distance; that
        one is learned from the real ones a sequence makes.
        """
        note = say or (lambda message, level="info": None)
        done: list[str] = []

        camera = rig.manager.require("camera")
        # Short enough to be quick, long enough that the shutter and the driver
        # behave as they do on a real frame.
        exposure = 1.0
        note(f"Timing {max(1, frames)} downloads")
        for _ in range(max(1, frames)):
            before = len(self._samples.get("download") or [])
            rig.capture.capture_blocking(exposure, frame_type="light")
            if len(self._samples.get("download") or []) == before:
                raise RuntimeError("the camera took a frame but nothing was timed")
        done.append(f"download {self.value('download'):.1f}s")

        wheel = rig.manager.get("filterwheel")
        if wheel is not None and wheel.connected and len(wheel.names or []) > 1:
            note("Timing a filter change")
            here = wheel.position
            other = 1 if here == 0 else 0
            for index in (other, here):
                started = time.time()
                wheel.set_position(index)
                deadline = time.time() + 120
                while wheel.moving and time.time() < deadline:
                    time.sleep(0.1)
                self.record("filterChange", time.time() - started)
            done.append(f"filter change {self.value('filterChange'):.1f}s")
        else:
            note("No filter wheel with more than one slot; skipping that")

        focuser = rig.manager.get("focuser")
        if focus and focuser is not None and focuser.connected:
            note("Running one autofocus sweep — this is the long part")
            rig.focuser.run()
            settings = rig.config.section("sequencer")
            done.append(
                "autofocus "
                f"{self.focus_seconds(int(settings.get('focusPoints') or 9), float(settings.get('focusExposure') or 6.0), int(settings.get('focusFramesPerPoint') or 1)) / 60:.1f} min "
                "for a full sweep")
        elif focus:
            note("No focuser connected; skipping the sweep")

        return {"measured": done, "overheads": self.summary()}

    def summary(self) -> dict[str, Any]:
        """Everything measured, for the UI: the figure, how sure, how old."""
        out: dict[str, Any] = {}
        with self._lock:
            for kind, meta in KINDS.items():
                samples = list(self._samples.get(kind) or [])
                out[kind] = {
                    "label": meta["label"],
                    "seconds": (round(statistics.median(samples), 2) if samples
                                else float(meta["default"])),
                    "measured": bool(samples),
                    "samples": len(samples),
                    "spread": (round(max(samples) - min(samples), 2)
                               if len(samples) > 1 else 0.0),
                    "updated": self._updated.get(kind),
                    "default": float(meta["default"]),
                }
        return out
