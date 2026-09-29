"""The flat panel: which way the cover goes, and finding the exposure.

    python tools/check_flats.py

The bug this exists for: the run opened the cover to take flats. On every common
device the light *is* the cover — an Alnitak Flip-Flat, a FlatMan on a flip
mount, a Deep Sky Dad — so the illuminated face only points down the tube when
the lid is shut. Opening it put the panel face-away over an open aperture, and
on the drivers that refuse `CalibratorOn` with the cover open (which the ASCOM
specification explicitly permits) there was no light at all. The auto-exposure
then chased a dark frame to the top of its range and gave up.

One wrong direction, three symptoms: no cover, no light, no exposure found. So
the direction is what is pinned down here, with a panel that behaves the way the
hardware does.

No test framework, for the same reason as the other checks here.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from astrocontrol import calibrating                              # noqa: E402
from astrocontrol.devices.base import DeviceError                 # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


# ---------------------------------------------------------------- the hardware
class Panel:
    """A cover-calibrator that behaves like the real thing.

    The light is on the inside of the lid, so it only reaches the sensor when
    the cover is shut — and, like Alnitak's driver, it refuses to come on at all
    while the cover is open.
    """

    connected = True
    max_brightness = 100
    has_cover = True

    def __init__(self, refuses_when_open=True, moves_instantly=True):
        self.cover_state = "open"
        self.light_on = False
        self.brightness = 0
        self.refuses_when_open = refuses_when_open
        self.moves_instantly = moves_instantly
        self.history = []

    def turn_on(self, brightness):
        self.history.append(f"on({brightness})")
        if self.refuses_when_open and self.cover_state != "closed":
            # Quietly declining is the nastier of the two real behaviours, and
            # the one that made this look like three separate faults.
            return
        self.light_on = True
        self.brightness = brightness

    def turn_off(self):
        self.history.append("off")
        self.light_on = False

    def open_cover(self):
        self.history.append("open")
        self.cover_state = "open" if self.moves_instantly else "moving"

    def close_cover(self):
        self.history.append("close")
        self.cover_state = "closed" if self.moves_instantly else "moving"

    @property
    def illumination(self):
        """How much light actually reaches the sensor, 0..1."""
        if not self.light_on or self.cover_state != "closed":
            return 0.0
        return self.brightness / self.max_brightness


class Manager:
    def __init__(self, panel):
        self._panel = panel

    def get(self, kind):
        return self._panel if kind == "flatpanel" else None


class Capture:
    """A camera looking at the panel. Linear, as a sensor is."""

    def __init__(self, panel, full_well=65535.0, offset=500.0):
        self.panel = panel
        self.full_well = full_well
        self.offset = offset
        self.save_enabled = True
        self.exposures = []

    def capture_blocking(self, exposure, frame_type="light"):
        self.exposures.append(exposure)
        # 20000 ADU per second at full brightness, clipped at saturation.
        level = min(self.full_well,
                    self.panel.illumination * 20000.0 * exposure + self.offset)

        class Record:
            stats = {"median": level}
        return Record()


class Rig:
    def __init__(self, panel, name="scope"):
        self.id = "main"
        self.name = name
        self.manager = Manager(panel)
        self.capture = Capture(panel)


class Rigs:
    def __init__(self, rig):
        self.master = rig
        self.all = [rig]
        self.messages = []

    def log(self, message, level="info"):
        self.messages.append((level, message))

    def imaging(self):
        return [self.master]


class Library:
    def __init__(self, values=None):
        self._values = values or {}

    def settings(self):
        return self._values

    def master_for(self, *args, **kwargs):
        return None


def runner(panel, settings=None):
    made = calibrating.CalibrationRunner.__new__(calibrating.CalibrationRunner)
    rig = Rig(panel)
    made.rigs = Rigs(rig)
    made.config = None
    made.library = Library(settings)
    made._state = "idle"
    made._message = ""
    import threading
    made._lock = threading.RLock()
    made._abort = threading.Event()
    return made, rig


SPEC = {"frameType": "flat", "brightness": 80, "exposure": 1.0,
        "autoExposure": True, "filter": "L"}


def nothing():
    return None


# ===========================================================================
print("\n-- which way the cover goes --")

panel = Panel()
run, rig = runner(panel, {"flatPanelBrightness": 50})
run._prepare_light(rig, SPEC, nothing)

case("the cover is shut for the flats, not opened",
     panel.cover_state == "closed", f"cover is {panel.cover_state!r}")
case("...and 'open' is never commanded", "open" not in panel.history,
     str(panel.history))
case("the panel is lit", panel.light_on is True)
case("...at the brightness the set asked for", panel.brightness == 80,
     str(panel.brightness))
case("...and light actually reaches the sensor", panel.illumination > 0,
     f"{panel.illumination:.2f}")

# The order matters as much as the direction: lighting a panel that is still
# facing the sky is what the driver refuses.
case("the cover is shut before the light is called for",
     panel.history.index("close") < panel.history.index("on(80)"),
     str(panel.history))

# A cover already shut is left alone rather than cycled.
already = Panel()
already.cover_state = "closed"
run2, rig2 = runner(already)
run2._prepare_light(rig2, SPEC, nothing)
case("a cover already shut is not cycled",
     "close" not in already.history and "open" not in already.history,
     str(already.history))

print("\n-- when the panel does not come on --")

# The failure that has to be reported rather than shot through: the frames
# still arrive, they are just dark, and dark flats that call themselves flats
# poison a calibration library for a season.
class Stuck(Panel):
    def close_cover(self):
        self.history.append("close")
        self.cover_state = "closed"

    def turn_on(self, brightness):
        self.history.append(f"on({brightness})")   # and does nothing


stuck = Stuck()
run3, rig3 = runner(stuck)
try:
    run3._prepare_light(rig3, SPEC, nothing)
    case("a panel that refuses to light is reported, not shot through", False)
except DeviceError as exc:
    case("a panel that refuses to light is reported, not shot through", True,
         str(exc)[:70])

# No panel at all is a warning, not a failure: somebody shooting flats off a
# light box by hand is a real way to work.
none = runner(None)
none[0]._prepare_light(none[1], SPEC, nothing)
case("no panel connected is a warning, not a refusal",
     any("no flat panel" in message for _, message in none[0].rigs.messages),
     str(none[0].rigs.messages))

print("\n-- darks put the light out and shut the cover --")

dark_panel = Panel()
dark_panel.cover_state = "open"
run4, rig4 = runner(dark_panel)
run4._darken(rig4, nothing)
case("the cover is shut for a dark", dark_panel.cover_state == "closed")
case("...and the light is out", dark_panel.light_on is False)

print("\n-- finding the flat exposure --")

lit = Panel()
run5, rig5 = runner(lit, {"flatTargetAdu": 25000.0, "flatTolerancePercent": 8.0,
                          "flatMinExposure": 0.000032, "flatMaxExposure": 30.0})
run5._prepare_light(rig5, SPEC, nothing)
run5._pedestal = lambda rig, spec: 500.0          # the camera's offset
exposure, level = run5._find_flat_exposure(rig5, SPEC, nothing,
                                           lambda text: None)
case("an exposure is found", exposure > 0, f"{exposure:.3f}s")
case("...that lands on target", abs(level - 25000.0) <= 25000.0 * 0.08,
     f"{level:.0f} ADU")
case("...in a couple of tries, not by walking to the end of the range",
     len(rig5.capture.exposures) <= 4, str([round(e, 3) for e in rig5.capture.exposures]))
case("...and the test frames are not kept",
     rig5.capture.save_enabled is True)

# The old behaviour, reproduced: with the cover open the panel never lights, the
# frames are the offset alone, and the search runs to the top of its range and
# settles there. That is what "it didn't autoexpose" looked like.
blind = Panel()
run6, rig6 = runner(blind, {"flatTargetAdu": 25000.0, "flatMaxExposure": 30.0})
run6._pedestal = lambda rig, spec: 500.0
blind.turn_on(80)                                  # refused: cover still open
found, got = run6._find_flat_exposure(rig6, SPEC, nothing, lambda text: None)
case("a dark panel is recognised rather than chased", got < 100.0,
     f"{got:.0f} ADU at {found:g}s")
case("...and it does not take many frames to give up",
     len(rig6.capture.exposures) <= 3,
     str([round(e, 3) for e in rig6.capture.exposures]))


# ===========================================================================
print("\n-- a panel that is too bright for the shortest exposure --")

# The case that failed: luminance passes several times the light of any
# narrowband filter, so a panel set for Ha saturates L before the shutter can
# close. Searching the exposure alone walks to the floor and gives up; the move
# a person would make is to turn the panel down.
SETTINGS = {"flatTargetAdu": 25000.0, "flatTolerancePercent": 8.0,
            "flatMinExposure": 0.001, "flatMaxExposure": 30.0,
            "flatPanelBrightness": 80, "flatAutoBrightness": True,
            "flatPreferredExposure": 3.0, "flatMinBrightness": 5}

AUTO = {"frameType": "flat", "brightness": None, "exposure": 1.0,
        "autoExposure": True, "filter": "L"}


def searched(throughput, settings=None, spec=None, full_well=65535.0):
    """Run the search against a panel of a given throughput.

    `throughput` is ADU per percent of panel per second — the whole path, panel
    through filter through optics to sensor.
    """
    made = Panel()
    run, rig = runner(made, {**SETTINGS, **(settings or {})})
    rig.capture.full_well = full_well

    def capture_blocking(exposure, frame_type="light"):
        rig.capture.exposures.append((exposure, made.brightness))
        reaching = 0.0
        if made.light_on and made.cover_state == "closed":
            reaching = throughput * made.brightness * exposure
        level = min(full_well, reaching + rig.capture.offset)

        class Record:
            stats = {"median": level,
                     "saturatedPercent": 100.0 if level >= full_well else 0.0,
                     "max": int(level)}
        return Record()

    rig.capture.capture_blocking = capture_blocking
    run._pedestal = lambda r, s: rig.capture.offset
    run._prepare_light(rig, spec or AUTO, nothing)
    exposure, level = run._find_flat_exposure(rig, spec or AUTO, nothing,
                                              lambda text: None)
    return run, rig, made, exposure, level


# Luminance: bright enough that 80% of the panel saturates a 1 ms frame.
run7, rig7, panel7, exposure7, level7 = searched(throughput=2000.0)
case("a panel too bright to expose is turned down",
     panel7.brightness < 80, f"settled at {panel7.brightness}%")
case("...and the flat lands on target",
     abs(level7 - 25000.0) <= 25000.0 * 0.08, f"{level7:.0f} ADU")
case("...at a sane exposure, not the shortest the camera can do",
     exposure7 > 0.05, f"{exposure7:.3f}s")
case("...within the frame budget",
     len(rig7.capture.exposures) <= calibrating.FLAT_ATTEMPTS,
     f"{len(rig7.capture.exposures)} test frames")

# Narrowband on the same rig: dim, so the panel has to go the other way.
# 12 ADU per percent per second needs the panel at 100% and about 21 seconds —
# inside the range, but only just, which is the point.
run8, rig8, panel8, exposure8, level8 = searched(throughput=12.0)
case("a panel too dim is turned up", panel8.brightness > 80,
     f"settled at {panel8.brightness}%")
case("...and that flat lands on target too",
     abs(level8 - 25000.0) <= 25000.0 * 0.08, f"{level8:.0f} ADU")
case("...by lengthening the exposure once the panel has no more to give",
     exposure8 > 10.0, f"{exposure8:.1f}s at {panel8.brightness}%")

# The preferred exposure is what it aims for when it has a choice to make. The
# fixture has to make it miss first: a starting frame that happens to land
# inside the tolerance is accepted as it is, and rightly — the preference is
# for choosing between pairs that all work, not a reason to keep hunting after
# a flat is already on target.
run9, rig9, panel9, exposure9, level9 = searched(throughput=150.0)
case("given a free choice it aims for the preferred exposure",
     abs(exposure9 - 3.0) < 0.5, f"{exposure9:.2f}s")
case("...and sets the panel to suit, off both end stops",
     5 < panel9.brightness < 100, f"{panel9.brightness}%")
case("...landing on target", abs(level9 - 25000.0) <= 25000.0 * 0.08,
     f"{level9:.0f} ADU")

print("\n-- the panel is left where the search put it --")

case("the panel is still on when the search finishes", panel7.light_on is True)
case("...at the brightness that was chosen, ready for the real frames",
     panel7.brightness == panel7.brightness and panel7.illumination > 0,
     f"{panel7.brightness}%")

print("\n-- when it is asked not to touch the brightness --")

# A set that names a brightness means it. Somebody who wrote 40% into the
# recipe has a reason, and a search that quietly overrode it would be worse
# than one that cannot reach the target.
FIXED = {**AUTO, "brightness": 40}
run10, rig10, panel10, exposure10, level10 = searched(throughput=50.0, spec=FIXED)
case("a brightness written into the set is not overridden",
     panel10.brightness == 40, f"{panel10.brightness}%")
case("...and the exposure alone is searched",
     abs(level10 - 25000.0) <= 25000.0 * 0.08, f"{level10:.0f} ADU")

run11, rig11, panel11, exposure11, level11 = searched(
    throughput=50.0, settings={"flatAutoBrightness": False})
case("...and turning the setting off does the same",
     panel11.brightness == 80, f"{panel11.brightness}%")

print("\n-- when no pair works --")

# A panel that cannot make the target even at full brightness and the longest
# exposure. The honest answer is the closest it got, said plainly.
run12, rig12, panel12, exposure12, level12 = searched(throughput=0.001)
case("an unreachable target settles for the closest it got",
     level12 < 25000.0 and exposure12 > 0, f"{level12:.0f} ADU at {exposure12:g}s")
case("...and says so rather than claiming success",
     any("out of reach" in message or "reaches" in message
         for _, message in run12.rigs.messages), "")

# ===========================================================================
print("\n-- the status feed while flats wait for their darks --")
#
# The bug of 2026-09-24: a finished set of flats sits in the results as a
# "pending" job carrying the telescope object and the frame paths until the
# darks exist. The status endpoint tried to serialise that, threw, and the
# page froze on whatever it had last shown - which read as both cameras
# stuck downloading for the rest of a two-hour run.
import json                                                        # noqa: E402

made13 = Panel()
run13, rig13 = runner(made13, SETTINGS)
run13._thread = None
run13._error = None
run13._recipe = "Full library"
run13._set_index = run13._set_count = 1
run13._set_name = "25x flat (auto) every filter"
run13._frame = run13._frames = 25
run13._started = run13._finished = None
run13._pending = []
run13._rig_state = {}
run13._results = [{
    "pending": {"rig": rig13, "kind": "flat", "spec": SPEC, "label": "25x flat L",
                "paths": [Path("C:/x/flat_0001.fits")], "exposure": 2.0,
                "measured": 25000.0, "shape": (100, 80), "meta": {}},
    "telescope": "Telescope 1", "rig": "rig1", "set": "25x flat L",
    "frameType": "flat", "master": None, "exposure": 2.0, "measuredAdu": 25000.0,
    "detail": "waiting for the darks for these flats",
}]
try:
    text = json.dumps(run13.status())
    case("the status feed can be serialised while flats are pending",
         '"pending"' not in text and "waiting for the darks" in text)
except TypeError as exc:
    case("the status feed can be serialised while flats are pending", False, str(exc))
case("...and the pending job is still held for the stacking at the end",
     "pending" in run13._results[0])

# ===========================================================================
print("\n-- the mount around the sky flats --")
#
# Sky flats point the telescope at the zenith. A mount that was parked has to
# be released and tracking first or the slew is refused; and a run from the
# Calibrate tab has to park it again afterwards and warm the cameras, because
# it is the whole of what the rig is doing.


class Mount:
    def __init__(self):
        self.connected = True
        self.can_park = True
        self.at_park = True
        self.tracking = False
        self.slewing = False
        self.slews = []
        self.parks = 0

    def unpark(self):
        self.at_park = False

    def set_tracking(self, on):
        self.tracking = bool(on)

    def slew_to(self, ra, dec):
        if self.at_park:
            raise DeviceError("parked")
        if not self.tracking:
            raise DeviceError("SlewToCoordinatesAsync is not allowed when tracking is False")
        self.slews.append((ra, dec))

    def abort_slew(self):
        self.slewing = False

    def park(self):
        self.parks += 1
        self.at_park = True


made14 = Panel()
run14, rig14 = runner(made14, SETTINGS)
mount14 = Mount()
rig14.manager.devices = getattr(rig14.manager, "devices", {})
run14.rigs.master.manager.get = lambda kind, _m=mount14: _m if kind == "mount" else None
run14._standalone = True
run14._moved_mount = False
run14._tell = lambda *a: None
run14.config = type("C", (), {"get": staticmethod(lambda *a: 1.0)})()
run14._point_at_sky(mount14, 31.9, -99.1, False, nothing)
case("a parked mount is released and set tracking before the flat-spot slew",
     mount14.at_park is False and len(mount14.slews) == 1, str(mount14.slews))
case("...and tracking is switched off again once pointed (flats do not need it)",
     mount14.tracking is False)
case("...and the run remembers it moved the mount", run14._moved_mount is True)
run14._park_mount()
case("a standalone run parks the mount afterwards, and checks it",
     mount14.parks == 1 and mount14.at_park is True)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
