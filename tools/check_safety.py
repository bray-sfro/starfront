"""Exercise the weather interlock, the roof, switched power and notifications.

    python tools/check_safety.py

The safety watcher is the only part of this program whose job is the equipment
rather than the data. Everything else failing costs a night; this failing costs
a mirror. So what is checked here is mostly the *unhappy* answers: a monitor
that has been unplugged, one whose driver throws, one that flickers, and one
that clears for a moment in the middle of a cloud bank.

The rule under all of it: **unsafe is the answer whenever the true answer is not
known.** A monitor that has stopped answering is not evidence of good weather.

No test framework, for the same reason as the other checks here.
"""
import os
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol import notify, safety                         # noqa: E402
from astrocontrol.config import Config                          # noqa: E402
from astrocontrol.devices.base import DeviceError               # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Monitor:
    def __init__(self, safe=True, throws=False):
        self.connected = True
        self.name = "Test monitor"
        self._safe = safe
        self.throws = throws

    @property
    def is_safe(self):
        if self.throws:
            raise DeviceError("the monitor is not answering")
        return self._safe


class Rigs:
    def __init__(self, devices):
        self.devices = devices

    @property
    def master(self):
        return self

    @property
    def manager(self):
        return self

    def get(self, kind):
        return self.devices.get(kind)


def watcher(devices, **overrides):
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    if overrides:
        config.update("safety", overrides)
    calls = {"unsafe": 0, "safe": 0, "detail": None}

    def unsafe(detail):
        calls["unsafe"] += 1
        calls["detail"] = detail

    def safe():
        calls["safe"] += 1

    return safety.SafetyWatcher(Rigs(devices), config, on_unsafe=unsafe,
                                on_safe=safe), calls, config


# ------------------------------------------------------- reading the monitor
w, calls, config = watcher({"safetymonitor": Monitor(safe=True)})
case("a monitor saying safe reads safe", w.read() is True)

w, calls, config = watcher({"safetymonitor": Monitor(safe=False)})
case("a monitor saying unsafe reads unsafe", w.read() is False)

# The important one: a driver that throws is not a driver saying yes.
w, calls, config = watcher({"safetymonitor": Monitor(throws=True)})
case("a monitor that throws reads unsafe", w.read() is False)

# Unless explicitly told to trust the sky over the sensor.
w, calls, config = watcher({"safetymonitor": Monitor(throws=True)},
                           unreachableIsUnsafe=False)
case("...unless that has been deliberately turned off", w.read() is None)

# No monitor at all is not the same as unsafe: an observatory without one is
# not being watched, not permanently clouded out.
w, calls, config = watcher({})
case("no monitor reads as nothing to say", w.read() is None)
case("and the rig is treated as safe", w.safe is True)
case("and nothing is being watched", w.watching is False)

w, calls, config = watcher({"safetymonitor": Monitor()}, enabled=False)
case("switching the watch off reads as nothing to say", w.read() is None)

# ------------------------------------------------------------- the grace period
#
# A cloud sensor flickering unsafe for one poll is not weather.
monitor = Monitor(safe=False)
w, calls, config = watcher({"safetymonitor": monitor}, graceSeconds=60.0)
w._poll()
case("a first unsafe reading does not act straight away", calls["unsafe"] == 0)
w._poll()
case("nor does a second, inside the grace period", calls["unsafe"] == 0)

# Past the grace period, it acts — once.
w, calls, config = watcher({"safetymonitor": Monitor(safe=False)}, graceSeconds=0.0)
w._poll()
case("past the grace period it acts", calls["unsafe"] == 1)
w._poll()
w._poll()
case("and does not act again while it stays unsafe", calls["unsafe"] == 1,
     f"{calls['unsafe']} calls")

# A flicker back to safe inside the grace period cancels the countdown.
monitor = Monitor(safe=False)
w, calls, config = watcher({"safetymonitor": monitor}, graceSeconds=60.0)
w._poll()
monitor._safe = True
w._poll()
monitor._safe = False
w._poll()
case("going safe again resets the countdown", calls["unsafe"] == 0)

# --------------------------------------------------------------- coming back
#
# Deliberately much slower: starting up into a gap in the cloud is how a rig
# ends up opening and closing its roof all night.
monitor = Monitor(safe=False)
w, calls, config = watcher({"safetymonitor": monitor},
                           graceSeconds=0.0, resumeAfterSeconds=600.0)
w._poll()
case("it shut down", calls["unsafe"] == 1)
monitor._safe = True
w._poll()
w._poll()
case("a moment of clear sky does not resume the night", calls["safe"] == 0)

config.update("safety", {"resumeAfterSeconds": 0.0})
w._poll()
case("once it has held safe long enough, it does", calls["safe"] == 1)
w._poll()
case("and says so only once", calls["safe"] == 1)

# Unplugging the monitor mid-countdown must not leave it ticking.
monitor = Monitor(safe=False)
devices = {"safetymonitor": monitor}
w, calls, config = watcher(devices, graceSeconds=60.0)
w._poll()
devices.pop("safetymonitor")
w._poll()
case("unplugging the monitor clears the countdown", w.status()["unsafeFor"] is None)

# ---------------------------------------------------------------- what it says
monitor = Monitor(safe=False)
w, calls, config = watcher({"safetymonitor": monitor}, graceSeconds=0.0)
w._poll()
status = w.status()
case("the status says what is happening",
     status["connected"] and status["safe"] is False and status["watching"],
     f"{ {k: status[k] for k in ('connected', 'safe', 'watching')} }")
case("and keeps a short history of the turns",
     any(e["kind"] == "unsafe" for e in status["events"]))

# ------------------------------------------------------------- notifications
config = Config(Path(tempfile.mkdtemp()) / "settings.json")
notifier = notify.Notifier(config)
case("nothing is sent while notifications are off",
     notifier.send("failure", "x") is False)

config.update("notify", {"enabled": True, "webhookUrl": "http://127.0.0.1:9/none"})
case("a wanted event is accepted", notifier.send("failure", "x") is True)

# Rate limited, so a rig failing in a loop does not empty a phone battery.
case("a second message straight away is held back",
     notifier.send("failure", "y") is False)

# ...except the test message, or pressing the button twice looks broken.
case("the test message ignores the rate limit",
     notifier.send("test", "test") is True)

config.update("notify", {"onRecovery": False})
case("an event that was turned off is not sent",
     notifier.send("recovery", "z") is False)

# A dead webhook must be survivable: it runs on its own thread and is recorded,
# never raised.
config.update("notify", {"minSecondsBetween": 0})
notifier.send("failure", "unreachable")
for _ in range(40):
    if notifier.status()["failed"]:
        break
    time.sleep(0.25)
case("a webhook that cannot be reached is recorded, not raised",
     notifier.status()["failed"] >= 1 and notifier.status()["lastError"],
     str(notifier.status()["lastError"])[:60])

case("and the status says whether there is anywhere to send at all",
     notifier.status()["configured"] is True)

config = Config(Path(tempfile.mkdtemp()) / "settings.json")
config.update("notify", {"enabled": True})
case("enabled with nowhere to send is reported as not configured",
     notify.Notifier(config).status()["configured"] is False)

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
