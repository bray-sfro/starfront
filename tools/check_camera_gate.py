"""Camera call quarantine (0.2.53d). No hardware.

A timeout does not cancel a driver call. Until that call returns, recovery
must not disconnect, power-cycle, or start another exposure, and the late
result is not a saved frame.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="camgate-"))
os.environ.setdefault("ASTRO_DATA_DIR", str(TMP / "data"))
os.environ.setdefault("HOME", str(TMP / "home"))
os.environ.setdefault("LOCALAPPDATA", str(TMP / "lad"))

from astrocontrol.cameragate import CameraGate                 # noqa: E402
from astrocontrol.camerahealth import CameraHealth             # noqa: E402
from astrocontrol.devices.base import DeviceError              # noqa: E402

results: list[bool] = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


# -- timeout leaves the call running and discards its result ---------------
gate = CameraGate()
started = threading.Event()
release = threading.Event()


def slow():
    started.set()
    release.wait(2.0)
    return "pixels"


try:
    gate.call(slow, 0.2, "download")
    timed = None
except DeviceError as exc:
    timed = str(exc)
case("a download timeout fails closed",
     timed and "cannot be cancelled" in timed and "download" in timed, timed)
case("the timed-out call quarantines the camera",
     gate.quarantined() and "download" in gate.quarantined())
try:
    gate.call(lambda: "second", 1.0, "exposure")
    overlapped = False
except DeviceError as exc:
    overlapped = "no second camera command" in str(exc)
case("a second exposure is not sent while the download is inside the driver",
     overlapped)

release.set()
case("the late call can finish and clear the quarantine",
     gate.wait_clear(2.0) and gate.quarantined() is None)
# The caller of the timed-out call did not receive the pixels.
case("the timed-out caller did not receive a frame", timed is not None)

# -- late completion is not a successful result ----------------------------
gate = CameraGate()
release = threading.Event()
holder = {}


def late():
    release.wait(2.0)
    return "late-pixels"


def runner():
    try:
        holder["value"] = gate.call(late, 0.15, "download")
    except DeviceError as exc:
        holder["error"] = str(exc)


thread = threading.Thread(target=runner)
thread.start()
time.sleep(0.3)
case("late completion is still quarantined until the driver returns",
     gate.quarantined() is not None and "value" not in holder)
release.set()
thread.join(2.0)
case("the abandoned download then leaves the driver", gate.wait_clear(2.0))
fresh = CameraGate()
token = fresh.begin("download")
fresh.abandon(token, "timed out")
case("a late driver return is discarded", fresh.finish(token) is False)
case("after the late return the caller still has no frame",
     "value" not in holder and "cannot be cancelled" in holder.get("error", ""),
     holder.get("error", ""))

# -- cancellation -----------------------------------------------------------
gate = CameraGate()
cancelled = {"on": False}


def block():
    time.sleep(2.0)
    return "nope"


def cancel():
    return cancelled["on"]


def run_cancel():
    try:
        gate.call(block, 5.0, "readout", cancel=cancel)
    except DeviceError as exc:
        holder_c.append(str(exc))


holder_c: list[str] = []
worker = threading.Thread(target=run_cancel)
worker.start()
time.sleep(0.1)
cancelled["on"] = True
worker.join(2.0)
case("cancellation fails the frame and does not pretend the driver stopped",
     holder_c and "cancelled" in holder_c[0] and "not interrupted" in holder_c[0],
     str(holder_c))
case("a cancelled call quarantines until it returns", gate.quarantined() is not None)
case("quarantine then clears", gate.wait_clear(3.0))

# -- serialization ----------------------------------------------------------
gate = CameraGate()
token = gate.begin("exposure")
try:
    gate.begin("download")
    serial = False
except DeviceError as exc:
    serial = "no second camera command" in str(exc)
gate.finish(token)
case("two camera calls cannot be in flight together", serial)

# -- reconnect is not attempted while quarantined ---------------------------
gate = CameraGate()
hold = threading.Event()


def stuck():
    hold.wait(3.0)


def timed():
    try:
        gate.call(stuck, 0.1, "download")
    except DeviceError:
        pass


threading.Thread(target=timed, daemon=True).start()
time.sleep(0.25)


class Manager:
    def __init__(self):
        self.lines = []

    def get(self, kind):
        return None

    def log(self, message, level="info"):
        self.lines.append((level, message))

    def disconnect(self, kind):
        self.lines.append(("disconnect", kind))


class Capture:
    def __init__(self):
        self.gate = gate
        self._state = "idle"

        class Config:
            def section(self, name):
                return {"cameraQuarantineSeconds": 0.0}

        self.config = Config()


calls = []
health = CameraHealth(
    "FSQ85", Manager(),
    reconnect=lambda stop: calls.append("reconnect") or {"connected": ["camera"], "failed": []},
    capture=Capture())
ok = health.heal("download timed out")
case("reconnect is not sent while a download is still inside the driver",
     ok is False and calls == [] and any("cannot be cancelled" in text
                                         for level, text in health.manager.lines),
     str(health.manager.lines))
case("disconnect was not sent either",
     not any(kind == "disconnect" for kind, text in health.manager.lines))

# -- a failed reconnect stops; it does not keep going ----------------------
hold.set()
gate.wait_clear(3.0)
health.reconnect = lambda stop: {"connected": [], "failed": ["camera: gone"]}
ok = health.heal("still lost")
case("three failed reconnects then a clear stop",
     ok is False and health.state.get("state") == "dead", str(health.state))

# -- 0..0 still refuses StartExposure (the existing hard guard) -------------
from astrocontrol.devices.ascom import AscomCamera             # noqa: E402


class LostCamera(AscomCamera):
    def __init__(self):
        super().__init__("ASCOM.ASICamera2.Camera", "ASI Camera (1)")
        self._connected = True
        self.sent = []

    def probe(self):
        return "its driver reports exposure limits of 0 to 0 s"

    def _invoke(self, name, *args, **kwargs):
        self.sent.append(name)


camera = LostCamera()
try:
    camera.start_exposure(8.0, True)
    guarded = False
except Exception as exc:
    guarded = "was not sent" in str(exc) and camera.sent == []
case("0..0 exposure limits block StartExposure", guarded, str(getattr(camera, "sent", None)))

print()
print(f"{sum(results)} of {len(results)} passed")
sys.exit(0 if all(results) else 1)
