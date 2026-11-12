"""The roof opened at the start of a target (0.2.53b).

Hakos, 2026-11-04: a sequence imaged a closed roof all night because nothing
ever sent ASCOM.HakosRoof.Dome an OpenShutter. Bray: "you should make it so
that the roof opens when a sequence starts". Bray's standing Hakos rule: a
failed or timed-out open is the technician's lock - never retried, no
close-back, no lights, alert, stop.

The real Sequencer, fake devices, and a simulated clock (no real waiting).
"""

from __future__ import annotations

import os
import sys
import tempfile
import time as _real_time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="roofopen-"))
os.environ.setdefault("ASTRO_DATA_DIR", str(TMP / "data"))
os.environ.setdefault("HOME", str(TMP / "home"))
os.environ.setdefault("LOCALAPPDATA", str(TMP / "lad"))

import astrocontrol.sequencer as sequencer_mod                 # noqa: E402
from astrocontrol.config import Config                          # noqa: E402
from astrocontrol.sequencer import Sequencer, _Skipped          # noqa: E402
from astrocontrol.devices.base import DeviceError               # noqa: E402

results: list[bool] = []
LOG: list[tuple[str, str]] = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Clock:
    """Stands in for the `time` module inside the sequencer."""
    def __init__(self):
        self.t = 1000.0
        self.base = _real_time.time()
        self.hooks = []

    def monotonic(self):
        return self.t

    def time(self):
        return self.base + self.t

    def sleep(self, seconds):
        self.t += max(0.01, float(seconds))
        for hook in list(self.hooks):
            hook(self.t)


CLOCK = Clock()
sequencer_mod.time = CLOCK


class Dome:
    """mode: ok (opens after `travel` s), lock (stays closed), stuck (reads
    opening for ever - HakosRoof 6.2's unknown text), raise (OpenShutter
    throws), error (reads error after the open)."""
    def __init__(self, state="closed", mode="ok", travel=20.0, name="Fake Roof"):
        self.connected = True
        self.can_shutter = True
        self.can_slave = False
        self.can_park = False
        self.name = name
        self.state = state
        self.mode = mode
        self.travel = travel
        self.commands = []
        self._open_at = None

    @property
    def shutter_state(self):
        if self._open_at is not None and CLOCK.t >= self._open_at:
            self.state, self._open_at = "open", None
        return self.state

    def open_shutter(self):
        self.commands.append("open")
        if self.mode == "raise":
            raise DeviceError("the roof server refused")
        if self.mode == "lock":
            return
        if self.mode == "stuck":
            self.state = "opening"
            return
        if self.mode == "error":
            self.state = "error"
            return
        self.state = "opening"
        self._open_at = CLOCK.t + self.travel

    def close_shutter(self):
        self.commands.append("close")
        self.state = "closed"


class Mount:
    connected = True
    at_park = True


class Manager:
    def __init__(self, devices):
        self.devices = devices

    def get(self, kind):
        return self.devices.get(kind)

    def require(self, kind):
        if kind not in self.devices:
            raise DeviceError(f"{kind} not connected")
        return self.devices[kind]

    def log(self, message, level="info"):
        LOG.append((level, message))


class Capture:
    def abort(self):
        pass

    def clear_context(self):
        pass

    def set_output(self, **kw):
        pass

    def set_context(self, **kw):
        pass


class Rig:
    def __init__(self, rid, name, config, devices):
        self.id, self.name, self.config = rid, name, config
        self.manager = Manager(devices)
        self.capture = Capture()
        self.solver = self.focuser = None


class Rigs:
    def __init__(self, rigs):
        self.master = rigs[0]
        self.all = list(rigs)

    def imaging(self):
        return self.all


class Board:
    def __init__(self):
        self.up = {}

    def raise_(self, key, level, title, detail="", fix="", sticky=False):
        self.up[key] = (level, title, detail)

    def clear(self, key):
        self.up.pop(key, None)


class Safety:
    def __init__(self, watching=True, safe=True):
        self.watching = watching
        self.safe_now = safe

    def read(self):
        return self.safe_now if self.watching else None


