"""PHD2 autoguider backend.

PHD2 exposes a JSON-RPC 1.0-ish server on TCP 4400: newline-delimited JSON in
both directions.  Unsolicited *events* arrive whenever PHD2 does something
(``GuideStep``, ``SettleDone``, ``StarLost`` ...); *responses* to our calls come
back tagged with the ``id`` we sent.

One reader thread owns the socket and demultiplexes the two streams, so a slow
or silent PHD2 can never block an HTTP handler.  Guide errors are kept in a
short rolling window and reduced to RMS, which is the number that actually tells
you whether the rig is guiding well.

**Connecting means more than opening the socket.**  PHD2 is a separate program
with its own equipment, and "connected" in its sense is four things, which is
what NINA and SGP both do and what this now does too:

  1. *PHD2 is running.*  If nothing answers on the port, launch `phd2.exe` and
     wait for the server to come up.  A guider that only works when you
     remembered to start PHD2 first is a guider that fails at 2am.
  2. *A profile is loaded.*  `set_profile` picks the equipment profile, and PHD2
     only allows it while its equipment is disconnected.
  3. *Its own camera and mount are connected* — `set_connected true`.  This is
     the step whose absence looks exactly like "guiding will not start": the
     socket is fine, `get_app_state` answers, and `guide` then fails because
     PHD2 has no camera.
  4. *There is a star.*  `guide` asks PHD2 to find one, but from a standing
     start that is far more reliable if we loop exposures and auto-select first,
     which is again what the other clients do.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .base import DeviceError, Guider

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4400

# Where PHD2 installs itself. The Windows installer's folder is "PHDGuiding2",
# not "PHD2", which is the sort of thing worth having written down.
_INSTALL_GUESSES = (
    r"C:\Program Files (x86)\PHDGuiding2\phd2.exe",
    r"C:\Program Files\PHDGuiding2\phd2.exe",
    r"C:\Program Files (x86)\PHD2\phd2.exe",
    r"C:\Program Files\PHD2\phd2.exe",
    "/usr/local/bin/phd2",
    "/usr/bin/phd2",
    "/Applications/PHD2.app/Contents/MacOS/PHD2",
)


def _from_registry() -> str | None:
    """Ask Windows where PHD2 was installed.

    More reliable than guessing folder names: it is what the installer actually
    recorded, so it survives PHD2 being put somewhere unusual or renaming its
    directory between versions.
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:                         # pragma: no cover - not Windows
        return None

    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, path in roots:
        try:
            with winreg.OpenKey(hive, path) as parent:
                for index in range(winreg.QueryInfoKey(parent)[0]):
                    try:
                        name = winreg.EnumKey(parent, index)
                        with winreg.OpenKey(parent, name) as key:
                            label = str(winreg.QueryValueEx(key, "DisplayName")[0])
                            if "phd" not in label.lower():
                                continue
                            for value in ("DisplayIcon", "InstallLocation"):
                                with contextlib.suppress(OSError):
                                    raw = str(winreg.QueryValueEx(key, value)[0])
                                    candidate = Path(raw.split(",")[0].strip().strip('"'))
                                    if candidate.is_dir():
                                        candidate = candidate / "phd2.exe"
                                    if candidate.exists():
                                        return str(candidate)
                    except OSError:
                        continue
        except OSError:
            continue
    return None

# How long to wait for a freshly launched PHD2 to answer on its port. It has a
# splash screen and loads a profile, so this is not instant.
LAUNCH_TIMEOUT = 45.0

# How long PHD2 gets to bring up its own camera and mount. Generous on purpose:
# an ASCOM camera plus a mount reached through TheSky is routinely a minute or
# more, and PHD2's server answers nothing while it works.
EQUIPMENT_TIMEOUT = 150.0


