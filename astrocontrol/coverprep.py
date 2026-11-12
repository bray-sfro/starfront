"""Open the flat-panel cover before any frame that needs sky, and prove it.

The Twin FSQ on EAGLE (2026-11-11, Starfront 0.2.53, ASCOM.OptecAlnitak) left
the cover shut after an afternoon flat. The night unparked, homed, slewed,
centred and autofocused with no "Opening the cover" line. Two things caused
that:

* `has_cover` was latched once at connect. An Alnitak that reports
  CoverState Unknown (or a read that failed and came back as NotPresent)
  was treated as a lamp with no cover for the rest of the night.
* The only open lived in `Capture.open_light_path`, and it ran for a light
  frame only, after centring and autofocus had already exposed through the lid.

This module re-reads CoverState when a sky frame is about to happen. Unknown
on a connected CoverCalibrator is an open attempt, not "no cover". NotPresent
is the only "there is no cover" answer. A failed read is not NotPresent and
is not treated as open.

Nothing here talks to a roof.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .devices.base import DeviceError
from . import roofguard

# ASCOM CoverStatus. 0 is NotPresent, a real answer, not a missing read.
COVER_STATES = {
    0: "notpresent",
    1: "closed",
    2: "moving",
    3: "open",
    4: "unknown",
    5: "error",
}

# ASCOM CalibratorStatus.
CALIBRATOR_STATES = {
    0: "notpresent",
    1: "off",
    2: "notready",
    3: "ready",
    4: "unknown",
    5: "error",
}

# A closed lid on the Twin FSQ read about 503 ADU with almost no spread,
# on both the 5 s L centring frames and the 6 s R autofocus frames.
# A sky frame through an open cover is not that flat.
_CAPPED_SPREAD = 45.0
_CAPPED_MEDIAN = 1500.0


class CoverHeld(DeviceError):
    """The run must not image, centre or focus until the cover is open."""


def map_cover(value: Any) -> str:
    """Map a driver CoverState. A missing value is unread, never NotPresent."""
    if value is None:
        return "unread"
    try:
        number = int(value)
    except (TypeError, ValueError):
        text = str(value or "").strip().lower()
        if text in COVER_STATES.values() or text in ("unread",):
            return text
        return "unread"
    return COVER_STATES.get(number, "unread")


def map_calibrator(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        number = int(value)
    except (TypeError, ValueError):
        text = str(value or "").strip().lower()
        if text in CALIBRATOR_STATES.values():
            return text
        return "unknown"
    return CALIBRATOR_STATES.get(number, "unknown")


def read_state(panel: Any) -> str:
    """The driver's CoverState right now. Never the value latched at connect."""
    if panel is None:
        return "notpresent"
    try:
        return map_cover(panel.cover_state)
    except Exception:
        return "unread"


def wants_open(frame_type: str, source: str | None = None) -> bool:
    """Lights and sky flats need the cover open. Darks, bias and panel flats do not."""
    kind = str(frame_type or "").lower()
    if kind in ("bias", "dark", "darkflat"):
        return False
    if kind == "flat":
        return str(source or "panel").lower() == "sky"
    return kind == "light"


def timeout_seconds(config: Any) -> float:
    """How long to wait for CoverState == Open.

    Observatories set `calibration.coverTimeoutSeconds` (EAGLE uses 180).
    The night checks turn `sequencer.coverTimeoutSeconds` down; when that
    value is not the 120 s default, it wins so a test does not wait the
    full calibration timeout.
    """
    seq_t = 120.0
    cal_t = 180.0
    try:
        seq_t = float(config.get("sequencer", "coverTimeoutSeconds", 120.0) or 120.0)
    except Exception:
        seq_t = 120.0
    try:
        cal_t = float(config.get("calibration", "coverTimeoutSeconds", 180.0) or 180.0)
    except Exception:
        cal_t = 180.0
    if abs(seq_t - 120.0) > 0.01:
        return max(0.05, seq_t)
    return max(0.05, seq_t, cal_t)


def auto_cover(config: Any) -> bool:
    try:
        return bool(config.get("calibration", "autoCover", True))
    except Exception:
        return True