def build(dome=None, safety=None, follower_dome=None, **settings):
    config = Config(Path(tempfile.mkdtemp(dir=TMP)) / "settings.json")
    if settings:
        config.update("sequencer", settings)
    devices = {"mount": Mount()}
    if dome is not None:
        devices["dome"] = dome
    lead = Rig("main", "FSQ85", config, devices)
    follower = Rig("f1", "Rokinon 135mm", config,
                   {"dome": follower_dome} if follower_dome else {})
    seq = Sequencer(Rigs([lead, follower]), config, None, None)
    seq.safety = safety
    seq.warnings = Board()
    told = []
    seq._tell = lambda event, subject, body="": told.append((event, subject))
    seq._dawn = lambda: None
    seq.told = told
    del LOG[:]
    return seq


def raises(fn):
    try:
        fn()
    except DeviceError as exc:
        return str(exc)
    return None


# 1. normal open, then image
d = Dome()
s = build(d)
ok = s._ensure_roof_open("m42")
case("closed roof: OpenShutter sent once and waited for open", ok and d.commands == ["open"]
     and d.state == "open", f"{d.commands} {d.state}")
case("normal open marks the run as the opener", s._roof_opened_by_run)
case("no safety monitor: warning stays up + alert sent",
     "roof.nosafety" in s.warnings.up and any("no safety monitor" in subj for _, subj in s.told),
     str(s.told))
case("the warning text is the agreed one",
     s.warnings.up.get("roof.nosafety", ("", ""))[1]
     == "Roof opened with no safety monitor - nothing will close it for weather")
# then a light: the gate lets it straight through
before = CLOCK.t
s._roof_gate_lights()
case("open roof: lights pass the gate at once", CLOCK.t == before)

# 2. already open
d = Dome(state="open")
s = build(d)
case("already open: nothing sent", s._ensure_roof_open("m42") and d.commands == [])

# 3. open timeout = locked: no retry, no lights, no close-back
d = Dome(mode="lock")
s = build(d)
why = raises(lambda: s._ensure_roof_open("m42"))
case("open that never reads open: stops with a lock error", why and "lock" in why, why)
case("timed-out open: OpenShutter sent exactly once", d.commands == ["open"], str(d.commands))
case("timed-out open: latched", bool(s._roof_locked))
case("timed-out open: critical warning + alert",
     "roof.locked" in s.warnings.up and any("LOCKED" in subj for _, subj in s.told))
why2 = raises(lambda: s._ensure_roof_open("next target"))
case("latched: the next target sends nothing", why2 and d.commands == ["open"], why2)
s._close_roof()
case("latched and reading closed: no close-back", d.commands == ["open"], str(d.commands))
s2 = build(Dome(mode="lock"), roofOpenTimeoutSeconds=60)
t0 = CLOCK.t
raises(lambda: s2._ensure_roof_open("m42"))
case("the open timeout is the setting (60 s here)", 59 <= CLOCK.t - t0 <= 64, f"{CLOCK.t - t0:.1f}")
s3 = build(Dome(mode="lock"))
t0 = CLOCK.t
raises(lambda: s3._ensure_roof_open("m42"))
case("default open timeout is 180 s", 179 <= CLOCK.t - t0 <= 184, f"{CLOCK.t - t0:.1f}")
# the gate: a latched, closed roof never gets a light
s._roof_gate_lights_was = None
s._dawn = lambda: CLOCK.time() + 300.0      # the night ends in five minutes
try:
    s._roof_gate_lights()
    gated = False
except _Skipped:
    gated = True
case("latched roof: lights held until the night ends, never taken", gated
     and d.commands == ["open"])

# 4. failed open (OpenShutter raises), and the driver's Error
d = Dome(mode="raise")
s = build(d)
why = raises(lambda: s._ensure_roof_open("m42"))
case("OpenShutter raising: locked, sent once", why and d.commands == ["open"]
     and s._roof_locked and "OpenShutter failed" in s._roof_locked, why)
d = Dome(mode="error")
s = build(d)
why = raises(lambda: s._ensure_roof_open("m42"))
case("driver Error while opening: locked after a few reads, no retry",
     why and d.commands == ["open"] and s._roof_locked, why)

