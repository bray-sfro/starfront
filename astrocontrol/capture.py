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
from .cameragate import CameraGate
from .config import Config, data_root, effective_site
from .devices.base import CameraLostError, DeviceError, read_slot
from .devices.manager import DeviceManager
from .imaging import fits, render, wcs
from .livestack import Plan, StackError
from .pipeline import Job

FRAME_TYPES = ("light", "dark", "bias", "flat", "darkflat")

#: A night folder is named for its date: `<root>/<target>/<night>`.
_NIGHT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LIGHT_PATH_TYPES = ("light", "flat")      # frame types where the shutter/sky matters

# How many frames may be waiting to be calibrated before the queue starts
# dropping them.  Calibration runs behind the exposure loop on purpose, so it is
# allowed to fall behind — but not without end, and not silently.
CALIBRATION_BACKLOG = 24

# How much larger than the camera's field a target's live-stack canvas is made:
# a fifth again each way, so dithering, drift and a meridian flip all still
# land inside it rather than being cropped away one sub at a time.
STACK_MARGIN = 1.2

_UNSAFE = re.compile(r"[^A-Za-z0-9._+-]+")


def clean_target(name: str) -> str:
    """Make a target name safe to put in a filename, without mangling it."""
    return _UNSAFE.sub("_", (name or "").strip()).strip("_")


def capture_root(config: Any) -> Path:
    """The folder frames are written under.

    A blank or missing ``capture.rootDirectory`` is ``data_root()/captures``,
    which on EAGLE is ``C:\\Users\\primalucelab\\Starfront\\captures``. Capture
    and the auto-upload backfill both call this. A second copy of the rule
    is how a blank setting made the backfill scan nothing.
    """
    raw = ""
    if config is not None:
        try:
            raw = config.get("capture", "rootDirectory", "") or ""
        except Exception:                              # noqa: BLE001 - same fallback
            section = config.section("capture") if hasattr(config, "section") else {}
            raw = (section or {}).get("rootDirectory") or ""
    text = str(raw or "").strip()
    if text:
        return Path(text).expanduser()
    return data_root() / "captures"


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


_GENERIC_TELESCOPE = re.compile(r"^\s*telescope\s*\d*\s*$", re.IGNORECASE)


def is_generic_telescope(name: str) -> bool:
    """'Telescope 1', 'telescope', 'Telescope12' - the names nobody chose."""
    return bool(_GENERIC_TELESCOPE.match(name or ""))


def pc_name(config: Any) -> str:
    """This PC's rig name: the fleet nodeName, else the hostname."""
    import socket                                        # noqa: PLC0415
    name = ""
    try:
        name = str((config.section("server") or {}).get("nodeName") or "").strip()
    except Exception:                                    # noqa: BLE001
        name = ""
    if not name:
        try:
            name = socket.gethostname()
        except Exception:                                # noqa: BLE001
            name = ""
    return name.strip()


