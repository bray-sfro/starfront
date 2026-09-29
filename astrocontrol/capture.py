"""Capture orchestration and the session image store.

One background thread owns the exposure loop.  HTTP handlers only ever queue a
request and read state, so a slow camera download can never block the UI.
"""

from __future__ import annotations

import contextlib
import os
import queue
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from . import astro
from .config import Config, data_root, effective_site
from .devices.base import DeviceError
from .devices.manager import DeviceManager
from .imaging import fits, render

FRAME_TYPES = ("light", "dark", "bias", "flat", "darkflat")
LIGHT_PATH_TYPES = ("light", "flat")      # frame types where the shutter/sky matters

# How many frames may be waiting to be calibrated before the queue starts
# dropping them.  Calibration runs behind the exposure loop on purpose, so it is
# allowed to fall behind — but not without end, and not silently.
CALIBRATION_BACKLOG = 24

_UNSAFE = re.compile(r"[^A-Za-z0-9._+-]+")


def clean_target(name: str) -> str:
    """Make a target name safe to put in a filename, without mangling it."""
    return _UNSAFE.sub("_", (name or "").strip()).strip("_")


def _utc(timestamp: float) -> str:
    """A moment as DATE-OBS wants it: ISO, UTC, milliseconds."""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _hms(hours: Any) -> str | None:
    """RA in hours as `HH MM SS.ss`, the form every stacker reads."""
    if hours is None:
        return None
    value = float(hours) % 24.0
    h = int(value)
    m = int((value - h) * 60.0)
    s = ((value - h) * 60.0 - m) * 60.0
    if s >= 59.995:
        s = 0.0
        m += 1
    if m >= 60:
        m = 0
        h = (h + 1) % 24
    return f"{h:02d} {m:02d} {s:05.2f}"


def _dms(degrees: Any) -> str | None:
    """Dec in degrees as `+DD MM SS.s`."""
    if degrees is None:
        return None
    value = float(degrees)
    sign = "-" if value < 0 else "+"
    value = abs(value)
    d = int(value)
    m = int((value - d) * 60.0)
    s = ((value - d) * 60.0 - m) * 60.0
    if s >= 59.95:
        s = 0.0
        m += 1
    if m >= 60:
        m = 0
        d += 1
    return f"{sign}{d:02d} {m:02d} {s:04.1f}"


def _frame_number(filename: str) -> int | None:
    match = re.search(r"_(\d{4})\.fits$", filename)
    return int(match.group(1)) if match else None


def _rounded(value: Any, places: int = 3) -> float | None:
    try:
        return None if value is None else round(float(value), places)
    except (TypeError, ValueError):
        return None


@dataclass
class ImageRecord:
    id: str
    filename: str
    path: str | None
    timestamp: float
    frame_type: str
    exposure: float
    binning: int
    gain: int
    offset: int
    width: int
    height: int
    filter: str | None = None
    object_name: str | None = None
    telescope: str | None = None
    focuser_position: int | None = None
    focuser_temperature: float | None = None
    ccd_temperature: float | None = None
    ra: float | None = None
    dec: float | None = None
    saved: bool = True
    stats: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """The compact form the image list uses; the histogram is fetched separately."""
        payload = asdict(self)
        stats = dict(payload.get("stats") or {})
        stats.pop("histogram", None)
        payload["stats"] = stats
        return payload