# 5. 'opening' is not open
d = Dome(mode="stuck")
s = build(d)
why = raises(lambda: s._ensure_roof_open("m42"))
case("reads 'opening' for ever (HakosRoof 6.2 unknown text): not open -> locked",
     why and "opening" in (s._roof_locked or ""), s._roof_locked)
d = Dome(state="opening")
s = build(d, pauseLightsUnlessRoofOpen=True)
s._dawn = lambda: CLOCK.time() + 120.0
try:
    s._roof_gate_lights()
    held = False
except _Skipped:
    held = True
case("gate: 'opening' holds the lights", held)

# 6. safety unsafe = no open (waits, never opens into it)
d = Dome()
saf = Safety(watching=True, safe=False)
s = build(d, saf)
s._dawn = lambda: CLOCK.time() + 600.0
ok = s._ensure_roof_open("m42")
case("unsafe: no OpenShutter, gives up at the night's end", ok is False and d.commands == [],
     f"{ok} {d.commands}")
d = Dome()
saf = Safety(watching=True, safe=False)
s = build(d, saf)
flip_at = CLOCK.t + 60.0
CLOCK.hooks.append(lambda t: setattr(saf, "safe_now", True) if t >= flip_at else None)
ok = s._ensure_roof_open("m42")
CLOCK.hooks.clear()
case("unsafe then safe: waits, then opens", ok and d.commands == ["open"] and d.state == "open")
case("with a safety monitor: no 'no safety monitor' warning",
     "roof.nosafety" not in s.warnings.up)

# 7. 'require safety monitor' blocks
d = Dome()
s = build(d, None, requireSafetyToOpen=True)
why = raises(lambda: s._ensure_roof_open("m42"))
case("require safety monitor, none: no open, stops with a reason",
     why and "safety monitor" in why and d.commands == [], why)
d = Dome()
s = build(d, Safety(watching=True, safe=True), requireSafetyToOpen=True)
case("require safety monitor, monitor safe: opens", s._ensure_roof_open("m42")
     and d.commands == ["open"])

# 8. closed mid-sequence: lights pause, alert after a minute, resume on open
d = Dome(state="closed")
s = build(d)
reopen_at = CLOCK.t + 150.0
CLOCK.hooks.append(lambda t: setattr(d, "state", "open") if t >= reopen_at else None)
t0 = CLOCK.t
s._roof_gate_lights()
CLOCK.hooks.clear()
case("closed mid-run: lights held until it reads open", CLOCK.t - t0 >= 150.0)
case("closed mid-run: alerted (once) while held", sum("roof is not open" in subj
                                                       for _, subj in s.told) == 1, str(s.told))
case("closed mid-run: the gate never sends a roof command", d.commands == [])
case("closed mid-run: warning cleared once open", "roof.closed" not in s.warnings.up)

# 9. tandem: only the lead drives the roof
lead_dome, f_dome = Dome(), Dome(name="follower roof")
s = build(lead_dome, None, follower_dome=f_dome)
s._ensure_roof_open("m42")
case("tandem: the lead's roof is opened", lead_dome.commands == ["open"])
case("tandem: the follower's dome is never commanded", f_dome.commands == [])
s = build(None, None, follower_dome=Dome(name="follower roof"))
case("tandem: a dome on the follower only is ignored (lead has none)",
     s._ensure_roof_open("m42") and s.rigs.all[1].manager.get("dome").commands == [])

# 10. setting off = old behaviour
d = Dome()
s = build(d, openRoofAtStart=False, pauseLightsUnlessRoofOpen=False)
ok = s._ensure_roof_open("m42")
t0 = CLOCK.t
s._roof_gate_lights()
case("openRoofAtStart off: nothing sent", ok and d.commands == [])
case("both off: lights not held (0.2.53a behaviour)", CLOCK.t == t0)
s._close_roof_after_run()
case("both off: nothing closed at the end that this run did not open", d.commands == [])

# 11. end of run: a roof this run opened is closed after the park
d = Dome()
s = build(d)
s._ensure_roof_open("m42")
s._close_roof_after_run()
case("end of run: the roof this run opened is closed", d.commands == ["open", "close"])
d = Dome()
s = build(d)
s._ensure_roof_open("m42")
s.manager.devices["mount"] = type("M", (), {"connected": True, "at_park": False})()
s._close_roof_after_run()
case("end of run, mount not parked: roof left, failure alert",
     d.commands == ["open"] and any("left open" in subj for _, subj in s.told))

