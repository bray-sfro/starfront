"""The camera that watches the telescope.

Not an imaging camera: a camera pointed *at* the rig, so you can see whether the
scope is where the software thinks it is, whether the cover is off, whether the
cables are about to wrap, and whether there is frost on everything.  A remote
observatory without one is a rig you are flying blind.

It runs on its own thread, continuously, from the moment the device connects
until it goes away.  Nothing else in the program waits on it and nothing it does
can fail a night — a pier cam that stops working is an inconvenience, not a
reason to lose the sky.

**Auto-exposure and auto-gain, because this is not an astronomical exposure.**
An imaging camera is told what to do; a pier cam has to cope with full
afternoon sun, dusk, moonlight and a pitch-dark dome on the same night, across
maybe fourteen stops, with nobody awake to adjust it.  So it measures what it
just got and corrects towards a target brightness:

  * **Exposure moves first**, because it costs nothing in noise. It is changed
    multiplicatively — the error is a ratio, not a difference, and doubling is
    the natural unit of light.
  * **Gain moves only when exposure has run out of room**, at the top or the
    bottom. Gain is the last resort because it buys brightness with noise, and
    at the long end it also keeps the frame rate from collapsing: ten seconds of
    exposure for one picture of a dark dome is not a live feed.
  * **Both are damped and have a deadband**, so a cloud crossing the Moon does
    not set off an oscillation that takes ten minutes to settle.

The measurement is a high percentile rather than the mean.  A pier cam's frame
is mostly dark sky and dark dome wall with a telescope in it, and a mean would
expose for the darkness and blow out the thing you actually want to look at.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np

from .devices.base import DeviceError
from .imaging import png, render

#: Full scale for the 16-bit frames the cameras hand back.
MAX_ADU = 65535.0

#: How long to wait before looking for the camera again when there is none.
IDLE_POLL = 2.0

#: Never ask a camera for a frame more often than this. A pier cam at thirty
#: frames a second would be a webcam; what is wanted is a look at the telescope,
#: and one or two a second is plenty for that at a fraction of the CPU.
MIN_PERIOD = 0.25

#: Trade gain back for exposure while the exposure is below this fraction of its
#: ceiling. The equilibrium is the lowest gain whose exposure still fits in the
#: range with room to spare, which is what "prefer the quiet option" means when
#: the only currency is time. Higher and the feed gets slow; lower and it gets
#: noisy sooner than it needs to.
GAIN_TRADE_BELOW = 0.25


class PierCamService:
    """Keeps a current picture of the telescope, and exposes it correctly."""

    def __init__(self, rigs: Any, config: Any) -> None:
        self.rigs = rigs
        self.config = config

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

        self._png: bytes = b""
        self._token: str = ""
        self._taken: float = 0.0
        self._error: str | None = None
        self._frames = 0
        self._level: float | None = None      # what the last frame measured, 0..1
        self._width = 0
        self._height = 0
        self._seconds = 0.0                   # how long the last frame took

        # The working exposure and gain. Kept here rather than read back from
        # the camera every frame: a driver that rounds or clamps what it is
        # given would otherwise drag the loop back to its own value each time.
        self._exposure: float | None = None
        self._gain: int | None = None

    # -- settings ----------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        return self.config.section("piercam")

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Begin watching. Safe to call more than once."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="piercam")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- the camera --------------------------------------------------------
    def _camera(self) -> Any | None:
        """The pier camera, if one is connected.

        Looked up every pass rather than held: the master telescope can change
        while the program is running, and a camera that is unplugged and
        reconnected should be picked up without restarting anything.
        """
        try:
            device = self.rigs.master.manager.get("piercam")
        except Exception:                        # noqa: BLE001 - no master yet
            return None
        return device if device is not None and device.connected else None

    # -- the loop ----------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            camera = self._camera()
            if camera is None:
                # Nothing connected. Forget the last picture rather than leave a
                # stale one on screen looking live.
                with self._lock:
                    if self._png:
                        self._png = b""
                        self._token = ""
                    self._exposure = self._gain = None
                self._stop.wait(IDLE_POLL)
                continue

            settings = self.settings()
            if not settings.get("enabled", True):
                self._stop.wait(IDLE_POLL)
                continue

            started = time.monotonic()
            try:
                self._grab(camera, settings)
                self._error = None
            except Exception as exc:             # noqa: BLE001 - never fatal
                self._error = str(exc)
                # A camera that has fallen over should not be hammered.
                self._stop.wait(2.0)
                continue

            # Pace the loop. A long exposure already took its time; a short one
            # would otherwise spin.
            interval = float(settings.get("intervalSeconds") or 1.0)
            spent = time.monotonic() - started
            self._stop.wait(max(MIN_PERIOD, interval - spent))

    def _grab(self, camera: Any, settings: dict[str, Any]) -> None:
        """One frame: expose, read, correct the exposure, render."""
        exposure, gain = self._current(camera, settings)

        if gain is not None:
            with _ignoring_driver_limits():
                camera.set_settings(gain=int(gain))

        started = time.monotonic()
        camera.start_exposure(float(exposure), light=True)
        deadline = time.monotonic() + float(exposure) + 60.0
        while not camera.image_ready:
            if self._stop.is_set():
                return
            if time.monotonic() > deadline:
                raise DeviceError("the pier camera did not finish its exposure")
            time.sleep(0.02)

        frame = camera.get_image()
        if frame is None or getattr(frame, "size", 0) == 0:
            raise DeviceError("the pier camera returned an empty frame")
        seconds = time.monotonic() - started

        level = _level_of(frame, float(settings.get("meterPercentile") or 95.0))
        if settings.get("auto", True):
            self._correct(camera, settings, level)

        payload, width, height = _to_png(
            frame, int(settings.get("maxDim") or 720),
            float(settings.get("gamma") or 2.2))

        with self._lock:
            self._png = payload
            self._frames += 1
            # The frame number, not the clock. A timestamp at millisecond
            # resolution can repeat for two frames taken in the same
            # millisecond, and an unchanged token is how the page decides it
            # already has this picture — so the new frame would never be
            # fetched. A counter cannot collide with itself.
            self._token = f"{self._frames}"
            self._taken = time.time()
            self._level = level
            self._width, self._height = width, height
            self._seconds = round(seconds, 3)

    def _current(self, camera: Any, settings: dict[str, Any]) -> tuple[float, int | None]:
        """The exposure and gain to use for this frame.

        On the first frame after connecting there is nothing to go on, so it
        starts from the middle of the range on a log scale rather than from
        either end: guessing bright and guessing dark are equally wrong, but
        guessing dark at the start of a sunny afternoon means several frames of
        pure white before it catches up.
        """
        low, high = _exposure_limits(settings)
        if not settings.get("auto", True):
            fixed = float(settings.get("exposure") or 0.1)
            gain = settings.get("gain")
            return _clamp(fixed, low, high), None if gain is None else int(gain)

        with self._lock:
            exposure, gain = self._exposure, self._gain
        if exposure is None:
            exposure = float(np.sqrt(low * high))
            gain = _gain_limits(camera, settings)[0]
            with self._lock:
                self._exposure, self._gain = exposure, gain
        return exposure, gain

    def _correct(self, camera: Any, settings: dict[str, Any], level: float) -> None:
        """Move exposure, then gain, towards the target brightness.

        Multiplicative on exposure because light is: a frame at a quarter of the
        target needs four times the exposure, whatever the numbers happen to be.
        Damped by taking a fraction of the correction each frame, and ignored
        entirely inside a deadband, because a live feed that breathes is worse
        to watch than one that is slightly off.
        """
        target = float(settings.get("target") or 0.35)
        deadband = float(settings.get("deadband") or 0.08)
        damping = _clamp(float(settings.get("damping") or 0.6), 0.05, 1.0)
        low, high = _exposure_limits(settings)
        gain_low, gain_high = _gain_limits(camera, settings)
        step = int(settings.get("gainStep") or 0) or max(1, (gain_high - gain_low) // 10)

        if level <= 0.0:
            level = 1.0 / MAX_ADU                # a black frame still needs a ratio

        with self._lock:
            exposure = self._exposure or float(np.sqrt(low * high))
            gain = self._gain if self._gain is not None else gain_low

        # Give gain back whenever the exposure can carry the load instead.
        #
        # Deliberately *before* the deadband check, because otherwise a gain
        # raised during one dark night is never lowered again: come dawn the
        # loop drops the exposure, lands on target, finds itself inside the
        # deadband and stops — leaving the feed noisy all day for no reason. A
        # settled loop is exactly when there is room to trade.
        #
        # One step at a time and only with real headroom, so the equilibrium is
        # the lowest gain whose exposure still fits comfortably in the range.
        # Each trade dips the next frame slightly; the loop takes it back over
        # the following two or three, which is invisible at a frame a second.
        if gain > gain_low and exposure < high * GAIN_TRADE_BELOW:
            with self._lock:
                self._gain = int(max(gain_low, gain - step))
            return

        if abs(level - target) <= deadband * target:
            return

        # A damped share of the correction, and never more than a factor of
        # four in one step — a cloud clearing off the Moon should not produce a
        # single frame of pure white followed by a single frame of pure black.
        ratio = _clamp((target / level) ** damping, 0.25, 4.0)
        wanted = exposure * ratio
        exposure = _clamp(wanted, low, high)

        # Gain rises only when the exposure has nowhere left to go. It is the
        # noisy way to buy brightness, so it is always the last resort.
        if wanted > high * 1.001 and gain < gain_high:
            gain = min(gain_high, gain + step)
        elif wanted < low * 0.999 and gain > gain_low:
            gain = max(gain_low, gain - step)

        with self._lock:
            self._exposure, self._gain = exposure, int(gain)

    # -- reporting ---------------------------------------------------------
    def frame(self) -> tuple[bytes, str]:
        with self._lock:
            return self._png, self._token

    def status(self) -> dict[str, Any]:
        camera = self._camera()
        settings = self.settings()
        with self._lock:
            return {
                "connected": camera is not None,
                "name": camera.name if camera is not None else None,
                "running": self.running,
                "enabled": bool(settings.get("enabled", True)),
                "auto": bool(settings.get("auto", True)),
                "token": self._token,
                "hasFrame": bool(self._png),
                "takenAt": self._taken or None,
                "age": round(time.time() - self._taken, 1) if self._taken else None,
                "frames": self._frames,
                "exposure": (None if self._exposure is None
                             else round(self._exposure, 4)),
                "gain": self._gain,
                "level": None if self._level is None else round(self._level, 4),
                "target": float(settings.get("target") or 0.35),
                "width": self._width,
                "height": self._height,
                "seconds": self._seconds,
                "error": self._error,
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _ignoring_driver_limits:
    """Setting gain is advisory: plenty of drivers refuse, and a pier cam that
    will not change gain is still a pier cam."""

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return kind is not None and issubclass(kind, (DeviceError, ValueError))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _exposure_limits(settings: dict[str, Any]) -> tuple[float, float]:
    low = max(1e-6, float(settings.get("minExposure") or 0.0001))
    high = max(low * 2, float(settings.get("maxExposure") or 8.0))
    return low, high


def _gain_limits(camera: Any, settings: dict[str, Any]) -> tuple[int, int]:
    """What the loop is allowed to do with gain.

    The camera's own range, narrowed by the settings when they ask for it. A
    ceiling matters more than it looks: the top of an ASI's gain range is
    unusable noise, and an auto-gain loop with nothing to stop it will find its
    way there on the first properly dark night.
    """
    low = int(getattr(camera, "gain_min", 0) or 0)
    high = int(getattr(camera, "gain_max", 0) or 0)
    if high <= low:
        return low, low
    wanted_low = settings.get("minGain")
    wanted_high = settings.get("maxGain")
    if wanted_low is not None:
        low = max(low, int(wanted_low))
    if wanted_high is not None:
        high = min(high, int(wanted_high))
    return low, max(low, high)


def _level_of(frame: np.ndarray, percentile: float) -> float:
    """How bright the frame is, 0..1.

    A high percentile rather than a mean. The picture is mostly dark dome with a
    telescope in it; metering on the average exposes for the wall and leaves the
    telescope a white smear.
    """
    sample = render._sample(frame).astype(np.float32)
    if sample.size == 0:
        return 0.0
    value = float(np.percentile(sample, _clamp(percentile, 50.0, 99.9)))
    return _clamp(value / MAX_ADU, 0.0, 1.0)


def _to_png(frame: np.ndarray, max_dim: int, gamma: float) -> tuple[bytes, int, int]:
    """An 8-bit picture of the telescope, small enough to send several a second.

    Linear with a gamma rather than the viewer's auto-stretch: the auto-stretch
    exists to drag faint nebulosity out of a black sky, and applied to a pier
    cam it turns a perfectly exposed shot of a telescope into a noisy grey mess.
    Auto-exposure has already put the picture where it should be; all this has
    to do is get it onto a screen.
    """
    factor = render.fit_factor(frame.shape[1], frame.shape[0], max_dim)
    small = render.downsample(frame, factor)
    scaled = np.clip(small.astype(np.float32) / MAX_ADU, 0.0, 1.0)
    if gamma and abs(gamma - 1.0) > 1e-3:
        scaled = scaled ** (1.0 / float(gamma))
    eight = (scaled * 255.0 + 0.5).astype(np.uint8)
    # Colour cameras hand back a Bayer mosaic; shown raw it is a fine
    # checkerboard. Nobody is measuring anything here, so the mosaic is simply
    # averaged away by the downsample above, which is why even a 1:1 feed is
    # binned at least once.
    if eight.ndim == 3 and eight.shape[2] not in (1, 3):
        eight = eight[:, :, 0]
    if eight.ndim == 3 and eight.shape[2] == 1:
        eight = eight[:, :, 0]
    return png.encode(eight, level=1), int(eight.shape[1]), int(eight.shape[0])