class CaptureService:
    # Frames captured with saving switched off exist only in this cache, so it
    # is deeper than it needs to be for saved frames alone.
    MAX_CACHED_FRAMES = 8

    def __init__(self, manager: DeviceManager, config: Config | None = None) -> None:
        self.manager = manager
        self.config = config or Config()
        self._directory_override: Path | None = None
        self.save_enabled = True
        self.target_name = ""
        # What goes in the OBJECT header.  Kept apart from `target_name` because
        # that one has to survive being put in a filename and this one does not:
        # OBJECT wants "M31 - Panel 3", the filename wants "M31_P3".  A mosaic
        # panel is a different object as far as a stacker is concerned, so every
        # panel — on the master and on every slave riding along with it — has to
        # say which panel it is.
        self.object_name = ""
        # Which mosaic panel is being shot, for the filename.  The folder stays
        # keyed on the target so one mosaic remains one folder.
        self.panel_label = ""
        # The telescope this camera belongs to: TELESCOP in the header, and the
        # folder frames land in once there is more than one of them.
        self.telescope = ""
        self.subfolder: str | None = None
        # What the sequencer knows about the frame that the camera cannot:
        # which target and panel, the panel's own coordinates and angle, and
        # the collaboration it is for. Written into every light frame so a
        # pipeline can sort a season of subs without asking anybody. Set by
        # `set_context`, cleared by `clear_context`.
        self.frame_context: dict[str, Any] = {}
        # Whether the next light is the first after a dither. A pipeline that
        # registers frames wants to know where the offsets are.
        self._dithered = False
        # The calibration library, and what sort of frames are being taken.
        # Together they decide whether a light gets a calibrated copy written
        # beside it as it lands: "" is manual work on the Image tab, which is
        # never calibrated automatically.
        self.library: Any = None
        # Where the time a download actually takes is recorded, so the planner
        # costs frames on what this camera does rather than on a typed guess.
        self.overheads: Any = None
        self.calibration_context: str = ""
        self._calibration_queue: queue.Queue | None = None
        self._calibration_worker: threading.Thread | None = None
        self._calibration_last: dict[str, Any] = {}
        self._calibration_said_already: dict[str, str] = {}
        self._sequences: dict[str, int] = {}
        self.images: list[ImageRecord] = []
        self._by_id: dict[str, ImageRecord] = {}
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._lock = threading.RLock()

        self._thread: threading.Thread | None = None
        self._abort = threading.Event()
        self._loop = False
        self._state = "idle"
        self._error: str | None = None
        self._request: dict[str, Any] = {}
        self._frames_taken = 0
        self._latest_id: str | None = None
        # Timed here rather than asked of the driver: `PercentCompleted` is
        # optional in ASCOM and plenty of cameras return 0 for it, which leaves
        # a progress bar that never moves through a five-minute sub.
        self._exposure_started: float | None = None
        self._exposure_seconds: float = 0.0
        # What the frame in flight is: its type and the filter it is through,
        # for the activity line at the top of the screen.
        self._frame_type = ""
        self._current_filter = ""

    # -- where frames go ---------------------------------------------------
    @staticmethod
    def night_name(when: datetime | None = None) -> str:
        """The date a night belongs to.

        Frames taken after midnight belong to the evening that started the day
        before, so a whole night's work lands in one folder instead of being
        split at midnight.  Noon is the cut.
        """
        when = when or datetime.now()
        night = when.date() if when.hour >= 12 else (when - timedelta(days=1)).date()
        return night.isoformat()

    @property
    def root_dir(self) -> Path:
        configured = (self.config.get("capture", "rootDirectory", "") or "").strip()
        return (Path(configured).expanduser() if configured
                else data_root() / "captures")

    @property
    def session_dir(self) -> Path:
        """`<root>/<target>/<night>`, or the folder that was set by hand.

        With more than one telescope each gets a folder of its own inside that,
        so two cameras shooting the same target on the same night cannot write
        over each other's frames.
        """
        if self._directory_override is not None:
            base = self._directory_override
        else:
            parts = [self.root_dir]
            if self.target_name:
                parts.append(self.target_name)
            parts.append(self.night_name())
            base = Path(*parts)
        return base / self.subfolder if self.subfolder else base

    # -- state -------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        elapsed = None
        remaining = None
        if self._exposure_started is not None and self._state == "exposing":
            elapsed = max(0.0, time.time() - self._exposure_started)
            remaining = max(0.0, self._exposure_seconds - elapsed)
        return {
            "state": self._state,
            "busy": self.busy,
            "exposureSeconds": self._exposure_seconds,
            "elapsed": round(elapsed, 2) if elapsed is not None else None,
            "remaining": round(remaining, 1) if remaining is not None else None,
            "loop": self._loop,
            "error": self._error,
            "framesTaken": self._frames_taken,
            "request": self._request,
            "latestImageId": self._latest_id,
            "sessionDir": str(self.session_dir),
            "rootDir": str(self.root_dir),
            "night": self.night_name(),
            "customDir": self._directory_override is not None,
            "saveEnabled": self.save_enabled,
            "target": self.target_name,
            "object": self.object_name or self.target_name,
            "panel": self.panel_label,
            "telescope": self.telescope,
            "frameType": self._frame_type,
            "filter": self._current_filter,
            "calibration": self.calibration_status(),
        }

    # -- output settings ---------------------------------------------------
    def set_output(self, save: bool | None = None, directory: str | None = None,
                   target: str | None = None,
                   object_name: str | None = None,
                   panel: str | None = None) -> dict[str, Any]:
        """Change where frames go, or whether they are written at all."""
        if directory is not None:
            text = directory.strip()
            if not text:
                # An empty folder puts us back on the target/night layout.
                self._directory_override = None
                self.manager.log(f"Saving frames under {self.root_dir}")
            else:
                path = Path(text).expanduser()
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise DeviceError(f"cannot use {path}: {exc}") from exc
                if not os.access(path, os.W_OK):
                    raise DeviceError(f"{path} is not writable")
                self._directory_override = path
                self.manager.log(f"Saving frames to {path}")
        if target is not None:
            self.target_name = clean_target(target)
            # Typing a target by hand on the Image tab means that is the object
            # too; the sequencer overrides it panel by panel afterwards.
            if object_name is None:
                self.object_name = " ".join(str(target or "").split())
            if panel is None:
                self.panel_label = ""
                # A target typed by hand is not a panel of anything the
                # planner knows about.
                self.frame_context = {}
        if object_name is not None:
            self.object_name = " ".join(str(object_name).split())[:64]
        if panel is not None:
            self.panel_label = clean_target(panel)
        if save is not None:
            self.save_enabled = bool(save)
            self.manager.log(
                "Frames will be saved to disk" if self.save_enabled
                else "Frames are NOT being saved", "info" if self.save_enabled else "warn")
        return self.status()

    def set_context(self, **fields: Any) -> None:
        """What the sequencer knows about the frames to come. See `frame_context`."""
        self.frame_context = {key: value for key, value in fields.items()
                              if value is not None}

    def clear_context(self) -> None:
        self.frame_context = {}
        self._dithered = False

    def mark_dithered(self) -> None:
        """The next light frame follows a dither."""
        self._dithered = True

    def snapshot_output(self) -> dict[str, Any]:
        """Everything `set_output` controls, so a borrower can give it back.

        A calibration run points the camera at the library for a few hundred
        frames.  Whatever the Image tab or a paused sequence had it set to has
        to come back afterwards, or the next light frame lands in the darks.
        """
        return {
            "save": self.save_enabled,
            "directory": self._directory_override,
            "target": self.target_name,
            "object": self.object_name,
            "panel": self.panel_label,
            "context": self.calibration_context,
        }

    def restore_output(self, state: dict[str, Any]) -> None:
        self.save_enabled = bool(state.get("save", True))
        self._directory_override = state.get("directory")
        self.target_name = state.get("target") or ""
        self.object_name = state.get("object") or ""
        self.panel_label = state.get("panel") or ""
        self.calibration_context = state.get("context") or ""

    # -- capture -----------------------------------------------------------
    def start(self, exposure: float, frame_type: str = "light", binning: int | None = None,
              gain: int | None = None, offset: int | None = None, loop: bool = False,
              count: int = 1) -> None:
        if self.busy:
            raise DeviceError("a capture is already running")
        frame_type = frame_type.lower()
        if frame_type not in FRAME_TYPES:
            raise DeviceError(f"frame type must be one of {', '.join(FRAME_TYPES)}")
        if exposure < 0 or exposure > 3600:
            raise DeviceError("exposure must be between 0 and 3600 seconds")

        camera = self.manager.require("camera")
        camera.set_settings(binning=binning, gain=gain, offset=offset)

        self._abort.clear()
        self._loop = bool(loop)
        self._error = None
        self._frames_taken = 0
        self._request = {"exposure": exposure, "frameType": frame_type,
                         "binning": camera.binning, "gain": camera.gain,
                         "offset": camera.offset, "count": max(1, int(count))}
        self._thread = threading.Thread(target=self._run, args=(exposure, frame_type,
                                                                max(1, int(count))),
                                        daemon=True, name="capture")
        self._thread.start()

    def capture_blocking(self, exposure: float, frame_type: str = "light") -> ImageRecord:
        """Take exactly one frame on the calling thread and return its record.

        Plate-solve centring needs a frame before it can decide anything, so it
        wants the exposure inline rather than handed to the capture thread.
        `busy` still reports True throughout: the caller's own thread is the one
        holding the camera, which is what stops the UI queueing a second run.
        """
        if self.busy:
            raise DeviceError("a capture is already running")
        camera = self.manager.require("camera")

        self._thread = threading.current_thread()
        self._abort.clear()
        self._loop = False
        self._error = None
        self._frames_taken = 0
        self._request = {"exposure": exposure, "frameType": frame_type,
                         "binning": camera.binning, "gain": camera.gain,
                         "offset": camera.offset, "count": 1}
        try:
            self._capture_one(exposure, frame_type)
            self._frames_taken = 1
        except Exception as exc:
            self._error = str(exc)
            raise
        finally:
            self._state = "idle"
            self._thread = None

        if self._latest_id is None:
            raise DeviceError("the exposure produced no frame")
        return self.record(self._latest_id)

    def abort(self) -> None:
        self._loop = False
        self._abort.set()
        camera = self.manager.get("camera")
        if camera is not None and camera.connected:
            try:
                camera.abort_exposure()
            except Exception as exc:
                self.manager.log(f"Abort failed: {exc}", "warn")
        self._state = "idle"
        self.manager.log("Capture aborted", "warn")

    def set_loop(self, loop: bool) -> None:
        """Turn looping off mid-run; the current frame still finishes."""
        self._loop = bool(loop)

    def _run(self, exposure: float, frame_type: str, count: int) -> None:
        taken = 0
        try:
            while not self._abort.is_set():
                self._capture_one(exposure, frame_type)
                taken += 1
                self._frames_taken = taken
                if self._abort.is_set() or (not self._loop and taken >= count):
                    break
        except Exception as exc:
            self._error = str(exc)
            self.manager.log(f"Capture failed: {exc}", "error")
        finally:
            self._state = "idle"
            self._loop = False

    # -- the light path ----------------------------------------------------
    def open_light_path(self) -> dict[str, Any]:
        """Put the panel out and open the cover, for a frame that needs sky.

        Called before every light frame — a manual capture, a sequence sub, a
        focus exposure, a plate solve — because all of them are ruined in the
        same way by a shut cover, and because the alternative is remembering.
        Calibration deliberately closes the cover for the darks and lights the
        panel for the flats; this is what undoes both, at the moment it matters
        rather than at the end of a run that may have been stopped half way.

        Anything the driver refuses is logged and carried past: a frame taken
        through a stuck cover is a wasted frame, but a night that stops dead
        because a cover reported the wrong state is a wasted night.
        """
        done: dict[str, Any] = {"panel": False, "cover": False}
        if not self.config.get("calibration", "autoCover", True):
            return done
        panel = self.manager.get("flatpanel")
        if panel is None or not panel.connected:
            return done

        try:
            if panel.light_on:
                panel.turn_off()
                done["panel"] = True
                self.manager.log("Flat panel switched off for a light frame")

            # "notpresent" and "unknown" are left alone: there is either no
            # cover or no way to tell, and driving one blind is worse.
            if panel.has_cover and panel.cover_state in ("closed", "moving"):
                if panel.cover_state == "closed":
                    self.manager.log("Opening the cover for a light frame")
                    panel.open_cover()
                timeout = float(self.config.get("calibration",
                                                "coverTimeoutSeconds", 180.0))
                deadline = time.monotonic() + timeout
                while panel.cover_state != "open":
                    if self._abort.is_set():
                        return done
                    if time.monotonic() > deadline:
                        self.manager.log(
                            f"The cover is still {panel.cover_state} after "
                            f"{timeout:g}s; taking the frame anyway", "warn")
                        return done
                    time.sleep(0.5)
                done["cover"] = True
                self.manager.log("Cover open", "success")
        except Exception as exc:                  # noqa: BLE001 - never lose a night
            self.manager.log(f"Could not open the light path: {exc}", "warn")
        return done

    def _capture_one(self, exposure: float, frame_type: str) -> None:
        camera = self.manager.require("camera")
        # Only frames that want sky: a flat wants the panel exactly as the
        # calibration run has just set it, and a dark wants the cover shut.
        if frame_type == "light":
            self.open_light_path()
        metadata = self._snapshot_rig()

        self._state = "exposing"
        self._exposure_started = time.time()
        self._exposure_seconds = float(exposure)
        self._frame_type = frame_type
        self._current_filter = str(metadata.get("filter") or "")
        # The moment the shutter opens, for DATE-OBS. It used to be stamped
        # when the frame was written, after the download - a minute late on
        # a long sub, which is a minute of error in every timing a pipeline
        # takes from the header.
        metadata["startedAt"] = self._exposure_started
        self.manager.log(
            f"Exposing {exposure:g}s {frame_type}"
            + (f" [{metadata['filter']}]" if metadata.get("filter") else ""))
        camera.start_exposure(exposure, light=frame_type in LIGHT_PATH_TYPES)

        # Wait for the driver, with a generous allowance for download.
        deadline = time.monotonic() + exposure + 300.0
        while not camera.image_ready:
            if self._abort.is_set():
                return
            if time.monotonic() > deadline:
                raise DeviceError("timed out waiting for the camera")
            time.sleep(0.05)
        if self._abort.is_set():
            return

        # Timed from the shutter closing to the frame being on disk. This is
        # the "everything else" half of what a frame costs, and it is the half
        # a plan used to guess at.
        download_started = time.monotonic()
        self._state = "downloading"
        frame = camera.get_image()
        if frame is None or frame.size == 0:
            raise DeviceError("camera returned an empty frame")

        self._state = "saving"
        record = self._store(frame, exposure, frame_type, camera, metadata)
        if self.overheads is not None:
            self.overheads.record("download", time.monotonic() - download_started)
        self._latest_id = record.id
        self.manager.log(
            f"{'Saved' if record.saved else 'Captured (not saved)'} {record.filename}  "
            f"median {record.stats.get('median')} ADU, max {record.stats.get('max')}",
            "success")

    def _snapshot_rig(self) -> dict[str, Any]:
        """Read the rest of the rig so the FITS header describes this frame."""
        meta: dict[str, Any] = {}
        wheel = self.manager.get("filterwheel")
        if wheel is not None and wheel.connected:
            names, position = wheel.names, wheel.position
            meta["filter"] = names[position] if 0 <= position < len(names) else None
        else:
            # No wheel to ask, so what the operator said is in the drawer. A
            # RASA's filter cannot be moved by anything, but the frame still has
            # to say which one it was taken through.
            fitted = str(self.config.get("camera", "fixedFilter", "") or "").strip()
            if fitted:
                meta["filter"] = fitted
        focuser = self.manager.get("focuser")
        if focuser is not None and focuser.connected:
            meta["focuserPosition"] = focuser.position
            meta["focuserTemperature"] = focuser.temperature
        mount = self.manager.get("mount")
        if mount is not None and mount.connected:
            meta["ra"] = mount.ra
            meta["dec"] = mount.dec
            with contextlib.suppress(Exception):
                meta["pierSide"] = mount.side_of_pier
        site = effective_site(self.config, self.manager)
        if site.get("latitude") is not None:
            meta["siteLatitude"] = site["latitude"]
            meta["siteLongitude"] = site["longitude"]
            meta["siteElevation"] = site.get("elevation")
        # The camera's angle on the sky: the rotator's, or the measured one.
        rotator = self.manager.get("rotator")
        if rotator is not None and rotator.connected:
            with contextlib.suppress(Exception):
                meta["positionAngle"] = float(rotator.position)
                meta["rotatorMechanical"] = rotator.mechanical_position
        else:
            angle = self.config.get("optics", "rotation", None)
            if angle is not None:
                meta["positionAngle"] = float(angle)
        # How the guider was doing when the frame started: the number a
        # pipeline grades subs by before it has measured a star.
        guider = self.manager.get("guider")
        if guider is not None and guider.connected:
            with contextlib.suppress(Exception):
                status = guider.status()
                meta["guideState"] = str(status.get("state") or "")
                meta["guideRms"] = status.get("rmsTotal")
                meta["guideRmsRa"] = status.get("rmsRa")
                meta["guideRmsDec"] = status.get("rmsDec")
        return meta

    def _filename(self, frame_type: str, exposure: float, camera,
                  metadata: dict[str, Any], ccd_temperature: float | None) -> str:
        """`M31_L_120s_-10C_0001.fits` when a target is named, otherwise the
        older type/exposure/timestamp form."""
        if not self.target_name:
            parts = [frame_type.upper()]
            if metadata.get("filter"):
                parts.append(str(metadata["filter"]))
            parts += [f"{exposure:g}s", f"g{camera.gain}", f"bin{camera.binning}",
                      datetime.now().strftime("%Y%m%d-%H%M%S")]
            return "_".join(parts) + ".fits"

        parts = [self.target_name]
        if self.panel_label:
            parts.append(self.panel_label)
        if frame_type != "light":
            parts.append(frame_type.upper())
        if metadata.get("filter"):
            parts.append(clean_target(str(metadata["filter"])))
        parts.append(f"{exposure:g}s")
        if ccd_temperature is not None:
            parts.append(f"{ccd_temperature:.0f}C")
        if camera.binning > 1:
            parts.append(f"bin{camera.binning}")
        prefix = "_".join(parts)

        sequence = self._sequences.get(prefix, 0) + 1
        # Pick up where an earlier session left off rather than overwriting it.
        if self.save_enabled:
            while (self.session_dir / f"{prefix}_{sequence:04d}.fits").exists():
                sequence += 1
        self._sequences[prefix] = sequence
        return f"{prefix}_{sequence:04d}.fits"

    def _header(self, frame: np.ndarray, exposure: float, frame_type: str, camera,
                metadata: dict[str, Any], ccd_temperature: float | None,
                filename: str) -> dict[str, Any]:
        """Everything a pipeline could want to know about this frame.

        The cards follow the names N.I.N.A., SGP and PixInsight already read,
        so a stack of these needs no translation: OBJECT, FILTER, EXPTIME,
        GAIN, OFFSET, CCD-TEMP, XBINNING for grouping; DATE-OBS at the
        shutter; OBJCTRA/OBJCTDEC in sexagesimal and RA/DEC in degrees;
        TELESCOP for which scope on a tandem rig. On top of those, what only
        this program knows: the target and panel by number, the panel's own
        framing, the collaboration it is for, whether the guider had a lock
        and how well, whether the frame follows a dither, and where the Sun
        and Moon were.  A card whose value is not known is left out rather
        than written as a lie.
        """
        from . import __version__

        started = float(metadata.get("startedAt") or time.time())
        ended = started + float(exposure)
        pixel = float(camera.pixel_size_um or 0.0) * camera.binning
        focal = self.config.get("optics", "focalLength")
        scale = (206.265 * pixel / float(focal)) if (focal and pixel) else None

        ra_hours = metadata.get("ra")
        dec_deg = metadata.get("dec")
        context = dict(self.frame_context) if frame_type == "light" else {}
        # The target's own coordinates, if the sequencer said; else the mount's.
        aim_ra = context.get("panelRa", ra_hours)
        aim_dec = context.get("panelDec", dec_deg)

        header: dict[str, Any] = {
            "IMAGETYP": (frame_type.capitalize(), "frame type"),
            "EXPTIME": (float(exposure), "exposure time in seconds"),
            "EXPOSURE": (float(exposure), "exposure time in seconds"),
            "DATE-OBS": (_utc(started), "UTC at start of exposure"),
            "DATE-END": (_utc(ended), "UTC at end of exposure"),
            "DATE-LOC": (datetime.fromtimestamp(started).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
                         "local time at start of exposure"),
            "JD": (round(astro.julian_from_timestamp(started + float(exposure) / 2.0), 6),
                   "Julian date at mid-exposure"),
            "MJD-OBS": (round(astro.julian_from_timestamp(started) - 2400000.5, 6),
                        "modified Julian date at start"),
            "XBINNING": (int(camera.binning), ""),
            "YBINNING": (int(camera.binning), ""),
            "GAIN": (int(camera.gain), ""),
            "OFFSET": (int(camera.offset), ""),
            "INSTRUME": (camera.name, "camera"),
            "XPIXSZ": (pixel, "microns, binned"),
            "YPIXSZ": (pixel, "microns, binned"),
            "CCD-TEMP": (ccd_temperature, "sensor temperature in C"),
            "SET-TEMP": (camera.setpoint, "cooler setpoint in C"),
            "BAYERPAT": (getattr(camera, "bayer_pattern", None) or None, "colour filter array"),
            "FILTER": (metadata.get("filter"), ""),
            "FOCUSPOS": (metadata.get("focuserPosition"), "focuser steps"),
            "FOCTEMP": (metadata.get("focuserTemperature"), "focuser temperature in C"),
            # For a mosaic this is the panel, not the whole object: a stacker
            # has to keep the panels apart, and a slave telescope's frames have
            # to say the same panel as the master's.
            "OBJECT": (self.object_name or self.target_name or None, "target"),
            "OBJCTRA": (_hms(aim_ra), "target RA, hours minutes seconds"),
            "OBJCTDEC": (_dms(aim_dec), "target Dec, degrees minutes seconds"),
            "RA": (None if ra_hours is None else round(float(ra_hours) * 15.0, 6),
                   "mount RA in degrees"),
            "DEC": (None if dec_deg is None else round(float(dec_deg), 6),
                    "mount Dec in degrees"),
            "PIERSIDE": ((str(metadata["pierSide"]).upper() if metadata.get("pierSide")
                          else None), "side of pier"),
            "TELESCOP": (self.telescope or None, "telescope"),
            # Focal length lets a plate solver work out the field on its own,
            # so it is worth carrying even though no driver reports it.
            "FOCALLEN": (focal, "millimetres"),
            "PIXSCALE": (None if scale is None else round(scale, 4), "arcseconds per pixel"),
            "POSANGLE": (metadata.get("positionAngle"), "camera position angle on the sky, degrees"),
            "ROTATANG": (metadata.get("rotatorMechanical"), "rotator mechanical angle"),
            "SITELAT": (metadata.get("siteLatitude"), "degrees north"),
            "SITELONG": (metadata.get("siteLongitude"), "degrees east"),
            "SITEELEV": (metadata.get("siteElevation"), "metres"),
            "OBSERVER": (self._observer(), ""),
            "SWCREATE": ("Starfront", ""),
            "SWVER": (__version__, "Starfront version"),
            "NIGHT": (self.night_name(datetime.fromtimestamp(started)), "the night this belongs to"),
            "FRAMENO": (_frame_number(filename), "frame number in this set"),
        }

        # Where it was in the sky: altitude, azimuth, airmass, hour angle,
        # and the Sun and Moon - the numbers a pipeline grades subs by.
        latitude, longitude = metadata.get("siteLatitude"), metadata.get("siteLongitude")
        if ra_hours is not None and dec_deg is not None and latitude is not None:
            with contextlib.suppress(Exception):
                when = datetime.fromtimestamp(started, tz=timezone.utc)
                lst = astro.local_sidereal_hours(float(longitude), when)
                altitude, azimuth = astro.ra_dec_to_alt_az(
                    float(ra_hours), float(dec_deg), lst, float(latitude))
                header["CENTALT"] = (round(altitude, 4), "altitude of frame centre, degrees")
                header["CENTAZ"] = (round(azimuth, 4), "azimuth of frame centre, degrees E of N")
                header["AIRMASS"] = (round(astro.airmass(altitude), 4)
                                     if astro.airmass(altitude) is not None else None, "")
                hour_angle = ((lst - float(ra_hours) + 12.0) % 24.0) - 12.0
                header["HA"] = (round(hour_angle, 5), "hour angle, hours, west positive")
                jd = astro.julian_from_timestamp(started)
                moon_ra, moon_dec = astro.moon_position(jd)
                moon_alt, _ = astro.ra_dec_to_alt_az(moon_ra, moon_dec, lst, float(latitude))
                header["MOONALT"] = (round(moon_alt, 3), "Moon altitude, degrees")
                header["MOONILLU"] = (round(astro.moon_illumination(jd), 4), "fraction of Moon lit")
                header["MOONSEP"] = (round(astro.separation_degrees(
                    float(ra_hours), float(dec_deg), moon_ra, moon_dec), 3),
                    "degrees from the Moon")
                header["SUNALT"] = (round(astro.sun_altitude(
                    started, float(latitude), float(longitude)), 3), "Sun altitude, degrees")

        if frame_type == "light":
            guide_state = metadata.get("guideState")
            header["GUIDESTA"] = (guide_state or None, "guider state at the start")
            header["GUIDING"] = ((guide_state == "Guiding") if guide_state else None,
                                 "guider had a lock at the start")
            header["GUIDERMS"] = (_rounded(metadata.get("guideRms")), "total guide RMS, arcseconds")
            header["GUIDRMSR"] = (_rounded(metadata.get("guideRmsRa")), "RA guide RMS, arcseconds")
            header["GUIDRMSD"] = (_rounded(metadata.get("guideRmsDec")), "Dec guide RMS, arcseconds")
            header["DITHERED"] = (self._dithered, "first frame after a dither")
            self._dithered = False

        # What only the program that planned the night knows.
        if context:
            header["TARGET"] = (context.get("target"), "the target, whole mosaic included")
            header["TARGETID"] = (context.get("targetId"), "Starfront target id")
            header["MOSAIC"] = (bool(context.get("mosaic")), "one panel of a mosaic")
            header["PANEL"] = (context.get("panel"), "panel number, from 1")
            header["NPANELS"] = (context.get("panels"), "panels in the mosaic")
            header["PANELPA"] = (context.get("panelAngle"), "framing angle of the panel, degrees")
            header["ENTRYID"] = (context.get("entryId"), "plan entry")
            header["PROJECT"] = (context.get("collabProjectName"), "collaboration")
            header["PROJID"] = (context.get("collabProject"), "collaboration id")
            header["COLTASK"] = (context.get("collabTask"), "collaboration task id")
        return header

    def _observer(self) -> str | None:
        who = (self.config.get("collab", "user", {}) or {}).get("name")
        return str(who) if who else None

    def _store(self, frame: np.ndarray, exposure: float, frame_type: str, camera,
               metadata: dict[str, Any]) -> ImageRecord:
        image_id = uuid.uuid4().hex[:12]
        ccd_temperature = camera.temperature
        filename = self._filename(frame_type, exposure, camera, metadata, ccd_temperature)
        header = self._header(frame, exposure, frame_type, camera, metadata,
                              ccd_temperature, filename)

        # With saving off the frame still reaches the viewer; it just never
        # touches the disk.  Framing and focusing runs would otherwise fill a
        # night's folder with subs nobody wants.
        path = fits.write(self.session_dir / filename, frame, header) if self.save_enabled else None

        record = ImageRecord(
            id=image_id, filename=filename, path=str(path) if path else None,
            saved=bool(path), timestamp=time.time(),
            frame_type=frame_type, exposure=float(exposure), binning=int(camera.binning),
            gain=int(camera.gain), offset=int(camera.offset),
            width=int(frame.shape[1]), height=int(frame.shape[0]),
            filter=metadata.get("filter"),
            object_name=self.object_name or self.target_name or None,
            telescope=self.telescope or None,
            focuser_position=metadata.get("focuserPosition"),
            focuser_temperature=metadata.get("focuserTemperature"),
            ccd_temperature=ccd_temperature,
            ra=metadata.get("ra"), dec=metadata.get("dec"),
            stats=render.statistics(frame))

        with self._lock:
            self.images.insert(0, record)
            self._by_id[record.id] = record
            self._remember(record.id, frame)
        if frame_type == "light":
            self._queue_calibration(record, camera, metadata)
        return record

    # -- calibrating what was just taken -----------------------------------
    def _queue_calibration(self, record: ImageRecord, camera,
                           metadata: dict[str, Any]) -> None:
        """Hand a fresh light frame to the calibrator, if one is wanted.

        On a worker thread rather than here: a survey burst is fifteen ten-second
        subs, and reading the frame back, dividing by a flat and writing a copy
        would add a tenth of the burst to every field for no reason at all.  The
        raw frame is already safe on disk before this is queued.
        """
        library = self.library
        if library is None or not record.saved or not record.path:
            return
        if not library.wanted_for(self.calibration_context):
            return

        want = {
            "width": record.width, "height": record.height,
            "binning": record.binning, "gain": record.gain, "offset": record.offset,
            "temperature": record.ccd_temperature,
            "filter": record.filter or "",
            "telescope": self.telescope or "",
            "camera": getattr(camera, "name", "") or "",
            "exposure": record.exposure,
        }
        if self._calibration_queue is None:
            self._calibration_queue = queue.Queue()
            self._calibration_worker = threading.Thread(
                target=self._calibration_loop, daemon=True,
                name=f"calibrate-{self.telescope or 'camera'}")
            self._calibration_worker.start()
        if self._calibration_queue.qsize() >= CALIBRATION_BACKLOG:
            self.manager.log(
                f"Calibration is {CALIBRATION_BACKLOG} frames behind; "
                f"{record.filename} was left raw", "warn")
            return
        self._calibration_queue.put((Path(record.path), want, record.filename))

    def _calibration_loop(self) -> None:
        while True:
            path, want, filename = self._calibration_queue.get()
            try:
                result = self.library.calibrate_file(path, want)
            except Exception as exc:              # noqa: BLE001 - never lose a frame over this
                self.manager.log(f"Could not calibrate {filename}: {exc}", "warn")
                continue
            self._calibration_last = {**result, "filename": filename,
                                      "at": time.time()}
            if result.get("calibrated"):
                self.manager.log(
                    f"Calibrated {filename} ({', '.join(result['steps'])})")
            else:
                # Said once per reason rather than once per frame: a whole
                # night of "no master flat" is one line worth reading.
                for kind, why in (result.get("reasons") or {}).items():
                    if self._calibration_said(kind, why):
                        self.manager.log(f"{filename} was left raw — {why}", "warn")

    def _calibration_said(self, kind: str, why: str) -> bool:
        """True the first time a given reason comes up, so the log stays short."""
        if self._calibration_said_already.get(kind) == why:
            return False
        self._calibration_said_already[kind] = why
        return True

    def calibration_status(self) -> dict[str, Any]:
        pending = (self._calibration_queue.qsize()
                   if self._calibration_queue is not None else 0)
        return {"context": self.calibration_context, "pending": pending,
                "last": self._calibration_last or None}

    # -- image access ------------------------------------------------------
    def _remember(self, image_id: str, frame: np.ndarray) -> None:
        self._cache[image_id] = frame
        self._cache.move_to_end(image_id)
        while len(self._cache) > self.MAX_CACHED_FRAMES:
            self._cache.popitem(last=False)

    def has(self, image_id: str) -> bool:
        """Whether this camera took a given frame.

        Ids are unique across the session, so with several telescopes running
        this is how a request carrying only an id finds the store that owns it.
        """
        return image_id in self._by_id

    def record(self, image_id: str) -> ImageRecord:
        found = self._by_id.get(image_id)
        if found is None:
            raise DeviceError(f"unknown image {image_id!r}")
        return found

    def frame(self, image_id: str) -> np.ndarray:
        with self._lock:
            cached = self._cache.get(image_id)
            if cached is not None:
                self._cache.move_to_end(image_id)
                return cached
        record = self.record(image_id)
        if not record.path:
            raise DeviceError(
                f"{record.filename} was captured with saving switched off and is no "
                "longer in memory")
        frame, _ = fits.read(record.path)
        with self._lock:
            self._remember(image_id, frame)
        return frame

    def listing(self, limit: int = 200) -> list[dict[str, Any]]:
        return [record.summary() for record in self.images[:limit]]
