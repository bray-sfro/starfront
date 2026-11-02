"""Plate solving with ASTAP.

ASTAP is a command line solver: it is handed a FITS file plus a hint about
where the telescope thinks it is pointing, and writes the answer to a small
`.ini` file beside its output path.  Everything here is that conversation, plus
the two things a solved position is actually good for at the eyepiece:

  * **sync**   - tell the mount where it really is, once.
  * **centre** - solve, sync, slew back to the target and solve again, until the
    target is under the crosshair.  This is what makes "go to M51" land M51 in
    the middle of the frame rather than somewhere in the general area.

Solves run on their own thread; the UI polls `status()` through the status
socket, exactly like a capture.
"""

from __future__ import annotations

import contextlib
import math
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from . import astro, astrometry
from .capture import CaptureService
from .config import Config
from .devices.base import DeviceError
from .devices.manager import DeviceManager
from .imaging import fits

# Where ASTAP puts itself when nobody tells the installer otherwise.
_CANDIDATES = (
    r"C:\Program Files\astap\astap.exe",
    r"C:\Program Files (x86)\astap\astap.exe",
    r"C:\astap\astap.exe",
    "/opt/astap/astap",
    "/usr/local/bin/astap",
    "/usr/bin/astap",
)

# ASTAP's own exit codes, which say considerably more than "it failed".
_EXIT_MESSAGES = {
    1: "no solution found — try a wider search radius or a longer exposure",
    2: "no star database found; install the ASTAP H17 or D50 database",
    16: "ASTAP reported an error reading the image",
    32: "no image file was supplied to ASTAP",
    33: "ASTAP could not read the image file",
}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class SolveResult:
    ra: float                       # hours, J2000
    dec: float                      # degrees, J2000
    rotation: float                 # degrees; position angle of the frame
    scale: float                    # arcseconds per pixel
    fov_width: float                # degrees
    fov_height: float               # degrees
    flipped: bool                   # mirror image (west left rather than right)
    image_id: str | None = None
    filename: str | None = None
    seconds: float = 0.0
    separation: float | None = None  # arcminutes from where the mount thought it was
    # Which of the three ways produced this, so the answer can say so: a
    # solution read out of a header and one that cost four minutes at
    # astrometry.net are not the same news.
    method: str = "astap"
    detail: str = ""

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["fovWidth"] = data.pop("fov_width")
        data["fovHeight"] = data.pop("fov_height")
        data["imageId"] = data.pop("image_id")
        return data


@dataclass
class _FileRecord:
    """Enough of a capture record to solve a file that was never captured here.

    `_run_astap` and `_interpret` want a frame's size, binning and rough
    pointing.  A FITS on disk has all of that in its header, whatever wrote it,
    so this reads it out rather than making the solver care where a frame
    came from.
    """

    id: str
    filename: str
    width: int
    height: int
    binning: int
    ra: float | None
    dec: float | None
    exposure: float = 0.0
    path: str = ""

    @staticmethod
    def _angle(value: Any, hours: bool) -> float | None:
        """A header angle, whether it is a number or sexagesimal text.

        `OBJCTRA` is conventionally `'12 34 56.7'` in hours and `OBJCTDEC`
        `'+41 16 09'` in degrees, but plenty of programs write a bare float
        instead, and some write degrees where the convention says hours.
        """
        if value is None:
            return None
        if isinstance(value, (int, float)):
            angle = float(value)
        else:
            text = str(value).strip().replace(":", " ")
            try:
                parts = [float(p) for p in text.split() if p]
            except ValueError:
                return None
            if not parts:
                return None
            sign = -1.0 if text.lstrip().startswith("-") else 1.0
            magnitude = abs(parts[0])
            if len(parts) > 1:
                magnitude += abs(parts[1]) / 60.0
            if len(parts) > 2:
                magnitude += abs(parts[2]) / 3600.0
            angle = sign * magnitude
        # An "RA in hours" greater than 24 cannot be hours, so it was written in
        # degrees. Under 24 it is genuinely ambiguous and the keyword's own
        # meaning wins — guessing there would turn a legitimate 10h into 40m.
        if hours and abs(angle) > 24.0:
            angle /= 15.0
        return angle

    @classmethod
    def from_header(cls, path: Path, header: dict[str, Any]) -> "_FileRecord":
        ra = cls._angle(header.get("OBJCTRA", header.get("RA")), hours=True)
        dec = cls._angle(header.get("OBJCTDEC", header.get("DEC")), hours=False)
        # A frame that has already been solved carries its answer in the WCS,
        # which is a far better hint than where the mount thought it was.
        if header.get("CRVAL1") is not None and header.get("CRVAL2") is not None:
            with contextlib.suppress(TypeError, ValueError):
                ra = float(header["CRVAL1"]) / 15.0
                dec = float(header["CRVAL2"])
        return cls(
            id=f"file:{path.name}",
            filename=path.name,
            width=int(header.get("NAXIS1", 0) or 0),
            height=int(header.get("NAXIS2", 0) or 0),
            binning=int(header.get("XBINNING", 1) or 1),
            ra=ra, dec=dec,
            exposure=float(header.get("EXPTIME", 0.0) or 0.0),
            path=str(path),
        )