# 12. auto-restart: opens nothing by default (0.2.53a); with reopenRoofOnAutoRestart
# it reopens through the same path (safety safe), never against a lock
d = Dome()
s = build(d, Safety(watching=True, safe=True))
why = raises(s.restart_unpark)
case("auto-restart, default: refuses, opens nothing (as 0.2.53a)", why and d.commands == [], why)
d = Dome()
s = build(d, Safety(watching=True, safe=True), reopenRoofOnAutoRestart=True)
s.manager.devices["mount"] = type("M", (), {"connected": True, "at_park": False,
                                            "can_park": True})()
s._mount_ready = lambda: s.manager.get("mount")
s._release_mount = lambda m: None
s.restart_unpark()
case("auto-restart: the roof is opened before the unpark", d.commands == ["open"])
d = Dome()
s = build(d, Safety(watching=True, safe=True), reopenRoofOnAutoRestart=True)
s._roof_locked = "not open after 180 s"
why = raises(s.restart_unpark)
case("auto-restart: a latched lock is never opened against", why and d.commands == [], why)

# 13. ASCOM ShutterStatus 0 is open (0.2.53a read it as error)
from astrocontrol.devices.ascom import AscomDome               # noqa: E402
ad = AscomDome.__new__(AscomDome)
ad.can_shutter = True
reads = {}
ad._get = lambda name, default=None: reads.get(name, default)
got = {}
for value, want in [(0, "open"), (1, "closed"), (2, "opening"), (3, "closing"), (4, "error")]:
    reads["ShutterStatus"] = value
    got[value] = ad.shutter_state
reads.clear()
got["none"] = ad.shutter_state
case("ASCOM dome: 0=open 1=closed 2=opening 3=closing 4=error, no answer=error",
     got == {0: "open", 1: "closed", 2: "opening", 3: "closing", 4: "error", "none": "error"},
     str(got))

# 14. the settings route takes the new fields
from astrocontrol.main import SequencerSettingsRequest         # noqa: E402
body = SequencerSettingsRequest(openRoofAtStart=True, requireSafetyToOpen=False,
                                roofOpenTimeoutSeconds=180, pauseLightsUnlessRoofOpen=True,
                                closeRoofAtEnd=True)
case("settings route accepts the roof fields", body.roofOpenTimeoutSeconds == 180)
from astrocontrol.config import DEFAULTS                       # noqa: E402
seqd = DEFAULTS["sequencer"]
case("defaults: open on, require off, 180 s, gate on, close at end on",
     (seqd["openRoofAtStart"], seqd["requireSafetyToOpen"], seqd["roofOpenTimeoutSeconds"],
      seqd["pauseLightsUnlessRoofOpen"], seqd["closeRoofAtEnd"]) == (True, False, 180.0, True, True))

# 15. order inside a real target: the roof is open before the first slew/home
d = Dome()
s = build(d)
s._site = lambda: (-23.24, 16.36)
seen = {}
def first_move(panel, label, rotate=True):
    seen["state"] = d.state
    raise _Skipped()
s._goto = first_move
s._rotate_for = lambda entry, target: False
entry = {"id": "e1", "name": "M42", "filters": []}
target = {"id": "t1", "ra": 5.6, "dec": -5.4}
try:
    s._run_entry(entry, target)
except _Skipped:
    pass
case("real _run_entry: OpenShutter before the first home/slew, open by then",
     d.commands == ["open"] and seen.get("state") == "open", str(seen))
d = Dome(mode="lock")
s = build(d)
s._site = lambda: (-23.24, 16.36)
seen.clear()
s._goto = first_move
s._rotate_for = lambda entry, target: False
why = raises(lambda: s._run_entry(entry, target))
case("real _run_entry: a locked roof stops the target before anything moves",
     why and "state" not in seen, why)

print()
print(f"{sum(results)} of {len(results)} passed")
sys.exit(0 if all(results) else 1)
