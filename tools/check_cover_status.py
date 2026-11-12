"""Cover badge, status payload, and the open/close/light buttons (0.2.53i).

    python tools/check_cover_status.py

The status request must return while a cover read is hung. Open Cover is
refused during a dark/bias/panel-flat run and while an exposure, slew or
autofocus is in progress. Nothing here commands a roof.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp(prefix="cover-status-"))

from astrocontrol import coverprep, coverstatus, roofguard          # noqa: E402
from astrocontrol.devices.base import DeviceError                    # noqa: E402
from fastapi import HTTPException                                    # noqa: E402
from astrocontrol.main import (get_status, rigs, set_cover,          # noqa: E402
                               set_flat_light)

results: list[bool] = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))


# ---------------------------------------------------------------- mapping
for number, word in ((0, "notpresent"), (1, "closed"), (2, "moving"),
                     (3, "open"), (4, "unknown"), (5, "error")):
    case(f"CoverState {number} is {word}", coverprep.map_cover(number) == word)

case("a failed CoverState read is not NotPresent",
     coverprep.map_cover(None) == "unread")
case("CalibratorStatus ready is the light",
     coverprep.map_calibrator(3) == "ready")
case("CalibratorStatus off is off", coverprep.map_calibrator(1) == "off")

# A value latched at connect is not what the badge or the open decision uses.
panel = SimpleNamespace(connected=True, has_cover=False, cover_state="unknown",
                        light_on=False, brightness=0, max_brightness=100)
case("Unknown is not treated as no cover",
     coverprep.read_state(panel) == "unknown")
case("lights want the cover open", coverprep.wants_open("light"))
case("sky flats want the cover open", coverprep.wants_open("flat", "sky"))
case("panel flats do not", not coverprep.wants_open("flat", "panel"))
case("darks do not", not coverprep.wants_open("dark"))
case("bias does not", not coverprep.wants_open("bias"))
case("dark flats do not", not coverprep.wants_open("darkflat"))


def badge(state, light_on=False, brightness=0, maximum=100, calibrator="off"):
    coverstatus.reset()
    device = SimpleNamespace(connected=True)
    coverstatus.note(device, {
        "connected": True, "coverState": state, "lightOn": light_on,
        "brightness": brightness, "maxBrightness": maximum,
        "calibratorState": calibrator, "readAt": time.time(), "stale": False,
    })
    return coverstatus.observe(coverstatus.cached(device))


for state, text in (
    ("open", "Cover open"),
    ("closed", "Cover closed"),
    ("moving", "Cover moving"),
    ("unknown", "Cover unknown"),
    ("error", "Cover error"),
    ("notpresent", "Cover not present"),
):
    shown = badge(state)
    case(f"badge says {text}", shown["text"].startswith(text), shown["text"])
    case(f"badge state {state}", shown["state"] == state, shown["state"])

lit = badge("closed", light_on=True, brightness=80, maximum=100, calibrator="ready")
case("light on shows brightness over the maximum",
     "light on 80/100" in lit["text"], lit["text"])
dark = badge("open", light_on=False, calibrator="off")
case("light off is on the badge", "light off" in dark["text"], dark["text"])
case("no panel, no badge",
     coverstatus.observe({"connected": False})["connected"] is False)

# ----------------------------------------------------------- the status API
class HangPanel:
    """CoverState never returns. The status request must not wait for it."""

    connected = True
    name = "ASCOM.OptecAlnitak.CoverCalibrator"
    driver_id = "ASCOM.OptecAlnitak.CoverCalibrator"
    max_brightness = 100
    calls = 0

    @property
    def cover_state(self):
        HangPanel.calls += 1
        time.sleep(30)
        return "closed"

    @property
    def light_on(self):
        time.sleep(30)
        return False

    @property
    def brightness(self):
        time.sleep(30)
        return 0

    def status(self):
        snap = coverstatus.cached(self)
        return {"connected": True, "name": self.name, **snap}


hang = HangPanel()
master = rigs.master
previous = master.manager._devices.get("flatpanel")
master.manager._devices["flatpanel"] = hang
try:
    started = time.monotonic()
    payload = get_status()
    elapsed = time.monotonic() - started
    case("/api/status returns while the cover driver is hung",
         elapsed < 1.5 and isinstance(payload, dict) and "cover" in payload,
         f"{elapsed:.2f}s keys={sorted(payload)[:8]}")
    case("the hung read is not reported as Open",
         payload["cover"].get("state") != "open", str(payload["cover"]))
    case("the payload names the cover state",
         "state" in payload["cover"] and "text" in payload["cover"],
         str(payload["cover"]))
    rig_covers = [entry.get("cover") for entry in payload.get("rigs") or []]
    case("each rig in the status payload has a cover",
         bool(rig_covers) and all(isinstance(item, dict) for item in rig_covers),
         str(rig_covers)[:200])
    again = time.monotonic()
    second = get_status()
    case("a second status read does not wait on the hung probe either",
         time.monotonic() - again < 1.5 and "cover" in second,
         f"{time.monotonic() - again:.2f}s")
    time.sleep(0.05)
    case("the hung CoverState read is on the probe, not the request",
         HangPanel.calls >= 1 and elapsed < 1.5, f"calls={HangPanel.calls}")
finally:
    if previous is None:
        master.manager._devices.pop("flatpanel", None)
    else:
        master.manager._devices["flatpanel"] = previous


# ------------------------------------------------------------- the buttons
class ButtonPanel:
    connected = True
    name = "ASCOM.OptecAlnitak.CoverCalibrator"
    max_brightness = 255

    def __init__(self):
        self.commands = []

    def open_cover(self):
        self.commands.append("open")

    def close_cover(self):
        self.commands.append("close")

    def turn_on(self, brightness):
        self.commands.append(f"on:{brightness}")

    def turn_off(self):
        self.commands.append("off")


button = ButtonPanel()
master.manager._devices["flatpanel"] = button
capture = master.capture
saved_state = capture._state
saved_focus = getattr(master.focuser, "_running", None)
try:
    set_flat_light(SimpleNamespace(on=True, brightness=None))
    case("Light on uses full brightness when none is given",
         button.commands[-1] == "on:255", str(button.commands))
    set_flat_light(SimpleNamespace(on=False, brightness=None))
    case("Light off is sent", button.commands[-1] == "off", str(button.commands))

    with roofguard.closed_for("panel flats"):
        refused = False
        try:
            set_cover(SimpleNamespace(on=True))
        except HTTPException as exc:
            refused = exc.status_code == 409 and "cover" in str(exc.detail).lower()
            case("Open Cover during calibration is HTTP 409",
                 refused, str(getattr(exc, "detail", exc)))
        case("and the cover was not opened", "open" not in button.commands,
             str(button.commands))
        set_cover(SimpleNamespace(on=False))
        case("Close Cover is still allowed during calibration",
             button.commands[-1] == "close", str(button.commands))

    capture._state = "exposing"
    try:
        set_cover(SimpleNamespace(on=True))
        case("Open Cover during an exposure is refused", False)
    except HTTPException as exc:
        case("Open Cover during an exposure is refused",
             exc.status_code == 409 and "exposure" in str(exc.detail),
             str(exc.detail))
    capture._state = saved_state

    class Slewing:
        connected = True
        slewing = True

    master.manager._devices["mount"] = Slewing()
    try:
        set_cover(SimpleNamespace(on=True))
        case("Open Cover during a slew is refused", False)
    except HTTPException as exc:
        case("Open Cover during a slew is refused",
             exc.status_code == 409 and "slew" in str(exc.detail),
             str(exc.detail))
    finally:
        master.manager._devices.pop("mount", None)

    if master.focuser is not None:
        master.focuser._running = True
        try:
            set_cover(SimpleNamespace(on=True))
            case("Open Cover during autofocus is refused", False)
        except HTTPException as exc:
            case("Open Cover during autofocus is refused",
                 exc.status_code == 409 and "autofocus" in str(exc.detail),
                 str(exc.detail))
        finally:
            master.focuser._running = saved_focus
    else:
        case("Open Cover during autofocus is refused", False, "no focuser on the rig")

    case("the refused opens never reached the driver",
         button.commands.count("open") == 0, str(button.commands))
finally:
    capture._state = saved_state
    if master.focuser is not None:
        master.focuser._running = saved_focus
    master.manager._devices.pop("flatpanel", None)

import inspect                                                       # noqa: E402
case("the new cover module does not command a roof",
     "open_shutter" not in inspect.getsource(coverprep)
     and "request_open" not in inspect.getsource(coverprep))

failed = len(results) - sum(results)
print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