def find_executable(configured: str = "") -> str | None:
    """Where phd2.exe is, or None.

    A configured path wins, then what the installer registered, then the usual
    install locations, then PATH.
    """
    text = (configured or "").strip().strip('"')
    if text:
        path = Path(text).expanduser()
        # Pointing at the install folder rather than the exe is an easy slip.
        if path.is_dir():
            for name in ("phd2.exe", "phd2"):
                if (path / name).exists():
                    return str(path / name)
        return str(path) if path.exists() else None

    registered = _from_registry()
    if registered:
        return registered
    for guess in _INSTALL_GUESSES:
        if os.path.exists(guess):
            return guess
    return shutil.which("phd2") or shutil.which("phd2.exe")


@contextlib.contextmanager
def _suppress_device_error():
    """For probes whose failure is not interesting — an older PHD2 that does
    not know a method, or a state where it declines to answer."""
    try:
        yield
    except DeviceError:
        pass


def server_answers(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

# GuideStep arrives once per guide exposure (typically 1-4 s), so 50 samples is
# a couple of minutes of guiding - long enough to be meaningful, short enough to
# react when seeing changes.
RMS_WINDOW = 50


def available() -> bool:
    """PHD2 needs nothing installed on our side; it is just a socket."""
    return True


def parse_target(driver_id: str) -> tuple[str, int]:
    """``"host:port"`` -> ``("host", port)``, with sane defaults."""
    text = (driver_id or "").strip()
    if not text:
        return DEFAULT_HOST, DEFAULT_PORT
    if ":" in text:
        host, _, port = text.rpartition(":")
        try:
            return (host or DEFAULT_HOST), int(port)
        except ValueError:
            return (host or DEFAULT_HOST), DEFAULT_PORT
    return text, DEFAULT_PORT


def list_devices() -> list[dict[str, Any]]:
    return [{
        "id": f"{DEFAULT_HOST}:{DEFAULT_PORT}",
        "name": f"PHD2 ({DEFAULT_HOST}:{DEFAULT_PORT})",
    }]


def create(driver_id: str, name: str | None = None,
           options: dict[str, Any] | None = None) -> "Phd2Guider":
    host, port = parse_target(driver_id)
    return Phd2Guider(f"{host}:{port}", name or f"PHD2 ({host}:{port})", options)


class Phd2Guider(Guider):
    """A connection to PHD2, which it will start if it has to."""

    RPC_TIMEOUT = 20.0

    def __init__(self, driver_id: str, name: str,
                 options: dict[str, Any] | None = None) -> None:
        super().__init__(driver_id, name)
        self.host, self.port = parse_target(driver_id)

        options = options or {}
        self.executable_hint = str(options.get("phd2Path") or "")
        self.auto_start = bool(options.get("autoStartPhd2", True))
        self.wanted_profile = str(options.get("phd2Profile") or "").strip()
        self.connect_equipment = bool(options.get("connectEquipment", True))
        self.auto_select_star = bool(options.get("autoSelectStar", True))

        self._launched: subprocess.Popen | None = None
        self._profile_name: str | None = None
        self._equipment_connected: bool | None = None
        self._notes: list[str] = []

        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()

        self._next_id = 1
        self._pending: dict[int, dict[str, Any]] = {}
        self._io_lock = threading.Lock()

        self._app_state = "Unknown"
        self._version: str | None = None
        self._pixel_scale: float | None = None
        self._settling: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._star_lost: str | None = None
        self._snr: float | None = None
        self._exposure_ms: int | None = None
        self._last_step: float = 0.0
        self._ra_errors: deque[float] = deque(maxlen=RMS_WINDOW)
        self._dec_errors: deque[float] = deque(maxlen=RMS_WINDOW)

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        if self._connected:
            return
        self._notes = []
        sock = self._open_socket()
        sock.settimeout(None)
        self._socket = sock
        self._stop.clear()
        self._connected = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name="phd2")
        self._reader.start()

        # Confirm PHD2 is actually talking to us before reporting success.
        try:
            self._app_state = str(self._call("get_app_state"))
            self._select_profile()
            self._connect_equipment()
            self._pixel_scale = self._as_float(self._call("get_pixel_scale"))
        except DeviceError:
            self.disconnect()
            raise

    def _open_socket(self) -> socket.socket:
        """Reach the PHD2 server, starting PHD2 first if nothing answers."""
        try:
            return socket.create_connection((self.host, self.port), timeout=5.0)
        except OSError as exc:
            first_failure = exc

        if not self.auto_start:
            raise DeviceError(
                f"could not reach PHD2 at {self.host}:{self.port} - is PHD2 "
                f"running with the server enabled? ({first_failure})")
        # Only a PHD2 on this machine can be started by us.
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            raise DeviceError(
                f"could not reach PHD2 at {self.host}:{self.port} - it is on "
                f"another machine, so it has to be started there "
                f"({first_failure})")

        executable = find_executable(self.executable_hint)
        if executable is None:
            raise DeviceError(
                "PHD2 is not running and its program could not be found. Set "
                "the path to phd2.exe in Equipment, or start PHD2 yourself with "
                "Tools -> Enable Server ticked.")

        self._notes.append(f"started {executable}")
        try:
            self._launched = subprocess.Popen(
                [executable], cwd=str(Path(executable).parent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Do not let PHD2 die with us: the operator may well want it to
                # outlive a restart of this program.
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
        except OSError as exc:
            raise DeviceError(f"could not start {executable}: {exc}") from exc

        deadline = time.monotonic() + LAUNCH_TIMEOUT
        while time.monotonic() < deadline:
            if self._launched.poll() is not None:
                raise DeviceError(
                    f"PHD2 started and exited straight away (code "
                    f"{self._launched.returncode}); try starting it by hand")
            try:
                return socket.create_connection((self.host, self.port), timeout=2.0)
            except OSError:
                time.sleep(1.0)

        raise DeviceError(
            f"PHD2 was started but its server did not answer on port {self.port} "
            f"within {LAUNCH_TIMEOUT:.0f}s. In PHD2, tick Tools -> Enable Server.")

    def _select_profile(self) -> None:
        """Load the wanted equipment profile, if one was asked for.

        PHD2 refuses to change profile while its equipment is connected, so the
        equipment is disconnected first — which is harmless, because connecting
        it is the very next step.
        """
        try:
            profiles = self._call("get_profiles") or []
        except DeviceError:
            return                              # older PHD2; leave the profile alone
        current = None
        try:
            current = self._call("get_profile")
        except DeviceError:
            pass
        if isinstance(current, dict):
            self._profile_name = str(current.get("name") or "") or None

        if not self.wanted_profile:
            return
        wanted = self.wanted_profile.strip().lower()
        match = next((p for p in profiles
                      if str(p.get("name", "")).strip().lower() == wanted
                      or str(p.get("id")) == self.wanted_profile), None)
        if match is None:
            names = ", ".join(str(p.get("name")) for p in profiles) or "none"
            self._notes.append(
                f"profile {self.wanted_profile!r} not found (have: {names})")
            return
        if isinstance(current, dict) and current.get("id") == match.get("id"):
            return                              # already the right one

        with _suppress_device_error():
            self._call("set_connected", [False])
        try:
            self._call("set_profile", [int(match["id"])])
            self._profile_name = str(match.get("name") or "")
            self._notes.append(f"profile {self._profile_name!r}")
        except DeviceError as exc:
            self._notes.append(f"could not select that profile: {exc}")

    def _connect_equipment(self) -> None:
        """Ask PHD2 to connect its own camera and mount.

        Without this the socket is up, `get_app_state` answers cheerfully, and
        guiding then refuses — which is a genuinely baffling failure, because
        everything on our side looks connected.
        """
        try:
            self._equipment_connected = bool(self._call("get_connected"))
        except DeviceError:
            self._equipment_connected = None
            return
        if self._equipment_connected or not self.connect_equipment:
            return

        self._notes.append("connecting PHD2's own camera and mount")
        # Fire and poll, rather than waiting on the reply.  Bringing up an ASCOM
        # camera and a mount behind TheSky can take a couple of minutes, and
        # while PHD2 is doing it its server answers nothing at all — so the
        # absence of a reply says "still working", not "failed".  What settles
        # it is `get_connected` eventually coming back true.
        self._send_only("set_connected", [True])

        deadline = time.monotonic() + EQUIPMENT_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(2.0)
            with _suppress_device_error():
                if bool(self._call("get_connected", timeout=10.0)):
                    self._equipment_connected = True
                    self._notes.append("PHD2 equipment connected")
                    return
        raise DeviceError(
            f"PHD2 did not report its equipment as connected within "
            f"{EQUIPMENT_TIMEOUT:.0f}s. Open PHD2 and connect the camera and "
            f"mount there — a driver may be waiting on a dialog.")

    def disconnect(self) -> None:
        self._stop.set()
        self._connected = False
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        with self._io_lock:
            for waiter in self._pending.values():
                waiter["error"] = "disconnected"
                waiter["event"].set()
            self._pending.clear()
        self._reset_metrics()

    def _reset_metrics(self) -> None:
        self._ra_errors.clear()
        self._dec_errors.clear()
        self._settling = None
        self._snr = None
        self._app_state = "Unknown"

    # -- transport ---------------------------------------------------------
    def _read_loop(self) -> None:
        buffer = b""
        sock = self._socket
        while not self._stop.is_set() and sock is not None:
            try:
                chunk = sock.recv(8192)
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                self._dispatch(message)

        if not self._stop.is_set():
            # PHD2 went away on its own (closed, crashed, network dropped).
            self._connected = False
            self._last_error = "PHD2 closed the connection"
            self._reset_metrics()

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "Event" in message:
            self._handle_event(message)
            return
        message_id = message.get("id")
        if message_id is None:
            return
        with self._io_lock:
            waiter = self._pending.get(int(message_id))
        if waiter is None:
            return
        error = message.get("error")
        if error:
            waiter["error"] = error.get("message") if isinstance(error, dict) else str(error)
        else:
            waiter["result"] = message.get("result")
        waiter["event"].set()

    def _send_only(self, method: str, params: Any = None) -> None:
        """Send a request without waiting for its answer.

        For the calls whose reply is not the interesting part — `set_connected`
        can take minutes and blocks PHD2's server while it runs, so what we
        actually want is to start it and then watch for the result.
        """
        if not self._connected or self._socket is None:
            raise DeviceError("PHD2 is not connected")
        with self._io_lock:
            call_id = self._next_id
            self._next_id += 1
            payload: dict[str, Any] = {"method": method, "id": call_id}
            if params is not None:
                payload["params"] = params
            try:
                self._socket.sendall((json.dumps(payload) + "\r\n").encode("utf-8"))
            except OSError as exc:
                raise DeviceError(f"PHD2 write failed: {exc}") from exc

    def _call(self, method: str, params: Any = None,
              timeout: float | None = None) -> Any:
        """Send one request and wait for its answer.

        `timeout` overrides the default for the calls that are allowed to take
        their time — connecting a camera, or a `guide` that does not come back
        until the mount has settled.
        """
        if not self._connected or self._socket is None:
            raise DeviceError("PHD2 is not connected")
        with self._io_lock:
            call_id = self._next_id
            self._next_id += 1
            waiter: dict[str, Any] = {"event": threading.Event()}
            self._pending[call_id] = waiter
            payload: dict[str, Any] = {"method": method, "id": call_id}
            if params is not None:
                payload["params"] = params
            line = (json.dumps(payload) + "\r\n").encode("utf-8")
            try:
                self._socket.sendall(line)
            except OSError as exc:
                self._pending.pop(call_id, None)
                raise DeviceError(f"PHD2 write failed: {exc}") from exc

        limit = self.RPC_TIMEOUT if timeout is None else float(timeout)
        if not waiter["event"].wait(limit):
            with self._io_lock:
                self._pending.pop(call_id, None)
            raise DeviceError(f"PHD2 did not answer {method} within {limit:g}s")
        with self._io_lock:
            self._pending.pop(call_id, None)
        if "error" in waiter:
            raise DeviceError(f"PHD2: {waiter['error']}")
        return waiter.get("result")

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    # -- events ------------------------------------------------------------
    def _handle_event(self, event: dict[str, Any]) -> None:
        name = event.get("Event")

        if name == "Version":
            self._version = str(event.get("PHDVersion") or "")
        elif name == "AppState":
            self._app_state = str(event.get("State") or "Unknown")
        elif name in ("GuidingStopped", "LoopingExposuresStopped"):
            self._app_state = "Stopped"
            self._settling = None
        elif name == "LoopingExposures":
            self._app_state = "Looping"
        elif name == "StartGuiding":
            self._app_state = "Guiding"
        elif name == "Paused":
            self._app_state = "Paused"
        elif name == "Resumed":
            self._app_state = "Guiding"
        elif name == "StartCalibration":
            self._app_state = "Calibrating"
        elif name == "GuideStep":
            self._app_state = "Guiding"
            self._star_lost = None
            self._last_step = time.time()
            self._snr = self._as_float(event.get("SNR"))
            ra = self._as_float(event.get("RADistanceRaw"))
            dec = self._as_float(event.get("DECDistanceRaw"))
            if ra is not None:
                self._ra_errors.append(ra)
            if dec is not None:
                self._dec_errors.append(dec)
        elif name == "StarLost":
            self._app_state = "LostLock"
            self._star_lost = str(event.get("Status") or "star lost")
        elif name in ("SettleBegin", "Settling"):
            self._settling = {
                "distance": self._as_float(event.get("Distance")),
                "time": self._as_float(event.get("Time")),
                "settleTime": self._as_float(event.get("SettleTime")),
                "starLocked": bool(event.get("StarLocked", True)),
            }
        elif name == "SettleDone":
            status = event.get("Status")
            self._settling = None
            if status:
                self._last_error = str(event.get("Error") or "settling failed")
            else:
                self._last_error = None
        elif name == "GuidingDithered":
            self._ra_errors.clear()
            self._dec_errors.clear()
        elif name == "ConfigurationChange":
            try:
                self._pixel_scale = self._as_float(self._call("get_pixel_scale"))
            except DeviceError:
                pass

        if name in ("GuideStep", "GuidingStopped", "StartGuiding"):
            exposure = event.get("Exposure") if name == "GuideStep" else None
            if exposure is not None:
                self._exposure_ms = int(exposure)

    # -- metrics -----------------------------------------------------------
    @staticmethod
    def _rms(values: deque[float]) -> float | None:
        if not values:
            return None
        return math.sqrt(sum(v * v for v in values) / len(values))

    # -- control -----------------------------------------------------------
    def start_guiding(self, settle_pixels: float = 1.5, settle_time: float = 8.0,
                      settle_timeout: float = 60.0, recalibrate: bool = False) -> None:
        """Start guiding, finding a star first if there is not one yet.

        `guide` on its own will try to select a star, but from a standing stop
        that fails more often than it should — PHD2 has no recent frame to look
        at.  Looping first and picking a star explicitly is what NINA and SGP
        do, and it turns "no star found" into a message that says which step
        actually failed.
        """
        self._require()
        if self._equipment_connected is False:
            raise DeviceError("PHD2's own camera and mount are not connected")

        if self.auto_select_star and self._app_state in ("Stopped", "Unknown"):
            self._find_a_star()

        # `guide` does not come back until PHD2 has settled, so it must not be
        # held to the ordinary short RPC timeout.
        self._call("guide", [
            {"pixels": float(settle_pixels), "time": float(settle_time),
             "timeout": float(settle_timeout)},
            bool(recalibrate),
        ], timeout=float(settle_timeout) + 90.0)

    def _find_a_star(self) -> None:
        """Loop exposures until PHD2 has a frame, then auto-select a star."""
        with _suppress_device_error():
            if self._call("get_lock_position"):
                return                          # already has one
        try:
            self._call("loop")
        except DeviceError as exc:
            raise DeviceError(f"PHD2 would not start looping: {exc}") from exc

        # Give it long enough for a couple of exposures at a typical guide
        # exposure, so auto-select has something to work with.
        exposure = (self._exposure_ms or 2000) / 1000.0
        deadline = time.monotonic() + max(12.0, exposure * 3 + 6.0)
        while time.monotonic() < deadline and self._app_state != "Looping":
            time.sleep(0.3)
        time.sleep(min(6.0, exposure + 1.0))

        try:
            self._call("find_star", timeout=45.0)
        except DeviceError as exc:
            raise DeviceError(
                f"PHD2 could not find a guide star: {exc}. Check the guide "
                f"camera is seeing stars and the exposure is long enough.") from exc

    def stop_guiding(self) -> None:
        self._require()
        self._call("stop_capture")

    def dither(self, pixels: float = 3.0, ra_only: bool = False,
               settle_pixels: float = 1.5, settle_time: float = 8.0,
               settle_timeout: float = 60.0) -> None:
        self._require()
        if self._app_state != "Guiding":
            raise DeviceError(f"PHD2 is {self._app_state}, not guiding - cannot dither")
        self._call("dither", [
            float(pixels), bool(ra_only),
            {"pixels": float(settle_pixels), "time": float(settle_time),
             "timeout": float(settle_timeout)},
        ], timeout=float(settle_timeout) + 60.0)

    def set_paused(self, paused: bool) -> None:
        self._require()
        self._call("set_paused", [bool(paused), "full"])

    # -- reporting ---------------------------------------------------------
    def profiles(self) -> list[dict[str, Any]]:
        """The equipment profiles PHD2 has, for the settings dropdown."""
        self._require()
        try:
            return [{"id": p.get("id"), "name": str(p.get("name") or "")}
                    for p in (self._call("get_profiles") or [])]
        except DeviceError:
            return []

    @property
    def state(self) -> str:
        return self._app_state

    @property
    def guiding(self) -> bool:
        return self._app_state == "Guiding"

    @property
    def settling(self) -> bool:
        return self._settling is not None

    def status(self) -> dict[str, Any]:
        if not self._connected:
            return {"connected": False}
        rms_ra = self._rms(self._ra_errors)
        rms_dec = self._rms(self._dec_errors)
        total = None
        if rms_ra is not None and rms_dec is not None:
            total = math.hypot(rms_ra, rms_dec)
        scale = self._pixel_scale or None

        def arcsec(value: float | None) -> float | None:
            return None if (value is None or scale is None) else value * scale

        return {
            "connected": True,
            "state": self._app_state,
            "guiding": self.guiding,
            "settling": self._settling,
            "version": self._version,
            "pixelScale": scale,
            "host": self.host,
            "port": self.port,
            "snr": self._snr,
            "starLost": self._star_lost,
            "error": self._last_error,
            "samples": len(self._ra_errors),
            "rmsRaPx": rms_ra,
            "rmsDecPx": rms_dec,
            "rmsTotalPx": total,
            "rmsRa": arcsec(rms_ra),
            "rmsDec": arcsec(rms_dec),
            "rmsTotal": arcsec(total),
            "lastStepAge": (time.time() - self._last_step) if self._last_step else None,
            # What connecting actually did, so "connected" can be trusted: which
            # profile is loaded, whether PHD2's own equipment is up, and whether
            # we had to start PHD2 ourselves.
            "profile": self._profile_name,
            "equipmentConnected": self._equipment_connected,
            "launched": self._launched is not None,
            "notes": list(self._notes),
        }
