"""Persistent settings: where the telescope stands, what it looks through, and
how it plate-solves.

These are the few things Starfront cannot learn from a driver.  They live in
one small JSON file beside the captures, so a rig keeps its setup between runs.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


#: Where everything a rig accumulates lives: settings, targets, sequences,
#: captures, the calibration library and the logs.
DATA_DIR = "Starfront"

#: What that folder was called when the program was AstroControl. An install
#: that already has one keeps using it.
FORMER_DATA_DIR = "AstroControl"


def data_root() -> Path:
    """The folder this rig keeps everything in.

    A rig that already has an `AstroControl` folder goes on using it, renaming
    notwithstanding. The alternative is a program that starts up one morning
    having forgotten its site, its filters, its targets and every sequence — all
    of which are still on disk under the old name, which makes it worse rather
    than better: nothing is lost and nothing can be found.

    Nothing is moved and nothing is copied. The rename is a name.
    """
    override = os.environ.get("ASTRO_DATA_DIR")
    if override:
        return Path(override)
    home = Path.home()
    former = home / FORMER_DATA_DIR
    if former.is_dir():
        return former
    return home / DATA_DIR


# Every key the UI may write, with the value used when the file has nothing to
# say.  Unknown keys are dropped on save so a typo cannot quietly persist.
DEFAULTS: dict[str, dict[str, Any]] = {
    "site": {
        # There is nothing sensible to guess: the mount usually knows, and the
        # planetarium asks for the coordinates when it does not.
        "latitude": None,          # degrees north
        "longitude": None,         # degrees east of Greenwich, negative west
        "elevation": 0.0,          # metres
        "useMount": True,          # prefer what the mount driver reports
    },
    "optics": {
        "focalLength": None,       # millimetres, for the field-of-view rectangle
        # The camera angle: the position angle of the sensor's up axis, 0 being
        # north up. Everything that has to know what shape of sky the sensor
        # covers depends on it — mosaic panels, survey footprints, and the
        # all-sky grid, which cannot tile without it.
        "rotation": 0.0,
        # Keep that number honest by taking it from every plate solve. Skipped
        # when a rotator is connected, where the angle is commanded per target
        # rather than fixed by how the camera sits in the focuser.
        "angleFromSolve": True,
        # The sensor is read from the camera when one is connected.  These are
        # the fallback, so a framing can be planned in the afternoon with the
        # rig switched off, which is when planning actually happens.
        "sensorWidth": None,       # pixels
        "sensorHeight": None,      # pixels
        "pixelSize": None,         # microns
        # What the optics actually correct, when that is smaller than the
        # sensor.  A RASA 8's 22 mm image circle does not reach the corners of
        # an APS-C chip, so tiling on the sensor field leaves the seams uncovered
        # by anything usable.  Degrees; blank means the whole sensor is good.
        "usableWidth": None,
        "usableHeight": None,
    },
    "camera": {
        # What is in this telescope's filter wheel, in slot order. Per telescope
        # because two scopes on one mount routinely carry different filters, and
        # planning the second one's night from the first one's wheel would be
        # quietly wrong. Used whenever the wheel itself cannot be asked — which
        # is most of the time, since planning happens in the afternoon with
        # everything switched off.
        "filterNames": [],
        # What is actually in the light path when there is no wheel to ask.
        # A RASA carries its filter in a drawer at prime focus: nothing can move
        # it, but the frames still have to say what it was, and the plan still
        # has to be able to allocate to it.
        "fixedFilter": "",
        # A one-shot colour camera. Set from the camera's own Bayer matrix the
        # first time it connects, and kept, so a collaboration can be told in
        # the afternoon with everything switched off.
        "colour": False,
        # Applied when a sequence starts, so a night always begins from the
        # same settings whatever was fiddled with during the afternoon.
        "gain": None,
        "offset": None,
        "setpoint": -10.0,
        "coolAtStart": True,
        "warmAtEnd": True,
        "coolToleranceC": 1.0,
        "coolTimeoutMinutes": 20.0,
        # Whether the sequence stands still while the sensor comes down to
        # temperature. Off by default: the cooler is switched on at the same
        # moment either way, and homing, slewing, centring and focusing are all
        # things the camera's temperature has nothing to do with. The wait then
        # happens where it actually matters — immediately before the first light
        # frame — by which time most of it has already elapsed.
        "waitForCooling": False,
        # Filter name -> bandpass in nanometres. Only narrowband filters have
        # one that matters, and it matters a great deal: a 3 nm Ha and a 7 nm Ha
        # are not the same data, and a collaboration is entitled to say which it
        # will take. Blank for a filter means "not stated".
        "filterBandpass": {},
        # Warming is ramped rather than switched off: a fast temperature change
        # risks condensation on the sensor window.
        "warmRateCPerMinute": 3.0,
    },
    # Weather, cloud, rain — whatever the observatory's safety monitor
    # aggregates into one answer. This is the only setting group in the program
    # that exists to protect the equipment rather than the data.
    "safety": {
        # Watch the monitor at all. Off only for a rig under a cover that
        # somebody is standing next to.
        "enabled": True,
        # What to do when it says unsafe. "shutdown" stops the sequence, parks,
        # closes the cover and the roof, and warms the cameras. "pause" holds
        # the run where it is and waits for safe to come back — right for a
        # roll-off that closes itself, wrong for one that does not.
        "onUnsafe": "shutdown",
        # How long it has to read unsafe before anything happens. A cloud sensor
        # flickering for one poll is not weather; ten seconds of it is.
        "graceSeconds": 15.0,
        # ...and how long it has to read safe again before a paused run resumes,
        # which is deliberately much longer. Starting up into a gap in the cloud
        # is how a rig ends up opening and closing all night.
        "resumeAfterSeconds": 600.0,
        # Whether a monitor that cannot be reached counts as unsafe. It does:
        # a monitor that has stopped answering is not evidence of good weather.
        # Turning this off is for a monitor known to be flaky, and it is a
        # decision to trust the sky instead of the sensor.
        "unreachableIsUnsafe": True,
        # Refuse to start a sequence at all while it reads unsafe.
        "blockStart": True,
    },
    # The camera that watches the telescope rather than the sky. It has to cope
    # with afternoon sun through to a pitch-dark dome on the same night with
    # nobody awake to adjust it, so it runs itself.
    "piercam": {
        "enabled": True,
        # Auto-exposure and auto-gain. On by default, and the reason the slot
        # exists at all: a fixed exposure is wrong within an hour of sunset.
        "auto": True,
        # The brightness the loop aims at, 0..1 of full scale, measured at
        # `meterPercentile`. About a third is where a lit telescope against a
        # dark dome looks right without clipping the bright metal.
        "target": 0.35,
        "meterPercentile": 95.0,
        # How far off target it has to be before anything moves. A live feed
        # that breathes gently all night is worse to watch than one slightly off.
        "deadband": 0.08,
        # How much of the correction to apply per frame, 0..1. Below 1 the loop
        # converges over a few frames instead of ringing.
        "damping": 0.6,
        # The range the loop may use. The bottom is daylight, the top is a dark
        # dome — and the top also sets how slow the feed can get.
        # A tenth of a millisecond: daylight on a white dome through a fast lens
        # needs the very bottom of what a camera can do, and a floor set too
        # high shows up as a white rectangle every afternoon.
        "minExposure": 0.0001,
        "maxExposure": 8.0,
        # Blank means the camera's own range. A ceiling is worth setting: the
        # top of an ASI's gain is unusable noise, and an auto-gain loop with
        # nothing to stop it will find its way there on the first dark night.
        "minGain": None,
        "maxGain": None,
        "gainStep": 0,                      # 0 means a tenth of the range
        # Used only when `auto` is off.
        "exposure": 0.1,
        "gain": None,
        # How often to take one, and how big to send it. This is a look at the
        # telescope, not a webcam: a frame a second at 720 px is plenty and
        # costs almost nothing.
        "intervalSeconds": 1.0,
        "maxDim": 720,
        "gamma": 2.2,
    },
    "capture": {
        # Drop a copy of the session log into the night's capture folder when a
        # sequence ends. The log itself lives under the data directory on the
        # machine that ran the night, which is no use at all when that machine
        # is at a remote site and you are not. The capture folder is usually
        # synced somewhere — that is where the data goes — so a copy there is a
        # copy you can actually read in the morning.
        "logWithData": True,
        # Blank means the default under the data directory. Frames go into
        # <root>/<target>/<night>, so one target's data across many nights sits
        # together and one night's work is a single folder.
        "rootDirectory": "",
    },
    "allsky": {
        # Defaults for a new all-sky survey. Once one is created these are
        # frozen into it: the grid has to mean the same thing in October as it
        # did in March, or the progress record against it is worthless.
        "decMin": -30.0,           # what a northern site can actually reach
        "decMax": 90.0,
        # At the equator. It grows towards the poles on its own, because a field
        # covers more degrees of RA the nearer the pole it sits — see allsky.py.
        "overlap": 0.20,
        # A field is only worth shooting where the air is thin. 40 degrees is
        # airmass 1.56; below that a survey frame costs more than it returns.
        "minAltitude": 40.0,
        # Above this, extra altitude buys nothing: airmass at 70° is 1.06 and at
        # the zenith 1.00. Without a ceiling the survey would spend a year on
        # the few rings either side of the zenith before touching the rest.
        "qualityAltitude": 70.0,
        # How far past the meridian a field may be shot. Nothing is ever shot
        # *before* the meridian, which is what removes the flips entirely.
        "maxHourAngle": 3.0,
        "minimiseFlips": True,
        "moonAvoidance": 30.0,
        "moonScaleByPhase": True,
        # Degrees of score given up per hour past the meridian, which is about
        # the rate a field near the zenith really loses altitude.
        "transitPenalty": 6.0,
        # Degrees of score given up per degree of slew to the next field. This
        # is what makes the survey walk its neighbours instead of hopping about
        # the sky: above the quality ceiling every candidate is equally good, so
        # the nearest one wins.
        "slewWeight": 2.0,
        # Degrees of score a part-finished field is given over an untouched one.
        # Large on purpose: a finished field can be stacked and looked at, and a
        # half-finished one cannot, so the survey leaves completed sky behind it
        # rather than a uniform smear of started fields.
        "finishBonus": 25.0,
        # The longest one visit to a field may last, in minutes. 0 means "the
        # whole goal in one go".
        #
        # This is the setting that decides whether a night comes out as a strip
        # you can assemble or a scatter you cannot. The sky turns a new field
        # onto the meridian every nine minutes or so at the equator, so a field
        # that takes longer than that cannot be followed by its neighbour — and
        # a forty-minute field lands as a dozen unconnected patches across half
        # the sky. Capped at ten minutes, the same night walks one joined-up
        # run of touching fields, and the depth is built up by coming back.
        "visitMinutes": 10.0,
    },
    "calibration": {
        # Where the master frames live. Blank means a folder beside the data,
        # but a library is usually worth keeping somewhere it gets backed up:
        # a season of darks is hours of closed-shutter time that cannot be
        # taken again on the night they turn out to be missing.
        "libraryDirectory": "",
        # Applying them to lights as they are captured. The raw frame is always
        # kept; a calibrated copy goes in a `calibrated` subfolder beside it.
        "applyTo": "survey",       # survey|all|off
        # What counts as a master that describes this frame. A dark taken at a
        # different temperature or a noticeably different exposure is not a
        # correction, it is a second source of error.
        "matchTemperatureC": 2.0,
        "matchExposurePercent": 5.0,
        # Dust moves, so a flat goes stale far sooner than a dark does.
        "maxFlatAgeDays": 30.0,
        "maxDarkAgeDays": 180.0,
        # Building masters.
        "stackMethod": "sigma",    # sigma|median|mean
        "sigmaLow": 3.0,
        "sigmaHigh": 3.0,
        "keepSubs": True,          # keep the individual frames after stacking
        # Flats. The exposure is found by measuring rather than guessed: what
        # reaches the sensor depends on the panel, the filter and the optics.
        "flatTargetAdu": 25000.0,
        "flatTolerancePercent": 8.0,
        # A fast astrograph in daylight needs microseconds, not tens of
        # milliseconds: a RASA under a sunlit sky is at the target level in well
        # under a thousandth of a second, and a floor set any higher simply
        # reports that the sky is too bright for a telescope that was working
        # fine. The default is what most CMOS cameras can actually do, so the
        # driver's own limit is the only one that binds. A camera that cannot
        # go this short clamps to whatever it can, which is the honest answer.
        "flatMinExposure": 0.000032,
        "flatMaxExposure": 30.0,
        # Where the panel is set when nothing else decides. With the setting
        # below on, this is only a starting guess for the first frame.
        "flatPanelBrightness": 50,
        # Dim the panel as well as shortening the exposure. A panel that is too
        # bright for the shortest exposure the camera can take cannot be exposed
        # correctly at all, and luminance is where that bites first: it passes
        # several times the light of any narrowband filter, so the panel setting
        # that suits Ha saturates L before the shutter can close.
        "flatAutoBrightness": True,
        # The exposure the search aims to land on, given a free choice of
        # brightness. Long enough that panel flicker and any shutter travel
        # average out, short enough that a full set is not an evening's work.
        # The search only leaves this band when the panel cannot be dimmed or
        # brightened enough to stay inside it.
        "flatPreferredExposure": 3.0,
        # The panel is not driven below this. Most dim by pulse width, and at
        # the bottom of the range the pulses start to beat against short
        # exposures and put bands across the frame.
        "flatMinBrightness": 5,
        # Sky flats: twilight through the telescope instead of a panel.
        #
        # Where to point. The zenith is the traditional answer and the one that
        # needs no thought; the anti-solar point at seventy-odd degrees is
        # measurably flatter, because that is the part of the twilight sky
        # furthest from both the solar gradient and the horizon glow.
        "skyFlatPointing": "zenith",   # zenith|antisolar
        "skyFlatAltitude": 80.0,       # only used for the anti-solar point
        # The sky is full of stars, so the telescope is moved between frames
        # and the stack combines them out. Without this a sky flat has the
        # brightest few hundred stars burned into it.
        # Flats do not need the telescope to track: nothing in the frame has to
        # stay still for a fraction of a second, and a field that drifts is a
        # field whose stars land somewhere new each time, which is what a flat
        # wants anyway. With tracking off the mount never crosses the meridian,
        # so there is no flip to design around and the true zenith is fine.
        "skyFlatTracking": False,
        # Only meaningful when tracking is left on: a German equatorial parked
        # at the zenith is sitting on the meridian, and a flip part way through
        # a set turns the second half of the flats upside down against the
        # first. Irrelevant while the mount is not tracking.
        "skyFlatMeridianOffset": 0.0,
        "skyFlatDitherArcmin": 2.0,
        # Twilight changes by a factor of two every few minutes, so the
        # exposure is re-derived from every frame. A frame that lands this far
        # from the target is thrown away rather than stacked.
        "skyFlatAcceptPercent": 40.0,
        "skyFlatSettleSeconds": 2.0,
        # A fast telescope saturates at its shortest exposure while the sky is
        # still bright, and in the evening that fixes itself in a minute or two.
        # So the set waits rather than giving up — but only for this long, and
        # only when the sky is heading the right way.
        "skyFlatMaxMinutes": 20.0,
        "skyFlatPollSeconds": 5.0,
        # Put the light out and open the cover before any light frame.
        #
        # Calibration closes the cover for the darks, and a night that starts
        # with dusk flats leaves the panel lit. Neither should be something you
        # have to remember: the frame that needs sky is the one that knows it
        # needs sky, so it opens the way itself.
        "autoCover": True,
        "coverTimeoutSeconds": 180.0,
    },
    "schedule": {
        "minAltitude": 30.0,       # degrees; below this a target is not worth shooting
        # What the rig costs between exposures. These are the fallback: once it
        # has timed itself the measured figures are used instead, and these stay
        # here untouched for a rig that has never run — or for one whose owner
        # would rather plan on numbers they chose.
        "useMeasured": True,
        "perFrameSeconds": 15.0,   # download and dither between subs
        "filterChangeSeconds": 20.0,
        "perPanelSeconds": 90.0,   # slew, settle and plate solve for each panel
        # Used when no filter wheel is connected, so a plan can still be made
        # in the afternoon.
        "filters": ["L", "R", "G", "B", "H", "O", "S"],
    },
    # What Auto-arrange puts in the slots it works out. Everything here is a
    # starting point the operator then edits on the plan — the arranger writes
    # an allocation, it does not shoot anything.
    "autoplan": {
        # Whether Auto-arrange chooses filters and exposures at all, or only
        # works out the running order and the times as it always did.
        "chooseExposures": True,
        # The sub length to use for each filter by name, under a dark sky.
        # This is the one that actually decides an exposure; the three by class
        # below are only the fallback for a filter with no entry here, so a
        # wheel with something unusual in it still gets a sensible answer.
        "filterExposures": {
            "L": 120.0, "R": 180.0, "G": 180.0, "B": 180.0,
            "H": 600.0, "O": 600.0, "S": 600.0,
        },
        # Sub lengths under a dark sky, by what the filter is for.
        "luminanceExposure": 120.0,
        "broadbandExposure": 180.0,
        "narrowbandExposure": 600.0,
        "minExposure": 30.0,
        # Above this lit fraction, with the Moon up, the night is a narrowband
        # night for any target that carries the filters for it.
        "narrowbandAboveIllumination": 0.40,
        # Far enough from the Moon that even broadband is fine whatever the
        # phase. The Moon's glow falls off steeply with distance; beyond about
        # ninety degrees it is the sky's own brightness that dominates.
        "moonSafeDegrees": 90.0,
        # Under a bright Moon a broadband sub fills with skyglow sooner, and the
        # answer is more shorter frames rather than fewer longer ones.
        "shortenUnderMoon": True,
        # Fewer frames than this through one filter is not worth the change it
        # costs, so that time goes to the filters that can carry a real count.
        "minFramesPerFilter": 3,
    },
    "sequencer": {
        # Autofocus triggers: whichever comes first wins.
        "autofocusOnStart": True,
        "autofocusOnFilterChange": True,
        "autofocusIntervalMinutes": 60.0,
        "autofocusTemperatureDelta": 1.0,   # degrees C since the last run
        # Refocus when the stars have grown this much against the size they
        # were right after the last successful focus run. 0 turns it off.
        "autofocusHfrPercent": 15.0,
        "focusExposure": 6.0,               # seconds per sweep point
        "focusStepSize": 100,               # focuser steps between points
        "focusPoints": 9,
        "focusBacklash": 0,                 # steps to overshoot when reversing
        # How the curve is turned into a position.  Trend lines fit a straight
        # line to each arm of the V and take the crossing point; the hyperbola
        # is the shape a defocus curve actually has.  Averaging the two is what
        # NINA does by default and is the steadiest of the lot.
        "focusMethod": "trendhyperbolic",   # trendlines|parabolic|hyperbolic|trendparabolic|trendhyperbolic
        "focusFramesPerPoint": 1,           # averaged, for noisy skies
        "focusAttempts": 3,                 # whole-run retries before giving up
        # A run is only accepted if the stars at the chosen position are no
        # worse than this multiple of what they were before it started.
        "focusMaxHfrRatio": 1.15,
        # Which filter to sweep on. Blank means "whatever is in the path", which
        # is what a single-filter rig wants. Naming one means every sweep is
        # measured through the same glass, so the offsets below stay meaningful
        # and a narrowband sweep does not have to find stars through 3 nm of Ha.
        "autofocusFilter": "",
        # Steps to add to the focuser when each filter is selected, relative to
        # whatever the last focus run landed on. Filters are not parfocal, and a
        # known offset is far quicker than a sweep on every change.
        # {"L": 0, "Ha": -140, ...}; a filter with no entry moves nothing.
        "filterOffsets": {},
        "useFilterOffsets": True,
        # A German equatorial has to flip; a fork mount reports no side of pier
        # and is left alone regardless of this setting.
        "meridianFlipEnabled": True,
        "flipPauseMinutes": 5.0,            # stop imaging this long before it
        "flipAfterMinutes": 2.0,            # and flip this long after it
        "flipSolve": True,                  # re-centre by plate solve after it
        "settleSeconds": 10.0,              # after every slew
        # What to do with the mount when the plan runs out. Parking is the safe
        # answer for an observatory that closes a roof behind it; a rig under a
        # cover is usually better left tracking where the operator left it.
        "parkAtEnd": False,
        "stopTrackingAtEnd": False,
        # Send the mount to its home switches before the first slew of the
        # night. A mount that has been power-cycled, nudged, or left parked by
        # somebody else does not know where it is, and every slew after that is
        # wrong by the same amount. Skipped silently by a mount that cannot home.
        "homeAtStart": True,
        # How long the homing run is given before the night goes on without it.
        "homeTimeoutMinutes": 5.0,
        # Run on loop: how long before astronomical dusk each night opens, so
        # cooling, homing and the first focus happen in twilight and the first
        # frame is taken in the dark.
        "loopLeadMinutes": 20.0,
        # Stop at the end of astronomical dark whatever the plan says. The
        # backstop the plan does not provide: a target with no end time set has
        # no end time at all, so without this a plan of several targets goes on
        # slewing, focusing and exposing into full daylight, one target at a
        # time, until it runs out of targets.
        "stopAtDawn": True,
        # Stop this long before dark actually ends, so the last frame finishes
        # in the dark rather than starting in it.
        "dawnMarginMinutes": 10.0,
        # How long shutting down waits for each moving part before reporting it
        # as stuck. A roll-off on a long screw drive is minutes; a flip-flat is
        # seconds. Both are reported rather than waited on for ever, because an
        # abort that never finishes is an abort that never parked.
        "coverTimeoutSeconds": 120.0,
        "roofTimeoutSeconds": 300.0,
        "parkTimeoutSeconds": 300.0,
        # How long Abort waits for the run to notice before parking anyway. It
        # has to cover the slowest thing the sequencer can be inside — a guider
        # settling, or a focus sweep — or the mount gets parked out from under a
        # run that then slews away again.
        "abortTimeoutSeconds": 300.0,
        # Watching the run: every sub is measured for star size, and every Nth
        # is plate solved to prove the telescope is still where it should be.
        "measureFrames": True,
        "solveEveryFrames": 10,             # 0 turns the pointing check off
        "pointingWarnArcmin": 5.0,
        "focusWarnPercent": 25.0,           # HFR drift that is worth flagging
    },
    # What the night does when something goes wrong at three in the morning.
    #
    # Shared rather than per telescope, because everything here is about the
    # mount and the guider: there is one of each, and a lost guide star stops
    # every telescope on the mount at once.
    "recovery": {
        "enabled": True,
        # -- the guide star ------------------------------------------------
        # PHD2 usually finds a star again by itself within an exposure or two,
        # so the grace period is there to stop a passing cloud triggering a
        # full re-acquisition that costs more time than the cloud did.
        "guidingRecovery": True,
        "guideGraceSeconds": 25.0,
        "guideRestartAttempts": 3,
        # Re-select a star and restart guiding; after this many failures the
        # attempt includes a fresh calibration, which is slow but is the only
        # thing that fixes a mount that has been moved by hand.
        "recalibrateAfterAttempts": 2,
        # A sub exposed while the guider had no lock is very likely trailed, and
        # is always flagged on the sub chart. Moving it out of the way as well
        # is off by default because it may be perfectly usable — that is a
        # judgement for the morning with the stack open, not for three a.m.
        # Nothing is ever deleted either way.
        "discardLostFrames": False,
        # -- the pointing ---------------------------------------------------
        # The pointing check already solves every Nth sub. Past this much drift
        # the object is heading out of frame, so slew back and solve on.
        "pointingRecovery": True,
        "recentreArcmin": 10.0,
        "recentreAttempts": 2,
        # -- the camera -----------------------------------------------------
        # A single failed download is not a reason to end the night; a camera
        # that has fallen off the USB bus is, and the difference is whether it
        # comes back when asked again.
        "frameRetryAttempts": 3,
        "frameRetrySeconds": 20.0,
        "reconnectDevices": True,
        # -- giving up ------------------------------------------------------
        # Recoveries that keep happening are a rig problem, not a weather one.
        "maxPerTarget": 8,
        "cooldownSeconds": 30.0,
        # What happens when recovery has run out of ideas: carry on with the
        # next target, or stop the night and make the mount safe.
        "onGiveUp": "next",        # next|stop|park
    },
    "guiding": {
        # PHD2 is a separate program, so connecting to it means making sure it
        # is running, that it has a profile loaded, and that its own camera and
        # mount are connected - the same three steps NINA and SGP take.
        "phd2Path": "",            # blank means "look in the usual places"
        "autoStartPhd2": True,     # launch it if the server does not answer
        "phd2Profile": "",         # equipment profile to load; blank keeps the current one
        "connectEquipment": True,  # tell PHD2 to connect its own camera and mount
        # Before guiding, PHD2 needs a star. Looping and auto-selecting one is
        # what the other clients do rather than hoping `guide` finds it.
        "autoSelectStar": True,
        # Guiding has to be *started*. Dithering between subs is no use if
        # nothing ever told PHD2 to guide in the first place.
        "startWithSequence": True,
        "settleTimeoutSeconds": 180.0,   # before the first frame of a target
        "ditherEnabled": True,
        # Every second frame. Dithering after every single sub spends a settle —
        # eight seconds or so — on every frame of the night, and pairs of subs
        # at the same position still stack out walking noise perfectly well.
        # A target that wants a different interval says so in its own options.
        "ditherEveryFrames": 3,
        "ditherPixels": 3.0,
        "ditherRaOnly": False,
        "settlePixels": 1.5,
        "settleTime": 8.0,
        "settleTimeout": 60.0,
    },
    "survey": {
        # The sweep region, in the Sun's frame. Not RA and Dec: see
        # solarsystem.py for why that is the whole point.
        "elongationMin": 30.0,
        "elongationMax": 60.0,
        "betaMin": -30.0,
        "betaMax": 30.0,
        "side": "morning",         # morning|evening|both
        # When to run. Twilight is the binding constraint - about 45 minutes.
        "sunHigh": -8.0,           # start when the Sun is this far down
        "sunLow": -18.0,           # stop when it reaches this
        # What counts as observable.
        "minAltitude": 20.0,       # working band is 20-30; hard floor at 15
        "maxAirmass": 0.0,         # 0 lets the altitude limit decide
        "moonAvoidance": 40.0,      # the radius for a *full* Moon
        # Scale that radius with the lit fraction. A waning crescent sits in
        # the morning twilight zone on its way to conjunction, so holding it to
        # the full-Moon distance throws away the darkest mornings of every
        # lunation for the sake of a 5%-lit sliver.
        "moonScaleByPhase": True,
        "galacticAvoidance": 10.0,  # degrees from the plane; 0 turns it off
        "revisitNights": 5.0,      # do not re-shoot a cell sooner than this
        # Acquisition. Synthetic tracking wants many short exposures on one
        # field in a single burst, not a few subs revisited later.
        "exposure": 30.0,
        "exposureCount": 36,       # 11 is the detection software's minimum
        "binning": 2,
        "dither": True,            # mandatory: without it pattern noise stacks
        "ditherPixels": 3.0,
        "ditherSeconds": 4.0,      # settle allowance between subs, for planning
        "panelOverheadSeconds": 25.0,
        "overlap": 0.08,           # an object in a seam is a missed discovery
        "filter": "",
    },
    "solver": {
        "astapPath": "",           # blank means "look in the usual places"
        "searchRadius": 15.0,      # degrees to search around the hint
        "downsample": 0,           # 0 lets ASTAP choose
        "maxStars": 500,
        "timeout": 120.0,          # seconds before a solve is given up on
        "exposure": 5.0,           # seconds, for the frames centring takes
        "tolerance": 1.0,          # arcminutes; how close counts as centred
        "attempts": 3,             # centring iterations before giving up
        # Where a frame goes when the local solver cannot manage it.
        #
        # Only ever used for a file opened as a framing reference, never during
        # a run: it needs the network and takes minutes, which is fine for a
        # planning question in the afternoon and useless at the telescope.
        # A frame from another rig can be at a scale this machine has no ASTAP
        # index files for, and that is exactly what this is good at.
        "astrometryEnabled": True,
        "astrometryKey": "",       # from nova.astrometry.net, under your profile
        "astrometryUrl": "https://nova.astrometry.net/api/",
        "astrometryTimeout": 600.0,
        # How close the framing angle has to be before the rotator is left
        # alone. A rotator is commanded in its own coordinates and agrees with
        # sky position angle only as far as its calibration does, so the solve
        # that centres the target also corrects the angle.
        "rotationTolerance": 1.0,
    },
    # Picking imaging tasks up from a collaboration server. Off until somebody
    # joins one, and incapable of stopping a night either way: the server is a
    # source of suggestions, never a dependency.
    "collab": {
        "enabled": False,
        # Baked in, so joining is one button. Blank means the built-in one;
        # Advanced can point at another for somebody running their own.
        "serverUrl": "",
        # Belongs to this machine, not to a person. It can fetch tasks and
        # report what was shot; it cannot administer anything.
        "token": "",
        # Belongs to *you*, not to this machine: it can create projects, enrol
        # telescopes and hand out work. Kept apart from the agent token above
        # on purpose — a credential that sits in a settings file next to a
        # telescope must not be able to administer anything, and the day one
        # leaks is the day that distinction is the only thing that matters.
        # Blank on most installs: only the person running the collaboration
        # has one.
        "adminToken": "",
        # Who *you* are on the server, from signing in with Discord: the
        # token the sign-in left behind, and the name and id it belongs to.
        # For everybody who is not the server's owner, this is what lets
        # them enrol their telescope and start collaborations.
        "userToken": "",
        "user": {},
        # Whose name goes on a project this machine creates. Filled in from
        # Discord on sign-in; typed by hand only by the server's owner.
        "coordinator": "",
        # How much of a night this rig will give a collaboration, in hours.
        # 0 means "as long as the target is up". It travels with the profile,
        # and it is what the coordinator's delegation is sized against: handing
        # twenty hours to somebody who gives two hours a weeknight is not an
        # assignment, it is a way of never finishing.
        "hoursPerNight": 0.0,
        # The part of the night, as local clock times ("21:00"). Blank at both
        # ends means whenever it is dark and the target is up. These become the
        # adopted target's pinned start and end in the plan, which is the same
        # mechanism as "start Cygnus at half nine, stop at one" — so the run
        # already knows how to honour them and auto-arrange already works
        # around them.
        "fromClock": "",
        "toClock": "",
        "pollMinutes": 10.0,
        # Whether the others in the collaboration may see where this
        # telescope points and what it is shooting, on their charts. Never
        # the observatory's location - only the sky.
        "sharePosition": True,
        # Whether an arriving task goes straight onto the plan. Off, and it
        # should stay off for most people: an assignment that silently rewrote
        # what a mount did tonight would be the software going rogue.
        "autoAccept": False,
    },
    # Telling somebody who is not in the room. A night that dies at one in the
    # morning is a night lost entirely unless something says so.
    "notify": {
        "enabled": False,
        # A URL to POST JSON to. Discord, Slack, Telegram, Pushover, ntfy and
        # anything self-hosted all accept one, which is why this is a webhook
        # rather than a list of services to keep up with.
        "webhookUrl": "",
        # Discord and Slack both want the message under a particular key.
        # "content" suits Discord, "text" suits Slack, "message" suits ntfy and
        # Pushover. Everything else in the payload is sent regardless.
        "messageField": "content",
        # Mail, for those who would rather have it in an inbox. Blank host
        # turns it off; the rest is an ordinary SMTP account.
        "smtpHost": "",
        "smtpPort": 587,
        "smtpUser": "",
        "smtpPassword": "",
        "smtpFrom": "",
        "smtpTo": "",
        "smtpStartTls": True,
        # What is worth interrupting somebody for. Failures and safety always
        # are; the rest is taste.
        "onSequenceEnd": True,
        "onFailure": True,
        "onRecovery": False,        # every rescue is a lot of messages
        "onGiveUp": True,
        "onSafety": True,
        # What the telescope is doing - a night opening, each target, a flip,
        # bed - and what a calibration run built. A handful a night.
        "onActivity": True,
        "onCalibration": True,
        "onWarning": True,
        # Never more than one *failure-class* message this often, so a rig
        # failing in a loop at 3am does not empty a phone battery. Activity
        # is spaced by the sky and is not held back.
        "minSecondsBetween": 60.0,
    },
    # The warnings board: where the lines are drawn for the things it watches.
    "warnings": {
        # Guiding RMS in arcseconds above which a warning goes up.
        "guideRmsArcsec": 1.5,
        # Free space on the capture drive, in GB.
        "diskWarnGb": 20.0,
        "diskCriticalGb": 5.0,
        # How long the cooler may sit off its setpoint before that is a warning.
        "coolingMinutes": 15.0,
    },
    # Not settings: bookkeeping about the settings file itself.
    "meta": {
        # Which one-time migrations have already run against this file.
        "migrations": [],
        # The first-light walk-through: whether it has been finished or
        # dismissed, and which step somebody had reached if they left it.
        "firstLightDone": False,
        "firstLightStep": 0,
    },
}


#: One-time moves of a changed default onto an existing settings file, as
#: (name, section, key, old default, new default).
#:
#: Only applied where the value is still sitting on the old default, so a
#: choice somebody actually made is never overwritten — see `Config._migrate`.
#: Names are permanent: removing one makes it run again.
MIGRATIONS: list[tuple[str, str, str, Any, Any]] = [
    # Dithering after every single sub spends a settle on every frame of the
    # night, and pairs of subs at one position still stack out walking noise.
    ("dither-every-two-frames", "guiding", "ditherEveryFrames", 1, 2),
    # Every third frame is the working default for every target: the dither
    # is the same one for all of them, set once under Equipment → Guiding,
    # and no longer a box on each target.
    ("dither-every-three-frames", "guiding", "ditherEveryFrames", 2, 3),
    # The collaboration server moved off the coordinator's PC onto the one
    # everybody uses; a rig still pointed at the old local one is pointed at
    # the built-in one, which blank now means.
    ("collab-server-built-in", "collab", "serverUrl", "http://127.0.0.1:8800", ""),
    # Filter names became one letter each. The stored lists are folded on
    # every load (see `_normalise_filters`); these move the two defaults that
    # carried the old spellings, so a file that never changed them gets the
    # new ones rather than a folded copy of the old.
    ("filters-one-letter-schedule", "schedule", "filters",
     ["L", "R", "G", "B", "Ha", "OIII", "SII"], ["L", "R", "G", "B", "H", "O", "S"]),
    ("filters-one-letter-autoplan", "autoplan", "filterExposures",
     {"L": 120.0, "R": 180.0, "G": 180.0, "B": 180.0,
      "Ha": 600.0, "OIII": 600.0, "SII": 600.0},
     {"L": 120.0, "R": 180.0, "G": 180.0, "B": 180.0,
      "H": 600.0, "O": 600.0, "S": 600.0}),
]


class Config:
    """The settings file, loaded once and written through on every change."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_root() / "settings.json")
        self._lock = threading.RLock()
        self._data = {section: dict(values) for section, values in DEFAULTS.items()}
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return                              # first run, or a file we cannot parse
        if not isinstance(stored, dict):
            return
        with self._lock:
            for section, values in stored.items():
                if section in self._data and isinstance(values, dict):
                    self._data[section].update(
                        {k: v for k, v in values.items() if k in DEFAULTS[section]})
        self._migrate()

    def _migrate(self) -> None:
        """Let a changed default reach a settings file that already exists.

        A stored value always wins over a default, which is right — but it means
        a default changed after the first run reaches new installs only, and the
        rigs that have been running for months go on doing the old thing for
        ever. That is not a default, it is a thing that happens to new users.

        The rule that makes this safe: a value is only moved if it is still
        sitting on the *old* default, so it can never overwrite a choice
        somebody actually made — only one they never made. Each migration runs
        once and says so in the file, so a value moved and then deliberately put
        back stays put.
        """
        ran = list(self._data["meta"].get("migrations") or [])
        changed = False
        for name, section, key, was, now in MIGRATIONS:
            if name in ran:
                continue
            ran.append(name)
            changed = True
            with self._lock:
                if self._data.get(section, {}).get(key) == was:
                    self._data[section][key] = now
        self._normalise_filters()
        if changed:
            with self._lock:
                self._data["meta"]["migrations"] = ran
            self.save()

    def _normalise_filters(self) -> None:
        """Every filter name in the file, in the program's one spelling.

        A settings file from before names were folded to a letter says "Ha"
        where the rest of the program now says "H"; left alone, the plan
        would ask for a filter the wheel no longer has. Done on every load
        rather than as a one-off migration, because the same file can be
        written by an older copy of the program on another machine.
        """
        from .filters import fold_settings
        with self._lock:
            for section in ("camera", "sequencer", "schedule", "autoplan"):
                fold_settings(section, self._data[section])

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self._data, indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(payload, "utf-8")
        except OSError:
            pass                                # a read-only home is not fatal

    # -- access ------------------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._data[name])

    def get(self, section: str, key: str, default: Any = None) -> Any:
        with self._lock:
            value = self._data.get(section, {}).get(key)
        return default if value is None else value

    def update(self, section: str, values: dict[str, Any]) -> dict[str, Any]:
        """Merge `values` into a section, ignoring keys that are not ours."""
        if section not in DEFAULTS:
            raise KeyError(f"unknown settings section {section!r}")
        # Filter names in the one spelling, whoever is writing them.
        from .filters import fold_settings
        values = fold_settings(section, dict(values))
        with self._lock:
            for key, value in values.items():
                if key in DEFAULTS[section]:
                    self._data[section][key] = value
            result = dict(self._data[section])
        self.save()
        return result

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {name: dict(values) for name, values in self._data.items()}


def effective_site(config: Config, manager: Any) -> dict[str, Any]:
    """The site the sky is drawn for.

    The mount is asked first when it is connected and the driver reports a
    location, because that is the one value guaranteed to match the coordinates
    the mount is actually slewing to.  Anything the mount cannot supply falls
    back to what was typed in the Site panel.
    """
    manual = config.section("site")
    site = {
        "latitude": manual.get("latitude"),
        "longitude": manual.get("longitude"),
        "elevation": manual.get("elevation") or 0.0,
        "source": "manual" if manual.get("latitude") is not None else "unset",
    }

    if not manual.get("useMount", True):
        return site

    mount = manager.get("mount")
    if mount is None or not mount.connected:
        return site
    try:
        reported = mount.site
    except Exception:                           # noqa: BLE001 - a driver quirk, not an error
        reported = None
    if not reported or reported.get("latitude") is None or reported.get("longitude") is None:
        return site

    return {
        "latitude": round(float(reported["latitude"]), 6),
        "longitude": round(float(reported["longitude"]), 6),
        "elevation": float(reported.get("elevation") or site["elevation"]),
        "source": "mount",
    }
