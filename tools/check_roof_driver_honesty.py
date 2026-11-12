"""Driver roof status is never a verified open or closed (0.2.53c).

The installed 0.2.53b build treated ASCOM ShutterStatus as the roof. These
cases use a fake driver object and a fake clock. They do not open a COM
connection, talk to a roof server, or read a driver key.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="roofstatus-"))
os.environ.setdefault("ASTRO_DATA_DIR", str(TMP / "data"))
os.environ.setdefault("HOME", str(TMP / "home"))
os.environ.setdefault("LOCALAPPDATA", str(TMP / "lad"))

import astrocontrol.devices.ascom as ascom_mod                 # noqa: E402
from astrocontrol.devices.ascom import AscomDome               # noqa: E402
from astrocontrol.devices.base import DeviceError              # noqa: E402

results: list[bool] = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Clock:
    def __init__(self):
        self.t = 5000.0

    def monotonic(self):
        return self.t

    def time(self):
        return self.t


CLOCK = Clock()
ascom_mod.time = CLOCK


def fresh():
    dome = AscomDome.__new__(AscomDome)
    dome.can_shutter = True
    dome.can_park = False
    dome.can_slave = False
    dome.name = "test roof"
    dome._prog_id = "ASCOM.HakosRoof.Dome"
    dome._connected = False
    dome._reset_shutter_account()
    dome.store = {}

    def _get(name, default=None):
        if name in dome.store:
            return dome.store[name]
        return default

    dome._get = _get
    dome._open = lambda prog_id: None
    dome._set = lambda *args, **kwargs: None
    dome._invoke = lambda *args, **kwargs: None
    dome._require = lambda: None
    return dome


# 1. Startup, before any driver read.
dome = fresh()
report = dome.shutter_report(dome._published_shutter())
case("startup is unknown, not open",
     report["shutterState"] == "unknown" and report["shutterQuality"] == "unknown")
case("startup is not verified", report["shutterVerified"] is False)

# 2. Integer 0 is the driver's open, still not verified.
dome = fresh()
dome.store["ShutterStatus"] = 0
state = dome.shutter_state
report = dome.shutter_report(state)
case("ShutterStatus 0 is driver open", state == "open" and report["shutterState"] == "open")
case("driver open is not verified", report["shutterVerified"] is False
     and report["shutterQuality"] == "driver")

# 3. A failed read does not keep the previous open.
CLOCK.t += 6
dome.store.pop("ShutterStatus")
case("a failed read is error, not the previous open", dome.shutter_state == "error")
case("that error is not verified open",
     dome.shutter_report()["shutterVerified"] is False
     and dome.shutter_report()["shutterState"] != "open")

# 4. A reading older than 30s with no fresh answer is stale, not open.
dome = fresh()
dome.store["ShutterStatus"] = 0
dome.shutter_state
dome.store.pop("ShutterStatus")
CLOCK.t += 31
report = dome.shutter_report(dome.shutter_state)
case("stale reading is unknown, not open",
     report["shutterState"] == "unknown" and report["shutterQuality"] == "stale",
     str(report))
case("stale reading keeps the last driver word beside it",
     report["shutterLastDriver"] == "open" and report["shutterVerified"] is False)

# 5. Open then closed, no command in progress, is a conflict.
dome = fresh()
dome.store["ShutterStatus"] = 0
dome.refresh_shutter()
CLOCK.t += 1.1
dome.store["ShutterStatus"] = 1
state = dome.refresh_shutter()
report = dome.shutter_report(state)
case("contradictory open/closed reads are a conflict",
     state == "closed" and report["shutterQuality"] == "conflict"
     and "flipped" in (report["shutterConflict"] or "")
     and report["shutterVerified"] is False, str(report))

# 6. Connect that reports Connected false does not become an open roof.
dome = fresh()
dome.store["Connected"] = False
rejected = False
try:
    dome.connect()
except DeviceError:
    rejected = True
status = dome.status()
case("rejected connect is not connected and not open",
     rejected and status.get("connected") is False
     and status.get("shutterState") == "unknown"
     and status.get("shutterVerified") is False, str(status))

# 7. OpenShutter raising does not publish open.
dome = fresh()
dome._connected = True
dome.store["ShutterStatus"] = 1
dome.refresh_shutter()

def refuse(*args, **kwargs):
    raise DeviceError("the roof server refused")

dome._invoke = refuse
raised = False
try:
    dome.open_shutter()
except DeviceError:
    raised = True
report = dome.shutter_report()
case("OpenShutter failure stays not-open and records the error",
     raised and report["shutterState"] == "closed"
     and report["shutterCommandPending"] is False
     and report["shutterCommandError"]
     and report["shutterVerified"] is False
     and report["shutterQuality"] == "error", str(report))

# 8. Accepting OpenShutter does not change a closed reading into open.
dome = fresh()
dome._connected = True
dome.store["ShutterStatus"] = 1
dome.refresh_shutter()
dome._invoke = lambda *args, **kwargs: None
dome.open_shutter()
report = dome.shutter_report(dome.refresh_shutter())
case("command acceptance is pending on the driver's current word",
     report["shutterCommandPending"] is True and report["shutterState"] == "closed"
     and report["shutterVerified"] is False and report["shutterQuality"] == "pending",
     str(report))

# 9. Close pending while the driver says opening is a conflict, not open.
CLOCK.t += 1.1
dome.store["ShutterStatus"] = 0
dome.refresh_shutter()
dome.close_shutter()
CLOCK.t += 1.1
dome.store["ShutterStatus"] = 2
state = dome.refresh_shutter()
report = dome.shutter_report(state)
case("close pending and driver opening is a conflict",
     state == "opening" and report["shutterQuality"] == "conflict"
     and "close command" in (report["shutterConflict"] or "")
     and report["shutterVerified"] is False, str(report))

# 10. The status cache does not follow every poll; a forced refresh does.
dome = fresh()
dome.store["ShutterStatus"] = 0
CLOCK.t += 6
case("fresh read sees open", dome.shutter_state == "open")
dome.store["ShutterStatus"] = 1
CLOCK.t += 1
case("a poll inside the cache window does not replace open by itself",
     dome.shutter_state == "open")
CLOCK.t += 0.2
case("refresh after a second sees the new driver word",
     dome.refresh_shutter() == "closed" and dome.shutter_report()["shutterVerified"] is False)

# 11. A later closed reading settles a close command without calling it verified.
dome = fresh()
dome._connected = True
dome.store["ShutterStatus"] = 2
dome._invoke = lambda *args, **kwargs: None
dome.refresh_shutter()
dome.close_shutter()
CLOCK.t += 1.1
dome.store["ShutterStatus"] = 1
state = dome.refresh_shutter()
report = dome.shutter_report(state)
case("driver closed after CloseShutter is unverified closed, not pending",
     state == "closed" and report["shutterCommandPending"] is False
     and report["shutterVerified"] is False and report["shutterQuality"] == "driver",
     str(report))

print()
print(f"{sum(results)} of {len(results)} passed")
sys.exit(0 if all(results) else 1)