def looks_capped(stats: dict[str, Any] | None, offset: Any = None) -> str | None:
    """A sky frame that is really a bias through a closed lid, or None.

    Both halves are required: the median sits near the bias/offset and the
    spread is tiny. A dim sky frame with stars in it is not flat enough to
    match, and a frame with no statistics is not judged.
    """
    if not stats:
        return None
    try:
        median = float(stats.get("median"))
    except (TypeError, ValueError):
        return None
    spread = stats.get("stdDev", stats.get("mad"))
    try:
        spread_value = float(spread)
    except (TypeError, ValueError):
        return None
    if spread_value > _CAPPED_SPREAD:
        return None
    ceiling = _CAPPED_MEDIAN
    try:
        if offset is not None:
            ceiling = max(ceiling, float(offset) + 1000.0)
    except (TypeError, ValueError):
        pass
    if median > ceiling:
        return None
    return (f"median {median:.0f} ADU with spread {spread_value:.1f} "
            f"(a closed cover reads like a bias, not a sky frame)")


def ensure_open(panel: Any, *, timeout: float, log: Callable[..., None],
                check: Callable[[], None] | None = None,
                pause: Callable[[float], None] | None = None,
                command: bool = True, announce_open: bool = True) -> str:
    """Open the cover and wait until the driver reports Open.

    Returns "open" or "absent" (no cover / no panel). Raises CoverHeld when
    the cover cannot be shown to be open. Does not command a roof.

    `command` false means the operator turned autoCover off: a cover that
    already reads open is accepted, and anything else holds without an
    OpenCover.
    """
    if panel is None or not getattr(panel, "connected", True):
        return "absent"
    if check is None:
        check = lambda: None
    if pause is None:
        pause = time.sleep
    try:
        panel.turn_off()
    except Exception as exc:                          # noqa: BLE001 - said, then the cover
        log(f"Could not turn the calibrator light off: {exc}", "warn")

    state = read_state(panel)
    if state == "notpresent":
        return "absent"
    if state == "open":
        if announce_open:
            log("Cover open", "success")
        return "open"
    if not command:
        raise CoverHeld(
            f"Holding the run: autoCover is off and the cover reads {state}, "
            "not open. No sky frame will be taken through it")
    if state == "unread":
        raise CoverHeld(
            "Holding the run: CoverState could not be read, so the panel is "
            "not being treated as coverless and OpenCover was not sent")

    # closed, moving, unknown, error. Unknown is the Alnitak case: the cover
    # is there, the driver has not said it is open, and the last latch said
    # "no cover". Ask once and then require Open.
    log(f"Opening the cover (driver reports {state})", "warn")
    try:
        panel.open_cover()
    except Exception as exc:                          # noqa: BLE001 - the driver error is the reason
        raise CoverHeld(
            f"Holding the run: OpenCover failed while the driver reported "
            f"{state}: {exc}") from exc

    deadline = time.monotonic() + max(0.05, float(timeout))
    last = state
    while True:
        check()
        last = read_state(panel)
        if last == "open":
            log("Cover open", "success")
            return "open"
        if time.monotonic() >= deadline:
            raise CoverHeld(
                f"Holding the run: the cover is {last}, not open, after "
                f"{float(timeout):g}s. No sky frame, centring frame or "
                "autofocus frame will be taken through it")
        remaining = deadline - time.monotonic()
        pause(min(0.25, max(0.0, remaining)))


def judge_sky_frame(stats: dict[str, Any] | None, offset: Any = None) -> None:
    """Raise CoverHeld when a sky frame looks like the lid was still shut."""
    reason = looks_capped(stats, offset)
    if reason:
        raise CoverHeld(
            "Holding the run: the cover was asked to open but the frame "
            f"looks like a bias through a closed cover ({reason}). "
            "Centring, autofocus and the next light are held")


def guard_blocks_open() -> str | None:
    """Why a cover open must not be sent, from the closed-roof calibration guard."""
    why = roofguard.blocked()
    if not why:
        return None
    return f"{why}; the cover stays closed"