class Solver:
    """Runs ASTAP, and the sync/centre routines built on top of it."""

    def __init__(self, manager: DeviceManager, capture: CaptureService,
                 config: Config) -> None:
        self.manager = manager
        self.capture = capture
        self.config = config

        self._thread: threading.Thread | None = None
        self._abort = threading.Event()
        self._process: subprocess.Popen | None = None
        self._state = "idle"
        self._message = ""
        self._error: str | None = None
        self._attempt = 0
        self._attempts = 0
        self._result: SolveResult | None = None
        # Why the camera angle was last declined, so each reason is said once
        # rather than on every pointing check all night.
        self._angle_reasons: set[str] = set()
        self._lock = threading.RLock()
        # (configured path, expires at, what was found)
        self._executable_cache: tuple[str, float, Path | None] | None = None

    # -- discovery ---------------------------------------------------------
    # Looking for ASTAP means walking PATH and stat-ing a handful of candidate
    # paths - a few milliseconds. That is nothing once, but `status()` is on the
    # status socket's path several times a second and for every telescope, where
    # it turns into real contention. The answer only changes when someone
    # installs ASTAP or edits the setting, so a short cache costs nothing.
    _LOOKUP_TTL = 5.0

    def executable(self) -> Path | None:
        """The ASTAP binary, from the settings file or the usual places."""
        configured = (self.config.get("solver", "astapPath", "") or "").strip()
        now = time.monotonic()
        cached = self._executable_cache
        if cached is not None and cached[0] == configured and now < cached[1]:
            return cached[2]
        found = self._look_for_executable(configured)
        self._executable_cache = (configured, now + self._LOOKUP_TTL, found)
        return found

    @staticmethod
    def _look_for_executable(configured: str) -> Path | None:
        if configured:
            path = Path(configured).expanduser()
            return path if path.is_file() else None
        for name in ("astap", "astap_cli"):
            found = shutil.which(name)
            if found:
                return Path(found)
        for candidate in _CANDIDATES:
            path = Path(candidate)
            if path.is_file():
                return path
        return None

    # -- state -------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        executable = self.executable()
        with self._lock:
            return {
                "available": executable is not None,
                "executable": str(executable) if executable else None,
                "busy": self.busy,
                "state": self._state,
                "message": self._message,
                "error": self._error,
                "attempt": self._attempt,
                "attempts": self._attempts,
                "result": self._result.payload() if self._result else None,
            }

    def _set(self, state: str, message: str = "") -> None:
        with self._lock:
            self._state = state
            self._message = message

    # -- entry points ------------------------------------------------------
    def start(self, mode: str = "solve", image_id: str | None = None,
              ra: float | None = None, dec: float | None = None,
              rotation: float | None = None) -> None:
        """Kick off a job. `mode` is one of solve, sync, center or goto."""
        if mode not in ("solve", "sync", "center", "goto"):
            raise DeviceError(f"unknown solve mode {mode!r}")
        if self.busy:
            raise DeviceError("a plate solve is already running")
        if mode != "goto" and self.executable() is None:
            raise DeviceError(
                "ASTAP was not found. Install it, or set its path in Site & Optics.")
        if mode in ("sync", "center", "goto"):
            mount = self.manager.require("mount")
            if mode == "sync" and not mount.can_sync:
                raise DeviceError("this mount cannot be synced")
        if mode == "goto" and ra is None:
            raise DeviceError("a framing needs coordinates to go to")

        self._abort.clear()
        with self._lock:
            self._error = None
            self._result = None
            self._attempt = 0
            self._attempts = (int(self.config.get("solver", "attempts", 3))
                              if mode in ("center", "goto") else 1)
        self._thread = threading.Thread(
            target=self._run, args=(mode, image_id, ra, dec, rotation),
            daemon=True, name="solver")
        self._thread.start()

    def abort(self) -> None:
        running = self.busy
        self._abort.set()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        # Only when there was something to abort. Shutdown aborts every service
        # unconditionally, and a warning per quit buries the one that matters in
        # a log that gets read precisely when a night has gone wrong.
        if running:
            self.manager.log("Plate solve aborted", "warn")

    # -- the job -----------------------------------------------------------
    def _run(self, mode: str, image_id: str | None, ra: float | None,
             dec: float | None, rotation: float | None = None) -> None:
        try:
            if mode == "goto":
                self._goto(ra, dec, rotation)
            elif mode == "center":
                self._center(ra, dec, rotation)
            else:
                result = self._solve_image(image_id)
                self._report(result)
                if mode == "sync":
                    self._sync(result)
        except Exception as exc:                # noqa: BLE001 - shown to the operator
            with self._lock:
                self._error = str(exc)
            self.manager.log(f"Plate solve failed: {exc}", "error")
        finally:
            self._set("idle")

    def _report(self, result: SolveResult) -> None:
        with self._lock:
            self._result = result
        self.manager.log(
            f"Solved {result.filename or 'frame'}: RA {result.ra:.5f}h  "
            f"Dec {result.dec:+.4f}°  {result.scale:.2f}\"/px  "
            f"rot {result.rotation:+.2f}°  ({result.seconds:.1f}s)", "success")
        self._remember_angle(result)

    def _remember_angle(self, result: SolveResult) -> None:
        """Keep the stored camera angle in step with what the sky says it is.

        The angle matters to everything that has to know what shape of sky the
        sensor covers — mosaic panels, survey footprints, and the all-sky grid,
        which cannot tile at all without it.  A solve measures it exactly, so
        there is no reason for the number in the settings to be the one somebody
        typed in six months ago.

        Not done when a rotator is connected: there the angle is commanded per
        target rather than fixed by how the camera sits in the focuser, and
        writing each target's angle into the setting would make it meaningless.

        Every path that declines says so, once per reason. A setting that
        silently does nothing is indistinguishable from one that is broken, and
        "the angle never updates" is not a report anybody can act on without
        knowing which of these three it was.
        """
        angle = round(float(result.rotation) % 360.0, 3)

        if not self.config.get("optics", "angleFromSolve", True):
            self._angle_note("angleFromSolve",
                             f"Plate solve measured {angle:.2f}°, but 'set the "
                             "camera angle from plate solves' is switched off "
                             "in Site & Optics")
            return
        rotator = self.manager.get("rotator")
        if rotator is not None and rotator.connected:
            self._angle_note("rotator",
                             f"Plate solve measured {angle:.2f}°, but a rotator "
                             "is connected, so the camera angle is commanded per "
                             "target rather than stored")
            return

        stored = self.config.get("optics", "rotation", None)
        try:
            if stored is not None:
                # The sensor is a rectangle: 95 and 275 degrees are the same
                # footprint, and the solver's choice between them is
                # arbitrary. Keep the half-turn the settings already use, so
                # a mosaic laid at 268 is not re-laid at 95 for a camera that
                # has not moved.
                angle = round(astro.same_half_turn(angle, float(stored)), 3)
                if abs(((float(stored) - angle + 180) % 360) - 180) < 0.05:
                    return                      # already saying the same thing
        except (TypeError, ValueError):
            pass
        self.config.update("optics", {"rotation": angle})
        self._angle_reasons.clear()             # it works; say so again if it stops
        self.manager.log(f"Camera angle set to {angle:.2f}° from the plate solve "
                         f"(was {stored if stored is None else round(float(stored), 2)}°)")

    def _angle_note(self, reason: str, message: str) -> None:
        """Say why the angle was not taken — once per reason, not once per frame.

        A pointing check runs every tenth sub, so an unguarded warning here would
        be a hundred identical lines by dawn in the log somebody reads precisely
        when the night has gone wrong.
        """
        if reason in self._angle_reasons:
            return
        self._angle_reasons.add(reason)
        self.manager.log(message, "warn")

    def adopt_angle(self, result: SolveResult, width: int, height: int) -> None:
        """Take a solved file's angle as this telescope's, if it is our frame.

        `solve_file` answers a question about a file on disk, and that file may
        have come off somebody else's rig entirely — a reference frame from a
        friend's scope says nothing about how *our* camera sits in the focuser,
        and writing its angle into the settings would be worse than leaving them
        alone.  What settles it is the frame size: a sub off this camera has
        this sensor's dimensions, at bin 1 or at a whole-number binning of it.

        A real solve of our own old frame is the best measurement of the camera
        angle there is — better than one taken tonight through cloud — so when
        the frame does match, it counts exactly like any other solve.
        """
        sensor_width = self.config.get("optics", "sensorWidth")
        sensor_height = self.config.get("optics", "sensorHeight")
        if not sensor_width or not sensor_height or not width or not height:
            return
        try:
            across = int(sensor_width) / int(width)
            down = int(sensor_height) / int(height)
        except (TypeError, ValueError, ZeroDivisionError):
            return
        # Same sensor, at bin 1 or an even binning of it.
        if across != down or across < 1 or across != int(across):
            return
        self._remember_angle(result)

    def _sync(self, result: SolveResult) -> None:
        mount = self.manager.require("mount")
        mount.sync_to(result.ra, result.dec)
        self.manager.log(
            f"Mount synced to RA {result.ra:.5f}h  Dec {result.dec:+.4f}°", "success")

    # -- going to a planned framing ----------------------------------------
    def _goto(self, ra: float | None, dec: float | None,
              rotation: float | None) -> None:
        """Put the rig on a framing: rotate, slew, and centre if we can.

        The rotator is started first because it is usually the slowest part and
        it can turn while the mount slews.  Centring is attempted only when a
        solver is actually available; without one this is still a useful
        "point at my plan" command, just an open-loop one.
        """
        mount = self.manager.require("mount")
        target_ra = astro.normalise_ra_hours(ra)
        target_dec = float(dec)

        rotator = self.manager.get("rotator")
        rotating = (rotation is not None and rotator is not None and rotator.connected)
        if rotating:
            self._set("rotating", f"rotating to position angle {rotation:.2f}°")
            self.manager.log(f"Rotator -> {rotation:.2f}°")
            rotator.move_absolute(float(rotation))
        elif rotation is not None and rotator is not None:
            self.manager.log("No rotator connected; framing angle not applied", "warn")

        self._set("slewing", "slewing to the framing")
        mount.slew_to(target_ra, target_dec)
        self._wait_for_slew(mount)

        if rotating:
            self._set("rotating", "waiting for the rotator")
            deadline = time.monotonic() + 300.0
            while rotator.moving:
                if self._abort.is_set():
                    raise DeviceError("aborted")
                if time.monotonic() > deadline:
                    raise DeviceError("the rotator did not finish in time")
                time.sleep(0.3)
            self.manager.log(f"Rotator at {rotator.position:.2f}°", "success")

        if self.executable() is None or self.manager.get("camera") is None:
            self.manager.log(
                f"On framing RA {target_ra:.5f}h  Dec {target_dec:+.4f}° "
                "(not plate solved)", "success")
            return

        self._center(target_ra, target_dec, rotation if rotating else None)

    # -- rotating onto a sky angle -----------------------------------------
    def _correct_rotation(self, wanted: float, measured: float) -> bool:
        """Turn the rotator by the error a plate solve just measured.

        A rotator is commanded in its own mechanical coordinates, and those
        agree with sky position angle only as far as its calibration does —
        which on most is "roughly". Open loop, the framing ends up a degree or
        three out, which nobody notices on one frame and everybody notices when
        a mosaic will not tile or this season's panels do not sit on last
        season's.

        A solve measures the real angle, so the error is known exactly: move the
        rotator by it and solve again. Returns whether it moved.
        """
        rotator = self.manager.get("rotator")
        if rotator is None or not rotator.connected:
            return False
        tolerance = float(self.config.get("solver", "rotationTolerance", 1.0))
        # Signed, and the short way round: 359° and 1° are two degrees apart.
        error = ((float(wanted) - float(measured) + 180.0) % 360.0) - 180.0
        if abs(error) <= tolerance:
            return False

        target = (float(rotator.position) + error) % 360.0
        self._set("rotating", f"{error:+.2f}° out — turning the rotator")
        self.manager.log(f"Framing angle measured {float(measured):.2f}°, wanted "
                         f"{float(wanted):.2f}°: {error:+.2f}° out; turning the rotator "
                         f"by {error:+.2f}° from {float(rotator.position):.2f}° to {target:.2f}°")
        try:
            rotator.move_absolute(target)
        except DeviceError as exc:
            self.manager.log(f"Could not turn the rotator: {exc}", "warn")
            return False
        deadline = time.monotonic() + 300.0
        while rotator.moving:
            if self._abort.is_set():
                raise DeviceError("aborted")
            if time.monotonic() > deadline:
                raise DeviceError("the rotator did not finish in time")
            time.sleep(0.3)
        return True

    # -- centring ----------------------------------------------------------
    def _center(self, ra: float | None, dec: float | None,
                rotation: float | None = None) -> None:
        mount = self.manager.require("mount")
        if not mount.can_sync:
            raise DeviceError("centring needs a mount that can be synced")

        target_ra = astro.normalise_ra_hours(ra) if ra is not None else mount.ra
        target_dec = dec if dec is not None else mount.dec
        tolerance = float(self.config.get("solver", "tolerance", 1.0))
        exposure = float(self.config.get("solver", "exposure", 5.0))
        attempts = int(self.config.get("solver", "attempts", 3))

        self.manager.log(
            f"Centring on RA {target_ra:.5f}h  Dec {target_dec:+.4f}° "
            f"(within {tolerance:g}')")

        if ra is not None or dec is not None:
            self._set("slewing", "slewing to the target")
            mount.slew_to(target_ra, target_dec)
            self._wait_for_slew(mount)

        can_sync = True
        for attempt in range(1, attempts + 1):
            if self._abort.is_set():
                raise DeviceError("centring aborted")
            with self._lock:
                self._attempt = attempt

            self._set("exposing", f"attempt {attempt} of {attempts}: exposing {exposure:g}s")
            # A centring frame is a means, not data.  Left saved it lands in the
            # target's folder named exactly like a real sub — same prefix, same
            # filter, just a different exposure — and for a survey where the
            # folder *is* the record, that is a frame nobody can tell from the
            # real thing six months later.  It still reaches the viewer; it just
            # never reaches the disk.
            saved = self.capture.save_enabled
            self.capture.save_enabled = False
            try:
                record = self.capture.capture_blocking(exposure)
            finally:
                self.capture.save_enabled = saved

            result = self._solve_image(record.id)
            result.separation = astro.separation_degrees(
                result.ra, result.dec, target_ra, target_dec) * 60.0
            self._report(result)

            centred = result.separation <= tolerance
            # Where it landed against where it was sent, in arcminutes on the
            # sky and split into the two axes, so a centring that is not
            # converging can be read for what is wrong - a mount off in RA
            # only is a different fault from one off in both.
            east = (((result.ra - target_ra + 12.0) % 24.0) - 12.0) * 15.0 * 60.0 \
                * math.cos(math.radians(target_dec))
            north = (result.dec - target_dec) * 60.0
            self.manager.log(
                f"Centre attempt {attempt}/{attempts}: {result.separation:.2f}' off "
                f"(RA {east:+.2f}', Dec {north:+.2f}')"
                + (f"; angle {result.rotation:.2f}° against {float(rotation):.2f}° wanted"
                   if rotation is not None else "")
                + (" - within tolerance" if centred else f" - tolerance {tolerance:g}'"))

            # Centre and rotate: the same solve that says where the telescope is
            # pointing also says which way up it is, so correcting both from one
            # frame costs nothing beyond the moving. Only when a framing angle
            # was actually asked for — a plain re-centre must not start turning
            # a rotator nobody mentioned.
            turned = False
            if rotation is not None and attempt < attempts:
                turned = self._correct_rotation(rotation, result.rotation)

            if centred and not turned:
                self.manager.log(
                    f"Centred: {result.separation:.2f}' from target after "
                    f"{attempt} attempt{'s' if attempt > 1 else ''}"
                    + (f", angle {result.rotation:.2f}°" if rotation is not None
                       else ""), "success")
                return

            if attempt == attempts:
                break

            # A rotator that has just moved needs another frame to confirm the
            # angle, but the pointing may be fine — re-slewing a centred mount
            # would only spoil it.
            if not centred:
                self._set("correcting",
                          f"{result.separation:.2f}' out — syncing and re-slewing")
                synced = False
                if can_sync:
                    try:
                        mount.sync_to(result.ra, result.dec)
                        synced = True
                    except Exception as exc:      # noqa: BLE001 - the driver's refusal
                        # Some drivers will not take a sync - TheSky's throws
                        # a null reference at it - and a centring that gave
                        # up there left every panel wherever the mount first
                        # put it. Not asked again this centring.
                        can_sync = False
                        self.manager.log(f"The mount would not sync ({exc}); nudging "
                                         "the slew by the measured error instead", "warn")
                if synced:
                    self.manager.log("Synced the mount to the solve; slewing to the "
                                     "target again")
                    mount.slew_to(target_ra, target_dec)
                else:
                    # The error is known, so the slew is nudged by it: ask for
                    # the target plus however far short of it the mount
                    # landed. Always from the *target*, never from the last
                    # nudged aim - compounding the nudge each attempt walked
                    # the mount steadily away from the field.
                    aim_ra = astro.normalise_ra_hours(target_ra + (target_ra - result.ra))
                    aim_dec = max(-90.0, min(90.0, target_dec + (target_dec - result.dec)))
                    self.manager.log(f"Nudging the slew by RA {-east:+.2f}', Dec {-north:+.2f}' "
                                     f"to RA {aim_ra:.5f}h  Dec {aim_dec:+.4f}°")
                    mount.slew_to(aim_ra, aim_dec)
                self._wait_for_slew(mount)

        last = self._result.separation if self._result else None
        raise DeviceError(
            f"still {last:.2f}' from the target after {attempts} attempts"
            if last is not None else f"could not centre after {attempts} attempts")

    def _wait_for_slew(self, mount: Any, timeout: float = 300.0) -> None:
        """Block until the mount stops moving.

        Some drivers still report `Slewing == False` for a moment after the
        command is accepted, so the settle at the top is not optional.
        """
        time.sleep(1.0)
        deadline = time.monotonic() + timeout
        while mount.slewing:
            if self._abort.is_set():
                raise DeviceError("centring aborted")
            if time.monotonic() > deadline:
                raise DeviceError("the mount did not finish slewing in time")
            time.sleep(0.3)
        time.sleep(1.5)                          # let tracking settle before exposing

    # -- measuring a frame while a run is in progress -----------------------
    def measure(self, image_id: str) -> SolveResult:
        """Solve one frame purely to find out where it points.

        Used by the sequencer to check pointing between subs.  Deliberately
        separate from `start`: it touches none of the run state, so a pointing
        check cannot make the UI think a centring run is under way, and it never
        syncs or moves anything.

        The camera angle is the one exception, and it is not run state. These
        are real solves of real frames off this camera, taken every few subs all
        night — collectively the best measurement of the angle the program ever
        gets. Leaving them out was why the stored angle could sit at last
        season's value through an entire run of successful solves.
        """
        if self.executable() is None:
            raise DeviceError("ASTAP was not found")
        record = self.capture.record(image_id)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="astrocontrol-check-") as workspace:
            source = self._fits_for(record, Path(workspace))
            values = self._run_astap(source, Path(workspace) / "check", record,
                                     track=False)
        result = self._interpret(values, record)
        result.seconds = round(time.monotonic() - started, 2)
        with contextlib.suppress(Exception):
            self._remember_angle(result)
        return result

    def solve_file(self, path: Path,
                   say: Callable[[str], None] | None = None) -> SolveResult:
        """Work out where a FITS file on disk points.  Three ways, in order.

        For using an old frame as a framing reference: "put the new mosaic where
        last spring's one was" is a question about a file on disk, not about
        anything the camera has taken tonight.

        **The header, if it has the answer.**  Anything written after a plate
        solve — by this program, by N.I.N.A., by SGP, by ASTAP itself — carries
        it in its WCS keywords, and reading six numbers beats spending twenty
        seconds rediscovering them.  It also means framing against your own data
        works on a laptop with no solver installed, which is where planning
        actually gets done.

        **Then ASTAP, blind.**  Blind because every hint this program could
        offer is about *this* telescope: where the mount is pointing tonight,
        and the field of the camera currently plugged in.  For a file from
        somewhere else those are not approximations, they are wrong, and a
        confident wrong hint is worse than none — the search goes to the wrong
        patch of sky at the wrong scale and correctly reports nothing there.

        **Then astrometry.net.**  A frame off another rig may be at a scale this
        machine has no ASTAP index files for, which is precisely the job that
        service exists for.  Slow, needs the network and a key, and only ever
        reached when both instant answers have failed.
        """
        path = Path(path)
        if not path.is_file():
            raise DeviceError(f"{path} is not a file")
        note = say or (lambda message: None)

        header = fits.read_header(path)
        record = _FileRecord.from_header(path, header)

        from_header = self._wcs_from_header(header, record)
        if from_header is not None:
            note(f"{path.name} already carries a plate solution")
            return from_header

        settings = self.config.section("solver")
        attempts: list[str] = []

        if self.executable() is not None:
            note(f"solving {path.name} with ASTAP (blind)")
            started = time.monotonic()
            try:
                with tempfile.TemporaryDirectory(prefix="astrocontrol-ref-") as work:
                    values = self._run_astap(path, Path(work) / "reference",
                                             record, track=False, blind=True)
                result = self._interpret(values, record)
                result.seconds = round(time.monotonic() - started, 2)
                result.method = "astap"
                return result
            except DeviceError as exc:
                attempts.append(f"ASTAP: {exc}")
                note(f"ASTAP could not solve it — {exc}")
        else:
            attempts.append("ASTAP: not installed")

        if settings.get("astrometryEnabled", True) and settings.get("astrometryKey"):
            note("handing it to astrometry.net")
            started = time.monotonic()
            try:
                calibration = astrometry.solve(
                    path, str(settings.get("astrometryKey") or ""),
                    str(settings.get("astrometryUrl") or astrometry.DEFAULT_URL),
                    float(settings.get("astrometryTimeout") or 600.0),
                    say=note, should_abort=lambda: self._abort.is_set())
                result = self._from_calibration(calibration, record)
                result.seconds = round(time.monotonic() - started, 2)
                return result
            except DeviceError as exc:
                attempts.append(f"astrometry.net: {exc}")
        elif not settings.get("astrometryKey"):
            attempts.append("astrometry.net: no API key set")
        else:
            attempts.append("astrometry.net: switched off")

        raise DeviceError(f"could not work out where {path.name} points. "
                          + "  ".join(attempts))

    @staticmethod
    def _from_calibration(calibration: dict[str, Any],
                          record: Any) -> SolveResult:
        """astrometry.net's answer, in the shape the rest of this uses.

        Its `orientation` is the position angle of the up axis measured east of
        north, which is the same convention as CROTA2 — and `parity` is 1 for a
        mirrored frame, which is the same information as a positive CDELT1.
        """
        scale = float(calibration.get("pixscale") or 0.0)
        degrees_per_pixel = scale / 3600.0
        return SolveResult(
            ra=round(astro.normalise_ra_hours(
                float(calibration.get("ra") or 0.0) / 15.0), 6),
            dec=round(float(calibration.get("dec") or 0.0), 5),
            rotation=round(float(calibration.get("orientation") or 0.0), 3),
            scale=round(scale, 4),
            fov_width=round(degrees_per_pixel * (record.width or 0), 5),
            fov_height=round(degrees_per_pixel * (record.height or 0), 5),
            flipped=float(calibration.get("parity") or -1) > 0,
            image_id=record.id,
            filename=record.filename,
            method="astrometry.net",
            detail=f"job {calibration.get('jobId')}",
        )

    @staticmethod
    def _wcs_from_header(header: dict[str, Any],
                         record: "_FileRecord") -> SolveResult | None:
        """The solution a frame already carries, or None if it carries none.

        `_interpret` reads exactly these keywords out of ASTAP's answer file,
        because ASTAP's answer file *is* a WCS — so a frame that has one can go
        through the same code rather than a second interpretation of the same
        six numbers that might one day disagree with it.
        """
        if not record.width or not record.height:
            return None
        needed = ("CRVAL1", "CRVAL2")
        if any(header.get(key) is None for key in needed):
            return None
        # Either CDELT or the CD matrix; without a scale there is no field size
        # and the framing rectangle would have nothing to be drawn against.
        values = {key: str(header[key]) for key in header
                  if key in ("CRVAL1", "CRVAL2", "CDELT1", "CDELT2",
                             "CROTA1", "CROTA2")}
        if "CDELT1" not in values and "CDELT2" not in values:
            cd11, cd12 = header.get("CD1_1"), header.get("CD1_2")
            cd21, cd22 = header.get("CD2_1"), header.get("CD2_2")
            if None in (cd11, cd12, cd21, cd22):
                return None
            with contextlib.suppress(TypeError, ValueError):
                scale_x = math.hypot(float(cd11), float(cd21))
                scale_y = math.hypot(float(cd12), float(cd22))
                values["CDELT1"] = str(-scale_x)
                values["CDELT2"] = str(scale_y)
                # From the standard relations
                #     CD1_2 = -CDELT2 sin(rot),  CD2_2 = CDELT2 cos(rot)
                # so this reads the angle off the second column, where CDELT2 is
                # positive on a normal sky orientation. Taking it off the first
                # column instead lands 180 degrees out whenever CDELT1 is
                # negative — which is to say, on nearly every real frame.
                values["CROTA2"] = str(math.degrees(
                    math.atan2(-float(cd12), float(cd22))))
        if "CDELT1" not in values and "CDELT2" not in values:
            return None
        result = Solver._interpret_values(values, record)
        result.method = "header"
        result.detail = "already solved"
        return result

    # -- one solve ---------------------------------------------------------
    def _solve_image(self, image_id: str | None) -> SolveResult:
        record = self._pick_record(image_id)
        started = time.monotonic()
        self._set("solving", f"solving {record.filename}")

        with tempfile.TemporaryDirectory(prefix="astrocontrol-solve-") as workspace:
            source = self._fits_for(record, Path(workspace))
            ini = self._run_astap(source, Path(workspace) / "solution", record)

        result = self._interpret(ini, record)
        result.seconds = round(time.monotonic() - started, 2)
        return result

    def _pick_record(self, image_id: str | None) -> Any:
        if image_id:
            return self.capture.record(image_id)
        if not self.capture.images:
            raise DeviceError("there is no frame to solve — take an exposure first")
        return self.capture.images[0]

    def _fits_for(self, record: Any, workspace: Path) -> Path:
        """A FITS file on disk for ASTAP to read.

        Saved frames are handed over as they are.  Frames captured with saving
        switched off still live in the cache, so they are written out here and
        thrown away with the workspace.
        """
        if record.path and Path(record.path).is_file():
            return Path(record.path)
        frame = self.capture.frame(record.id)
        header = {
            "EXPTIME": (float(record.exposure), "exposure time in seconds"),
            "XBINNING": (int(record.binning), ""),
            "YBINNING": (int(record.binning), ""),
            "OBJCTRA": (record.ra, "RA in hours"),
            "OBJCTDEC": (record.dec, "Dec in degrees"),
        }
        return fits.write(workspace / "frame.fits", frame, header)

    def _hints(self, record: Any) -> tuple[float | None, float | None, float | None]:
        """Where to look, and how wide the frame is, in degrees."""
        ra, dec = record.ra, record.dec
        if ra is None or dec is None:
            mount = self.manager.get("mount")
            if mount is not None and mount.connected:
                ra, dec = mount.ra, mount.dec

        fov = None
        focal_length = self.config.get("optics", "focalLength")
        camera = self.manager.get("camera")
        pixel_size = getattr(camera, "pixel_size_um", 0.0) if camera else 0.0
        if focal_length and pixel_size and record.height:
            # 206.265 arcsec per (micron / mm), i.e. the small-angle formula.
            arcsec_per_pixel = 206.265 * pixel_size * record.binning / float(focal_length)
            fov = arcsec_per_pixel * record.height / 3600.0
        return ra, dec, fov

    def _run_astap(self, source: Path, output: Path, record: Any,
                   track: bool = True, blind: bool = False) -> dict[str, str]:
        """Run the solver.

        `track` publishes the process so `abort` can kill it; a background
        pointing check leaves it alone so that aborting a centring run does not
        kill the check, or the other way round.

        `blind` searches the whole sky and passes no field-size hint. That is
        for a file that did not come off this rig: the hints are all derived
        from *this* telescope's optics and *this* session's pointing, and a
        confident wrong hint is worse than none at all — ASTAP will search a
        fifteen-degree circle around the wrong place at the wrong scale and
        report, correctly, that there is nothing there.
        """
        executable = self.executable()
        if executable is None:
            raise DeviceError("ASTAP was not found")

        ra, dec, fov = (None, None, None) if blind else self._hints(record)
        radius = float(self.config.get("solver", "searchRadius", 15.0))
        downsample = int(self.config.get("solver", "downsample", 0))
        max_stars = int(self.config.get("solver", "maxStars", 500))
        timeout = float(self.config.get("solver", "timeout", 120.0))

        args = [str(executable), "-f", str(source), "-o", str(output),
                "-s", str(max_stars)]
        if ra is not None and dec is not None:
            # ASTAP takes RA in hours and "south pole distance" rather than Dec.
            args += ["-ra", f"{astro.normalise_ra_hours(ra):.6f}",
                     "-spd", f"{dec + 90.0:.6f}", "-r", f"{radius:g}"]
        else:
            args += ["-r", "180"]                # blind solve: search everywhere
        if fov:
            args += ["-fov", f"{fov:.4f}"]
        if downsample:
            args += ["-z", str(downsample)]

        process = None
        try:
            process = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", creationflags=_NO_WINDOW)
            if track:
                self._process = process
            output_text, _ = process.communicate(timeout=timeout)
            code = process.returncode
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise DeviceError(f"ASTAP gave up after {timeout:g}s") from None
        except OSError as exc:
            raise DeviceError(f"could not run ASTAP: {exc}") from exc
        finally:
            if track:
                self._process = None

        if track and self._abort.is_set():
            raise DeviceError("plate solve aborted")

        ini_path = output.with_suffix(".ini")
        values = self._read_ini(ini_path)

        if values.get("PLTSOLVD", "").upper().startswith("T"):
            return values

        tail = (output_text or "").strip().splitlines()
        detail = (values.get("ERROR") or values.get("WARNING")
                  or _EXIT_MESSAGES.get(code)
                  or (tail[-1] if tail else f"exit code {code}"))
        raise DeviceError(f"ASTAP did not solve: {detail}")

    @staticmethod
    def _read_ini(path: Path) -> dict[str, str]:
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            return {}
        values: dict[str, str] = {}
        for line in text.splitlines():
            if "=" in line and not line.startswith((";", "[")):
                key, _, value = line.partition("=")
                values[key.strip().upper()] = value.strip()
        return values

    def _interpret(self, values: dict[str, str], record: Any) -> SolveResult:
        return self._interpret_values(values, record)

    @staticmethod
    def _interpret_values(values: dict[str, str], record: Any) -> SolveResult:
        """One reading of a WCS, whether ASTAP wrote it or the frame carried it.

        ASTAP's answer file *is* a WCS, so a frame that already has one goes
        through this rather than through a second interpretation of the same six
        numbers that might one day disagree with it.
        """
        def number(key: str, default: float = 0.0) -> float:
            try:
                return float(values.get(key, default))
            except (TypeError, ValueError):
                return default

        ra_degrees = number("CRVAL1")
        dec_degrees = number("CRVAL2")
        # CDELT is degrees per pixel; a negative CDELT1 is the normal sky
        # orientation, so a positive one means the image is mirrored.
        cdelt1 = number("CDELT1")
        cdelt2 = number("CDELT2") or cdelt1
        scale = abs(cdelt2) * 3600.0

        return SolveResult(
            ra=round(astro.normalise_ra_hours(ra_degrees / 15.0), 6),
            dec=round(dec_degrees, 5),
            rotation=round(number("CROTA2", number("CROTA1")), 3),
            scale=round(scale, 4),
            fov_width=round(abs(cdelt1 or cdelt2) * record.width, 5),
            fov_height=round(abs(cdelt2) * record.height, 5),
            flipped=cdelt1 > 0,
            image_id=record.id,
            filename=record.filename,
        )