class CaptureService:
    # Frames captured with saving switched off exist only in this cache, so it
    # is deeper than it needs to be for saved frames alone.
    MAX_CACHED_FRAMES = 8
    #: The PC's auto-upload queue, set once by the program for every rig.
    shared_uploader: Any = None

    # A frame taken without saving outlives the cache as a reduced copy, so
    # the focus and centring frames of a night can still be looked at. The
    # viewer draws a whole frame at about this size anyway; only the sharp
    # crop at full zoom is lost, and these are frames nobody zooms into.
    PREVIEW_LONG_SIDE = 1600
    MAX_PREVIEWS = 80

    def __init__(self, manager: DeviceManager, config: Config | None = None) -> None:
        self.manager = manager
        self.config = config or Config()
        self._directory_override: Path | None = None
        self._previews: OrderedDict[str, tuple[np.ndarray, int]] = OrderedDict()
        self._start_lock = threading.Lock()
        # One mutating camera call at a time. A timeout does not cancel the
        # driver call; it quarantines it (0.2.53d).
        self.gate = CameraGate()
        # calib-layout: True when the override already names the telescope, so
        # `session_dir` must not add the per-telescope subfolder to it.
        self._directory_exact = False
        self.save_enabled = True
        # Borrowers that want frames kept off the disk - a focus sweep, a
        # centring frame, the focuser check - are counted rather than each
        # remembering and restoring `save_enabled` themselves.  Two of them
        # overlapping (a sweep still finishing as the next run's sweep began)
        # used to restore each other's answer and leave saving off for a
        # whole target's lights.
        self._save_suspended = 0
        self._save_before = True
        self._save_lock = threading.Lock()
        #: Brings this telescope's camera back on its remembered driver.  Set
        #: by whoever owns the telescopes; None means a failed frame is
        #: simply a failed frame.
        self.reconnect_camera: Any = None
        #: The telescope's CameraHealth (0.2.52e): the camera is checked
        #: before every exposure, and one that has stopped answering is
        #: reconnected rather than sent StartExposure. None: not checked.
        self.health: Any = None
        #: The gain/offset every light must be taken at (0.2.53): a callable
        #: returning {"gain", "offset", "source"} or None, set by the Rig
        #: (the telescope's profile, else what this session was running at).
        self.wanted_settings: Any = None
        #: Told the gain/offset this session runs at when the profile leaves
        #: them blank; set by the Rig.
        self.note_settings: Any = None
        #: Gain/offset a manual Capture asked for, which outrank the profile
        #: for that run only. None for the sequencer's frames.
        self._explicit_settings: dict[str, Any] | None = None
        #: The last saved light whose header GAIN/OFFSET did not match what
        #: was wanted, or that had nothing to match against; read by the
        #: warnings board. None when the last light matched.
        self.settings_mismatch: dict[str, Any] | None = None
        self._said_unset = False
        # This camera's usual gain/offset from its own past lights (0.2.53a),
        # cached per (camera, folder); see `historical_settings`.
        self._history_key: tuple | None = None
        self._history: dict[str, Any] | None = None
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
        # Master or follower. The auto-upload gate reads this; a follower
        # does not upload unless its own camera.uploadCollab is set.
        self.role = ""
        self.rig_key = ""
        self.upload_collab = None
        # Where this telescope is in the PC's list (1-based) and how many there
        # are: RIGID, and the "-N" on a fallback TELESCOP (0.2.53a).
        self.rig_index = 1
        self.rig_count = 1
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
        # The live stack every light goes into, calibrated on the way. Set by
        # the rig; None means frames are taken and nothing is stacked.
        self.stacker: Any = None
        self._stack_said_already: str = ""
        # The collaboration auto-upload queue (astrocontrol/autoupload.py),
        # shared by every rig on this PC. Set by the program; None sends nothing.
        self.uploader: Any = None
        self._sequences: dict[str, int] = {}
        self.images: list[ImageRecord] = []
        self._by_id: dict[str, ImageRecord] = {}
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._lock = threading.RLock()

        self._thread: threading.Thread | None = None
        # Bumped by `release_stuck`; a blocking capture remembers the value it
        # started under, and one that finds it changed was given up on.
        self._generation = 0
        self._owner_generation: dict[int, int] = {}
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
        return capture_root(self.config)

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
        if self._directory_override is not None and self._directory_exact:
            return base
        return base / self.subfolder if self.subfolder else base

    # -- state -------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def blocks_recovery(self) -> str | None:
        """Why a reconnect or power change must not start, or None."""
        reason = self.gate.quarantined()
        if reason:
            return reason
        if self._state in ("exposing", "downloading", "saving", "recovering") and self.busy:
            return f"a frame is {self._state}"
        return None

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
                   panel: str | None = None,
                   exact: bool = False) -> dict[str, Any]:
        """Change where frames go, or whether they are written at all.

        `exact` (calib-layout): use `directory` as is, without the
        per-telescope subfolder a multi-telescope rig normally adds.
        """
        if directory is not None:
            self._directory_exact = bool(exact) and bool(directory.strip())
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
            with self._save_lock:
                if self._save_suspended:
                    # A borrower has saving off for the moment; the choice is
                    # what comes back when it is done.
                    self._save_before = bool(save)
                else:
                    self.save_enabled = bool(save)
            self.manager.log(
                "Frames will be saved to disk" if bool(save)
                else "Frames are NOT being saved", "info" if bool(save) else "warn")
        return self.status()

    def suspend_saving(self) -> None:
        """Keep frames off the disk until the matching `resume_saving`.

        Nested calls are fine: saving comes back when the last borrower is
        done, to whatever it was before the first one started - or to what
        `set_output` was asked for in the meantime.
        """
        with self._save_lock:
            if self._save_suspended == 0:
                self._save_before = self.save_enabled
                self.save_enabled = False
            self._save_suspended += 1

    def resume_saving(self) -> None:
        with self._save_lock:
            if self._save_suspended == 0:
                return
            self._save_suspended -= 1
            if self._save_suspended == 0:
                self.save_enabled = self._save_before

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
            "exact": self._directory_exact,
            "target": self.target_name,
            "object": self.object_name,
            "panel": self.panel_label,
            "context": self.calibration_context,
        }

    def restore_output(self, state: dict[str, Any]) -> None:
        self.save_enabled = bool(state.get("save", True))
        self._directory_override = state.get("directory")
        self._directory_exact = bool(state.get("exact", False))
        self.target_name = state.get("target") or ""
        self.object_name = state.get("object") or ""
        self.panel_label = state.get("panel") or ""
        self.calibration_context = state.get("context") or ""

    # -- capture -----------------------------------------------------------
    def start(self, exposure: float, frame_type: str = "light", binning: int | None = None,
              gain: int | None = None, offset: int | None = None, loop: bool = False,
              count: int = 1) -> None:
        # One start at a time (0.2.50): `busy` is only true once the thread
        # is running, and between the check and the thread there is a driver
        # call (the settings), so two quick presses of Capture both got
        # through and two exposures fought over one camera.
        if not self._start_lock.acquire(blocking=False):
            raise DeviceError("a capture is already starting")
        try:
            self._start(exposure, frame_type, binning, gain, offset, loop, count)
        finally:
            self._start_lock.release()

    def _start(self, exposure: float, frame_type: str, binning: int | None,
               gain: int | None, offset: int | None, loop: bool, count: int) -> None:
        if self.busy:
            raise DeviceError("a capture is already running")
        frame_type = frame_type.lower()
        if frame_type not in FRAME_TYPES:
            raise DeviceError(f"frame type must be one of {', '.join(FRAME_TYPES)}")
        if exposure < 0 or exposure > 3600:
            raise DeviceError("exposure must be between 0 and 3600 seconds")

        camera = self.manager.require("camera")
        camera.set_settings(binning=binning, gain=gain, offset=offset)
        # What this run was asked for is what every one of its frames is
        # checked against; and with no gain/offset in the profile, it is what
        # a reconnect puts back.
        self._explicit_settings = ({"gain": None if gain is None else int(gain),
                                    "offset": None if offset is None else int(offset),
                                    "source": {"gain": "this capture",
                                               "offset": "this capture"}}
                                   if gain is not None or offset is not None else None)
        if self._explicit_settings is not None and callable(self.note_settings):
            with contextlib.suppress(Exception):
                self.note_settings(gain, offset, "the Camera panel")

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
        with self._start_lock:
            if self.busy:
                raise DeviceError("a capture is already running")
            camera = self.manager.require("camera")
            self._thread = threading.current_thread()
            self._owner_generation[threading.get_ident()] = self._generation
            # The sequencer's frames: the telescope's own gain/offset.
            self._explicit_settings = None
        self._abort.clear()
        self._loop = False
        self._error = None
        self._frames_taken = 0
        self._request = {"exposure": exposure, "frameType": frame_type,
                         "binning": camera.binning, "gain": camera.gain,
                         "offset": camera.offset, "count": 1}
        try:
            try:
                self._capture_one(exposure, frame_type)
            except Exception as exc:              # noqa: BLE001 - decided below
                # A focus or centring frame that the camera dropped gets the
                # same reconnect-and-one-more-try as a frame taken by hand.
                if self._abort.is_set() or not self._reconnect_and_retry(exc):
                    raise
                self._capture_one(exposure, frame_type)
            self._frames_taken = 1
        except Exception as exc:
            if not self._abandoned():
                self._error = str(exc)
            raise
        finally:
            # A thread that was given up on and comes back later must not
            # mark a newer capture as finished.
            if self._thread is threading.current_thread():
                self._state = "idle"
                self._thread = None
            self._owner_generation.pop(threading.get_ident(), None)

        if self._latest_id is None:
            raise DeviceError("the exposure produced no frame")
        return self.record(self._latest_id)

    def _abandoned(self) -> bool:
        """Was this thread's capture given up on by `release_stuck`?"""
        mine = self._owner_generation.get(threading.get_ident())
        return mine is not None and mine != self._generation

    def release_stuck(self) -> bool:
        """Free the camera from a capture whose thread is stuck in the driver.

        The thread is left where it is - it cannot be interrupted - but the
        service stops counting it as busy, so a reconnected camera can take
        the next frame. Whatever the stuck thread returns later is dropped.
        True when there was something to release.
        """
        with self._start_lock:
            thread = self._thread
            if thread is None or not thread.is_alive() \
                    or thread is threading.current_thread():
                return False
            self._generation += 1
            self._thread = None
            self._state = "idle"
        self.manager.log("Gave up on a capture stuck in the camera driver", "warn")
        return True

    def abort(self) -> None:
        self._loop = False
        self._abort.set()
        # An exposure or download still inside the driver cannot be aborted
        # from here. Sending AbortExposure would only run later, on top of it.
        stuck = self.gate.quarantined()
        if stuck:
            self.manager.log(
                "Capture cancelled. AbortExposure was not sent: " + stuck, "warn")
        else:
            camera = self.manager.get("camera")
            if camera is not None and camera.connected:
                try:
                    self.gate.call(camera.abort_exposure, 5.0, "abort")
                except Exception as exc:
                    self.manager.log(f"Abort failed: {exc}", "warn")
                    with contextlib.suppress(Exception):
                        self.release_stuck()
        self._state = "idle"
        self.manager.log("Capture aborted", "warn")

    def set_loop(self, loop: bool) -> None:
        """Turn looping off mid-run; the current frame still finishes."""
        self._loop = bool(loop)

    def _reconnect_and_retry(self, exc: Exception) -> bool:
        """After a frame failed: bring the camera back, if that is wanted.

        A camera that misses a download is usually a driver that has wedged
        or a USB link that dropped, and a disconnect-reconnect on the
        remembered driver is what puts it right.  Optional (Equipment >
        Recovery): True when the camera is back and the frame is worth
        trying once more, False when the failure should stand.
        """
        if self.reconnect_camera is None:
            return False
        if isinstance(exc, CameraLostError):
            # The camera check has already reconnected it - three times - and
            # said it is gone; one more reconnect here would only hold up Stop.
            return False
        settings = self.config.section("recovery") if hasattr(self.config, "section") else {}
        if not settings.get("enabled", True) or not settings.get("reconnectManualCaptures", True):
            return False
        # One retry, and only when the failed call has actually left the driver.
        reason = self.gate.quarantined()
        if reason:
            raw = settings.get("cameraQuarantineSeconds")
            grace = 5.0 if raw is None else float(raw)
            if not self.gate.wait_clear(grace):
                self.manager.log(
                    "Camera call still inside the driver; not reconnecting and "
                    "not starting another exposure. " + reason, "error")
                return False
        self.manager.log(f"Camera failed ({exc}); disconnecting and reconnecting it, "
                         "then trying the frame again", "warn")
        self._state = "recovering"
        camera = self.manager.get("camera")
        if camera is not None:
            with contextlib.suppress(Exception):
                camera.abort_exposure()
        with contextlib.suppress(Exception):
            self.manager.disconnect("camera")
        try:
            self.reconnect_camera()
        except Exception as again:                # noqa: BLE001 - reported below
            self.manager.log(f"Could not reconnect the camera: {again}", "error")
            return False
        camera = self.manager.get("camera")
        if camera is None or not camera.connected:
            self.manager.log("The camera did not come back", "error")
            return False
        self.manager.log("The camera is back", "success")
        return True

    def _run(self, exposure: float, frame_type: str, count: int) -> None:
        taken = 0
        try:
            while not self._abort.is_set():
                try:
                    self._capture_one(exposure, frame_type)
                except Exception as exc:          # noqa: BLE001 - decided below
                    if self._abort.is_set() or not self._reconnect_and_retry(exc):
                        raise
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

            # NotPresent: lamp only, do not command. Unknown is not that.
            # A failed read is reported and not commanded.
            action = panel.cover_prepare() if hasattr(panel, "cover_prepare") else (
                "open" if panel.has_cover and panel.cover_state in ("closed", "moving")
                else "skip")
            if action == "unreadable":
                self.manager.log(
                    "CoverState could not be read — not treating the panel as "
                    "coverless, and not sending OpenCover", "error")
                return done
            if action == "open":
                state = str(panel.cover_state)
                self.manager.log(
                    f"Opening the cover for a light frame (driver reports {state})")
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

    def _capture_one(self, exposure: float, frame_type: str,
                     again: bool = True) -> None:
        camera = self.manager.require("camera")
        # Is anybody there (0.2.52e)? A camera its driver has lost is
        # reconnected - it alone - before anything is sent to it, and one that
        # will not come back stops this frame with the plain warning, instead
        # of StartExposure putting up the driver's "between 0 and 0s" box.
        health = self.health
        if health is not None:
            health.ensure("before an exposure", self._abort.is_set)
            if self._abort.is_set():
                return
            camera = self.manager.require("camera")
        # Only frames that want sky: a flat wants the panel exactly as the
        # calibration run has just set it, and a dark wants the cover shut.
        wanted = None
        if frame_type == "light":
            self.open_light_path()
            # Gain and offset read back from the driver, and set again if the
            # camera is not at them, before every light (0.2.53). A
            # calibration frame keeps whatever its own run set.
            wanted = self._ensure_settings(camera)
        metadata = self._snapshot_rig()
        if frame_type == "light":
            # A wheel that has not settled must not write a blank FILTER, and
            # a colour camera with no wheel is OSC, not a guessed mono slot.
            metadata["filter"] = self._light_filter()

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
        try:
            start_budget = max(60.0, float(exposure) + 60.0)
            self.gate.call(
                lambda: camera.start_exposure(exposure, light=frame_type in LIGHT_PATH_TYPES),
                start_budget, "exposure", cancel=self._abort.is_set)
        except CameraLostError as exc:
            # Lost between the check and the shutter: not sent, so nothing is
            # stuck. Reconnected once here, then the frame is started again.
            if health is None or self._abort.is_set():
                raise
            if not health.heal(f"StartExposure: {exc}", self._abort.is_set):
                raise CameraLostError(health.plain(str(exc))) from exc
            camera = self.manager.require("camera")
            if frame_type == "light":
                # A reopened camera is at its driver's defaults.
                wanted = self._ensure_settings(camera)
            self._exposure_started = time.time()
            metadata["startedAt"] = self._exposure_started
            self.gate.call(
                lambda: camera.start_exposure(exposure, light=frame_type in LIGHT_PATH_TYPES),
                max(60.0, float(exposure) + 60.0), "exposure",
                cancel=self._abort.is_set)

        # Wait for the driver, with an allowance for download. A camera that
        # has not said it is done this long after it should have is treated
        # as failed, so the reconnect can happen instead of a wait for ever.
        # Each ImageReady read is itself bounded: a wedged property read must
        # not hold this loop for the driver's own three-minute COM timeout.
        try:
            hung = float(self.config.get("recovery", "hungSeconds", 120.0) or 120.0)
        except Exception:                         # noqa: BLE001 - a bare config
            hung = 120.0
        try:
            poll = float(self.config.get("recovery", "imageReadyTimeoutSeconds", 15.0)
                         or 15.0)
        except Exception:                         # noqa: BLE001 - a bare config
            poll = 15.0
        deadline = time.monotonic() + exposure + max(30.0, hung)
        while True:
            if self._abort.is_set():
                self._state = "idle"
                return
            try:
                ready = bool(self.gate.call(
                    lambda: camera.image_ready, poll, "readout",
                    cancel=self._abort.is_set))
            except DeviceError:
                self._state = "idle"
                raise
            if ready:
                break
            lost = getattr(camera, "lost_reason", None)
            if isinstance(lost, str) and lost:
                # A driver box closed on the camera's thread (driverdialogs):
                # this exposure is not coming. Reconnected, and the frame
                # started once more.
                if health is None or not again or self._abort.is_set():
                    raise CameraLostError(f"{camera.name} is not responding: {lost}")
                if not health.heal(f"during the exposure: {lost}", self._abort.is_set):
                    raise CameraLostError(health.plain(lost))
                return self._capture_one(exposure, frame_type, again=False)
            if time.monotonic() > deadline:
                self._state = "idle"
                raise DeviceError(f"the camera has not finished its exposure "
                                  f"{hung:g}s after it should have")
            time.sleep(0.05)
        if self._abort.is_set():
            self._state = "idle"
            return

        # Timed from the shutter closing to the frame being on disk. This is
        # the "everything else" half of what a frame costs, and it is the half
        # a plan used to guess at.
        download_started = time.monotonic()
        self._state = "downloading"
        # With a deadline (0.2.51): on 2026-10-14 02:24 a download never came
        # back and the program sat "running" for twelve and a half hours. A
        # native call cannot be cancelled, so an overrun is left on its own
        # thread and the frame is reported failed - which is what lets the
        # reconnect, the power box and the rest of the night happen.
        try:
            download = float(self.config.get("recovery", "downloadTimeoutSeconds", 120.0)
                             or 0.0)
        except Exception:                         # noqa: BLE001 - a bare config
            download = 120.0
        try:
            frame = self.gate.call(camera.get_image, download, "download",
                                   cancel=self._abort.is_set)
        except DeviceError:
            # Not saved, not uploaded. The driver call, if still inside the
            # camera, is quarantined and its late pixels are discarded.
            self._state = "idle"
            raise
        if self._abandoned():
            self._state = "idle"
            raise DeviceError("the frame was given up on while it downloaded")
        if frame is None or getattr(frame, "size", 0) == 0:
            self._state = "idle"
            raise DeviceError("camera returned an empty frame")

        self._state = "saving"
        record = self._store(frame, exposure, frame_type, camera, metadata)
        if frame_type == "light":
            self._check_header_settings(record, wanted)
        if self.overheads is not None:
            self.overheads.record("download", time.monotonic() - download_started)
        self._latest_id = record.id
        self.manager.log(
            f"{'Saved' if record.saved else 'Captured (not saved)'} {record.filename}  "
            f"median {record.stats.get('median')} ADU, max {record.stats.get('max')}",
            "success")

    # -- gain and offset (0.2.53) ------------------------------------------
    def _wanted(self) -> dict[str, Any] | None:
        if self._explicit_settings is not None:
            return self._explicit_settings
        if callable(self.wanted_settings):
            try:
                return self.wanted_settings()
            except Exception:                     # noqa: BLE001 - no target is no check
                return None
        return None

    @staticmethod
    def _read_back(camera: Any) -> dict[str, int | None]:
        read = getattr(camera, "read_settings", None)
        if callable(read):
            return dict(read())
        return {"gain": getattr(camera, "gain", None), "offset": getattr(camera, "offset", None)}

    def _ensure_settings(self, camera: Any) -> dict[str, Any] | None:
        """Put the camera at the wanted gain/offset and prove it, before a light.

        2026-11-04, EAGLE (twin FSQ): the telescope's profile had no gain or
        offset, so nothing was ever sent; the ASICamera2 driver happened to be
        at 100 / 50 at dusk. A failed StartExposure at 02:54 reconnected the
        camera, the reopened driver came up at 0 / 0, the restore had nothing
        to put back, and the next 36 subs were shot at gain 0 / offset 0.

        Reads first, so a camera that is already right is only read. Writes go
        through the driver wrapper's own checks (a lost ASICamera2 is never
        written to - that is the "between 0 and 0" box), after the camera
        check has already run. Returns what was wanted, for the header check.
        """
        wanted = self._wanted()
        try:
            actual = self._read_back(camera)
        except CameraLostError:
            raise
        except Exception as exc:                  # noqa: BLE001 - said, not fatal
            self.manager.log(f"Could not read the camera's gain/offset: {exc}", "warn")
            actual = {"gain": None, "offset": None}
        if wanted is None:
            # Nothing set anywhere: hold what the camera is at now, so a
            # reconnect later tonight puts it back rather than 0 / 0.
            if callable(self.note_settings) and (actual.get("gain") is not None
                                                 or actual.get("offset") is not None):
                with contextlib.suppress(Exception):
                    self.note_settings(actual.get("gain"), actual.get("offset"),
                                       "the camera at the first light frame")
            if not self._said_unset:
                self._said_unset = True
                self.manager.log(
                    f"No gain/offset is set for {self.telescope or 'this telescope'}, "
                    "and no past lights of this camera to take them from: "
                    f"holding the camera's gain {actual.get('gain')} / offset "
                    f"{actual.get('offset')} for this session. Set them under "
                    "Equipment > Camera so every frame matches the calibration "
                    "library.", "warn")
            return None
        fix = {key: int(wanted[key]) for key in ("gain", "offset")
               if wanted.get(key) is not None and actual.get(key) != int(wanted[key])}
        if not fix:
            return wanted
        source = wanted.get("source") or {}
        self.manager.log(
            "Camera " + ", ".join(f"{key} was {actual.get(key)}" for key in fix)
            + "; setting " + ", ".join(f"{key} {value} ({source.get(key, 'wanted')})"
                                       for key, value in fix.items())
            + " before the light frame", "warn")
        try:
            camera.set_settings(gain=fix.get("gain"), offset=fix.get("offset"))
        except CameraLostError:
            raise
        except Exception as exc:                  # noqa: BLE001 - flagged below
            self.manager.log(f"Could not set the camera's gain/offset: {exc}", "error")
        try:
            after = self._read_back(camera)
        except CameraLostError:
            raise
        except Exception:                         # noqa: BLE001 - unreadable
            after = {"gain": None, "offset": None}
        still = {key: after.get(key) for key, value in fix.items()
                 if after.get(key) is not None and after.get(key) != value}
        if still:
            self.manager.log(
                "The camera did not take the gain/offset: "
                + ", ".join(f"{key} reads {value}, wanted {fix[key]}"
                            for key, value in still.items())
                + "; the frame is taken anyway and flagged", "error")
        return wanted

    def _check_header_settings(self, record: Any, wanted: dict[str, Any] | None) -> None:
        """Say so when a saved light's GAIN/OFFSET is not the profile's."""
        if wanted is None:
            self.settings_mismatch = {"kind": "unset", "file": record.filename,
                                      "gain": record.gain, "offset": record.offset,
                                      "at": time.time()}
            return
        wrong = {key: {"header": getattr(record, key), "wanted": int(wanted[key]),
                       "source": (wanted.get("source") or {}).get(key, "")}
                 for key in ("gain", "offset")
                 if wanted.get(key) is not None and getattr(record, key) != int(wanted[key])}
        if not wrong:
            sources = wanted.get("source") or {}
            held = [key for key in ("gain", "offset")
                    if wanted.get(key) is None or sources.get(key) != "profile"]
            # Right, but only because this session remembered it: still worth
            # a notice until the profile says it for itself.
            history = wanted.get("history") or {}
            from_history = bool(held) and all(sources.get(k) == "history" for k in held)
            self.settings_mismatch = None if not held or self._explicit_settings else {
                "kind": "history" if from_history else "unset",
                "file": record.filename, "gain": record.gain,
                "offset": record.offset, "at": time.time(), "keys": held,
                "count": history.get("count"), "lights": history.get("lights")}
            return
        self.settings_mismatch = {"kind": "mismatch", "file": record.filename,
                                  "fields": wrong, "at": time.time(),
                                  "saved": bool(record.saved)}
        self.manager.log(
            f"{record.filename} was taken at "
            + ", ".join(f"{key.upper()} {w['header']}" for key, w in wrong.items())
            + " but this telescope's settings say "
            + ", ".join(f"{key} {w['wanted']}" for key, w in wrong.items())
            + " - it will not match the calibration frames", "warn")

    def _colour_camera(self) -> bool:
        camera = self.manager.get("camera")
        if camera is None:
            return bool(self.config.get("camera", "colour", False))
        if getattr(camera, "bayer_pattern", None):
            return True
        return bool(self.config.get("camera", "colour", False))

    def _light_filter(self) -> str | None:
        """The FILTER a light may wear. Never blank, never guessed.

        A connected wheel must be reporting one named slot. A colour camera
        with no wheel is OSC-RGB, which is what the collab server calls a
        one-shot colour frame. A mono camera with no wheel keeps the filter
        fitted in the drawer, or no card at all when nothing is fitted.
        """
        wheel = self.manager.get("filterwheel")
        if wheel is not None and getattr(wheel, "connected", False):
            names = [str(name) for name in (wheel.names or [])]
            slot, said_moving = read_slot(wheel)
            if slot is None or not (0 <= slot < len(names)) \
                    or not names[slot].strip() or names[slot] == "-":
                raise DeviceError(
                    "the filter wheel has not settled on a known filter; "
                    "the light was not taken and FILTER was not guessed")
            if said_moving:
                self.manager.log(
                    f"The filter wheel reported moving, but the position read "
                    f"is {names[slot]}; using that", "warn")
            return names[slot]
        if self._colour_camera():
            fitted = str(self.config.get("camera", "fixedFilter", "") or "").strip()
            if fitted.upper().startswith("OSC"):
                return fitted
            return "OSC-RGB"
        fitted = str(self.config.get("camera", "fixedFilter", "") or "").strip()
        return fitted or None

    def upload_identity(self) -> dict[str, Any]:
        """Who shot this frame, for the upload gate. Not the folder name."""
        camera = self.manager.get("camera")
        bayer = ""
        camera_name = ""
        if camera is not None:
            bayer = str(getattr(camera, "bayer_pattern", "") or "")
            camera_name = str(getattr(camera, "name", "") or "")
        colour = self._colour_camera()
        return {
            "role": self.role or "",
            "rigId": self.rig_key or "",
            "name": self.telescope or "",
            "camera": camera_name,
            "bayer": bayer,
            "colour": colour,
            "mono": bool(camera_name) and not colour,
            "upload": self.upload_collab,
        }

    def _snapshot_rig(self) -> dict[str, Any]:
        """Read the rest of the rig so the FITS header describes this frame."""
        meta: dict[str, Any] = {}
        wheel = self.manager.get("filterwheel")
        if wheel is not None and wheel.connected:
            names = list(wheel.names or [])
            slot, _said = read_slot(wheel)
            meta["filter"] = names[slot] if slot is not None and 0 <= slot < len(names) else None
        else:
            # No wheel to ask, so what the operator said is in the drawer. A
            # RASA's filter cannot be moved by anything, but the frame still has
            # to say which one it was taken through.
            fitted = str(self.config.get("camera", "fixedFilter", "") or "").strip()
            if fitted:
                meta["filter"] = fitted
            elif self._colour_camera():
                # A one-shot colour camera with nothing in front of it. The
                # collab server calls that OSC-RGB. "L" would park the frame
                # waiting for luminance flats.
                meta["filter"] = "OSC-RGB"
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
            "TELESCOP": (self.header_telescope() or None, "telescope"),
            "OBSERVAT": (self._observatory(), "observatory"),
            "RIGNAME": (pc_name(self.config) or None, "Starfront PC / rig name"),
            "RIGID": (self.rig_id() or None, "stable rig id: PC-telescope index"),
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

    # -- the camera's usual gain/offset (0.2.53a) ---------------------------
    #: How many of the newest lights are read. Headers only, a few KB each.
    HISTORY_FILES = 300

    def historical_settings(self, instrument: str) -> dict[str, Any] | None:
        """The gain/offset this camera has shot most of its lights at.

        The mode (gain, offset) pair across the newest HISTORY_FILES lights in
        this telescope's own capture folders (`<root>/<target>/<night>[/<rig>]`)
        whose INSTRUME is this camera. TELESCOP is not used: it changed in
        0.2.53a. 0 / 0 frames - the ASICamera2 reopen bug - are left out when
        any other value exists. Cached with its source and count; None when
        there is no history.
        """
        name = (instrument or "").strip()
        key = (name.lower(), str(self.root_dir), self.subfolder or "")
        if key == self._history_key:
            return self._history
        result: dict[str, Any] | None = None
        try:
            result = self._scan_history(name)
        except Exception as exc:                          # noqa: BLE001 - no history
            self.manager.log(f"Could not read past lights for the usual gain/offset: {exc}",
                             "warn")
        self._history_key, self._history = key, result
        if result:
            self.manager.log(
                f"{self.telescope or 'This telescope'}: no gain/offset set; using historical "
                f"gain {result['gain']} / offset {result['offset']} (from {result['count']} "
                f"of {result['lights']} lights of {name or 'this camera'} in {result['folder']}"
                + (f"; {result['zeroes']} 0/0 frames ignored" if result["zeroes"] else "")
                + ")", "info")
        return result

    def _scan_history(self, instrument: str) -> dict[str, Any] | None:
        from collections import Counter
        root = Path(self.root_dir)
        if not root.is_dir():
            return None
        pattern = (f"*/*/{self.subfolder}/*.fit*" if self.subfolder else "*/*/*.fit*")
        files = []
        for path in root.glob(pattern):
            with contextlib.suppress(OSError):
                files.append((path.stat().st_mtime, path))
        files.sort(key=lambda item: item[0], reverse=True)
        pairs: list[tuple[int, int]] = []
        for _, path in files[: self.HISTORY_FILES]:
            try:
                header = fits.read_header(path)
            except Exception:                             # noqa: BLE001 - skip a bad file
                continue
            if not str(header.get("IMAGETYP") or "").strip().lower().startswith("light"):
                continue
            if instrument and str(header.get("INSTRUME") or "").strip().lower() \
                    != instrument.lower():
                continue
            try:
                pairs.append((int(header["GAIN"]), int(header["OFFSET"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not pairs:
            return None
        real = [p for p in pairs if p != (0, 0)]
        zeroes = len(pairs) - len(real)
        use = real or pairs
        (gain, offset), count = Counter(use).most_common(1)[0]   # newest wins a tie
        return {"gain": gain, "offset": offset, "count": count, "lights": len(pairs),
                "zeroes": zeroes if real else 0, "folder": str(root),
                "source": f"history of {count} lights"}

    # -- which telescope, for the header (0.2.53a) -------------------------
    def header_telescope(self) -> str:
        """TELESCOP: the configured scope name, never a generic "Telescope N".

        Processing tells rigs apart by this card, and every rig that was never
        renamed said "Telescope 1". In order: the telescope's own
        `optics.telescopeName`; its name in Starfront unless that is generic;
        this PC's name (with "-N" when it carries more than one telescope).
        """
        configured = ""
        try:
            configured = str(self.config.get("optics", "telescopeName", "") or "").strip()
        except Exception:                                # noqa: BLE001
            configured = ""
        if configured:
            return configured[:68]
        name = (self.telescope or "").strip()
        if name and not is_generic_telescope(name):
            return name[:68]
        pc = pc_name(self.config)
        if not pc:
            return name
        return (f"{pc}-{self.rig_index}" if self.rig_count > 1 else pc)[:68]

    def rig_id(self) -> str:
        pc = pc_name(self.config)
        return f"{pc}-{int(self.rig_index)}"[:68] if pc else ""

    def telescope_names(self) -> list[str]:
        """Every name this telescope's calibration may carry: the TELESCOP of
        frames from 0.2.53a on, and its Starfront name from before."""
        names = [n for n in (self.header_telescope(), self.telescope or "") if n]
        return list(dict.fromkeys(names))

    def _observatory(self) -> str | None:
        try:
            value = self.config.get("site", "observatory", "Starfront Observatories")
        except Exception:                                # noqa: BLE001
            value = "Starfront Observatories"
        return str(value).strip()[:68] or None

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
            if not record.saved:
                self._keep_preview(record.id, frame)
        if frame_type == "light":
            self._queue_calibration(record, camera, metadata)
            self._queue_stack(record, frame, header, camera)
            self._queue_upload(record, camera)
        return record

    def _queue_upload(self, record: ImageRecord, camera) -> None:
        """Put a saved collaboration light on the auto-upload queue, with the
        masters that calibrate it. Only appends to a list: the sending is the
        uploader's own thread, in the daytime, and never touches the file."""
        uploader = self.uploader or CaptureService.shared_uploader
        if uploader is None or not record.saved or not record.path:
            return
        try:
            identity = None
            if hasattr(self, "upload_identity"):
                identity = self.upload_identity()
            uploader.light_saved(record.path, dict(self.frame_context or {}),
                                 self.telescope or "", self._calibration_want(record, camera),
                                 night=self.night_name(), filter_name=record.filter or "",
                                 identity=identity)
        except Exception:                               # noqa: BLE001 - never stops a night
            pass

    # -- stacking what was just taken --------------------------------------
    def _calibration_want(self, record: ImageRecord, camera) -> dict[str, Any]:
        """What a master has to match to calibrate this frame."""
        return {
            "width": record.width, "height": record.height,
            "binning": record.binning, "gain": record.gain, "offset": record.offset,
            "temperature": record.ccd_temperature,
            "filter": record.filter or "",
            "telescope": self.telescope or "",
            # 0.2.53a: TELESCOP may now differ from the Starfront name, so a
            # master carrying either is this telescope's own.
            "telescopes": self.telescope_names(),
            "camera": getattr(camera, "name", "") or "",
            "exposure": record.exposure,
        }

    def _queue_stack(self, record: ImageRecord, frame: np.ndarray,
                     header: dict[str, Any], camera) -> None:
        """Hand a fresh light to the live stack.

        One stack per target per filter per night, on a canvas a fifth larger
        than the camera's field, cut from the first frame's own geometry and
        then kept. The frame is calibrated as it goes in, with whatever
        masters the library has for it, whether or not a calibrated copy is
        wanted on disk; the raw frame is never touched. Nothing here can hold
        up the next exposure: the work happens on the stacker's own thread.
        """
        stacker = self.stacker
        if stacker is None or not stacker.enabled:
            return
        settings = stacker.settings()
        if not self.frame_context.get("collabProject") and \
                not settings.get("stackOwnTargets", True):
            return

        project = (self.object_name or self.target_name or "").strip() or "untitled"
        filter_name = (record.filter or "").strip() or "none"
        stacker.begin_night(self.night_name())

        plan = stacker.existing_plan(project, filter_name)
        if plan is None:
            try:
                solution = wcs.from_header(header, record.width, record.height,
                                           record.binning)
            except wcs.WcsError as exc:
                self._stack_say(f"{record.filename} was not stacked — {exc}")
                return
            across, down = solution.field
            ra, dec = solution.centre
            plan = Plan(ra=ra, dec=dec, width=across * STACK_MARGIN,
                        height=down * STACK_MARGIN, scale=solution.scale,
                        rotation=solution.rotation,
                        maxPixels=int(settings.get("maxPixels") or 3000))

        want = self._calibration_want(record, camera)
        library = self.library
        path = Path(record.path) if record.path else None
        # A frame that was saved is read back when its turn comes; one that
        # was not is the only copy, so it is held.
        held = None if path is not None else frame
        require = not settings.get("stackUncalibrated", True)

        def load() -> tuple[np.ndarray, dict[str, Any], dict[str, Any]]:
            pixels, cards = (fits.read(path) if path is not None
                             else (held, header))
            if library is None:
                info: dict[str, Any] = {
                    "calibrated": False, "steps": [],
                    "reasons": {"library": "there is no calibration library"}}
            else:
                pixels, info = library.calibrate_array(pixels, want)
            if require and not info.get("calibrated"):
                reasons = "; ".join(str(why) for why in
                                    (info.get("reasons") or {}).values())
                raise StackError(f"no matching masters ({reasons})")
            return pixels, cards, info

        # The tile's identity comes from the file name, so a saved frame is
        # recognised if it is ever offered twice. An unsaved one has no file,
        # and its name alone can repeat within a night, so its record id is
        # what makes it one frame rather than a repeat of the last.
        job = Job(path or Path(f"{record.filename}#{record.id}"), project, filter_name, plan,
                  night=self.night_name(), agent="", seconds=record.exposure,
                  task=str(self.frame_context.get("collabTask") or ""),
                  telescope=self.telescope or "", loader=load)
        stacker.submit(job)

    def _stack_say(self, message: str) -> None:
        """Once per reason: a night of the same complaint is one line."""
        if self._stack_said_already == message:
            return
        self._stack_said_already = message
        self.manager.log(message, "warn")

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

        want = self._calibration_want(record, camera)
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

    def _keep_preview(self, image_id: str, frame: np.ndarray) -> None:
        """A reduced copy of an unsaved frame, kept after the full one is let go."""
        long_side = max(frame.shape[0], frame.shape[1])
        factor = max(1, -(-long_side // self.PREVIEW_LONG_SIDE))
        self._previews[image_id] = (render.downsample(frame, factor), factor)
        self._previews.move_to_end(image_id)
        while len(self._previews) > self.MAX_PREVIEWS:
            self._previews.popitem(last=False)

    def viewable(self, image_id: str) -> bool:
        """Whether the viewer can be given something for this frame."""
        record = self._by_id.get(image_id)
        if record is None:
            return False
        if record.path:
            return True
        with self._lock:
            return image_id in self._cache or image_id in self._previews

    def frame_for_view(self, image_id: str) -> tuple[np.ndarray, int]:
        """The frame for looking at, and how many times smaller than the
        real one it is: 1 for the frame itself, more for the reduced copy
        that stands in once an unsaved frame has left the cache."""
        with self._lock:
            cached = self._cache.get(image_id)
            if cached is not None:
                self._cache.move_to_end(image_id)
                return cached, 1
            record = self._by_id.get(image_id)
            if record is not None and not record.path:
                preview = self._previews.get(image_id)
                if preview is not None:
                    return preview
        return self.frame(image_id), 1

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
        return [{**record.summary(), "viewable": self.viewable(record.id)}
                for record in self.images[:limit]]

    # -- the night before --------------------------------------------------
    def recall_recent(self, limit: int = 100) -> int:
        """Bring the most recent night's subs back into the list.

        The list of frames is memory: it empties when the program closes, and
        the morning after a run the viewer opened on nothing at all however
        many subs were on disk. So at startup the newest night folder under the
        capture root is read back - headers only, the pixels come off the file
        when a frame is actually looked at - and its saved lights take their
        place in the list as if they had just been shot. Only lights: a focus
        or centring frame is never saved, and a flat or dark is not what
        anybody wants to open the morning on.

        Returns how many frames came back.
        """
        root = self.root_dir
        if not root.is_dir():
            return 0
        nights: list[Path] = []
        try:
            for target_dir in root.iterdir():
                if not target_dir.is_dir():
                    continue
                for night_dir in target_dir.iterdir():
                    if night_dir.is_dir() and _NIGHT_NAME.match(night_dir.name):
                        nights.append(night_dir)
        except OSError:
            return 0
        if not nights:
            return 0
        newest = max(night.name for night in nights)
        folders = [night for night in nights if night.name == newest]
        if self.subfolder:
            folders = [night / self.subfolder for night in folders
                       if (night / self.subfolder).is_dir()]

        files: list[Path] = []
        for folder in folders:
            with contextlib.suppress(OSError):
                files.extend(path for path in folder.iterdir()
                             if path.is_file() and path.suffix.lower() in (".fits", ".fit"))
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)

        recalled: list[ImageRecord] = []
        for path in files[:limit]:
            record = self._recall_one(path)
            if record is not None:
                recalled.append(record)
        if not recalled:
            return 0
        with self._lock:
            known = {record.path for record in self.images if record.path}
            fresh = [record for record in recalled if record.path not in known]
            for record in fresh:
                self.images.append(record)
                self._by_id[record.id] = record
            self.images.sort(key=lambda record: record.timestamp, reverse=True)
        return len(fresh)

    @staticmethod
    def _recall_one(path: Path) -> ImageRecord | None:
        """One saved light as a list entry, from its header. None if it is not a light."""
        try:
            header = fits.read_header(path)
        except Exception:
            return None
        kind = str(header.get("IMAGETYP") or "").strip().lower()
        if "light" not in kind:
            return None

        def text(key: str) -> str | None:
            value = header.get(key)
            if value is None:
                return None
            value = str(value).strip()
            return value if value and value != "None" else None

        def number(key: str) -> float | None:
            value = header.get(key)
            if value is None or isinstance(value, bool):
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        try:
            taken = path.stat().st_mtime
        except OSError:
            taken = time.time()
        when = text("DATE-OBS")
        if when:
            for form in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
                try:
                    taken = datetime.strptime(when, form).replace(
                        tzinfo=timezone.utc).timestamp()
                    break
                except ValueError:
                    continue
        ra = number("RA")
        focus = number("FOCUSPOS")
        return ImageRecord(
            id=uuid.uuid4().hex[:12], filename=path.name, path=str(path), saved=True,
            timestamp=taken, frame_type="light",
            exposure=number("EXPTIME") or number("EXPOSURE") or 0.0,
            binning=int(number("XBINNING") or 1),
            gain=int(number("GAIN") or 0), offset=int(number("OFFSET") or 0),
            width=int(number("NAXIS1") or 0), height=int(number("NAXIS2") or 0),
            filter=text("FILTER"), object_name=text("OBJECT"), telescope=text("TELESCOP"),
            focuser_position=None if focus is None else int(focus),
            focuser_temperature=number("FOCTEMP"), ccd_temperature=number("CCD-TEMP"),
            ra=None if ra is None else ra / 15.0, dec=number("DEC"),
            stats={})
