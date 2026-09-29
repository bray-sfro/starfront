"""HTTP and WebSocket API, plus the static UI."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (allsky, astro, autoplan, calibrating, calibration, catalog,
               collabadmin, collabclient, filteroffsets, framing, logs, notify,
               overheads,
               piercam, plans, safety, schedule, solarsystem, survey)
from . import collab
from . import filters
from . import ninaimport
from . import warnings
from .capture import clean_target
from .config import Config, effective_site
from .coverage import CoverageStore
from .devices import phd2
from .devices.base import DeviceError
from .devices.phd2 import DEFAULT_HOST as DEFAULT_PHD2_HOST
from .devices.phd2 import DEFAULT_PORT as DEFAULT_PHD2_PORT
from .equipment import (MASTER_DEVICE_KINDS, RIG_SECTIONS, EquipmentStore)
from .imaging import fits, png, render
from .plans import PlanStore
from .rigs import RigSet
from .sequencer import Sequencer
from .targets import TargetStore

WEB_DIR = Path(__file__).parent / "web"

# Settings sections that describe the observatory rather than one telescope, and
# so travel with an equipment profile as a single shared block.
SHARED_SECTIONS = ("site", "capture", "schedule", "guiding", "solver",
                   "calibration", "recovery")

# Idempotent, and here as well as in run.py because uvicorn can import this
# module directly (--reload, or `uvicorn astrocontrol.main:app`).
logs.setup()

config = Config()
equipment = EquipmentStore()
library = calibration.Library(config)
# What this rig costs between exposures, measured as it works. Shared by every
# telescope: they download over the same bus and slew on the same mount.
overhead_store = overheads.OverheadStore()
rigs = RigSet(config, equipment, library, overhead_store)
targets = TargetStore()
surveys = survey.SurveyImages()
coverage = CoverageStore()
plan = PlanStore()
sequences = plans.SequenceStore()
recipes = calibrating.RecipeStore()
calibrator = calibrating.CalibrationRunner(rigs, config, library)
progress = allsky.ProgressStore()
sequencer = Sequencer(rigs, config, targets, plan, coverage, recipes, calibrator,
                      progress)
# The camera watching the telescope. Runs itself from the moment one is
# connected; nothing waits on it and nothing it does can fail a night.
pier = piercam.PierCamService(rigs, config)
pier.start()

# Measuring how far apart the filters focus, so a filter change can be a move
# of known size rather than a five-minute sweep.
offsets_run = filteroffsets.OffsetRun(rigs, config)

# Picking imaging tasks up from a collaboration server. Started either way: it
# does nothing at all until a server and a token are configured, and it can
# never hold up a night.
collab_client = collabclient.CollabClient(rigs, config, targets, log=rigs.log)
collab_client.start()

# The other half of the same conversation: administering a collaboration rather
# than contributing to one. Stateless, starts nothing, and holds the coordinator
# token so it never has to reach the browser.
collab_admin = collabadmin.CollabAdmin(config)

# Telling somebody who is not in the room.
notifier = notify.Notifier(config, log=rigs.log)
notifier.origin = lambda: rigs.master.name
sequencer.notifier = notifier
calibrator.notifier = notifier
calibrator.warm_cameras = sequencer.warm_down

# The weather. Watches from start-up rather than from the first sequence,
# because the dangerous state is a roof open over a telescope and that can be
# true with nothing running.
safety_watcher = safety.SafetyWatcher(
    rigs, config,
    on_unsafe=lambda detail: _weather_turned(False, detail),
    on_safe=lambda: _weather_turned(True, None),
    log=rigs.log)
sequencer.safety = safety_watcher

# Everything that could quietly go wrong, watched on a timer and said out
# loud: the board the strip at the top of the window reads, and the source
# of the "warning" notifications.
warning_board = warnings.WarningBoard(log=rigs.log, notifier=notifier)
sequencer.warnings = warning_board


def _night_bounds() -> tuple[float | None, float | None] | None:
    site = effective_site(config, rigs.master.manager)
    if site.get("latitude") is None:
        return None
    night = schedule.night(float(site["latitude"]), float(site["longitude"]))
    return (night.get("duskAstronomical") or night.get("sunset"),
            night.get("dawnAstronomical") or night.get("sunrise"))


def _entry_observable_minutes(entry: dict[str, Any]) -> float | None:
    site = effective_site(config, rigs.master.manager)
    if site.get("latitude") is None:
        return None
    target = next((t for t in targets.listing() if t["id"] == entry.get("targetId")), None)
    if target is None or target.get("type") == "allsky":
        return None
    info = _target_schedule(target, _night_for(None), float(site["latitude"]),
                            float(site["longitude"]),
                            float(config.get("schedule", "minAltitude", 30.0)), entry)
    return float(info.get("availableSeconds") or 0.0) / 60.0


warning_context = warnings.Context(
    config=config, rigs=rigs, sequencer=sequencer, plan=plan, targets=targets,
    library=library, calibrator=calibrator, collab=collab_client,
    safety=safety_watcher, notifier=notifier,
    coverage=lambda: library.coverage(_calibration_needs(rigs.master)),
    observable_minutes=_entry_observable_minutes,
    night=_night_bounds)
watchdog = warnings.Watchdog(warning_board, warning_context, log=rigs.log)
watchdog.start()


def _weather_turned(safe: bool, detail: str | None) -> None:
    """The monitor has made up its mind. Act, and say so."""
    if safe:
        sequencer.weather_safe()
        notifier.send("safety", "The sky is safe again",
                      "The safety monitor has read safe long enough to be believed.")
        return
    notifier.send("safety", "The sky is not safe",
                  (detail or "The safety monitor says unsafe.")
                  + "\n\nShutting the observatory down."
                  if config.get("safety", "onUnsafe", "shutdown") == "shutdown"
                  else (detail or "The safety monitor says unsafe.")
                  + "\n\nHolding the run until it clears.")
    sequencer.weather_unsafe(detail)


safety_watcher.start()

app = FastAPI(title="Starfront", version="0.1.0")


def _rig(rig_id: str | None = None):
    """The telescope a request is about; the master when it does not say."""
    return _guard(rigs.get, rig_id)


class _Master:
    """One of the master telescope's services, looked up on every use.

    The mount, the guider, the site and the event log belong to whichever rig is
    the master, and that can change while the program is running — so these
    cannot be bound once at import time the way they used to be.
    """

    def __init__(self, attribute: str) -> None:
        self._attribute = attribute

    def __getattr__(self, name: str) -> Any:
        return getattr(getattr(rigs.master, self._attribute), name)


manager = _Master("manager")
capture = _Master("capture")
solver = _Master("solver")
autofocuser = _Master("focuser")


@app.exception_handler(DeviceError)
async def _device_error_handler(_request, exc: DeviceError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def _guard(action, *args, **kwargs) -> Any:
    """Run a device call, turning driver failures into clean 400s."""
    try:
        return action(*args, **kwargs)
    except DeviceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:                       # noqa: BLE001 - driver misbehaviour
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class ConnectRequest(BaseModel):
    backend: str
    driverId: str
    name: str | None = None
    # Settings belonging to the driver itself, written into its ASCOM profile
    # in the moment before it is opened.  This is how several telescopes share
    # one driver: each names the device it wants on its way up.
    options: dict[str, str] | None = None


class DriverSetupRequest(BaseModel):
    driverId: str = Field(min_length=1, max_length=200)


class RememberDeviceRequest(BaseModel):
    backend: str = Field(min_length=1, max_length=20)
    driverId: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, max_length=120)
    options: dict[str, str] | None = None


class ScanRequest(BaseModel):
    host: str | None = None
    port: int | None = 11111


class ExposeRequest(BaseModel):
    exposure: float = Field(ge=0, le=3600)
    frameType: str = "light"
    binning: int | None = Field(default=None, ge=1, le=8)
    gain: int | None = None
    offset: int | None = None
    loop: bool = False
    count: int = Field(default=1, ge=1, le=999)


class CoolerRequest(BaseModel):
    on: bool | None = None
    setpoint: float | None = Field(default=None, ge=-60, le=40)


class CameraSettingsRequest(BaseModel):
    binning: int | None = Field(default=None, ge=1, le=8)
    gain: int | None = None
    offset: int | None = None


class FilterRequest(BaseModel):
    position: int = Field(ge=0, le=63)


class FilterNamesRequest(BaseModel):
    names: list[str]


class FocuserMoveRequest(BaseModel):
    position: int | None = None
    delta: int | None = None


class SlewRequest(BaseModel):
    ra: float = Field(ge=0, lt=24, description="right ascension in hours")
    dec: float = Field(ge=-90, le=90, description="declination in degrees")


class TrackingRequest(BaseModel):
    on: bool


class JogRequest(BaseModel):
    direction: str
    rate: float = Field(default=0.5, gt=0, le=10.0, description="degrees per second")


class FlatPanelRequest(BaseModel):
    on: bool
    brightness: int | None = Field(default=None, ge=0, le=100000)


class OutputRequest(BaseModel):
    save: bool | None = None
    directory: str | None = None
    target: str | None = None


class LogRequest(BaseModel):
    message: str = Field(min_length=1, max_length=300)
    level: str = Field(default="info", pattern="^(info|success|warn|error)$")


class SiteRequest(BaseModel):
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    elevation: float | None = Field(default=None, ge=-500, le=9000)
    useMount: bool | None = None


class OpticsRequest(BaseModel):
    angleFromSolve: bool | None = None
    focalLength: float | None = Field(default=None, gt=0, le=50000,
                                      description="millimetres")
    rotation: float | None = Field(default=None, ge=-360, le=360)
    sensorWidth: int | None = Field(default=None, gt=0, le=60000, description="pixels")
    sensorHeight: int | None = Field(default=None, gt=0, le=60000, description="pixels")
    pixelSize: float | None = Field(default=None, gt=0, le=100, description="microns")
    # What the optics actually correct, when the image circle is smaller than
    # the sensor. Degrees.
    usableWidth: float | None = Field(default=None, gt=0, le=90)
    usableHeight: float | None = Field(default=None, gt=0, le=90)


class CameraSettingsStoreRequest(BaseModel):
    # In slot order. Blank entries are dropped, so a wheel with empty positions
    # can be described honestly.
    filterNames: list[str] | None = Field(default=None, max_length=32)
    # Filter name -> bandpass in nanometres. Only narrowband has one that
    # matters, and it matters a great deal: a 3 nm Ha and a 7 nm Ha are not the
    # same data, and a collaboration is entitled to say which it will take.
    # There was nowhere to state it, so a project asking for 3 nm could be
    # joined by nobody at all.
    filterBandpass: dict[str, float] | None = Field(default=None)
    # What is fitted on a telescope with no wheel — a RASA's drawer filter.
    fixedFilter: str | None = Field(default=None, max_length=24)
    # A one-shot colour camera. Learned from the camera when it connects;
    # this is for saying so before it has.
    colour: bool | None = None
    gain: int | None = Field(default=None, ge=0, le=100000)
    offset: int | None = Field(default=None, ge=0, le=100000)
    setpoint: float | None = Field(default=None, ge=-60, le=40)
    coolAtStart: bool | None = None
    warmAtEnd: bool | None = None
    waitForCooling: bool | None = None
    coolToleranceC: float | None = Field(default=None, gt=0, le=20)
    coolTimeoutMinutes: float | None = Field(default=None, ge=1, le=180)
    warmRateCPerMinute: float | None = Field(default=None, gt=0, le=30)


class SafetySettingsRequest(BaseModel):
    enabled: bool | None = None
    onUnsafe: str | None = Field(default=None, pattern="^(shutdown|pause)$")
    graceSeconds: float | None = Field(default=None, ge=0, le=3600)
    resumeAfterSeconds: float | None = Field(default=None, ge=0, le=86400)
    unreachableIsUnsafe: bool | None = None
    blockStart: bool | None = None


class NotifySettingsRequest(BaseModel):
    enabled: bool | None = None
    webhookUrl: str | None = Field(default=None, max_length=1000)
    messageField: str | None = Field(default=None, max_length=40)
    smtpHost: str | None = Field(default=None, max_length=200)
    smtpPort: int | None = Field(default=None, ge=1, le=65535)
    smtpUser: str | None = Field(default=None, max_length=200)
    smtpPassword: str | None = Field(default=None, max_length=400)
    smtpFrom: str | None = Field(default=None, max_length=200)
    smtpTo: str | None = Field(default=None, max_length=600)
    smtpStartTls: bool | None = None
    onSequenceEnd: bool | None = None
    onFailure: bool | None = None
    onRecovery: bool | None = None
    onGiveUp: bool | None = None
    onSafety: bool | None = None
    onActivity: bool | None = None
    onCalibration: bool | None = None
    onWarning: bool | None = None
    minSecondsBetween: float | None = Field(default=None, ge=0, le=3600)


class CollabSettingsRequest(BaseModel):
    enabled: bool | None = None
    serverUrl: str | None = Field(default=None, max_length=400)
    token: str | None = Field(default=None, max_length=200)
    adminToken: str | None = Field(default=None, max_length=200)
    coordinator: str | None = Field(default=None, max_length=80)
    hoursPerNight: float | None = Field(default=None, ge=0, le=24)
    # "21:00", or blank for "whenever it is dark".
    fromClock: str | None = Field(default=None, max_length=5)
    toClock: str | None = Field(default=None, max_length=5)
    pollMinutes: float | None = Field(default=None, ge=1, le=1440)
    autoAccept: bool | None = None
    # Whether the others may see where this telescope points and what it is
    # shooting. Never the observatory's location.
    sharePosition: bool | None = None


class CollabTaskRequest(BaseModel):
    state: str = Field(pattern="^(accepted|declined|complete)$")


class CollabJoinRequest(BaseModel):
    """Signing up for a share of somebody's project."""

    # Hours per filter. Zero takes whatever the project asks for.
    hours: float = Field(default=0.0, ge=0.0, le=24.0)
    # Sub length. Zero takes the middle of what the project will accept.
    exposure: float = Field(default=0.0, ge=0.0, le=3600.0)
    # With a rotator: turn the camera to the project's angle, so a single
    # target is framed the way it was framed and a mosaic's panels lie along
    # the project's grid. Off, the camera stays at whatever angle it sits at.
    # Meaningless without a rotator, and ignored.
    matchRotation: bool = True


class CollabAgentRequest(BaseModel):
    """Enrolling somebody's telescope."""

    name: str = Field(min_length=1, max_length=80)
    owner: str = Field(default="", max_length=80)


class CollabProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # A rectangle of sky: ra/dec/width/height/rotation, degrees throughout.
    # Optional when `targetId` names one of your own targets to take it from.
    region: dict[str, Any] | None = None
    # A collaboration aimed at one thing you have already framed. The commonest
    # case by far, and it should not need a rectangle dragged out for it: the
    # framing decision was made when the target was saved.
    targetId: str | None = Field(default=None, max_length=64)
    # "single" or "mosaic". Left out, it follows the target: a single framing
    # is a single-target collaboration, a mosaic or a drawn region is a mosaic.
    kind: str | None = Field(default=None, pattern="^(single|mosaic)$")
    requirements: dict[str, Any] = Field(default_factory=dict)
    # Hours wanted at every point in the region, per filter.
    goals: dict[str, float] = Field(default_factory=dict)
    notes: str = Field(default="", max_length=2000)


class CollabDelegateRequest(BaseModel):
    agent: str = Field(min_length=1, max_length=64)
    region: dict[str, Any]
    filters: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=500)


class CollabPlanRequest(BaseModel):
    """Work out how to cover a project with the telescopes chosen."""

    agents: list[str] = Field(default_factory=list, max_length=50)
    # Fraction of the field neighbouring tiles keep in common. Two panels that
    # merely touch do not register against each other.
    overlap: float = Field(default=0.1, ge=0.0, le=0.5)


class CollabVerdictRequest(BaseModel):
    accepted: bool
    reason: str = Field(default="", max_length=500)


class OffsetRunRequest(BaseModel):
    # Blank means every filter the wheel reports.
    filters: list[str] | None = Field(default=None, max_length=32)
    # Round-robin passes. More is better and costs a sweep per filter each, so
    # two is the default: enough to see whether the numbers agree.
    passes: int = Field(default=2, ge=1, le=5)
    # What everything is measured against. Blank uses the autofocus filter.
    reference: str = Field(default="", max_length=24)


class DomeShutterRequest(BaseModel):
    open: bool


class SwitchRequest(BaseModel):
    index: int = Field(ge=0, le=255)
    # A relay takes 0 or 1; a dew heater takes its own range, which the driver
    # reports and the device clamps to.
    value: float = Field(ge=-100000, le=100000)


class PierCamSettingsRequest(BaseModel):
    enabled: bool | None = None
    auto: bool | None = None
    target: float | None = Field(default=None, gt=0.01, le=0.95)
    meterPercentile: float | None = Field(default=None, ge=50, le=99.9)
    deadband: float | None = Field(default=None, ge=0, le=0.5)
    damping: float | None = Field(default=None, gt=0, le=1)
    minExposure: float | None = Field(default=None, gt=0, le=60)
    maxExposure: float | None = Field(default=None, gt=0, le=120)
    minGain: int | None = Field(default=None, ge=0, le=100000)
    maxGain: int | None = Field(default=None, ge=0, le=100000)
    gainStep: int | None = Field(default=None, ge=0, le=10000)
    exposure: float | None = Field(default=None, gt=0, le=120)
    gain: int | None = Field(default=None, ge=0, le=100000)
    intervalSeconds: float | None = Field(default=None, ge=0.25, le=60)
    maxDim: int | None = Field(default=None, ge=160, le=2000)
    gamma: float | None = Field(default=None, ge=1.0, le=4.0)


class ScheduleSettingsRequest(BaseModel):
    # The lowest the telescope will shoot at: below this a target is not worth
    # the frame, whatever else the plan says.
    minAltitude: float | None = Field(default=None, ge=0, le=85)
    useMeasured: bool | None = None
    perFrameSeconds: float | None = Field(default=None, ge=0, le=600)
    filterChangeSeconds: float | None = Field(default=None, ge=0, le=600)
    perPanelSeconds: float | None = Field(default=None, ge=0, le=3600)


class CaptureSettingsRequest(BaseModel):
    rootDirectory: str | None = Field(default=None, max_length=400)


class SequencerSettingsRequest(BaseModel):
    autofocusOnStart: bool | None = None
    autofocusOnFilterChange: bool | None = None
    autofocusIntervalMinutes: float | None = Field(default=None, ge=0, le=600)
    autofocusTemperatureDelta: float | None = Field(default=None, ge=0, le=20)
    autofocusHfrPercent: float | None = Field(default=None, ge=0, le=200)
    focusExposure: float | None = Field(default=None, gt=0, le=120)
    focusStepSize: int | None = Field(default=None, gt=0, le=100000)
    focusPoints: int | None = Field(default=None, ge=5, le=31)
    focusBacklash: int | None = Field(default=None, ge=0, le=10000)
    focusMethod: str | None = Field(
        default=None,
        pattern="^(trendlines|parabolic|hyperbolic|trendparabolic|trendhyperbolic)$")
    focusFramesPerPoint: int | None = Field(default=None, ge=1, le=10)
    focusAttempts: int | None = Field(default=None, ge=1, le=5)
    focusMaxHfrRatio: float | None = Field(default=None, ge=1.0, le=3.0)
    autofocusFilter: str | None = Field(default=None, max_length=24)
    # Focuser steps per filter name. A value of None for a filter drops it.
    filterOffsets: dict[str, int] | None = Field(default=None)
    useFilterOffsets: bool | None = None
    meridianFlipEnabled: bool | None = None
    flipPauseMinutes: float | None = Field(default=None, ge=0, le=120)
    flipAfterMinutes: float | None = Field(default=None, ge=0, le=120)
    flipSolve: bool | None = None
    settleSeconds: float | None = Field(default=None, ge=0, le=600)
    parkAtEnd: bool | None = None
    stopTrackingAtEnd: bool | None = None
    homeAtStart: bool | None = None
    homeTimeoutMinutes: float | None = Field(default=None, ge=0.5, le=60)
    loopLeadMinutes: float | None = Field(default=None, ge=0, le=180)
    measureFrames: bool | None = None
    solveEveryFrames: int | None = Field(default=None, ge=0, le=200)
    pointingWarnArcmin: float | None = Field(default=None, gt=0, le=120)
    focusWarnPercent: float | None = Field(default=None, ge=0, le=200)


class GuidingSettingsRequest(BaseModel):
    phd2Path: str | None = Field(default=None, max_length=400)
    autoStartPhd2: bool | None = None
    phd2Profile: str | None = Field(default=None, max_length=120)
    connectEquipment: bool | None = None
    autoSelectStar: bool | None = None
    startWithSequence: bool | None = None
    settleTimeoutSeconds: float | None = Field(default=None, ge=10, le=900)
    ditherEnabled: bool | None = None
    ditherEveryFrames: int | None = Field(default=None, ge=1, le=50)
    ditherPixels: float | None = Field(default=None, gt=0, le=100)
    ditherRaOnly: bool | None = None
    settlePixels: float | None = Field(default=None, gt=0, le=50)
    settleTime: float | None = Field(default=None, ge=0, le=600)
    settleTimeout: float | None = Field(default=None, ge=5, le=600)


class CalibrateOverheadsRequest(BaseModel):
    """Timing the rig on purpose rather than waiting for a night to do it."""

    frames: int = Field(default=3, ge=1, le=10)
    # The sweep is the long part, and the one worth skipping when the sky is
    # not good enough for a focus run to mean anything.
    focus: bool = True


class AutoplanSettingsRequest(BaseModel):
    """What Auto-arrange puts in the slots it works out."""

    chooseExposures: bool | None = None
    # Seconds per sub, by filter name. A filter left out falls back to the
    # class default below.
    filterExposures: dict[str, float] | None = None
    luminanceExposure: float | None = Field(default=None, gt=0, le=3600)
    broadbandExposure: float | None = Field(default=None, gt=0, le=3600)
    narrowbandExposure: float | None = Field(default=None, gt=0, le=3600)
    minExposure: float | None = Field(default=None, gt=0, le=600)
    narrowbandAboveIllumination: float | None = Field(default=None, ge=0, le=1)
    moonSafeDegrees: float | None = Field(default=None, ge=0, le=180)
    shortenUnderMoon: bool | None = None
    minFramesPerFilter: int | None = Field(default=None, ge=1, le=50)


class RecoverySettingsRequest(BaseModel):
    """What the night does when something goes wrong while nobody is watching."""

    enabled: bool | None = None
    guidingRecovery: bool | None = None
    guideGraceSeconds: float | None = Field(default=None, ge=0, le=600)
    guideRestartAttempts: int | None = Field(default=None, ge=0, le=20)
    recalibrateAfterAttempts: int | None = Field(default=None, ge=0, le=20)
    discardLostFrames: bool | None = None
    pointingRecovery: bool | None = None
    recentreArcmin: float | None = Field(default=None, gt=0, le=600)
    recentreAttempts: int | None = Field(default=None, ge=0, le=10)
    frameRetryAttempts: int | None = Field(default=None, ge=0, le=20)
    frameRetrySeconds: float | None = Field(default=None, ge=0, le=600)
    reconnectDevices: bool | None = None
    maxPerTarget: int | None = Field(default=None, ge=0, le=100)
    cooldownSeconds: float | None = Field(default=None, ge=0, le=3600)
    onGiveUp: str | None = Field(default=None, pattern="^(next|stop|park)$")


class SolverSettingsRequest(BaseModel):
    astapPath: str | None = None
    searchRadius: float | None = Field(default=None, gt=0, le=180)
    downsample: int | None = Field(default=None, ge=0, le=4)
    maxStars: int | None = Field(default=None, ge=10, le=10000)
    timeout: float | None = Field(default=None, ge=5, le=1800)
    exposure: float | None = Field(default=None, gt=0, le=600)
    tolerance: float | None = Field(default=None, gt=0, le=120)
    attempts: int | None = Field(default=None, ge=1, le=10)
    astrometryEnabled: bool | None = None
    astrometryKey: str | None = Field(default=None, max_length=200)
    astrometryUrl: str | None = Field(default=None, max_length=400)
    astrometryTimeout: float | None = Field(default=None, ge=30, le=3600)


class SolveRequest(BaseModel):
    mode: str = Field(default="solve", pattern="^(solve|sync|center|goto)$")
    imageId: str | None = None
    ra: float | None = Field(default=None, ge=0, lt=24)
    dec: float | None = Field(default=None, ge=-90, le=90)
    rotation: float | None = Field(default=None, ge=-360, le=360)


class RotatorMoveRequest(BaseModel):
    position: float | None = Field(default=None, ge=-360, le=360)
    delta: float | None = Field(default=None, ge=-360, le=360)


class RotatorSyncRequest(BaseModel):
    position: float = Field(ge=0, lt=360)


class TargetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    ra: float = Field(ge=0, lt=24, description="right ascension in hours")
    dec: float = Field(ge=-90, le=90)
    rotation: float = Field(default=0.0, ge=-360, le=360)
    panelWidth: float = Field(default=0.0, ge=0, le=90)
    panelHeight: float = Field(default=0.0, ge=0, le=90)
    rows: int = Field(default=1, ge=1, le=20)
    columns: int = Field(default=1, ge=1, le=20)
    overlap: float = Field(default=0.1, ge=0, lt=0.9)
    align: str = Field(default="aligned", pattern="^(fixed|aligned)$")
    notes: str = Field(default="", max_length=500)
    survey: str = Field(default="", max_length=80)


class TargetEditRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    notes: str | None = Field(default=None, max_length=500)


class PlanMetaRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    clearDate: bool = False


class PlanEntryRequest(BaseModel):
    targetId: str = Field(min_length=1, max_length=64)


class PlanCalibrationRequest(BaseModel):
    recipeId: str = Field(min_length=1, max_length=64)
    # Blank means every telescope, which is what flats and darks normally want.
    rigId: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=120)


class CalibrationSetRequest(BaseModel):
    id: str | None = Field(default=None, max_length=32)
    frameType: str = Field(pattern="^(bias|dark|darkflat|flat)$")
    count: int = Field(default=25, ge=1, le=calibrating.MAX_COUNT)
    exposure: float = Field(default=0.0, ge=0, le=3600)
    binning: int = Field(default=1, ge=1, le=8)
    gain: int | None = Field(default=None, ge=0, le=100000)
    offset: int | None = Field(default=None, ge=0, le=100000)
    filter: str = Field(default="", max_length=24)
    # One set covering the whole wheel, rather than a row per filter. Expanded
    # when it is shot, and per telescope, so two scopes with different wheels
    # are both covered by the one line.
    allFilters: bool = False
    # Flats only: a lamp on the front of the telescope, or the twilight sky.
    source: str = Field(default="panel", pattern="^(panel|sky)$")
    # Left unsaid, these take their sensible default from the frame type: a flat
    # measures its own exposure, and a dark for the flats follows whatever the
    # flats settled on. Saying False has to be a decision, not an omission.
    autoExposure: bool | None = None
    followsFlat: bool | None = None
    # Blank means half brightness, from the calibration settings.
    brightness: int | None = Field(default=None, ge=0, le=100)


class CalibrationRecipeRequest(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=60)
    sets: list[CalibrationSetRequest] = Field(min_length=1,
                                              max_length=calibrating.MAX_SETS)


class CalibrationRunRequest(BaseModel):
    recipeId: str | None = Field(default=None, max_length=64)
    # A recipe can also be run without being saved, straight off the tab.
    sets: list[CalibrationSetRequest] | None = Field(default=None,
                                                     max_length=calibrating.MAX_SETS)
    name: str | None = Field(default=None, min_length=1, max_length=60)
    rigId: str | None = Field(default=None, max_length=64)


class AllSkyFilterRequest(BaseModel):
    name: str = Field(default="", max_length=24)
    exposure: float = Field(gt=0, le=3600)
    count: int = Field(ge=1, le=1000)


class AllSkyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    decMin: float = Field(default=-30.0, ge=-90, le=90)
    decMax: float = Field(default=90.0, ge=-90, le=90)
    overlap: float = Field(default=0.20, ge=0.0, le=0.9)
    stagger: bool = True
    # Blank uses the smallest usable field across the telescopes on the mount,
    # which is the only field size every one of them can actually deliver.
    fieldWidth: float | None = Field(default=None, gt=0, le=90)
    fieldHeight: float | None = Field(default=None, gt=0, le=90)
    goal: list[AllSkyFilterRequest] = Field(min_length=1, max_length=12)


class AllSkyGoalRequest(BaseModel):
    goal: list[AllSkyFilterRequest] = Field(min_length=1, max_length=12)


class AllSkyPreviewRequest(BaseModel):
    decMin: float = Field(default=-30.0, ge=-90, le=90)
    decMax: float = Field(default=90.0, ge=-90, le=90)
    overlap: float = Field(default=0.20, ge=0.0, le=0.9)
    fieldWidth: float | None = Field(default=None, gt=0, le=90)
    fieldHeight: float | None = Field(default=None, gt=0, le=90)
    goal: list[AllSkyFilterRequest] = Field(default_factory=list, max_length=12)


class AllSkySettingsRequest(BaseModel):
    minAltitude: float | None = Field(default=None, ge=0, le=85)
    qualityAltitude: float | None = Field(default=None, ge=20, le=90)
    maxHourAngle: float | None = Field(default=None, ge=0.25, le=12)
    minimiseFlips: bool | None = None
    moonAvoidance: float | None = Field(default=None, ge=0, le=180)
    moonScaleByPhase: bool | None = None
    transitPenalty: float | None = Field(default=None, ge=0, le=60)
    slewWeight: float | None = Field(default=None, ge=0, le=50)
    finishBonus: float | None = Field(default=None, ge=0, le=200)
    visitMinutes: float | None = Field(default=None, ge=0, le=600)
    decMin: float | None = Field(default=None, ge=-90, le=90)
    decMax: float | None = Field(default=None, ge=-90, le=90)
    overlap: float | None = Field(default=None, ge=0, le=0.9)


class CalibrationSettingsRequest(BaseModel):
    libraryDirectory: str | None = Field(default=None, max_length=400)
    applyTo: str | None = Field(default=None, pattern="^(survey|all|off)$")
    matchTemperatureC: float | None = Field(default=None, ge=0, le=50)
    matchExposurePercent: float | None = Field(default=None, ge=0, le=100)
    maxFlatAgeDays: float | None = Field(default=None, ge=0, le=3650)
    maxDarkAgeDays: float | None = Field(default=None, ge=0, le=3650)
    stackMethod: str | None = Field(default=None, pattern="^(sigma|median|mean)$")
    sigmaLow: float | None = Field(default=None, ge=0.5, le=10)
    sigmaHigh: float | None = Field(default=None, ge=0.5, le=10)
    keepSubs: bool | None = None
    autoCover: bool | None = None
    coverTimeoutSeconds: float | None = Field(default=None, ge=5, le=1800)
    flatTargetAdu: float | None = Field(default=None, ge=100, le=65000)
    flatTolerancePercent: float | None = Field(default=None, ge=0.5, le=50)
    # A microsecond floor, so the camera's own shortest exposure is the only
    # limit: a fast astrograph in daylight really does want that end of it.
    flatMinExposure: float | None = Field(default=None, ge=0.000001, le=600)
    flatMaxExposure: float | None = Field(default=None, ge=0.01, le=600)
    flatPanelBrightness: int | None = Field(default=None, ge=0, le=100)
    # Dim the panel as well as shortening the exposure. Without it a panel too
    # bright for the camera's shortest exposure cannot be exposed correctly at
    # all, which is the ordinary case for luminance.
    flatAutoBrightness: bool | None = None
    flatPreferredExposure: float | None = Field(default=None, ge=0.01, le=600)
    flatMinBrightness: int | None = Field(default=None, ge=1, le=100)
    skyFlatPointing: str | None = Field(default=None, pattern="^(zenith|antisolar)$")
    skyFlatAltitude: float | None = Field(default=None, ge=30, le=90)
    skyFlatTracking: bool | None = None
    skyFlatMeridianOffset: float | None = Field(default=None, ge=0, le=45)
    skyFlatDitherArcmin: float | None = Field(default=None, ge=0, le=60)
    skyFlatAcceptPercent: float | None = Field(default=None, ge=2, le=90)
    skyFlatSettleSeconds: float | None = Field(default=None, ge=0, le=120)
    skyFlatMaxMinutes: float | None = Field(default=None, ge=1, le=180)
    skyFlatPollSeconds: float | None = Field(default=None, ge=1, le=120)


class PlanFilterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=24)
    exposure: float = Field(gt=0, le=3600)
    count: int = Field(ge=0, le=10000)


class PlanFiltersRequest(BaseModel):
    filters: list[PlanFilterRequest] = Field(default_factory=list, max_length=20)


class PlanNotesRequest(BaseModel):
    notes: str = Field(default="", max_length=400)


class PlanOrderRequest(BaseModel):
    entryIds: list[str] = Field(min_length=1, max_length=60)


class PlanTimesRequest(BaseModel):
    startAt: float | None = Field(default=None, ge=0)
    endAt: float | None = Field(default=None, ge=0)
    clearStart: bool = False
    clearEnd: bool = False


class ArrangeRequest(BaseModel):
    """Whether Auto-arrange fills the slots as well as working them out."""

    chooseExposures: bool | None = None


class SequenceSaveRequest(BaseModel):
    """Keeping a plan under a name, to bring back another night."""

    name: str = Field(min_length=1, max_length=120)
    # Given, this overwrites that saved sequence; otherwise a sequence of the
    # same name is replaced, and failing that a new one is made.
    id: str | None = Field(default=None, max_length=64)


class PlanOptionsRequest(BaseModel):
    """One target's own sequence settings."""

    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=9)
    # 0 means "use the observatory's floor", which is why the lower bound is 0
    # rather than the lowest altitude worth shooting at.
    minAltitude: float | None = Field(default=None, ge=0, le=85)
    moonAvoidance: float | None = Field(default=None, ge=0, le=180)
    filterOrder: str | None = Field(default=None, pattern="^(grouped|rotate)$")
    focusOnStart: bool | None = None
    ditherEveryFrames: int | None = Field(default=None, ge=0, le=50)
    goalHours: float | None = Field(default=None, ge=0, le=1000)
    # Which panels of a mosaic to shoot. An empty list means all of them, so
    # this is how a re-shoot is cleared as well as how it is set.
    panels: list[int] | None = Field(default=None, max_length=400)
    # Collaboration target, rig with a rotator: turn to the project's angle.
    collabMatchRotation: bool | None = None


class FramingReferenceRequest(BaseModel):
    """A FITS file to frame against."""

    path: str = Field(min_length=1, max_length=1000)
    # The planner draws it a few hundred pixels wide; anything more is bytes
    # over the socket for detail no one will see.
    maxDim: int = Field(default=1600, ge=256, le=4000)


class MosaicPreviewRequest(BaseModel):
    ra: float = Field(ge=0, lt=24)
    dec: float = Field(ge=-90, le=90)
    panelWidth: float = Field(gt=0, le=90)
    panelHeight: float = Field(gt=0, le=90)
    rows: int = Field(default=1, ge=1, le=20)
    columns: int = Field(default=1, ge=1, le=20)
    overlap: float = Field(default=0.1, ge=0, lt=0.9)
    rotation: float = Field(default=0.0, ge=-360, le=360)
    align: str = Field(default="aligned", pattern="^(fixed|aligned)$")


class SurveyRegionFields(BaseModel):
    """The sweep's shape in the Sun's frame, plus how to acquire it."""
    elongationMin: float | None = Field(default=None, ge=0, le=180)
    elongationMax: float | None = Field(default=None, ge=0, le=180)
    betaMin: float | None = Field(default=None, ge=-90, le=90)
    betaMax: float | None = Field(default=None, ge=-90, le=90)
    side: str | None = Field(default=None, pattern="^(morning|evening|both)$")
    sunHigh: float | None = Field(default=None, ge=-30, le=0)
    sunLow: float | None = Field(default=None, ge=-30, le=0)
    minAltitude: float | None = Field(default=None, ge=0, le=80)
    maxAirmass: float | None = Field(default=None, ge=0, le=20)
    moonAvoidance: float | None = Field(default=None, ge=0, le=180)
    moonScaleByPhase: bool | None = None
    galacticAvoidance: float | None = Field(default=None, ge=0, le=90)
    revisitNights: float | None = Field(default=None, ge=0, le=365)
    exposure: float | None = Field(default=None, gt=0, le=600)
    exposureCount: int | None = Field(default=None, ge=1, le=500)
    binning: int | None = Field(default=None, ge=1, le=4)
    dither: bool | None = None
    ditherPixels: float | None = Field(default=None, gt=0, le=100)
    ditherSeconds: float | None = Field(default=None, ge=0, le=120)
    panelOverheadSeconds: float | None = Field(default=None, ge=0, le=600)
    overlap: float | None = Field(default=None, ge=0, lt=0.9)
    filter: str | None = Field(default=None, max_length=24)


class SurveySettingsRequest(SurveyRegionFields):
    pass


class SurveyPlanRequest(SurveyRegionFields):
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class SurveySaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    # Evening sweeps run first, morning sweeps last; that is just when the
    # twilight is.
    position: str = Field(default="end", pattern="^(start|end)$")
    settings: dict[str, Any] | None = None


class SurveyCell(BaseModel):
    cell: str = Field(min_length=1, max_length=32)
    elongation: float | None = None
    beta: float | None = None
    side: str | None = None


class SurveyCoverageRequest(BaseModel):
    cells: list[SurveyCell] = Field(default_factory=list, max_length=2000)
    when: float | None = None


class TelescopeRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)


class TelescopeEditRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    # Hand this telescope the mount and the guider.
    master: bool = False


class ProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    # Given, this saves over an existing profile rather than making a new one.
    id: str | None = Field(default=None, max_length=32)


class GuideRequest(BaseModel):
    settlePixels: float = Field(default=1.5, gt=0, le=50)
    settleTime: float = Field(default=8.0, ge=0, le=600)
    settleTimeout: float = Field(default=60.0, ge=1, le=3600)
    recalibrate: bool = False


class DitherRequest(BaseModel):
    pixels: float = Field(default=3.0, gt=0, le=100)
    raOnly: bool = False
    settlePixels: float = Field(default=1.5, gt=0, le=50)
    settleTime: float = Field(default=8.0, ge=0, le=600)
    settleTimeout: float = Field(default=60.0, ge=1, le=3600)


# ---------------------------------------------------------------------------
# Equipment
# ---------------------------------------------------------------------------

@app.get("/api/backends")
def get_backends() -> dict[str, Any]:
    return {"backends": rigs.master.manager.backends()}


@app.get("/api/drivers/{kind}")
def get_drivers(kind: str, refresh: bool = False) -> dict[str, Any]:
    """Which drivers a slot can pick from.

    The same everywhere: an ASCOM registration and an Alpaca scan describe the
    machine and the network, not one telescope.  Which of them a given
    telescope actually connects is a separate question.
    """
    return {"drivers": _guard(rigs.master.manager.drivers, kind, refresh)}


@app.get("/api/drivers/{kind}/settings")
def get_driver_settings(kind: str, driverId: str,
                        rig: str | None = None) -> dict[str, Any]:
    """What an ASCOM driver keeps in its own profile for this ProgID.

    A driver that serves several identical cameras has to be told which one is
    meant, and where it keeps that is its own business — a serial number, a
    device index, a name.  Rather than guess at the key, show what is actually
    stored and let the telescope pin the one that matters.
    """
    target = _rig(rig)
    pinned: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        pinned = (equipment.rig(target.id)["devices"].get(kind)
                  or {}).get("options", {})
    return {"driverId": driverId,
            "settings": _guard(target.manager.driver_settings, kind, driverId),
            "pinned": pinned}


@app.post("/api/drivers/{kind}/setup")
def open_driver_setup(kind: str, body: DriverSetupRequest,
                      rig: str | None = None) -> dict[str, Any]:
    """Open the driver's own setup window, on its own apartment thread."""
    target = _rig(rig)
    _guard(target.manager.open_driver_setup, kind, body.driverId)
    return {"opened": True, "driverId": body.driverId, "rig": target.id}


@app.get("/api/diagnostics")
def diagnostics(lines: int = Query(default=300, ge=10, le=5000)) -> dict[str, Any]:
    """The file log and any crash report.

    The window has no console behind it, so this is how the tail of the last run
    — including the one that did not survive — gets looked at.
    """
    return {
        "logDir": str(logs.log_dir()),
        "log": logs.tail(lines),
        "crash": logs.crash_report(),
    }


@app.get("/api/guider/phd2")
def phd2_info() -> dict[str, Any]:
    """Whether PHD2 can be found and started, and which profiles it offers.

    The Equipment dialog asks before you connect, so a missing install or a
    mistyped path is something you find out in the afternoon rather than at the
    telescope.
    """
    settings = config.section("guiding")
    executable = phd2.find_executable(settings.get("phd2Path", ""))
    guider = rigs.master.manager.get("guider")
    profiles: list[dict[str, Any]] = []
    if guider is not None and guider.connected:
        with contextlib.suppress(Exception):
            profiles = guider.profiles()
    return {
        "executable": executable,
        "found": executable is not None,
        "running": phd2.server_answers(DEFAULT_PHD2_HOST, DEFAULT_PHD2_PORT),
        "profiles": profiles,
    }


@app.post("/api/alpaca/scan")
def scan_alpaca(body: ScanRequest) -> dict[str, Any]:
    found = _guard(rigs.scan_alpaca, body.host, body.port)
    return {"found": found}


@app.post("/api/devices/{kind}/connect")
def connect_device(kind: str, body: ConnectRequest,
                   rig: str | None = None) -> dict[str, Any]:
    target = _rig(rig)
    if kind in MASTER_DEVICE_KINDS and not target.is_master:
        raise HTTPException(
            status_code=400,
            detail=f"the {kind} belongs to the master telescope "
                   f"({rigs.master.name}); there is only one of it")
    # A slot with no options named of its own keeps whatever it was last given,
    # so connecting from the dropdown does not quietly forget the device id.
    remembered: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        remembered = equipment.rig(target.id)["devices"].get(kind) or {}
    options = body.options
    if options is None and remembered.get("driverId") == body.driverId:
        options = remembered.get("options") or None
    device = _guard(target.manager.connect, kind, body.backend, body.driverId,
                    body.name, options)
    # Remember the choice so a profile can put it back, and so the rig can be
    # reconnected in one click at the start of the next night.
    with contextlib.suppress(Exception):
        equipment.set_device(target.id, kind, {"backend": body.backend,
                                               "driverId": body.driverId,
                                               "name": device.name,
                                               "options": options or {}})
    return {"connected": True, "name": device.name, "rig": target.id}


@app.post("/api/devices/{kind}/disconnect")
def disconnect_device(kind: str, rig: str | None = None) -> dict[str, Any]:
    target = _rig(rig)
    _guard(target.manager.disconnect, kind)
    return {"connected": False, "rig": target.id}


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    return _snapshot()


@app.post("/api/log")
def add_log(body: LogRequest) -> dict[str, Any]:
    """Let the page put its own failures in the session log.

    A browser-side error that only ever reaches the developer console is
    invisible in the desktop window, which is where this actually runs.
    """
    rigs.log(body.message[:300], body.level)
    return {"logged": True}


def _snapshot() -> dict[str, Any]:
    master = rigs.master
    site = effective_site(config, master.manager)
    # Every telescope's state, gathered once. The master's slice is then reused
    # for the top-level keys rather than asked for a second time: reading a
    # device's status means a string of driver calls, and on ASCOM they all
    # queue on that telescope's one apartment thread — the same thread a
    # connection is trying to use. Asking twice, several times a second, is felt
    # as lag on every button in the Equipment dialog.
    everything = rigs.status()
    mine = next((entry for entry in everything if entry["id"] == master.id),
                None) or {}
    # What the guider was last asked to do, alongside what it is doing. A
    # `guide` takes minutes and the button that started it has to be able to say
    # so, or it looks broken and gets pressed again.
    devices = dict(mine.get("devices", {}))
    devices["guider"] = {**(devices.get("guider") or {}),
                         "task": dict(guider_task)}
    mine = {**mine, "devices": devices}
    return {
        # The master's own state stays at the top level: one telescope is the
        # ordinary case, and it should read exactly as it always has.
        "devices": mine.get("devices", {}),
        "capture": mine.get("capture", {}),
        "solver": mine.get("solver", {}),
        "rigs": everything,
        "masterRig": master.id,
        "sequence": {**sequencer.status(), "shutdown": dict(shutdown_task)},
        # Carries the frame token, so the feed knows when to fetch a new picture
        # without polling the image itself.
        "piercam": pier.status(),
        # The weather, and whether anybody is being told about it.
        "safety": safety_watcher.status(),
        "notify": notifier.status(),
        "filterOffsets": offsets_run.status(),
        "collab": collab_client.status(),
        # The run, plus where the library resolved to — the settings form shows
        # the folder a blank field actually means.
        "calibration": {**calibrator.status(), "libraryRoot": str(library.root)},
        "site": site,
        "lst": (round(astro.local_sidereal_hours(site["longitude"]), 6)
                if site.get("longitude") is not None else None),
        "events": rigs.recent_events(60),
        # What is wrong right now, most serious first.
        "warnings": warning_board.snapshot(),
    }


@app.get("/api/warnings")
def get_warnings() -> dict[str, Any]:
    return warning_board.snapshot()


@app.post("/api/warnings/{key}/acknowledge")
def acknowledge_warning(key: str) -> dict[str, Any]:
    """Seen it: the warning stays on the list but leaves the strip until it
    changes level or comes back after clearing."""
    if not warning_board.acknowledge(key):
        raise HTTPException(status_code=404, detail="no such warning")
    return warning_board.snapshot()


@app.post("/api/warnings/check")
def check_warnings_now() -> dict[str, Any]:
    """One pass of every check, now, rather than at the next tick."""
    watchdog.pass_once()
    return warning_board.snapshot()


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

@app.post("/api/camera/expose")
def start_exposure(body: ExposeRequest, rig: str | None = None) -> dict[str, Any]:
    target = _rig(rig)
    # Deliberately taking a frame outranks a warm-down that is still running
    # from the last sequence.
    sequencer.cancel_warming("an exposure was started")
    _guard(target.capture.start, body.exposure, body.frameType, body.binning,
           body.gain, body.offset, body.loop, body.count)
    return {"started": True}


@app.post("/api/camera/abort")
def abort_exposure(rig: str | None = None) -> dict[str, Any]:
    _guard(_rig(rig).capture.abort)
    return {"aborted": True}


@app.post("/api/camera/loop")
def set_loop(body: TrackingRequest, rig: str | None = None) -> dict[str, Any]:
    _rig(rig).capture.set_loop(body.on)
    return {"loop": body.on}


@app.post("/api/camera/cooler")
def set_cooler(body: CoolerRequest, rig: str | None = None) -> dict[str, Any]:
    target = _rig(rig)
    camera = _guard(target.manager.require, "camera")
    if body.setpoint is not None:
        _guard(camera.set_setpoint, body.setpoint)
        target.manager.log(f"Cooler setpoint {body.setpoint:g} C")
    if body.on is not None:
        _guard(camera.set_cooler, body.on)
        target.manager.log(f"Cooler {'on' if body.on else 'off'}")
    return {"ok": True}


@app.post("/api/camera/settings")
def set_camera_settings(body: CameraSettingsRequest,
                        rig: str | None = None) -> dict[str, Any]:
    camera = _guard(_rig(rig).manager.require, "camera")
    _guard(camera.set_settings, body.binning, body.gain, body.offset)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Filter wheel
# ---------------------------------------------------------------------------

@app.post("/api/filterwheel/position")
def set_filter(body: FilterRequest, rig: str | None = None) -> dict[str, Any]:
    target = _rig(rig)
    wheel = _guard(target.manager.require, "filterwheel")
    _guard(wheel.set_position, body.position)
    names = wheel.names
    label = names[body.position] if body.position < len(names) else str(body.position)
    target.manager.log(f"Filter -> {label}")
    return {"ok": True}


@app.post("/api/filterwheel/names")
def set_filter_names(body: FilterNamesRequest,
                     rig: str | None = None) -> dict[str, Any]:
    wheel = _guard(_rig(rig).manager.require, "filterwheel")
    _guard(wheel.set_names, body.names)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Focuser
# ---------------------------------------------------------------------------

@app.post("/api/focuser/move")
def move_focuser(body: FocuserMoveRequest, rig: str | None = None) -> dict[str, Any]:
    target = _rig(rig)
    focuser = _guard(target.manager.require, "focuser")
    if body.position is not None:
        _guard(focuser.move_to, body.position)
        target.manager.log(f"Focuser -> {body.position}")
    elif body.delta is not None:
        _guard(focuser.move_relative, body.delta)
        target.manager.log(f"Focuser {body.delta:+d} steps")
    else:
        raise HTTPException(status_code=400, detail="provide either position or delta")
    return {"ok": True}


@app.post("/api/focuser/halt")
def halt_focuser(rig: str | None = None) -> dict[str, Any]:
    target = _rig(rig)
    focuser = _guard(target.manager.require, "focuser")
    _guard(focuser.halt)
    target.manager.log("Focuser halted", "warn")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Mount
#
# There is one mount however many telescopes are riding on it, so these always
# talk to the master.  A slave has no pointing of its own to command.
# ---------------------------------------------------------------------------

@app.post("/api/mount/slew")
def slew(body: SlewRequest) -> dict[str, Any]:
    mount = _guard(manager.require, "mount")
    _guard(mount.slew_to, body.ra, body.dec)
    manager.log(f"Slewing to RA {body.ra:.4f}h  Dec {body.dec:+.3f}")
    return {"ok": True}


@app.post("/api/mount/sync")
def sync(body: SlewRequest) -> dict[str, Any]:
    mount = _guard(manager.require, "mount")
    _guard(mount.sync_to, body.ra, body.dec)
    manager.log(f"Synced to RA {body.ra:.4f}h  Dec {body.dec:+.3f}")
    return {"ok": True}


@app.post("/api/mount/abort")
def abort_slew() -> dict[str, Any]:
    mount = _guard(manager.require, "mount")
    _guard(mount.abort_slew)
    manager.log("Slew aborted", "warn")
    return {"ok": True}


@app.post("/api/mount/tracking")
def set_tracking(body: TrackingRequest) -> dict[str, Any]:
    mount = _guard(manager.require, "mount")
    _guard(mount.set_tracking, body.on)
    manager.log(f"Tracking {'on' if body.on else 'off'}")
    return {"ok": True}


@app.post("/api/mount/park")
def park() -> dict[str, Any]:
    mount = _guard(manager.require, "mount")
    _guard(mount.park)
    manager.log("Parking mount")
    return {"ok": True}


@app.post("/api/mount/unpark")
def unpark() -> dict[str, Any]:
    mount = _guard(manager.require, "mount")
    _guard(mount.unpark)
    manager.log("Mount unparked")
    return {"ok": True}


@app.post("/api/mount/jog")
def jog(body: JogRequest) -> dict[str, Any]:
    mount = _guard(manager.require, "mount")
    _guard(mount.jog, body.direction.lower(), body.rate)
    return {"ok": True}


@app.post("/api/mount/jog/stop")
def stop_jog() -> dict[str, Any]:
    mount = _guard(manager.require, "mount")
    _guard(mount.stop_jog)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Rotator
# ---------------------------------------------------------------------------

@app.post("/api/rotator/move")
def move_rotator(body: RotatorMoveRequest, rig: str | None = None) -> dict[str, Any]:
    target = _rig(rig)
    rotator = _guard(target.manager.require, "rotator")
    if body.position is not None:
        _guard(rotator.move_absolute, body.position)
        target.manager.log(f"Rotator -> position angle {body.position:g}°")
    elif body.delta is not None:
        _guard(rotator.move_relative, body.delta)
        target.manager.log(f"Rotator {body.delta:+g}°")
    else:
        raise HTTPException(status_code=400, detail="provide either position or delta")
    return {"ok": True}


@app.post("/api/rotator/halt")
def halt_rotator(rig: str | None = None) -> dict[str, Any]:
    target = _rig(rig)
    rotator = _guard(target.manager.require, "rotator")
    _guard(rotator.halt)
    target.manager.log("Rotator halted", "warn")
    return {"ok": True}


@app.post("/api/rotator/sync")
def sync_rotator(body: RotatorSyncRequest, rig: str | None = None) -> dict[str, Any]:
    target = _rig(rig)
    rotator = _guard(target.manager.require, "rotator")
    _guard(rotator.sync, body.position)
    target.manager.log(f"Rotator synced to {body.position:g}°")
    return {"ok": True}


@app.post("/api/rotator/reverse")
def reverse_rotator(body: TrackingRequest, rig: str | None = None) -> dict[str, Any]:
    rotator = _guard(_rig(rig).manager.require, "rotator")
    _guard(rotator.set_reversed, body.on)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Flat panel
# ---------------------------------------------------------------------------

@app.post("/api/flatpanel/light")
def set_flat_light(body: FlatPanelRequest, rig: str | None = None) -> dict[str, Any]:
    target = _rig(rig)
    panel = _guard(target.manager.require, "flatpanel")
    if body.on:
        brightness = body.brightness if body.brightness is not None else panel.max_brightness
        _guard(panel.turn_on, brightness)
        target.manager.log(f"Flat panel on at {brightness}")
    else:
        _guard(panel.turn_off)
        target.manager.log("Flat panel off")
    return {"ok": True}


@app.post("/api/flatpanel/cover")
def set_cover(body: TrackingRequest, rig: str | None = None) -> dict[str, Any]:
    target = _rig(rig)
    panel = _guard(target.manager.require, "flatpanel")
    _guard(panel.open_cover if body.on else panel.close_cover)
    target.manager.log(f"Cover {'opening' if body.on else 'closing'}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Where frames go
# ---------------------------------------------------------------------------

@app.post("/api/capture/output")
def set_output(body: OutputRequest, rig: str | None = None) -> dict[str, Any]:
    return _guard(_rig(rig).capture.set_output, body.save, body.directory, body.target)


# ---------------------------------------------------------------------------
# Guider (PHD2)
# ---------------------------------------------------------------------------

#: The guider command in flight, if any. One at a time, because PHD2 is one
#: program with one socket and two overlapping `guide` calls confuse it.
guider_task: dict[str, Any] = {"busy": False, "what": "", "error": None,
                               "since": 0.0}


def _guider_action(what: str, action, *args, interrupt: bool = False) -> None:
    """Start a guider call on a thread, and report how it went.

    These calls are slow in a way HTTP is not built for: `guide` does not come
    back until PHD2 has found a star, calibrated if it must, and settled — two
    or three minutes from a standing start — and `dither` waits out its own
    settle. Run inline they held the request open for all of it with nothing on
    screen, so the button looked dead, and the natural response is to press it
    again. That second press is the real damage: two `guide` calls into one
    PHD2 socket, or a `stop` arriving in the middle of a `guide`.

    So the request returns at once and the answer arrives the way every other
    slow thing here reports itself — through the status socket, which is
    already carrying PHD2's state four times a second.

    `interrupt` is for stopping, which must always be allowed. A `guide` that is
    stuck waiting for a star it will never find is exactly when Stop is pressed,
    and refusing it because the guider is busy would be refusing the one command
    that helps.
    """
    if guider_task["busy"] and not interrupt:
        raise HTTPException(
            status_code=409,
            detail=f"the guider is busy: {guider_task['what']}")

    guider_task.update({"busy": True, "what": what, "error": None,
                        "since": time.time()})
    rigs.log(f"{what}…")

    def run() -> None:
        try:
            action(*args)
            rigs.log(f"{what}: done", "success")
        except DeviceError as exc:
            guider_task["error"] = str(exc)
            rigs.log(f"{what} failed: {exc}", "error")
        except Exception as exc:                   # noqa: BLE001 - driver misbehaviour
            guider_task["error"] = f"{type(exc).__name__}: {exc}"
            rigs.log(f"{what} failed: {type(exc).__name__}: {exc}", "error")
        finally:
            guider_task["busy"] = False

    threading.Thread(target=run, daemon=True, name="guider").start()


@app.post("/api/guider/guide")
def start_guiding(body: GuideRequest) -> dict[str, Any]:
    guider = _guard(manager.require, "guider")
    _guider_action(
        f"Guiding requested, settling to {body.settlePixels:g}px "
        f"for {body.settleTime:g}s",
        guider.start_guiding, body.settlePixels, body.settleTime,
        body.settleTimeout, body.recalibrate)
    return {"ok": True}


@app.post("/api/guider/stop")
def stop_guiding() -> dict[str, Any]:
    guider = _guard(manager.require, "guider")
    # Always allowed, even mid-`guide`: PHD2 takes `stop_capture` at any time,
    # and the pending call then fails, which is the point of pressing Stop.
    _guider_action("Stopping guiding", guider.stop_guiding, interrupt=True)
    return {"ok": True}


@app.post("/api/guider/dither")
def dither(body: DitherRequest) -> dict[str, Any]:
    guider = _guard(manager.require, "guider")
    _guider_action(f"Dither {body.pixels:g}px{' (RA only)' if body.raOnly else ''}",
                   guider.dither, body.pixels, body.raOnly, body.settlePixels,
                   body.settleTime, body.settleTimeout)
    return {"ok": True}


@app.post("/api/guider/pause")
def pause_guiding(body: TrackingRequest) -> dict[str, Any]:
    guider = _guard(manager.require, "guider")
    _guider_action(f"Guiding {'paused' if body.on else 'resumed'}",
                   guider.set_paused, body.on)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Collaborations
# ---------------------------------------------------------------------------

@app.get("/api/collab")
def collab_status() -> dict[str, Any]:
    return _collab_state()


@app.post("/api/settings/collab")
def set_collab_settings(body: CollabSettingsRequest) -> dict[str, Any]:
    values = body.model_dump(exclude_unset=True)
    # The form shows a mask where a credential is stored and is supposed to omit
    # the field unless somebody types a new one. Dropping the mask here as well
    # costs nothing and rules out the one failure this design can have: a token
    # silently overwritten with three asterisks, which looks like nothing at all
    # went wrong until the next poll fails.
    for credential in ("token", "adminToken"):
        if values.get(credential) == "***":
            values.pop(credential)
    saved = config.update("collab", values)
    collab_client.start()
    # No token is ever sent back, for the same reason the mail password is
    # not: it is a credential, and a form that round-trips one keeps it in the
    # browser's memory for no benefit.
    return {"collab": _masked_collab(saved),
            "status": collab_client.status(),
            "coordinator": collab_admin.status()}


def _masked_collab(saved: dict[str, Any]) -> dict[str, Any]:
    return {**saved,
            "token": "***" if saved.get("token") else "",
            "adminToken": "***" if saved.get("adminToken") else "",
            "userToken": "***" if saved.get("userToken") else ""}


#: The sign-in under way, if any: the code the server gave us and when.
login_task: dict[str, Any] = {"code": "", "url": "", "since": 0.0}


@app.post("/api/collab/login")
def collab_login() -> dict[str, Any]:
    """Sign in with Discord: open the browser, and start listening.

    The browser does the Discord part; this program only has to notice when
    it is done, which `/api/collab/login/poll` does. Nothing is typed and no
    secret passes through this window.
    """
    answer = _admin(collab_admin.begin_login)
    login_task.update({"code": answer.get("code") or "",
                       "url": answer.get("url") or "", "since": time.time()})
    opened = False
    with contextlib.suppress(Exception):
        import webbrowser
        opened = bool(webbrowser.open(login_task["url"]))
    return {"url": login_task["url"], "opened": opened,
            "expiresIn": answer.get("expiresIn")}


@app.get("/api/collab/login/poll")
def collab_login_poll() -> dict[str, Any]:
    """Has the sign-in in the browser finished? Keep the token if so."""
    code = login_task.get("code") or ""
    if not code:
        return {"state": "idle"}
    answer = _admin(collab_admin.poll_login, code)
    if answer.get("state") == "done" and answer.get("token"):
        user = answer.get("user") or {}
        config.update("collab", {"userToken": answer["token"],
                                 "user": {"id": user.get("id") or "",
                                          "name": user.get("name") or "",
                                          "admin": bool(user.get("admin")),
                                          "canStart": bool(user.get("canStart"))},
                                 "coordinator": user.get("name") or ""})
        login_task.update({"code": "", "url": "", "since": 0.0})
        rigs.log(f"Collaboration: signed in as {user.get('name') or 'somebody'}",
                 "success")
        # Signed in is joined: the telescope is enrolled behind the same
        # button, so a new person presses one thing and is done.
        _drop_stale_admin_token()
        enrolled = None
        try:
            enrolled = _ensure_enrolled()
        except Exception as exc:                   # noqa: BLE001
            rigs.log(f"Collaboration: signed in, but this telescope could not "
                     f"be enrolled - {exc}", "warn")
        with contextlib.suppress(Exception):
            collab_client.poll()
        return {"state": "done", "user": user, "enrolled": enrolled,
                **_collab_state()}
    if answer.get("state") in ("expired", "claimed"):
        login_task.update({"code": "", "url": "", "since": 0.0})
    return {"state": answer.get("state") or "pending", "url": login_task.get("url")}


@app.post("/api/collab/logout")
def collab_logout() -> dict[str, Any]:
    """Sign out: tell the server, and forget the token here either way."""
    with contextlib.suppress(Exception):
        collab_admin.logout()
    config.update("collab", {"userToken": "", "user": {}})
    rigs.log("Collaboration: signed out", "info")
    return _collab_state()


@app.post("/api/collab/poll")
def collab_poll() -> dict[str, Any]:
    """Ask the server what to shoot, now, rather than waiting for the timer."""
    try:
        collab_client.poll()
    except Exception as exc:                       # noqa: BLE001 - offline is normal
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # The poll itself brought the plan into step (see `_follow_server`), so
    # there is nothing more to do here than say where things stand.
    return _collab_state()


def _follow_server(tasks: list[dict[str, Any]]) -> None:
    """Bring every adopted collaboration target into step with the server.

    Runs after every poll, on the client's own thread. The server deals each
    rig's panels afresh each night, from what everybody has collected, and
    the deal is the whole point: a plan that went on shooting last night's
    list until somebody pressed *Poll* with the right switch on would be one
    night behind for ever, and was. What moves is only what the server owns
    — the share, tonight's frames on it, the window; see `_resync_task` for
    what is deliberately left alone. A task nobody has adopted is untouched:
    joining is still a decision somebody makes.
    """
    for task in tasks or []:
        try:
            _resync_task(task)
        except Exception as exc:                   # noqa: BLE001 - never fatal
            rigs.log(f"Collaboration: could not bring the plan into step - {exc}",
                     "warn")


collab_client.on_poll = _follow_server


def _collab_window(now: float | None = None) -> tuple[float | None, float | None]:
    """The hours this rig gives a collaboration, as moments tonight.

    Clock times, because that is how somebody says it — "nine till one" — and
    because a window stored as an instant is eighteen hours stale by the next
    evening. The plan's own slot times already work that way and already roll
    forward keeping the clock time, so this hands them over and lets the
    existing machinery do the rest.
    """
    settings = config.section("collab")
    return schedule.clock_window(settings.get("fromClock"),
                                 settings.get("toClock"), now)


def _adopt_task(task: dict[str, Any], match_rotation: bool = True) -> dict[str, Any]:
    """Turn an accepted task into a target and a place in tonight's plan.

    This is what accepting has to mean. A task that was accepted and then sat
    on the Collab tab would be a promise nobody could keep: the sequencer works
    from the plan, and a chunk of sky that never reached it is a chunk nobody
    photographs.

    The chunk was sized to the *smallest* field in the collaboration, so on a
    wider rig it is one frame and on a narrower one it is a small mosaic. Both
    are the same target here, because depth is integration time at a point on
    the sky: each panel wants the hours the task asked for, not a share of them.
    """
    region = collab.Region.read(task["region"])
    profile = collab.RigProfile.read(collab_client.profile())
    field = profile.field()
    if field is None:
        raise HTTPException(
            status_code=400,
            detail="this telescope has not been told what it can see - set the "
                   "focal length, sensor size and pixel size in Site & Optics")

    # Already adopted: hand back what is there rather than making a second copy.
    for existing in targets.listing():
        if (existing.get("collab") or {}).get("task") == task["id"]:
            entry = next((e for e in plan.raw()["entries"]
                          if e.get("targetId") == existing["id"]), None)
            return {"target": existing, "entry": entry, "clamped": False,
                    "adopted": False}
    return _make_target(task, region, field, match_rotation)


def adopted_for(task_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The target and plan entry this task was adopted as, if it was."""
    if not task_id:
        return None, None
    for target in targets.listing():
        if (target.get("collab") or {}).get("task") == task_id:
            entry = next((e for e in plan.raw()["entries"]
                          if e.get("targetId") == target["id"]), None)
            return target, entry
    return None, None


def _collab_geometry(task: dict[str, Any], region: Any, field: Any,
                     match_rotation: bool = True) -> dict[str, Any]:
    """How this rig lays a collaboration out: angle, alignment and grid.

    The one place that decides it, used when the target is first made and
    again whenever something it depends on moves - the camera's measured
    angle on a rig with no rotator, or the operator's choice on one with.
    """
    overlap = 0.1
    rig = rigs.master
    turnable = collabclient.has_rotator(rig)
    single = task.get("kind") == "single"
    if turnable and match_rotation:
        # A rotator can put the camera at whatever angle the project asks,
        # and correct each panel for the convergence of the meridians so the
        # composite stays a rectangle: the grid is laid at the project's angle
        # and the frames stay parallel.
        angle = float(region.rotation or 0.0)
        align = "aligned"
        across, down = region.width, region.height
    else:
        # No rotator, or one the operator chose not to turn: the camera sits
        # at whatever angle it is at and every panel is shot there. The grid
        # is laid at *that* angle, the frames are not corrected between
        # panels — there is nothing to correct them with — and it has to be
        # big enough to cover the north-up region measured along the camera's
        # own axes. At 268 degrees that swaps the rows and the columns.
        angle = float(rig.config.get("optics", "rotation", 0.0) or 0.0)
        align = "fixed"
        across, down = collab.camera_frame(region, angle)
    if single:
        # One object, one frame, whatever the camera. The project's rectangle
        # is the framer's field, not a region to be covered; a narrower camera
        # gets the object in the middle of its own frame and that is the
        # contribution.
        rows = columns = 1
    else:
        columns = collab.tiles_across(across, field[0], overlap)
        rows = collab.tiles_across(down, field[1], overlap)
    how = ""
    if not turnable:
        how = (" (no rotator: camera fixed at its own angle"
               + ("" if single else ", frames not corrected between panels")
               + ")")
    elif not match_rotation:
        how = " (rotator left where it is, not turned to the project's angle)"
    return {"angle": angle, "align": align, "rows": rows, "columns": columns,
            "overlap": overlap, "turnable": turnable, "single": single, "how": how}


def _make_target(task: dict[str, Any], region: Any, field: Any,
                 match_rotation: bool = True) -> dict[str, Any]:
    """Create the target and its plan entry. See `_adopt_task`."""
    layout = _collab_geometry(task, region, field, match_rotation)
    angle, align = layout["angle"], layout["align"]
    rows, columns, overlap = layout["rows"], layout["columns"], layout["overlap"]
    name = task.get("projectName") or "Collaboration"
    how = layout["how"]
    target = _guard(
        targets.create, name,
        ra=region.ra / 15.0, dec=region.dec,
        rotation=angle,
        panel_width=field[0], panel_height=field[1],
        rows=rows, columns=columns, overlap=overlap, align=align,
        notes=(task.get("note") or "") + how,
        collab={"project": task.get("project"), "projectName": name,
                "task": task["id"], "version": int(task.get("version") or 1),
                "server": collab_client.server_url()})
    # Which of the mosaic's panels are this rig's. The target is the *whole*
    # project so the picture shows all of it; the share is what gets shot.
    share = _share_panels(task, target)
    targets.stamp_collab(target["id"], {"share": share})
    target = targets.get(target["id"])
    rigs.log(f"Collaboration: {name} is in the target list "
             f"({rows}x{columns} panels, {len(share) or 'all'} of them yours) - "
             "drag it into tonight in the Planner", "success")
    return {"target": target, "entry": None, "clamped": False, "adopted": True}


#: How far the camera's measured angle may differ from the one a fixed-angle
#: mosaic was laid at before the mosaic is laid again. Two degrees across a
#: five-degree field is a sixth of a degree at the edge - inside the overlap.
ANGLE_TOLERANCE = 2.0


def _angle_gap(a: float, b: float) -> float:
    return abs(((float(a) - float(b) + 180.0) % 360.0) - 180.0)


def _reframe_collab_target(target: dict[str, Any],
                           task: dict[str, Any] | None = None,
                           match_rotation: bool | None = None,
                           why: str = "") -> dict[str, Any] | None:
    """Lay a collaboration target out again if its geometry has moved.

    The geometry is not the operator's: it follows the camera's angle on a
    rig with no rotator, and the operator's *turn to the project's angle*
    choice on one with. Whenever either moves, the panels are re-laid, the
    share re-mapped onto the new panels, and the plan entry brought along.
    Returns the new target, or None when nothing needed doing.
    """
    stamp = target.get("collab") or {}
    if not stamp.get("task"):
        return None
    task = task or _task_for_target(target)
    if not task or not task.get("region"):
        return None
    region = collab.Region.read(task["region"])
    width = float(target.get("panelWidth") or 0.0)
    height = float(target.get("panelHeight") or 0.0)
    if width <= 0 or height <= 0:
        return None
    entry = next((e for e in plan.raw()["entries"]
                  if e.get("targetId") == target["id"]), None)
    if match_rotation is None:
        match_rotation = bool(plans.options_for(entry or {}).get(
            "collabMatchRotation", True))
    layout = _collab_geometry(task, region, (width, height), match_rotation)
    same = (target.get("align") == layout["align"]
            and int(target.get("rows") or 1) == layout["rows"]
            and int(target.get("columns") or 1) == layout["columns"]
            and _angle_gap(target.get("rotation") or 0.0, layout["angle"])
            <= ANGLE_TOLERANCE)
    if same:
        return None
    fresh = _guard(targets.reframe, target["id"], layout["angle"],
                   layout["rows"], layout["columns"], layout["align"])
    share = _share_panels(task, fresh)
    targets.stamp_collab(fresh["id"], {"share": share})
    fresh = targets.get(fresh["id"])
    if entry is not None:
        _plan_share(entry["id"], fresh)
        _apply_task_allocation(entry["id"], task, fresh)
    rigs.log(f"Collaboration: {fresh['name']} laid out again"
             f"{' - ' + why if why else ''}: {layout['rows']}x{layout['columns']} "
             f"panels at {layout['angle']:.1f}° ({layout['align']}), "
             f"{len(share) or 'all'} of them tonight's", "warn")
    return fresh


def _camera_angle_measured(target_id: str, measured: float) -> None:
    """The sequencer has just measured where the camera really sits.

    On a rig with no rotator the angle in the settings is what somebody
    typed, and the mosaic was laid at that. The plate solve on the first
    slew is the truth; if the two disagree by more than the tolerance, the
    settings are already updated by the solver, and here the mosaic is laid
    again at the real angle and the server is told so that it re-tiles its
    cells to match. The run then starts the target over on the new panels.
    """
    target = targets.get(target_id)
    fresh = _reframe_collab_target(
        target, why=f"the camera measures {measured:.1f}° on the sky")
    if fresh is None:
        return
    # The server tiles cells from the angle the profile reports, which has
    # just changed; a poll makes it re-tile and deal tonight's list on the
    # new cells, and `_follow_server` brings the share back onto the panels.
    with contextlib.suppress(Exception):
        collab_client.poll()


def _share_panels(task: dict[str, Any], target: dict[str, Any]) -> list[int]:
    """The task's share, as indices of the target's own panels.

    Matched by where the centres fall on the sky rather than by row and
    column, because the server numbers its cells south-to-north and the
    program numbers its panels in a boustrophedon from the north-east — two
    conventions that would have to be kept in step for ever. A centre is a
    centre.

    An empty share means "all of it": either the server predates shares, or
    the deal genuinely gave this rig the whole region.
    """
    cells = task.get("cells") or []
    share = task.get("share") or []
    panels = target.get("panels") or []
    if not cells or not share or not panels:
        return []
    chosen: list[int] = []
    for index in share:
        if not 0 <= index < len(cells):
            continue
        cell = cells[index]
        nearest = min(
            panels,
            key=lambda panel: astro.separation_degrees(
                float(panel["ra"]), float(panel["dec"]),
                float(cell["ra"]) / 15.0, float(cell["dec"])))
        if nearest["index"] not in chosen:
            chosen.append(int(nearest["index"]))
    return sorted(chosen)


def _task_for_target(target: dict[str, Any]) -> dict[str, Any] | None:
    stamp = target.get("collab") or {}
    if not stamp.get("task"):
        return None
    return next((entry for entry in collab_client.tasks()
                 if entry.get("id") == stamp["task"]), None)


def _hours_allocation(task: dict[str, Any], panels: int, hours: float,
                      overheads: dict[str, float]) -> list[dict[str, Any]]:
    """Turn "I can give it three hours" into frames, the task's way.

    **The exposures are the project's, not the operator's.** A collaboration
    that accepted whatever sub length each contributor felt like would be
    stacking frames that do not belong in the same stack, and the one thing a
    participant is agreeing to is to shoot it the way the project says. So what
    is chosen here is *how much*, and the split between filters keeps the
    proportions the task asked for — three hours of an Ha-and-OIII task is not
    three hours of Ha.
    """
    rows = [entry for entry in (task.get("filters") or [])
            if float(entry.get("exposure") or 0) > 0]
    if not rows or hours <= 0:
        return []
    weights = [max(0.0, float(entry.get("hours") or 0.0)) for entry in rows]
    total = sum(weights) or float(len(rows))
    if not sum(weights):
        weights = [1.0] * len(rows)

    panels = max(1, panels)
    per_frame = float(overheads.get("perFrame", 15.0))
    budget = hours * 3600.0 / panels

    allocation = []
    fractions = []
    spent = 0.0
    for entry, weight in zip(rows, weights):
        exposure = float(entry["exposure"])
        cost = exposure + per_frame
        share = budget * (weight / total)
        count = int(share // cost)
        allocation.append({"name": entry.get("filter"), "exposure": exposure,
                           "count": max(0, count)})
        fractions.append((share / cost) - count)
        spent += count * cost

    # Whatever is left over after each filter took its whole frames goes to
    # whoever came closest to earning another. Rounding every filter down on its
    # own throws away a frame's worth of night for no reason, and on a short
    # session that is a noticeable part of it.
    left = budget - spent
    order = sorted(range(len(rows)), key=lambda i: fractions[i], reverse=True)
    progress = True
    while left > 0 and progress:
        progress = False
        for index in order:
            cost = float(rows[index]["exposure"]) + per_frame
            if cost <= left:
                allocation[index]["count"] += 1
                left -= cost
                progress = True

    # A budget too small for even one frame of anything still has to come back
    # with something, or "twenty minutes" reads as "nothing planned" and there
    # is no telling a small night from a broken one.
    if not any(row["count"] for row in allocation):
        allocation[0]["count"] = 1
    return allocation


def _apply_task_allocation(entry_id: str, task: dict[str, Any],
                           target: dict[str, Any]) -> bool:
    """Put a task's filters and counts onto its plan entry.

    The counts are *per panel*. When the server has said what tonight's visit
    to each panel is (`visit.frames`, sized so the night gets round every
    panel on the share with enough frames on each to stack) that is what goes
    on; the run then visits the share's panels in order, each at tonight's
    depth, and gets through them. Without one - an older server - the counts
    are the project's full depth and the run stops when time runs out.
    """
    tonight = ((task.get("visit") or {}).get("frames") or {})
    allocation = []
    for item in (task.get("filters") or []):
        name = item.get("filter")
        count = (int(tonight[name]) if name in tonight
                 else collab.FilterTask.read(item).frames())
        allocation.append({"name": name,
                           "exposure": float(item.get("exposure") or 0.0),
                           "count": count})
    result = _guard(plan.set_filters, entry_id, allocation, _share_count(target),
                    1e12, _overheads())
    return bool(result.get("clamped"))


def _share_count(target: dict[str, Any]) -> int:
    """How many of the mosaic's panels this rig actually shoots.

    The counts on a plan entry are per panel, and the budget is what it costs
    to do them all — so the number that matters is the share, not the whole
    grid. A rig holding twelve of forty panels is doing twelve panels' work.
    """
    share = (target.get("collab") or {}).get("share") or []
    if share:
        return len(share)
    return max(1, len(target.get("panels") or []) or 1)


def _project_rules(task: dict[str, Any]) -> dict[str, float]:
    """The altitude floor and Moon distance the project's creator set.

    Read from the requirements the last poll brought back, by project; the
    task in hand's requirements are the fallback for a server that only
    sends the current one.
    """
    status = collab_client.status()
    by_project = status.get("requirementsByProject") or {}
    wants = by_project.get(str(task.get("project") or ""))
    if wants is None and (status.get("task") or {}).get("id") == task.get("id"):
        wants = status.get("requirements")
    if wants is None:
        wants = task.get("requirements")
    return collab.Requirements.read(wants or {}).rules()


def _apply_project_rules(entry_id: str, task: dict[str, Any]) -> None:
    """Write the project's rules onto its plan entry, if it has one."""
    with contextlib.suppress(Exception):
        plan.set_options(entry_id, _project_rules(task))


def _plan_share(entry_id: str, target: dict[str, Any]) -> None:
    """Limit the plan entry to the panels that are this rig's.

    The mosaic on the plan is the whole project, so the picture shows all of
    it; the `panels` option is what turns that into the part this telescope
    shoots. An empty share is the whole thing and clears the option.
    """
    share = (target.get("collab") or {}).get("share") or []
    # A share that is the whole mosaic is no restriction at all, and is stored
    # as none: "panels 1, 2, 3 ... 12 only" on a twelve-panel mosaic is noise
    # that reads as a limit where there is none.
    if share and len(share) >= len(target.get("panels") or []):
        share = []
    _guard(plan.set_options, entry_id, {"panels": list(share)})


def _resync_task(task: dict[str, Any]) -> dict[str, Any] | None:
    """Bring an adopted target back into step with what the server now says.

    A coordinator can change a task after it was accepted: more hours, a
    different filter, a chunk moved. Left alone, the plan would go on shooting
    what was agreed a week ago.

    The allocation, the share of panels and the window are re-applied;
    everything the *operator* chose about the entry — where it sits in the
    plan, its priority, its altitude floor — is left exactly as it is. A sync
    that reset somebody's own settings would be a worse problem than the one it
    solves.

    The share is the one thing on the entry that looks like the operator's and
    is not: the server moves it as other rigs join and as frames come in, and
    that is the whole reason a rig polls.
    """
    target, entry = adopted_for(task.get("id") or "")
    if target is None or entry is None:
        return None

    version = int(task.get("version") or 1)
    stamped = (target.get("collab") or {}).get("version")
    if stamped is not None and int(stamped) == version:
        return None

    # The server may have re-tiled its cells - the camera's angle moved, or
    # this rig's rotator choice did - and the panels have to follow before the
    # share can be mapped onto them.
    fresh = _reframe_collab_target(target, task, why="the server's tiling moved")
    if fresh is not None:
        target = fresh
    share = _share_panels(task, target)
    targets.stamp_collab(target["id"], {"version": version, "share": share})
    target = targets.get(target["id"])
    _plan_share(entry["id"], target)
    _apply_project_rules(entry["id"], task)

    clamped = _apply_task_allocation(entry["id"], task, target)
    start, end = _collab_window()
    if start or end:
        _guard(plan.set_times, entry["id"], start, end)

    rigs.log(f"Collaboration: {target['name']} brought into step with the "
             f"server (revision {version}, {len(share) or 'all'} of "
             f"{len(target.get('panels') or []) or 1} panels)", "info")
    entry = next((e for e in plan.raw()["entries"] if e["id"] == entry["id"]),
                 entry)
    return {"target": target, "entry": entry, "clamped": clamped,
            "version": version}


@app.post("/api/collab/task")
def collab_respond(body: CollabTaskRequest) -> dict[str, Any]:
    """Accept, decline or finish the task in hand.

    Accepting is deliberately a decision somebody makes: a task that put itself
    on the plan would be the program deciding what a telescope does tonight.
    Once made, though, the decision is carried out — accepting puts the chunk on
    the plan, because an acceptance that changed nothing would be worse than no
    button at all.
    """
    task = (collab_client.status() or {}).get("task")
    try:
        collab_client.respond(body.state)
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Adopt the task *as the server now holds it*. Accepting is itself a change
    # of state and bumps the revision, so adopting the copy read a moment
    # earlier stamped the target one revision behind and every freshly accepted
    # chunk reported itself as out of step with the server that had just agreed
    # it.
    task = (collab_client.status() or {}).get("task") or task

    adopted = None
    if body.state == "accepted" and task:
        try:
            adopted = _adopt_task(task)
        except HTTPException:
            raise
        except Exception as exc:                   # noqa: BLE001
            # The server has already been told. Saying what went wrong here is
            # far better than unwinding an acceptance somebody meant.
            rigs.log(f"Collaboration: accepted, but could not be put on the "
                     f"plan - {exc}", "warn")
    return {**collab_client.status(), "adopted": adopted,
            "plan": plan.raw() if adopted else None}


def _collab_state() -> dict[str, Any]:
    """The client's status, plus what this machine has done about it.

    Three questions the Collab tab has to answer and the client alone cannot:
    is the task in hand already on the plan, is what is on the plan still what
    the server says, and — if it is out of step — may this be put right without
    asking.
    """
    status = collab_client.status()
    task = status.get("task") or {}
    target, entry = adopted_for(task.get("id") or "")
    version = int(task.get("version") or 1)

    stamped = (target or {}).get("collab", {}).get("version")
    if target is not None and stamped is None:
        # Adopted before revisions were recorded. The honest reading of "no
        # stamp" is "unknown", and nagging that something is out of step on no
        # evidence is worse than missing one change — so what is on the plan
        # becomes the baseline, once, and changes from here are caught.
        targets.stamp_collab(target["id"], {"version": version})
        stamped = version

    return {
        **status,
        # Two different questions, and they used to share one answer. A chunk
        # becomes a *target* when it is joined or accepted; whether it is being
        # shot tonight is decided separately, in the Planner, alongside every
        # other target. Conflating them is what made the Collab tab look like a
        # second planner.
        "hasTarget": target is not None,
        "onPlan": bool(target and entry),
        "targetId": (target or {}).get("id"),
        "entryId": (entry or {}).get("id"),
        # Whether this telescope can turn its camera at all, and where it
        # sits now: what decides whether joining offers "turn to the
        # project's angle" as a choice.
        "rotator": collabclient.has_rotator(rigs.master),
        "cameraAngle": float(rigs.master.config.get("optics", "rotation", 0.0) or 0.0),
        # Who this program is on the server, and what that lets it do. The
        # owner holds the admin token and may do anything; somebody signed in
        # with Discord is a member, who may enrol their own telescope and -
        # role permitting - start collaborations. The server enforces all of
        # it; this is so the tab can offer the right buttons.
        "person": _person_state(),
        "login": {"pending": bool(login_task.get("code")),
                  "url": login_task.get("url") or ""},
        # Whether this telescope has a token here, and how often it asks.
        "enrolled": bool(config.get("collab", "token", "")),
        "pollMinutes": float(config.get("collab", "pollMinutes", 10) or 10),
        "sharePosition": bool(config.get("collab", "sharePosition", True)),
        "telescope": rigs.master.name,
        "defaultServer": collab.DEFAULT_SERVER,
        # Whether the server offers Discord sign-in at all. Known from the
        # last health check; None until the server has answered once.
        "serverAuth": _server_offers_discord(),
        # The coordinator has changed the task since it was adopted.
        "stale": bool(target and int(stamped or version) != version),
    }


#: What the server last said about sign-in, so the tab is not asking on
#: every tick. (offers Discord?, when it was asked)
_server_auth: dict[str, Any] = {"value": None, "at": 0.0}


def _server_offers_discord() -> bool | None:
    if time.time() - _server_auth["at"] > 60.0:
        _server_auth["at"] = time.time()
        try:
            _server_auth["value"] = bool(collab_admin.auth_status().get("discord"))
        except Exception:                          # noqa: BLE001 - offline is normal
            pass
    return _server_auth["value"]


#: Whether the stored owner token is one this server knows, checked at most
#: once a minute. A token from another server would otherwise make the tab
#: say "server owner" and hide the one button that would put things right.
_admin_check: dict[str, Any] = {"at": 0.0, "token": ""}


def _admin_token_known() -> bool:
    token = str(config.get("collab", "adminToken", "") or "")
    if not token:
        return False
    if _admin_check["token"] == token and time.time() - _admin_check["at"] < 60.0:
        return True
    try:
        collab_admin.me()
    except collabadmin.CollabError as exc:
        if exc.status == 401:
            _drop_stale_admin_token()
            return False
        return True                                # offline: believe the file
    _admin_check.update({"at": time.time(), "token": token})
    return True


def _person_state() -> dict[str, Any]:
    user = dict(config.get("collab", "user", {}) or {})
    signed_in = bool(config.get("collab", "userToken", "")) and bool(user.get("id"))
    # An owner is somebody the server lists by Discord id, or - for anybody
    # still running the old way - the holder of an admin token it knows.
    admin = (signed_in and bool(user.get("admin"))) or _admin_token_known()
    if signed_in and not bool(config.get("collab", "token", "")):
        # Signed in but never enrolled - a join that was cut short. Put right
        # here, so the tab never shows "joined" over a telescope with no
        # token, and nobody has to find a button for it.
        with contextlib.suppress(Exception):
            _ensure_enrolled()
    return {
        "admin": admin,
        "signedIn": signed_in,
        "id": str(user.get("id") or ""),
        "name": ("coordinator" if admin and not signed_in
                 else str(user.get("name") or "")),
        "canStart": admin or (signed_in and bool(user.get("canStart", True))),
    }


@app.get("/api/collab/open")
def collab_open() -> dict[str, Any]:
    """Collaborations anybody can join, and whether this rig qualifies."""
    try:
        return {"projects": collab_client.open_projects()}
    except Exception as exc:                       # noqa: BLE001 - offline is normal
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/collab/join/{project_id}")
def collab_join(project_id: str, body: CollabJoinRequest) -> dict[str, Any]:
    """Sign up for a share of a project, and put it in the target list.

    The chunk becomes a target and nothing more. What it does tonight is a
    decision made in the Planner, alongside every other target, rather than
    something a different tab arranges behind the Planner's back.
    """
    try:
        answer = collab_client.join(project_id, body.hours, body.exposure)
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    task = answer.get("task")
    adopted = None
    if task:
        try:
            adopted = _adopt_task(task)
        except HTTPException:
            raise
        except Exception as exc:                   # noqa: BLE001
            rigs.log("Collaboration: joined, but the chunk could not be made "
                     f"into a target - {exc}", "warn")
    return {**_collab_state(), "joined": adopted}


@app.post("/api/collab/enrol")
def collab_enrol_self() -> dict[str, Any]:
    """Give this telescope a token of its own, using the coordinator's.

    Enrolling a rig used to be a form: mint a token over here, copy it into a
    box over there. On the machine that holds both credentials that is a step
    with no decision in it, and a step with no decision in it is one to remove.
    """
    name = rigs.master.name or "telescope"
    if not collab_admin.configured():
        raise HTTPException(
            status_code=400,
            detail="sign in with Discord first (or, if you run the server, "
                   "store its owner token) - enrolling needs to know whose "
                   "telescope this is")
    made = _admin(collab_admin.add_agent, name, str(
        config.get("collab", "coordinator", "") or ""))
    token = made.get("token") or ""
    if not token:
        raise HTTPException(status_code=502,
                            detail="the server did not hand back a token")
    config.update("collab", {"token": token, "enabled": True})
    collab_client.start()
    try:
        collab_client.poll()
    except Exception:                              # noqa: BLE001 - offline is normal
        pass
    rigs.log(f"Collaboration: {name} enrolled on {collab_client.server_url()}",
             "success")
    return {**_collab_state(), "enrolled": made.get("agent")}


def _drop_stale_admin_token() -> None:
    """An owner token this server does not know is from another server.

    The coordinator's own token from the days the server ran on their PC
    would otherwise outrank the Discord sign-in that just succeeded and turn
    every call into "not the server owner". It is forgotten, with a line in
    the log; the real owner token goes in under Advanced.
    """
    if not config.get("collab", "adminToken", ""):
        return
    try:
        collab_admin.me()
    except collabadmin.CollabError as exc:
        if exc.status == 401:
            config.update("collab", {"adminToken": ""})
            rigs.log("Collaboration: the stored owner token belongs to another "
                     "server and has been forgotten. Press Join with Discord; "
                     "the owner token for this server goes under Advanced.",
                     "warn")


def _ensure_enrolled() -> dict[str, Any] | None:
    """Give this telescope a token on the server if it has none that works.

    Called the moment somebody has signed in, so that joining is one button:
    sign in, and the telescope is enrolled behind it. A token from another
    server - the coordinator's old one on their own PC, say - is found out by
    saying hello with it and replaced; a token that works is left alone.
    """
    token = str(config.get("collab", "token", "") or "").strip()
    if token:
        try:
            collab_client.hello()
            return None                            # already enrolled here
        except Exception as exc:                   # noqa: BLE001
            if "401" not in str(exc):
                return None                        # offline: leave it be
    name = rigs.master.name or "telescope"
    made = collab_admin.add_agent(name, str(config.get("collab", "coordinator", "") or ""))
    fresh = made.get("token") or ""
    if not fresh:
        return None
    config.update("collab", {"token": fresh, "enabled": True})
    collab_client.start()
    rigs.log(f"Collaboration: {name} enrolled on {collab_client.server_url()}",
             "success")
    return made.get("agent")


def _collab_hours_hint() -> float:
    """Hours a night this rig gives collaborations, read off the plan.

    The time a collaboration's entry can really use tonight: the target's
    window, clipped by the times pinned on the entry and by dawn - the same
    figure the Plan tab's Tonight line is worked out from. Telling the server
    that, rather than a guess, is what makes its list for the night the list
    the telescope can actually shoot: a panel is either reachable tonight or
    it is not, and the server should never deal one that is not.
    """
    best = 0.0
    with contextlib.suppress(Exception):
        known = {t["id"]: t for t in targets.listing()}
        for entry in plan.raw().get("entries") or []:
            target = known.get(entry.get("targetId") or "")
            if not target or not (target.get("collab") or {}).get("project"):
                continue
            with contextlib.suppress(Exception):
                _entry, info = _entry_budget(entry["id"])
                best = max(best, float(info.get("availableSeconds") or 0.0) / 3600.0)
    return round(best, 2)


collab_client.hours_hint = _collab_hours_hint
sequencer.on_camera_angle = _camera_angle_measured


def _presence_hint() -> dict[str, Any]:
    """Where this telescope points and what it is doing, for the group.

    The mount's coordinates when it is connected, the sequencer's state, and
    the name of the target being shot - never the observatory's location.
    """
    said: dict[str, Any] = {}
    mount = rigs.master.manager.get("mount")
    if mount is not None and mount.connected:
        with contextlib.suppress(Exception):
            said["ra"] = float(mount.ra)
            said["dec"] = float(mount.dec)
            said["slewing"] = bool(mount.slewing)
    run = sequencer.status()
    state = "idle"
    if run.get("running"):
        state = "paused" if run.get("paused") else str(run.get("state") or "running")
    elif said.get("slewing"):
        state = "slewing"
    elif calibrator.running:
        state = "calibrating"
    said["state"] = state
    entry_id = run.get("entryId") if run.get("running") else None
    if entry_id:
        entry = next((e for e in plan.raw()["entries"] if e["id"] == entry_id), None)
        if entry is not None:
            said["target"] = str(entry.get("name") or "")
            target = next((t for t in targets.listing()
                           if t["id"] == entry.get("targetId")), None)
            stamp = (target or {}).get("collab") or {}
            if stamp.get("project"):
                said["project"] = str(stamp["project"])
    return said


collab_client.presence_hint = _presence_hint


@app.post("/api/collab/projects/{project_id}/take")
def collab_take(project_id: str, body: CollabJoinRequest) -> dict[str, Any]:
    """Take part in a collaboration and put it in tonight's plan.

    One press, because it is one decision. Joining, making the chunk into a
    target and putting that target in the plan are three steps only in the sense
    that the program does three things; to the operator it is "I will shoot some
    of this tonight".

    Idempotent at every stage: a collaboration already joined is not joined
    twice, and a target already in the plan is not added twice.
    """
    status = collab_client.status()
    held = next((task for task in (status.get("tasks") or [])
                 if task.get("project") == project_id), None)
    if held is None:
        try:
            answer = collab_client.join(project_id, body.hours, body.exposure)
        except Exception as exc:                   # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        held = answer.get("task")
    if held is None:
        raise HTTPException(status_code=502,
                            detail="the server gave nothing to shoot")

    adopted = _adopt_task(held, body.matchRotation)
    target = adopted["target"]
    entry = next((e for e in plan.raw()["entries"]
                  if e.get("targetId") == target["id"]), None)
    clamped = False
    if entry is None:
        entry = _guard(plan.add, target["id"], target["name"])
        _plan_share(entry["id"], target)
        _apply_project_rules(entry["id"], held)
        clamped = _apply_task_allocation(entry["id"], held, target)
        start, end = _collab_window()
        if start or end:
            entry = _guard(plan.set_times, entry["id"], start, end)
        rigs.log(f"Collaboration: {target['name']} is in tonight's plan",
                 "success")

    return {**_collab_state(), "target": target, "entry": entry,
            "clamped": clamped, "plan": plan.raw()}


@app.post("/api/collab/adopt")
def collab_adopt_all() -> dict[str, Any]:
    """Make sure every chunk this rig holds is in the target list.

    Idempotent, and the thing that makes the Planner honest after a rig has
    been away: chunks joined from another machine, or accepted before this one
    last started, are otherwise held by the server and invisible here.
    """
    made = []
    for task in collab_client.tasks():
        if task.get("state") not in ("accepted", "offered"):
            continue
        try:
            result = _adopt_task(task)
        except Exception as exc:                   # noqa: BLE001
            rigs.log(f"Collaboration: {task.get('projectName')} could not be "
                     f"made into a target - {exc}", "warn")
            continue
        if result.get("adopted"):
            made.append(result["target"])
    return {**_collab_state(), "made": made}


class CollabHoursRequest(BaseModel):
    """How much of tonight to give a chunk. The only number that is yours."""

    hours: float = Field(ge=0.0, le=24.0)


@app.post("/api/collab/entries/{entry_id}/hours")
def collab_set_hours(entry_id: str, body: CollabHoursRequest) -> dict[str, Any]:
    """Give a collaboration chunk however much of tonight you can spare.

    The exposures and the filter proportions belong to the project and are not
    editable here — a collaboration whose contributors each chose their own sub
    length would be stacking frames that do not belong in the same stack. What
    is yours to decide is how long the telescope is theirs for.
    """
    entry = next((e for e in plan.raw()["entries"] if e["id"] == entry_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="no such plan entry")
    target = _guard(targets.get, entry.get("targetId") or "")
    task = _task_for_target(target)
    if task is None:
        raise HTTPException(
            status_code=400,
            detail="that target is not a collaboration chunk this rig holds")

    # The hours are a *duration*, pinned on the entry as its end. Each panel
    # keeps the project's full depth; what the hours decide is how long the
    # telescope walks the share before it stops. Dividing the hours across the
    # panels instead — the first version of this — gave a forty-panel share a
    # few minutes per panel and a box that read "847 hours" for a night.
    _apply_task_allocation(entry_id, task, target)

    # The window the sky gives it tonight, before any pin of our own clips it.
    latitude, longitude = _require_site()
    info = _target_schedule(target, _night_for(None), latitude, longitude,
                            float(config.get("schedule", "minAltitude", 30.0)),
                            entry)
    sky = info.get("skyWindow") or info["window"]
    rises, sets = sky.get("rises"), sky.get("sets")

    if body.hours <= 0:
        # "All the time it has": pinned to the window's own ends, so the box
        # says 01:33 -> 04:02 rather than emptying. Clearing the end meant the
        # same thing to the run but read as a reset to the person looking at
        # it, and it is their end time.
        if not rises or not sets:
            raise HTTPException(
                status_code=400,
                detail=f"{target['name']} is not observable tonight, so there is "
                       "no window to give it")
        entry = _guard(plan.set_times, entry_id, start_at=float(rises),
                       end_at=float(sets))
        return {"entry": entry, "clamped": False,
                "hours": round((sets - rises) / 3600.0, 2), "plan": plan.raw()}

    # Counted from when the target's turn can begin: an existing start, or the
    # moment it rises above the floor tonight.
    start = entry.get("startAt") or rises
    if not start:
        raise HTTPException(
            status_code=400,
            detail=f"{target['name']} is not observable tonight, so there is "
                   "no window to give hours out of")
    end = float(start) + body.hours * 3600.0
    entry = _guard(plan.set_times, entry_id, start_at=float(start), end_at=end)
    return {"entry": entry, "clamped": False, "hours": body.hours,
            "plan": plan.raw()}


@app.post("/api/collab/sync")
def collab_sync() -> dict[str, Any]:
    """Bring the adopted target back into step with the server."""
    task = (collab_client.status() or {}).get("task")
    if not task:
        raise HTTPException(status_code=400, detail="there is no task in hand")
    synced = _resync_task(task)
    if synced is None:
        return {**_collab_state(), "synced": None}
    return {**_collab_state(), "synced": synced, "plan": plan.raw()}


# -- the coordinator's half -------------------------------------------------
#
# Every one of these is a call this program makes to the collaboration server on
# the operator's behalf, rather than one the page makes itself. That is the
# whole point: the coordinator token can rewrite every project on the server,
# and a credential in a page is a credential in devtools.

def _admin(call, *args, **kwargs):
    """Run a coordinator call and turn its failure into an honest status.

    401 from the server means the token is wrong, not that this program broke,
    and a server that is simply not running is the most ordinary outcome of
    all — neither deserves a 500 and a stack trace.
    """
    try:
        return call(*args, **kwargs)
    except collabadmin.CollabError as exc:
        raise HTTPException(status_code=exc.status or 502,
                            detail=str(exc)) from exc


@app.get("/api/collab/server")
def collab_server() -> dict[str, Any]:
    """The coordinator's view: is it up, who is enrolled, what is running.

    Never fails. The tab has to render when the server is down, because that is
    exactly when somebody needs to be told what is wrong.
    """
    return collab_admin.status()


@app.post("/api/collab/agents")
def collab_add_agent(body: CollabAgentRequest) -> dict[str, Any]:
    """Enrol a telescope and mint its token.

    The token comes back once and is stored nowhere here — it gets pasted into
    that observatory's settings, which is the only machine with any business
    holding it.
    """
    return _admin(collab_admin.add_agent, body.name.strip(), body.owner.strip())


@app.get("/api/collab/projects")
def collab_projects() -> dict[str, Any]:
    return {"projects": _admin(collab_admin.projects)}


@app.get("/api/collab/projects/{project_id}")
def collab_project(project_id: str) -> dict[str, Any]:
    return _admin(collab_admin.project, project_id)


@app.post("/api/collab/projects")
def collab_add_project(body: CollabProjectRequest) -> dict[str, Any]:
    source = body.region
    kind = body.kind or "mosaic"
    if body.targetId:
        source = _region_of_target(body.targetId)
        if body.kind is None:
            kind = ("single" if _guard(targets.get, body.targetId).get("type")
                    == "single" else "mosaic")
    if not source:
        raise HTTPException(
            status_code=400,
            detail="choose one of your targets, or draw a rectangle in the "
                   "Planner")
    try:
        region = collab.Region.read(source).payload()
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="a project needs a centre and a size: "
                   f"right ascension, declination, width and height ({exc})") from exc

    made = _admin(collab_admin.add_project, body.name.strip(), region,
                  body.requirements, body.goals,
                  str(config.get("collab", "coordinator", "") or ""),
                  body.notes, kind)
    # So it appears in the list straight away. A collaboration you started that
    # you cannot see is one you cannot join, and the list is fetched as an
    # *agent* while this was created as a coordinator — two different calls, and
    # nothing would have refreshed the first.
    with contextlib.suppress(Exception):
        collab_client.poll()
    return {**made, "state": _collab_state()}


def _region_of_target(target_id: str) -> dict[str, Any]:
    """A saved target as a rectangle of sky.

    A mosaic already knows how much sky it covers. A single framing covers one
    field, and the field is this rig's — which is the honest answer to "how big
    is this collaboration": as big as the thing you framed. Somebody joining
    with a narrower camera will mosaic it, somebody with a wider one will cover
    it in a single frame, and neither needs the other's geometry.
    """
    target = _guard(targets.get, target_id)
    extent = target.get("extent") or {}
    width = float(extent.get("width") or 0.0)
    height = float(extent.get("height") or 0.0)
    if width <= 0 or height <= 0:
        width = float(target.get("panelWidth") or 0.0)
        height = float(target.get("panelHeight") or 0.0)
    if width <= 0 or height <= 0:
        profile = collab.RigProfile.read(collab_client.profile())
        field = profile.field()
        if field is None:
            raise HTTPException(
                status_code=400,
                detail=f"{target['name']} has no size recorded, and this "
                       "telescope has not been told what it can see - set the "
                       "focal length, sensor size and pixel size in Site & "
                       "Optics")
        width, height = field
    return {"ra": float(target["ra"]) * 15.0, "dec": float(target["dec"]),
            "width": width, "height": height,
            "rotation": float(target.get("rotation") or 0.0)}


class CollabProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    region: dict[str, Any] | None = None
    targetId: str | None = Field(default=None, max_length=64)
    kind: str | None = Field(default=None, pattern="^(single|mosaic)$")
    requirements: dict[str, Any] | None = None
    goals: dict[str, float] | None = None
    notes: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, pattern="^(open|closed)$")


@app.post("/api/collab/projects/{project_id}/update")
def collab_update_project(project_id: str,
                          body: CollabProjectUpdateRequest) -> dict[str, Any]:
    """Change a collaboration you started.

    Whether it is yours is the server's call, made on the coordinator token;
    this program only holds that token for whoever runs it. The region can be
    re-drawn, or taken again from one of your targets, the same two ways it was
    first chosen.
    """
    changes = body.model_dump(exclude_none=True)
    changes.pop("targetId", None)
    if body.targetId:
        changes["region"] = _region_of_target(body.targetId)
        if body.kind is None:
            changes["kind"] = ("single" if _guard(targets.get, body.targetId)
                               .get("type") == "single" else "mosaic")
    if not changes:
        raise HTTPException(status_code=400, detail="nothing to change")
    made = _admin(collab_admin.update_project, project_id, changes)
    with contextlib.suppress(Exception):
        collab_client.poll()
    return {**made, "state": _collab_state()}


@app.post("/api/collab/projects/{project_id}/plan")
def collab_plan(project_id: str, body: CollabPlanRequest) -> dict[str, Any]:
    """Propose how to cover the project with the telescopes chosen.

    A proposal, not an assignment: it comes back to the window as a table to be
    looked at and changed before anything is handed out. Nothing has been told
    to any telescope at the point this returns.
    """
    answer = _admin(collab_admin.project, project_id)
    project = answer.get("project") or {}
    try:
        region = collab.Region.read((project.get("payload") or {})["region"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400,
                            detail="that project has no region") from exc

    enrolled = {agent["id"]: agent for agent in _admin(collab_admin.agents)}
    chosen = [agent_id for agent_id in body.agents if agent_id in enrolled]
    if not chosen:
        raise HTTPException(status_code=400,
                            detail="choose at least one telescope")

    # The tile has to fit the *smallest* field, or the rig with the narrowest
    # view is handed sky it cannot photograph in one frame.
    #
    # A telescope that has not said what it can see is dropped from the deal
    # rather than merely mentioned. It used to be listed as left out *and* dealt
    # chunks, which is incoherent: a chunk is cut to fit a field, and there is no
    # field to cut it to.
    fields: list[tuple[float, float]] = []
    unknown: list[str] = []
    usable: list[str] = []
    for agent_id in chosen:
        field = (enrolled[agent_id].get("profile") or {}).get("field")
        if field and field[0] and field[1]:
            fields.append((float(field[0]), float(field[1])))
            usable.append(agent_id)
        else:
            unknown.append(enrolled[agent_id].get("name") or agent_id)
    chosen = usable
    if not fields:
        raise HTTPException(
            status_code=400,
            detail="none of those telescopes has said what it can see yet — "
                   "each one has to connect to the server once, with its focal "
                   "length, sensor and pixel size set")

    width = min(field[0] for field in fields)
    height = min(field[1] for field in fields)
    tiles = collab.chunk(region, width, height, body.overlap)

    # Dealt by what each rig actually gives a collaboration, not evenly. An
    # equal share regardless hands the same work to somebody with two hours a
    # weeknight as to somebody with a remote rig running every clear hour, and
    # the project then waits on the person who never had the time.
    hours = {}
    for agent_id in chosen:
        profile = enrolled[agent_id].get("profile") or {}
        hours[agent_id] = float(profile.get("hoursPerNight") or 0.0)
    shares = collab.share_out(tiles, chosen, hours)

    return {
        "field": {"width": width, "height": height},
        "tiles": len(tiles),
        "unknown": unknown,
        # Whether anybody has said what they can give. Without it the deal is
        # equal, and the window has to say so rather than implying a fairness
        # nobody asked for.
        "weighted": any(value > 0 for value in hours.values()),
        "shares": [
            {"agent": agent_id,
             "name": enrolled[agent_id].get("name") or agent_id,
             "hoursPerNight": hours[agent_id] or None,
             "windowFrom": (enrolled[agent_id].get("profile") or {}).get("windowFrom") or "",
             "windowTo": (enrolled[agent_id].get("profile") or {}).get("windowTo") or "",
             "regions": [tile.payload() for tile in regions]}
            for agent_id, regions in shares.items()],
    }


@app.post("/api/collab/projects/{project_id}/tasks")
def collab_delegate(project_id: str, body: CollabDelegateRequest) -> dict[str, Any]:
    """Hand one telescope one chunk of sky."""
    try:
        region = collab.Region.read(body.region).payload()
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400,
                            detail=f"that chunk is not a rectangle of sky ({exc})") from exc
    filters = []
    for entry in body.filters:
        try:
            filters.append(collab.FilterTask.read(entry).payload())
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="each filter needs a name, a sub length and a number of "
                       f"hours ({exc})") from exc
    if not filters:
        raise HTTPException(status_code=400,
                            detail="a task with no filters asks for nothing")
    return _admin(collab_admin.add_task, project_id, body.agent, region,
                  filters, body.note)


@app.post("/api/collab/contributions/{row_id}/verdict")
def collab_verdict(row_id: str, body: CollabVerdictRequest) -> dict[str, Any]:
    """Overrule the automatic judgement on one night's data.

    Seeing varies, and a night the numbers reject may be the only data anybody
    has on that patch of sky.
    """
    return _admin(collab_admin.set_verdict, row_id, body.accepted,
                  body.reason.strip())


# ---------------------------------------------------------------------------
# Weather, the roof, and switched power
# ---------------------------------------------------------------------------

@app.post("/api/settings/safety")
def set_safety_settings(body: SafetySettingsRequest) -> dict[str, Any]:
    values = body.model_dump(exclude_unset=True)
    saved = config.update("safety", values)
    safety_watcher.start()
    return {"safety": saved, "status": safety_watcher.status()}


@app.post("/api/settings/notify")
def set_notify_settings(body: NotifySettingsRequest) -> dict[str, Any]:
    values = body.model_dump(exclude_unset=True)
    saved = config.update("notify", values)
    # The password is never sent back: a settings form that round-trips a
    # password is a password in the browser's memory for no reason.
    return {"notify": {**saved, "smtpPassword": "***" if saved.get("smtpPassword")
                       else ""},
            "status": notifier.status()}


@app.post("/api/notify/test")
def notify_test() -> dict[str, Any]:
    """Send one now, so the settings can be proved before a night depends on them."""
    sent = notifier.send("test", "Test message",
                         "If you are reading this, Starfront can reach you.")
    if not sent:
        raise HTTPException(
            status_code=400,
            detail="nowhere to send it — set a webhook URL or an SMTP host")
    return {"sent": True, "status": notifier.status()}


@app.post("/api/dome/shutter")
def dome_shutter(body: DomeShutterRequest) -> dict[str, Any]:
    """Open or close the roof. The one control here that can hit a telescope."""
    dome = _guard(manager.require, "dome")
    if not getattr(dome, "can_shutter", False):
        raise HTTPException(status_code=400, detail="this dome has no shutter")
    if body.open and not safety_watcher.safe:
        raise HTTPException(
            status_code=409,
            detail="the safety monitor says it is not safe to open")
    _guard(dome.open_shutter if body.open else dome.close_shutter)
    rigs.log(f"Roof: {'opening' if body.open else 'closing'}", "warn")
    return {"ok": True}


@app.post("/api/dome/park")
def dome_park() -> dict[str, Any]:
    dome = _guard(manager.require, "dome")
    _guard(dome.park)
    return {"ok": True}


@app.post("/api/dome/slave")
def dome_slave(body: TrackingRequest) -> dict[str, Any]:
    dome = _guard(manager.require, "dome")
    _guard(dome.set_slaved, body.on)
    rigs.log(f"Dome slaving {'on' if body.on else 'off'}")
    return {"ok": True}


@app.get("/api/switch")
def switch_channels() -> dict[str, Any]:
    device = manager.get("switch")
    if device is None or not device.connected:
        return {"connected": False, "channels": []}
    return {"connected": True, "name": device.name,
            "channels": _guard(lambda: device.channels)}


@app.post("/api/switch")
def switch_set(body: SwitchRequest) -> dict[str, Any]:
    """Turn an outlet on or off, or set a dew heater's level.

    The same call for both: an ASCOM Switch channel is either a relay or a
    value in a range, and the driver says which.
    """
    device = _guard(manager.require, "switch")
    channel = next((c for c in device.channels if c["index"] == body.index), None)
    label = (channel or {}).get("name", f"channel {body.index}")
    _guard(device.set_value, body.index, body.value)
    rigs.log(f"Switch: {label} -> "
             + ("on" if channel and channel.get("boolean") and body.value
                else "off" if channel and channel.get("boolean")
                else f"{body.value:g}"))
    return {"ok": True, "channels": device.channels}


# ---------------------------------------------------------------------------
# The camera watching the telescope
# ---------------------------------------------------------------------------

@app.get("/api/piercam")
def piercam_status() -> dict[str, Any]:
    return pier.status()


@app.get("/api/piercam/frame.png")
def piercam_frame() -> Response:
    """The newest picture of the telescope.

    Polled by the UI rather than streamed. A pier cam runs at about a frame a
    second, and a poll of a plain PNG is one code path that works through every
    proxy, reconnects for free after a dropped link, and can be pointed at by an
    `<img>` — where a multipart stream is none of those things for a feed this
    slow.
    """
    payload, token = pier.frame()
    if not payload:
        raise HTTPException(status_code=404, detail="no pier camera frame yet")
    return Response(content=payload, media_type="image/png", headers={
        # Always the newest one: the caller cache-busts with the token from
        # `/api/piercam`, and anything cached beyond that is a stale telescope.
        "Cache-Control": "no-store",
        "X-Piercam-Token": token,
    })


@app.post("/api/settings/piercam")
def set_piercam_settings(body: PierCamSettingsRequest) -> dict[str, Any]:
    values = body.model_dump(exclude_unset=True)
    saved = config.update("piercam", values)
    # A change to the exposure range or to auto/manual should take effect on the
    # next frame, not whenever the loop next happens to restart.
    pier.start()
    return {"piercam": saved, "status": pier.status()}


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def _image_source(image_id: str):
    """The capture store holding a frame, whichever telescope took it.

    The viewer only ever has an id, and an id is unique across the session, so
    it should not have to know which camera the frame came off.
    """
    for rig in rigs.all:
        if rig.capture.has(image_id):
            return rig.capture
    raise HTTPException(status_code=404, detail=f"unknown image {image_id!r}")


@app.get("/api/images")
def list_images(limit: int = Query(default=200, ge=1, le=1000),
                rig: str | None = None) -> dict[str, Any]:
    """The session's frames, newest first.

    Without a telescope named this is every telescope's frames interleaved in
    the order they were actually taken, which is how a night reads.
    """
    if rig:
        return {"images": _rig(rig).capture.listing(limit)}
    merged: list[dict[str, Any]] = []
    for telescope in rigs.all:
        for row in telescope.capture.listing(limit):
            merged.append({**row, "rig": telescope.id})
    merged.sort(key=lambda row: row["timestamp"], reverse=True)
    return {"images": merged[:limit]}


@app.get("/api/images/{image_id}/stats")
def image_stats(image_id: str) -> dict[str, Any]:
    source = _image_source(image_id)
    record = _guard(source.record, image_id)
    frame = _guard(source.frame, image_id)
    return {"image": record.summary(), "stats": record.stats,
            "autoStretch": render.auto_stretch(frame)}


@app.get("/api/images/{image_id}/render.png")
def image_render(
    image_id: str,
    auto: bool = True,
    black: float = Query(default=0.0, ge=0.0, le=1.0),
    white: float = Query(default=1.0, ge=0.0, le=1.0),
    midtone: float = Query(default=0.5, gt=0.0, lt=1.0),
    invert: bool = False,
    maxDim: int = Query(default=1600, ge=64, le=8192),
    region: str | None = None,
) -> Response:
    frame = _guard(_image_source(image_id).frame, image_id)

    stretch = render.auto_stretch(frame) if auto else {"black": black, "white": white,
                                                       "midtone": midtone}
    stretch["invert"] = invert

    crop = None
    if region:
        try:
            x, y, w, h = (int(float(part)) for part in region.split(","))
            crop = (x, y, w, h)
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail="region must be 'x,y,width,height'") from exc

    array, info = render.render_png_array(frame, stretch, maxDim, crop)
    body = png.encode(array)
    return Response(content=body, media_type="image/png", headers={
        "Cache-Control": "public, max-age=3600",
        "X-Render-Factor": str(info["factor"]),
        "X-Render-Black": f"{info['stretch']['black']:.6f}",
        "X-Render-Midtone": f"{info['stretch']['midtone']:.6f}",
    })


@app.get("/api/images/{image_id}/pixel")
def image_pixel(image_id: str, x: int = Query(ge=0), y: int = Query(ge=0)) -> dict[str, Any]:
    """Raw ADU under the cursor, plus the mean of a small box around it."""
    frame = _guard(_image_source(image_id).frame, image_id)
    height, width = frame.shape
    if not (0 <= x < width and 0 <= y < height):
        raise HTTPException(status_code=400, detail="pixel is outside the frame")
    box = frame[max(0, y - 2):y + 3, max(0, x - 2):x + 3]
    return {"x": x, "y": y, "value": int(frame[y, x]),
            "boxMean": round(float(box.mean()), 1), "boxMax": int(box.max())}


@app.get("/api/images/{image_id}/download")
def image_download(image_id: str) -> FileResponse:
    record = _guard(_image_source(image_id).record, image_id)
    if not record.path:
        raise HTTPException(status_code=400,
                            detail="this frame was captured with saving switched off")
    return FileResponse(record.path, media_type="application/fits", filename=record.filename)


# ---------------------------------------------------------------------------
# Site, optics and solver settings
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings(rig: str | None = None) -> dict[str, Any]:
    """Settings as one telescope sees them.

    The per-telescope sections come back merged — the shared value with that
    telescope's own overrides on top — so a form can be filled from this
    without knowing which is which.  `overrides` says what the telescope has
    actually claimed for itself, which is what the form needs to show a "same
    as the master" state.
    """
    target = _rig(rig)
    everything = target.config.all()
    # The mail password never leaves the machine. The form shows a mask and
    # sends nothing back unless somebody actually types a new one, so it is
    # never in the browser's memory and never in a log of the traffic.
    notify_settings = dict(everything.get("notify") or {})
    if notify_settings.get("smtpPassword"):
        notify_settings["smtpPassword"] = "***"
    # The agent token is a credential too, and leaves no more than the password.
    collab_settings = dict(everything.get("collab") or {})
    if collab_settings.get("token"):
        collab_settings["token"] = "***"
    # And the coordinator token more so: the agent token can fetch a task, this
    # one can rewrite every project on the server.
    if collab_settings.get("adminToken"):
        collab_settings["adminToken"] = "***"
    if collab_settings.get("userToken"):
        collab_settings["userToken"] = "***"
    return {
        **everything,
        "notify": notify_settings,
        "collab": collab_settings,
        "site": {**config.section("site"),
                 "effective": effective_site(config, target.manager)},
        "rig": target.id,
        "rigSections": list(RIG_SECTIONS),
        "overrides": target.config.overrides,
    }


class MetaRequest(BaseModel):
    """The first-light walk-through's bookmark."""

    firstLightDone: bool | None = None
    firstLightStep: int | None = Field(default=None, ge=0, le=20)


@app.post("/api/settings/meta")
def set_meta(body: MetaRequest) -> dict[str, Any]:
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")
    return {"meta": config.update("meta", values)}


@app.post("/api/settings/site")
def set_site(body: SiteRequest) -> dict[str, Any]:
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")
    saved = config.update("site", values)
    manager.log("Observing site updated")
    return {"site": saved, "effective": effective_site(config, manager)}


@app.post("/api/settings/optics")
def set_optics(body: OpticsRequest, rig: str | None = None) -> dict[str, Any]:
    """The optics of one telescope: focal length, sensor, camera angle.

    Per telescope, obviously — a 530 mm refractor and a 400 mm astrograph on the
    same mount do not share a field of view.
    """
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")
    return {"optics": _guard(_rig(rig).config.update, "optics", values)}


@app.post("/api/settings/camera")
def set_camera_defaults(body: CameraSettingsStoreRequest,
                        rig: str | None = None) -> dict[str, Any]:
    target = _rig(rig)
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")

    # One spelling per filter, at the door: "Ha" typed here is "H" stored.
    if "fixedFilter" in values:
        values["fixedFilter"] = filters.canonical(values["fixedFilter"])
    if "filterBandpass" in values:
        values["filterBandpass"] = filters.canonical_keys(values["filterBandpass"])

    renamed = ""
    if "filterNames" in values:
        values["filterNames"] = filters.canonical_list(values["filterNames"])
        # Offered to the driver first, for the few that will take a name. ASCOM's
        # `Names` is read-only, so this normally does nothing at all — which is
        # why it is no longer what the rest of the program depends on.
        wheel = target.manager.get("filterwheel")
        if wheel is not None and wheel.connected and values["filterNames"]:
            try:
                wheel.set_names(list(values["filterNames"]))
                renamed = "and written to the wheel"
            except Exception:                     # noqa: BLE001 - the usual case
                renamed = "and used in place of the wheel's own slot numbers"

    before = list(target.config.get("camera", "filterNames", []) or [])
    saved = _guard(target.config.update, "camera", values)
    if "filterNames" in values:
        # The authority, whatever the driver did with them: applied to the live
        # wheel here so the buttons, the sequencer's filter matching and the
        # FITS headers all change together, without a reconnect.
        target.manager.apply_filter_names()
        target.manager.log(f"Filters for {target.name}: "
                           f"{', '.join(values['filterNames']) or 'none'} {renamed}")
        if before and not values["filterNames"]:
            # A list that had names and now has none is worth a warning with
            # the names in it. Twice in one log the names were saved and then,
            # milliseconds later, saved again as nothing - by what, the log
            # could not say, because "none" is all it recorded. Next time it
            # will at least say what was lost, and the traffic log which
            # client asked for it.
            target.manager.log(
                f"Filters for {target.name} were CLEARED - they had been "
                f"{', '.join(before)}. If nobody pressed Clear, another window "
                "or profile load may have saved an empty list.", "warn")
    # Gain and offset are worth applying at once when the camera is there,
    # rather than waiting for the next sequence to start.
    camera = target.manager.get("camera")
    if camera is not None and camera.connected:
        with contextlib.suppress(Exception):
            camera.set_settings(gain=values.get("gain"), offset=values.get("offset"))
    return {"camera": saved}


@app.post("/api/settings/capture")
def set_capture_settings(body: CaptureSettingsRequest) -> dict[str, Any]:
    if body.rootDirectory is None:
        raise HTTPException(status_code=400, detail="nothing to change")
    text = body.rootDirectory.strip()
    if text:
        folder = Path(text).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=400,
                                detail=f"cannot use {folder}: {exc}") from exc
        if not os.access(folder, os.W_OK):
            raise HTTPException(status_code=400, detail=f"{folder} is not writable")
    saved = config.update("capture", {"rootDirectory": text})
    manager.log(f"Frames will be saved under {capture.root_dir}")
    return {"capture": saved, "sessionDir": str(capture.session_dir)}


@app.post("/api/settings/schedule")
def set_schedule_settings(body: ScheduleSettingsRequest) -> dict[str, Any]:
    """How the night is planned, and how low the telescope will go.

    The minimum elevation is the one that matters: it decides which targets are
    worth a window at all, when the sequencer gives up on one that is setting,
    and how much of the sky an all-sky survey can ever reach from here.
    """
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")
    saved = config.update("schedule", values)
    if "minAltitude" in values:
        manager.log(f"Minimum elevation for captures: "
                    f"{values['minAltitude']:g}°")
    return {"schedule": saved}


@app.post("/api/settings/sequencer")
def set_sequencer_settings(body: SequencerSettingsRequest,
                           rig: str | None = None) -> dict[str, Any]:
    """How to focus and watch one telescope.

    Focus step size, sweep width and the refocus triggers are properties of an
    optical train, so a slave keeps its own.  The mount-shaped settings in the
    same section — the meridian flip, the settle time after a slew — are read
    from the master whatever a slave has stored, because there is only one
    mount to flip.
    """
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")
    if "filterOffsets" in values:
        values["filterOffsets"] = filters.canonical_keys(values["filterOffsets"])
    if "autofocusFilter" in values:
        values["autofocusFilter"] = filters.canonical(values["autofocusFilter"])
    return {"sequencer": _guard(_rig(rig).config.update, "sequencer", values)}


@app.post("/api/settings/guiding")
def set_guiding_settings(body: GuidingSettingsRequest) -> dict[str, Any]:
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")
    return {"guiding": config.update("guiding", values)}


@app.get("/api/overheads")
def get_overheads() -> dict[str, Any]:
    """What this rig has measured itself to cost between exposures."""
    focus = config.section("sequencer")
    return {
        "overheads": overhead_store.summary(),
        "useMeasured": bool(config.get("schedule", "useMeasured", True)),
        "planning": _overheads(),
        # What a sweep of the shape currently configured would cost, which is
        # the number the plan is actually charged.
        "focusRunSeconds": round(overhead_store.focus_seconds(
            int(focus.get("focusPoints") or 9),
            float(focus.get("focusExposure") or 6.0),
            int(focus.get("focusFramesPerPoint") or 1)), 1),
        "busy": calibrate_overheads_task["running"],
        "detail": calibrate_overheads_task["detail"],
    }


#: The calibration runs on a thread of its own — it takes a whole autofocus
#: sweep — so the request returns at once and the UI watches this.
calibrate_overheads_task: dict[str, Any] = {"running": False, "detail": "",
                                            "error": None, "done": []}


@app.post("/api/overheads/calibrate")
def calibrate_overheads(body: CalibrateOverheadsRequest,
                        rig: str | None = None) -> dict[str, Any]:
    """Time this rig on purpose: downloads, a filter change, one focus sweep."""
    if calibrate_overheads_task["running"]:
        raise HTTPException(status_code=409, detail="already measuring")
    target = _rig(rig)
    if sequencer.owns_the_cameras:
        raise HTTPException(
            status_code=409,
            detail="a sequence is using the cameras; stop it first")
    target.manager.require("camera")

    def note(message: str, level: str = "info") -> None:
        calibrate_overheads_task["detail"] = message
        manager.log(f"Overhead calibration: {message}", level)

    def work() -> None:
        try:
            result = overhead_store.calibrate(target, frames=body.frames,
                                              focus=body.focus, say=note)
            calibrate_overheads_task["done"] = result["measured"]
            note("done — " + ", ".join(result["measured"]), "success")
        except Exception as exc:                # noqa: BLE001 - shown to the operator
            calibrate_overheads_task["error"] = str(exc)
            note(f"failed — {exc}", "error")
        finally:
            calibrate_overheads_task["running"] = False

    calibrate_overheads_task.update({"running": True, "error": None, "done": [],
                                     "detail": "starting"})
    threading.Thread(target=work, daemon=True, name="overhead-calibration").start()
    return {"started": True}


@app.delete("/api/overheads")
def clear_overheads() -> dict[str, Any]:
    """Forget what was measured and go back to the typed figures."""
    overhead_store.clear()
    manager.log("Measured overheads cleared")
    return {"overheads": overhead_store.summary()}


@app.post("/api/settings/autoplan")
def set_autoplan_settings(body: AutoplanSettingsRequest) -> dict[str, Any]:
    """What Auto-arrange puts in the slots it works out."""
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")
    return {"autoplan": config.update("autoplan", values)}


@app.post("/api/settings/recovery")
def set_recovery_settings(body: RecoverySettingsRequest) -> dict[str, Any]:
    """What a sequence does when it loses the guide star or the target.

    Shared rather than per telescope: everything here is about the one mount
    and the one guider, and a lost star stops every telescope on the mount.
    """
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")
    return {"recovery": config.update("recovery", values)}


@app.post("/api/settings/solver")
def set_solver_settings(body: SolverSettingsRequest) -> dict[str, Any]:
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")
    saved = config.update("solver", values)
    return {"solver": saved, "status": solver.status()}


# ---------------------------------------------------------------------------
# Plate solving
# ---------------------------------------------------------------------------

@app.get("/api/solve")
def solver_status(rig: str | None = None) -> dict[str, Any]:
    return _rig(rig).solver.status()


@app.post("/api/solve")
def start_solve(body: SolveRequest, rig: str | None = None) -> dict[str, Any]:
    """Solve a frame from one telescope.

    A solve uses that telescope's own focal length as its hint, so a slave can
    measure its own field and rotation.  Syncing or centring still moves the one
    mount, which is why those are refused on a slave — it would be pointing the
    master by proxy, from a frame the master did not take.
    """
    target = _rig(rig)
    if body.mode in ("sync", "center", "goto") and not target.is_master:
        raise HTTPException(
            status_code=400,
            detail=f"{target.name} does not carry the mount; solve on "
                   f"{rigs.master.name} to move it")
    _guard(target.solver.start, body.mode, body.imageId, body.ra, body.dec,
           body.rotation)
    return {"started": True, "mode": body.mode}


@app.post("/api/solve/abort")
def abort_solve(rig: str | None = None) -> dict[str, Any]:
    _rig(rig).solver.abort()
    return {"aborted": True}


# ---------------------------------------------------------------------------
# Sky catalog
# ---------------------------------------------------------------------------

@app.get("/api/catalog")
def get_catalog() -> dict[str, Any]:
    return {"objects": catalog.listing()}


@app.get("/api/catalog/search")
def search_catalog(q: str = Query(min_length=1, max_length=80),
                   limit: int = Query(default=30, ge=1, le=200)) -> dict[str, Any]:
    """Bundled objects first; CDS Sesame resolves anything else, when online."""
    return catalog.lookup(q, limit)


# ---------------------------------------------------------------------------
# Framing: what the camera covers, and where mosaic panels land
# ---------------------------------------------------------------------------

def _camera_field(target=None) -> dict[str, Any]:
    """One telescope's angular field, preferring a plate solve over arithmetic."""
    target = target or rigs.master
    solved = target.solver.status().get("result")
    if solved and solved.get("fovWidth") and solved.get("fovHeight"):
        return {"width": solved["fovWidth"], "height": solved["fovHeight"],
                "scale": solved["scale"], "rotation": solved["rotation"],
                "source": "solve"}

    optics = target.config.section("optics")
    focal_length = optics.get("focalLength")
    if not focal_length:
        return {"width": None, "height": None, "source": "no focal length"}

    # A connected camera knows its own sensor; otherwise use what was typed in,
    # so a framing can be planned with the rig switched off.
    camera = target.manager.get("camera")
    if camera is not None and camera.connected and camera.sensor_width:
        sensor = (camera.sensor_width, camera.sensor_height, camera.pixel_size_um)
        source = "camera"
    else:
        sensor = (optics.get("sensorWidth"), optics.get("sensorHeight"),
                  optics.get("pixelSize"))
        source = "settings"
    if not all(sensor):
        return {"width": None, "height": None, "source": "no sensor size"}

    try:
        field = framing.field_of_view(int(sensor[0]), int(sensor[1]),
                                      float(sensor[2]), float(focal_length))
    except ValueError as exc:
        return {"width": None, "height": None, "source": str(exc)}
    return {**field, "rotation": optics.get("rotation") or 0.0, "source": source}


@app.get("/api/framing")
def get_framing(rig: str | None = None) -> dict[str, Any]:
    """The field to frame with, plus every telescope's field for comparison.

    A mosaic is laid out for one telescope's field — normally the master's,
    since that is what the panel geometry is saved against — but seeing what
    the other scopes cover on the same panels is exactly what you want when
    deciding whether a two-panel mosaic is worth it.
    """
    target = _rig(rig)
    return {
        "field": _camera_field(target),
        "rig": target.id,
        "fields": [{"rig": telescope.id, "name": telescope.name,
                    "role": telescope.role, **_camera_field(telescope)}
                   for telescope in rigs.all],
        "surveys": list(survey.SURVEYS),
    }


@app.post("/api/framing/mosaic")
def preview_mosaic(body: MosaicPreviewRequest) -> dict[str, Any]:
    """Panel centres for a mosaic.

    The planner draws its own preview while you drag, but everything that gets
    saved comes from here so there is one authority on where a panel actually is.
    """
    panels = _guard(framing.mosaic_panels, body.ra * 15.0, body.dec,
                    body.panelWidth, body.panelHeight, body.rows, body.columns,
                    body.overlap, body.rotation, body.align)
    extent = framing.mosaic_extent(body.panelWidth, body.panelHeight,
                                   body.rows, body.columns, body.overlap)
    seams = framing.mosaic_seams(panels, body.panelWidth, body.panelHeight, body.overlap)
    return {"panels": panels, "extent": extent, "seams": seams}


#: The last FITS opened as a framing reference, rendered once and kept so the
#: planner can draw it without re-reading a 120-megabyte file on every redraw.
#:
#: Solving runs on a thread and is polled, because the last resort is
#: astrometry.net and that can take minutes — a blocking request would time out
#: in the browser long before the answer came back.
framing_reference: dict[str, Any] = {
    "png": b"", "token": "", "info": None,
    "busy": False, "detail": "", "error": None,
}


def _read_reference(path: Path, max_dim: int) -> None:
    """Render and solve one frame.  Runs on its own thread."""
    def note(message: str) -> None:
        framing_reference["detail"] = message
        manager.log(f"Framing reference: {message}")

    try:
        image, header = fits.read(path)
        note(f"{path.name}: {image.shape[1]}×{image.shape[0]} pixels")

        # Rendered with the same auto-stretch the viewer uses, so a linear sub
        # that looks black on disk comes up correctly exposed here too. Done
        # before the solve, which is the slow half.
        stretch = render.auto_stretch(image)
        pixels, meta = render.render_png_array(image, stretch, max_dim=max_dim)
        payload = png.encode(pixels)

        solved = rigs.master.solver.solve_file(path, say=note)

        token = uuid.uuid4().hex[:12]
        framing_reference.update({
            "png": payload,
            "token": token,
            "info": {
                "filename": path.name,
                "path": str(path),
                "ra": solved.ra,
                "dec": solved.dec,
                "rotation": solved.rotation,
                "scale": solved.scale,
                "fovWidth": solved.fov_width,
                "fovHeight": solved.fov_height,
                "flipped": solved.flipped,
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
                "exposure": float(header.get("EXPTIME", 0.0) or 0.0),
                "filter": str(header.get("FILTER", "") or ""),
                "object": str(header.get("OBJECT", "") or ""),
                "telescope": str(header.get("TELESCOP", "") or ""),
                "date": str(header.get("DATE-OBS", "") or ""),
                "seconds": solved.seconds,
                "method": solved.method,
                "detail": solved.detail,
                "rendered": {"width": meta.get("width"), "height": meta.get("height")},
                "token": token,
            },
        })
        manager.log(
            f"Framing reference: {path.name} at {solved.ra:.4f}h "
            f"{solved.dec:+.3f}°, {solved.rotation:.1f}°, "
            f"{solved.fov_width:.3f}×{solved.fov_height:.3f}° "
            f"({solved.method})", "success")
        # A solve is a solve: if this frame came off this camera, it has just
        # measured the camera angle, and the settings should say so. Declined
        # for a frame off another rig — see `adopt_angle`.
        rigs.master.solver.adopt_angle(solved, int(image.shape[1]), int(image.shape[0]))
        framing_reference["detail"] = f"solved by {solved.method}"
    except Exception as exc:                    # noqa: BLE001 - shown to the operator
        framing_reference["error"] = str(exc)
        framing_reference["detail"] = str(exc)
        manager.log(f"Framing reference failed: {exc}", "error")
    finally:
        framing_reference["busy"] = False


@app.post("/api/framing/reference")
def open_framing_reference(body: FramingReferenceRequest) -> dict[str, Any]:
    """Open a FITS frame and use where it points as the framing background.

    "Put the new mosaic where last spring's one was" is a question about a file
    on disk rather than about anything the camera has taken tonight — and a
    survey cutout, however pretty, is not what your telescope sees. A real frame
    shows the field at your focal length, through your filters, with your
    gradients and your star shapes, which is what a framing decision is
    actually made against.

    The frame is plate solved, so the framing rectangle lands on the real sky
    rather than on the pixels. Returns at once; poll `GET` for the answer.
    """
    if framing_reference["busy"]:
        raise HTTPException(status_code=409, detail="already opening a frame")
    path = Path(body.path).expanduser()
    if not path.is_file():
        raise HTTPException(status_code=400, detail=f"{path} is not a file")

    framing_reference.update({"busy": True, "error": None, "info": None,
                              "detail": "reading the frame"})
    threading.Thread(target=_read_reference, args=(path, body.maxDim),
                     daemon=True, name="framing-reference").start()
    return {"started": True}


@app.get("/api/framing/reference")
def get_framing_reference() -> dict[str, Any]:
    """How the open is going, and its answer once there is one."""
    return {
        "busy": bool(framing_reference["busy"]),
        "detail": framing_reference["detail"],
        "error": framing_reference["error"],
        "reference": framing_reference["info"],
    }


@app.get("/api/framing/reference/image.png")
def framing_reference_image(token: str = Query(default="")) -> Response:
    if not framing_reference["png"] or token != framing_reference["token"]:
        raise HTTPException(status_code=404, detail="no framing reference is open")
    return Response(content=framing_reference["png"], media_type="image/png",
                    headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/survey/image")
def survey_image(
    ra: float = Query(ge=0, lt=24, description="right ascension in hours"),
    dec: float = Query(ge=-90, le=90),
    fov: float = Query(gt=0, le=90, description="image width in degrees"),
    width: int = Query(default=1000, ge=64, le=4000),
    height: int = Query(default=700, ge=64, le=4000),
    rotation: float = Query(default=0.0, ge=-360, le=360),
    hips: str = Query(default="CDS/P/DSS2/color"),
) -> Response:
    payload, cached = _guard(surveys.cutout, ra * 15.0, dec, fov, width, height,
                             rotation, hips)
    return Response(content=payload, media_type="image/jpeg", headers={
        "Cache-Control": "private, max-age=86400",
        "X-Survey-Cache": "hit" if cached else "miss",
    })


# ---------------------------------------------------------------------------
# Solar system survey: twilight sweeps for comets and near-Earth asteroids
# ---------------------------------------------------------------------------

def _usable_field(rig) -> dict[str, Any] | None:
    """The field a telescope can actually tile with.

    Prefers what the optics correct over what the sensor covers: a RASA 8's
    image circle does not reach an APS-C chip's corners, and tiling on the
    sensor field leaves seams covered only by unusable sky.
    """
    optics = rig.config.section("optics")
    width = optics.get("usableWidth")
    height = optics.get("usableHeight")
    if width and height:
        return {"width": float(width), "height": float(height), "source": "usable"}
    field = _camera_field(rig)
    if not field.get("width") or not field.get("height"):
        return None
    return {"width": float(field["width"]), "height": float(field["height"]),
            "source": field.get("source", "computed")}


def _survey_field() -> dict[str, Any]:
    """The field the sweep tiles with, across every telescope on the mount.

    They all ride the same mount, so they all shoot each panel together — which
    means the grid has to be laid out for the *smallest* of them.  Tile on the
    widest and the narrow instruments miss the seams entirely, which for a
    survey is a missed discovery rather than a cosmetic gap.
    """
    fields = []
    for rig in rigs.all:
        field = _usable_field(rig)
        if field is None:
            fields.append({"rig": rig.id, "name": rig.name, "role": rig.role,
                           "width": None, "height": None,
                           "detail": "no focal length or sensor size"})
            continue
        fields.append({"rig": rig.id, "name": rig.name, "role": rig.role, **field})

    known = [f for f in fields if f.get("width")]
    if not known:
        return {"width": None, "height": None, "telescopes": fields,
                "detail": "no telescope has a known field of view"}
    smallest = min(known, key=lambda f: min(f["width"], f["height"]))
    return {
        "width": smallest["width"],
        "height": smallest["height"],
        "limitedBy": smallest["name"],
        "telescopes": fields,
    }


def _survey_context(date_text: str | None = None) -> dict[str, Any]:
    latitude, longitude = _require_site()
    # A survey grid runs along the ecliptic, so each panel wants its own camera
    # angle. Whether the rig can oblige decides whether the panels really abut,
    # so the planner is told what this setup can actually do.
    rotator = None
    for rig in rigs.all:
        device = rig.manager.get("rotator")
        if device is not None and device.connected:
            rotator = {"rig": rig.id, "name": rig.name}
            break
    settings = config.section("survey")
    settings["hasRotator"] = rotator is not None
    settings["cameraAngle"] = rigs.master.config.get("optics", "rotation", 0.0)
    return {
        "site": {"latitude": latitude, "longitude": longitude},
        "night": _night_for(date_text),
        "settings": settings,
        "rotator": rotator,
        "field": _survey_field(),
    }


@app.get("/api/survey")
def get_survey(date: str | None = Query(default=None)) -> dict[str, Any]:
    """Settings, tonight's twilight windows and which field the sweep will use."""
    context = _survey_context(date)
    settings = context["settings"]
    windows = {}
    for which in ("evening", "morning"):
        window = solarsystem.twilight_window(
            context["site"]["latitude"], context["site"]["longitude"],
            context["night"], float(settings.get("sunHigh", -8.0)),
            float(settings.get("sunLow", -18.0)), which)
        if window is not None:
            windows[which] = window
    return {**context, "windows": windows,
            "coverage": {"cells": len(coverage.last_observed())}}


@app.get("/api/survey/viability")
def survey_viability(date: str | None = Query(default=None),
                     floor: float = Query(default=5.0, ge=0, le=60),
                     sunAltitude: float = Query(default=-12.0, ge=-30, le=0)
                     ) -> dict[str, Any]:
    """How close to the Sun tonight's twilight can reach, morning and evening.

    The one number that says whether a comet sweep is worth running: on the
    wrong dates the zone is under the horizon and no amount of scheduling helps.
    """
    latitude, longitude = _require_site()
    night = _night_for(date)
    return {
        "night": night,
        "floorAltitude": floor,
        "sunAltitude": sunAltitude,
        **{which: solarsystem.viability(latitude, longitude, night, which,
                                        floor, sunAltitude)
           for which in ("morning", "evening")},
    }


@app.get("/api/survey/season")
def survey_season(year: int | None = Query(default=None, ge=1900, le=2200),
                  floor: float = Query(default=5.0, ge=0, le=60),
                  step: int = Query(default=5, ge=1, le=30)) -> dict[str, Any]:
    """Mode B viability across a year — which mornings not to miss."""
    latitude, longitude = _require_site()
    when = year or _dt.date.today().year
    return {
        "year": when,
        "floorAltitude": floor,
        "days": _guard(solarsystem.season, latitude, longitude, when, floor, step),
    }


@app.post("/api/survey/optimise")
def optimise_survey(body: SurveyPlanRequest) -> dict[str, Any]:
    """Settle tonight's parameters from the geometry, and say why.

    Most of the knobs in this tab are not really free on a given night — the
    season decides whether the comet zone is reachable, which side of the Sun
    is worth having, and how near the Sun the sweep can start.
    """
    context = _survey_context(body.date)
    field = context["field"]
    if not field.get("width"):
        raise HTTPException(status_code=400,
                            detail="the telescope's field of view is not known")
    # A named side narrows the search instead of being overruled: asking for
    # the best *evening* sweep has to be able to come back with one, even on a
    # night where morning is the better half.
    only = body.side if body.side in ("morning", "evening") else None
    result = _guard(solarsystem.optimise, context["site"]["latitude"],
                    context["site"]["longitude"], context["night"], field,
                    context["settings"], None, only)
    if not result.get("ok"):
        raise HTTPException(status_code=400,
                            detail=result.get("detail", "nothing to optimise"))
    return {**result, "night": context["night"], "field": field}


@app.get("/api/survey/modes")
def survey_modes() -> dict[str, Any]:
    return {"modes": solarsystem.MODES}


@app.post("/api/survey/settings")
def set_survey_settings(body: SurveySettingsRequest) -> dict[str, Any]:
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")
    return {"survey": config.update("survey", values)}


@app.post("/api/survey/plan")
def plan_survey(body: SurveyPlanRequest) -> dict[str, Any]:
    """Work out tonight's sweep without saving anything."""
    context = _survey_context(body.date)
    field = context["field"]
    if not field.get("width"):
        raise HTTPException(
            status_code=400,
            detail=field.get("detail", "the telescope's field of view is not known")
            + " — set the focal length and sensor size in Site & Optics.")

    settings = {**context["settings"], **body.model_dump(exclude_none=True)}
    region = {k: settings[k] for k in
              ("elongationMin", "elongationMax", "betaMin", "betaMax", "side")}
    observed = coverage.last_observed() if settings.get("revisitNights") else {}

    result = _guard(solarsystem.plan, region, field, context["site"],
                    context["night"], settings, observed)
    return {**result, "night": context["night"], "settings": settings,
            "site": context["site"]}


@app.post("/api/survey/save")
def save_survey(body: SurveySaveRequest) -> dict[str, Any]:
    """Turn a planned sweep into a target and put it in the night's plan.

    The sweep is stored with its Sun-relative definition, not just the RA and
    Dec it works out to tonight, so the same sweep can be regenerated for
    another date and land in the right place rather than where the Sun was.
    """
    context = _survey_context(body.date)
    field = context["field"]
    if not field.get("width"):
        raise HTTPException(status_code=400,
                            detail="the telescope's field of view is not known")
    settings = {**context["settings"], **(body.settings or {})}
    region = {k: settings[k] for k in
              ("elongationMin", "elongationMax", "betaMin", "betaMax", "side")}
    observed = coverage.last_observed() if settings.get("revisitNights") else {}
    result = _guard(solarsystem.plan, region, field, context["site"],
                    context["night"], settings, observed)
    if not result["panels"]:
        raise HTTPException(
            status_code=400,
            detail="nothing to save: no panel in the region is observable in "
                   "tonight's twilight window")

    # Evening and morning are separate runs hours apart, so they become separate
    # targets: one to prepend to the night and one to append. Putting both in a
    # single target would ask the sequencer to shoot dusk and dawn as one block.
    by_window: dict[str, list[dict[str, Any]]] = {}
    for panel in result["panels"]:
        by_window.setdefault(panel["window"], []).append(panel)

    saved: list[dict[str, Any]] = []
    for which, panels in by_window.items():
        name = body.name if len(by_window) == 1 else f"{body.name} ({which})"
        target = _guard(targets.create_survey, name, {**region, "side": which},
                        settings, panels, field, context["night"])
        entry = _guard(plan.add, target["id"], target["name"])

        # Evening twilight comes before everything else and morning after it,
        # which is simply when the twilight is.
        wants_front = (which == "evening") if len(by_window) > 1 else (
            body.position == "start")
        if wants_front:
            order = [entry["id"]] + [e["id"] for e in plan.raw()["entries"]
                                     if e["id"] != entry["id"]]
            _guard(plan.reorder, order)

        # One allocation covers the whole sweep: every panel gets the same burst.
        _guard(plan.set_filters, entry["id"],
               [{"name": settings.get("filter") or _available_filters()[0],
                 "exposure": float(settings.get("exposure", 30.0)),
                 "count": int(settings.get("exposureCount", 36))}],
               len(panels), 1e12, _overheads())
        saved.append({"target": target, "entry": entry, "window": which,
                      "panels": len(panels)})
        rigs.log(f"Survey sweep saved: {target['name']} — {len(panels)} panels",
                 "success")

    return {"saved": saved, "target": saved[0]["target"],
            "entry": saved[0]["entry"], "plan": plan.raw(),
            "summary": solarsystem.summarise(result)}


@app.get("/api/survey/coverage")
def get_coverage() -> dict[str, Any]:
    return {"cells": coverage.listing()}


@app.post("/api/survey/coverage")
def record_coverage(body: SurveyCoverageRequest) -> dict[str, Any]:
    """Mark cells as observed, so the revisit filter knows about them."""
    counted = coverage.record_many([c.model_dump() for c in body.cells], body.when)
    return {"recorded": counted, "cells": len(coverage.last_observed())}


@app.delete("/api/survey/coverage")
def clear_coverage() -> dict[str, Any]:
    removed = coverage.clear()
    rigs.log(f"Survey coverage history cleared ({removed} cells)", "warn")
    return {"cleared": removed}


# ---------------------------------------------------------------------------
# Target list
# ---------------------------------------------------------------------------

@app.get("/api/targets")
def list_targets() -> dict[str, Any]:
    return {"targets": targets.listing()}


@app.post("/api/targets")
def create_target(body: TargetRequest) -> dict[str, Any]:
    target = _guard(targets.create, body.name, body.ra, body.dec, body.rotation,
                    body.panelWidth, body.panelHeight, body.rows, body.columns,
                    body.overlap, body.notes, body.survey, body.align)
    manager.log(
        f"Target saved: {target['name']}"
        + (f" ({len(target['panels'])} panels)" if target["panels"] else ""), "success")
    return {"target": target}


@app.post("/api/targets/{target_id}")
def edit_target(target_id: str, body: TargetEditRequest) -> dict[str, Any]:
    return {"target": _guard(targets.update, target_id, body.name, body.notes)}


@app.delete("/api/targets/{target_id}")
def delete_target(target_id: str) -> dict[str, Any]:
    _guard(targets.delete, target_id)
    manager.log("Target removed")
    return {"deleted": True}


@app.delete("/api/targets")
def clear_targets() -> dict[str, Any]:
    """Empty the target list.

    The plan entries that pointed at them go with them - a plan entry for a
    target that no longer exists is pruned the next time the plan is read,
    and pruning it here means the Plan tab never shows the gap. What has been
    collected on them (the integration log) is not a target and is kept.
    Collaboration chunks come off too; the collaboration is still joined on
    the server, and *Add to tonight* on the Collab tab makes the target again.
    """
    removed = targets.clear()
    plan.prune(set(), {r["id"] for r in recipes.listing()})
    manager.log(f"Target list cleared ({removed} removed)")
    return {"removed": removed}


# ---------------------------------------------------------------------------
# The night, and the imaging plan
# ---------------------------------------------------------------------------

def _overheads() -> dict[str, float]:
    """What the night costs besides the shutter being open.

    Measured where the rig has measured itself, typed where it has not.  The
    typed numbers stay as the fallback rather than being overwritten, so a rig
    that has never run still plans, and unticking `useMeasured` puts the plan
    straight back on figures the operator controls.
    """
    settings = config.section("schedule")
    typed = {
        "perFrame": float(settings.get("perFrameSeconds") or 15.0),
        "filterChange": float(settings.get("filterChangeSeconds") or 20.0),
        "perPanel": float(settings.get("perPanelSeconds") or 90.0),
    }
    if not settings.get("useMeasured", True):
        return {**typed, "focusRun": 0.0, "focusEvery": 0.0, "measured": False}

    # What a frame costs besides its exposure is the download plus the dither
    # that follows it — two separate measurements, because a rig that does not
    # dither still pays the download.
    per_frame = overhead_store.value("download") + overhead_store.value("dither")
    focus = config.section("sequencer")
    return {
        "perFrame": round(per_frame, 2) if overhead_store.measured("download") else typed["perFrame"],
        "filterChange": (overhead_store.value("filterChange")
                         if overhead_store.measured("filterChange")
                         else typed["filterChange"]),
        "perPanel": (overhead_store.value("slew")
                     if overhead_store.measured("slew") else typed["perPanel"]),
        # Autofocus, which the plan used to cost at nothing at all. A sweep of
        # this rig's shape, and how often the settings say one is due.
        "focusRun": round(overhead_store.focus_seconds(
            int(focus.get("focusPoints") or 9),
            float(focus.get("focusExposure") or 6.0),
            int(focus.get("focusFramesPerPoint") or 1)), 1),
        "focusEvery": float(focus.get("autofocusIntervalMinutes") or 0.0),
        "focusOnFilter": bool(focus.get("autofocusOnFilterChange", True)),
        "measured": overhead_store.measured("download"),
    }


def _require_site() -> tuple[float, float]:
    site = effective_site(config, manager)
    if site.get("latitude") is None or site.get("longitude") is None:
        raise HTTPException(
            status_code=400,
            detail="The observing site is not set. Open Site & Optics and enter "
                   "your latitude and longitude, or connect a mount that reports them.")
    return float(site["latitude"]), float(site["longitude"])


def _night_for(date_text: str | None) -> dict[str, Any]:
    latitude, longitude = _require_site()
    day = None
    if date_text:
        try:
            day = _dt.date.fromisoformat(date_text)
        except ValueError as exc:
            raise HTTPException(status_code=400,
                                detail=f"{date_text!r} is not a date (YYYY-MM-DD)") from exc
    return schedule.night(latitude, longitude, day)


def _available_filters(target=None) -> list[str]:
    """The filter wheel's names when it is connected, the configured list if not.

    Per telescope: two scopes on the same mount routinely carry different
    filters, and planning the second one's night from the first one's wheel
    would be quietly wrong.
    """
    return calibrating.filter_names(target or rigs.master, config)


def _telescope_summary() -> list[dict[str, Any]]:
    """The telescopes as the planner needs to see them."""
    return [{"id": rig.id, "name": rig.name, "role": rig.role,
             "filters": _available_filters(rig),
             "hasCamera": (camera := rig.manager.get("camera")) is not None
             and camera.connected}
            for rig in rigs.all]


def _target_schedule(target: dict[str, Any], night_info: dict[str, Any],
                     latitude: float, longitude: float, minimum_altitude: float,
                     entry: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything the planner needs to know about one target tonight.

    When the entry has a start or end pinned on its graph, the budget is the
    pinned slot rather than the whole time the target is up.
    """
    window = schedule.observable(target["ra"], target["dec"], latitude, longitude,
                                 night_info, minimum_altitude)
    sky = window
    if entry and (entry.get("startAt") or entry.get("endAt")):
        window = schedule.clip_window(window, entry.get("startAt"), entry.get("endAt"))
    curve_from = night_info.get("sunset") or night_info["windowStart"]
    curve_to = night_info.get("sunrise") or night_info["windowEnd"]
    curve = schedule.altitude_curve(target["ra"], target["dec"], latitude, longitude,
                                    curve_from, curve_to)

    # The count the budget is worked out against. When the entry is limited to
    # some of the mosaic's panels — a re-shoot of three, or a collaboration
    # share of twelve out of forty — that is how many the run visits, so it is
    # how many the night is charged for. Charging for the whole grid made a
    # share look five times more expensive than the work it actually was.
    panels = max(1, len(target.get("panels") or []))
    if entry is not None and target.get("panels"):
        picked = {int(i) for i in (plans.options_for(entry).get("panels") or [])}
        present = {int(p.get("index") or 0) for p in target["panels"]}
        if picked & present:
            panels = len(picked & present)
    order: dict[str, Any] = {}
    if target.get("panels") and target.get("type") == "survey":
        # A survey sweep is already in the order it will be shot — the planner
        # sorted it by what is about to set, inside a window minutes long.
        # Re-deriving a mosaic's boustrophedon from it is both wrong and
        # impossible: its panels are a scattered selection, not a grid, and
        # they carry no row or column to walk along.
        order = {"order": [p.get("index") for p in target["panels"]],
                 "source": "survey"}
    elif target.get("panels"):
        order = schedule.tile_order(target["panels"], latitude, longitude,
                                    night_info, minimum_altitude)

    available = window["longestMinutes"] * 60.0
    return {
        "window": window,
        "skyWindow": sky,
        "curve": curve,
        "curveStart": curve_from,
        "curveEnd": curve_to,
        "panels": panels,
        "availableSeconds": round(available, 1),
        "perPanelSeconds": round(available / panels, 1) if panels else 0.0,
        "tileOrder": order,
    }


def _repair_collab_allocation(entry: dict[str, Any],
                              target: dict[str, Any]) -> dict[str, Any]:
    """A collaboration entry with a task and no frames gets its frames back.

    A read endpoint that writes, and deliberately so. An earlier version of the
    hours control divided a night across thirty panels, got no whole frames on
    any of them, and the budget trim then emptied the list — leaving an entry
    that said "nothing planned" and would have said it for ever, since nothing
    else ever touched the allocation again. The state is inconsistent on its
    face — a chunk whose task names filters cannot honestly have none — and the
    plan is where somebody looks when the night has no frames, so the plan is
    where it is put right. Once: after this the entry has frames and the
    condition never recurs.
    """
    stamp = target.get("collab") or {}
    if not stamp.get("task"):
        return entry
    task = _task_for_target(target)
    if not task or not task.get("filters"):
        return entry
    # Behind the server: a newer deal has arrived since the target was last
    # stamped. The poll normally applies it the moment it lands; this is the
    # same step taken on the way to showing the plan, so the plan can never
    # show a list the server has already replaced.
    if int(stamp.get("version") or 0) != int(task.get("version") or 1):
        _resync_task(task)
        return next((e for e in plan.raw()["entries"] if e["id"] == entry["id"]),
                    entry)
    if any(int(f.get("count") or 0) > 0 for f in (entry.get("filters") or [])):
        return entry
    _apply_task_allocation(entry["id"], task, target)
    rigs.log(f"Collaboration: {entry['name']} had no frames planned; the "
             "project's depth has been put back on every panel", "warn")
    return next((e for e in plan.raw()["entries"] if e["id"] == entry["id"]),
                entry)


def _cost_tonight(target: dict[str, Any], used: float,
                  allocations: dict[str, list[dict[str, Any]]],
                  info: dict[str, Any], walk: list[Any],
                  overheads: dict[str, float],
                  options: dict[str, Any]) -> dict[str, Any]:
    """What a plan entry costs the night, and what it will get through.

    For an ordinary target the two are the same number: the allocation is
    tonight's ask. For a collaboration chunk they are not. Its allocation is the
    project's depth on every panel of the share — a season's work on a big
    mosaic — and the night is charged for what the run will actually reach
    before the window closes. Charging the whole share made the plan's summary
    read "over by a hundred hours" against a night that was, in fact, fine.
    The share's full cost is kept alongside under its own name.
    """
    forecast = {
        rig_id: schedule.tonight(rows, walk, info["availableSeconds"], overheads,
                                 options.get("filterOrder", "grouped"))
        for rig_id, rows in allocations.items()}
    whole_frames = sum(sum(f["count"] for f in rows) * info["panels"]
                       for rows in allocations.values())
    result = {
        "tonight": forecast,
        "usedSeconds": round(used, 1),
        "frames": whole_frames,
        "shareSeconds": round(used, 1),
        "shareFrames": whole_frames,
    }
    if (target.get("collab") or {}).get("project") and forecast:
        result["usedSeconds"] = round(max(f["seconds"] for f in forecast.values()), 1)
        result["frames"] = max(sum(f["frames"].values()) for f in forecast.values())
    return result


def _collab_entry(target: dict[str, Any]) -> dict[str, Any] | None:
    """The collaboration side of a plan entry, or None for an ordinary target.

    Everything here is *shared* — what the project wants, and what every
    contributor together has collected — because the question a participant
    actually has is "is this nearly done, and does it still need me?", and no
    amount of detail about their own frames answers it.
    """
    stamp = target.get("collab") or {}
    if not stamp.get("project"):
        return None
    task = _task_for_target(target)
    status = collab_client.status()
    project = next((row for row in (status.get("open") or [])
                    if row.get("id") == stamp["project"]), None)

    return {
        "project": stamp["project"],
        "projectName": stamp.get("projectName") or "",
        "task": stamp.get("task") or "",
        # The sub lengths and the split between filters, as the project set
        # them. Shown, never offered for editing.
        "filters": (task or {}).get("filters") or [],
        # Tonight's visit to each panel of the share, as the server sized it:
        # enough frames on each to stack, and as many panels as the night
        # holds. The filters above are the project's full depth; this is the
        # slice of it the rig gives tonight.
        "visit": (task or {}).get("visit") or {},
        "goals": (project or {}).get("goals") or {},
        "collected": (project or {}).get("collected") or {},
        "notes": (project or {}).get("notes") or "",
        # Whether the numbers above could be refreshed at all. A stale depth
        # bar that says nothing about being stale is worse than none.
        "known": project is not None,
        "checkedAt": status.get("checkedAt"),
        # Which of the mosaic's panels are this rig's, and how many there are
        # in all. The picture shows the whole project; this says which part of
        # it the telescope is for.
        "share": list(stamp.get("share") or []),
        "totalPanels": max(1, len(target.get("panels") or []) or 1),
        # The project's own rectangle of sky, north-up, so the framing can draw
        # what the collaboration covers as well as the part of it this rig is
        # shooting. The two are different shapes: the mosaic is laid along the
        # camera's axes and circumscribes the region.
        "region": (task or {}).get("region"),
    }


def _calibration_entry(entry: dict[str, Any],
                       by_recipe: dict[str, Any]) -> dict[str, Any]:
    """A calibration task as the Plan tab sees it.

    It has no window and no altitude curve — nothing about the sky says when to
    take darks — so what it reports instead is what it will shoot and roughly
    how long that takes.
    """
    recipe = by_recipe.get(entry.get("recipeId") or "")
    overheads = _overheads()
    per_frame = float(overheads.get("perFrame") or 0.0)

    sets: list[dict[str, Any]] = []
    seconds = 0.0
    frames = 0
    wheel = len(_available_filters()) or 1
    for spec in (recipe or {}).get("sets") or []:
        # A set covering the whole wheel costs its count once per filter, even
        # though it is one line in the recipe.
        count = int(spec.get("count") or 0) * (wheel if spec.get("allFilters") else 1)
        # An auto exposure is not known until the panel has been measured, so a
        # second a frame is the honest placeholder rather than zero.
        exposure = float(spec.get("exposure") or 0.0)
        if spec.get("frameType") == "flat" and spec.get("autoExposure") and not exposure:
            exposure = 1.0
        seconds += count * (exposure + per_frame)
        frames += count
        sets.append({**spec, "seconds": round(count * (exposure + per_frame), 1)})

    scope = entry.get("rigId") or ""
    named = "every telescope"
    if scope:
        # A telescope that has since been taken off the mount must not bring the
        # whole Plan tab down with it.
        try:
            named = rigs.get(scope).name
        except DeviceError:
            named = "a telescope that is no longer in the setup"
    return {
        **entry,
        "kind": "calibration",
        "recipe": recipe,
        "recipeName": (recipe or {}).get("name"),
        "missing": recipe is None,
        "sets": sets,
        "panels": 1,
        "frames": frames,
        "usedSeconds": round(seconds, 1),
        # Every telescope shoots the set at the same time, so the task costs the
        # night one set's worth of time however many scopes are on the mount.
        "telescope": named,
        "window": None,
    }


def _allsky_entry(entry: dict[str, Any], target: dict[str, Any],
                  night_info: dict[str, Any], latitude: float, longitude: float,
                  overheads: dict[str, float]) -> dict[str, Any]:
    """An all-sky survey as the Plan tab sees it.

    It has no window of its own — the grid covers the whole sky, so something in
    it is always up — and it is never finished.  What it reports instead is how
    many fields fit in the time it has been given, which is the number that
    decides whether the slot is worth having.
    """
    parameters = target.get("allsky") or {}
    goal = parameters.get("goal") or []
    scopes = max(1, len(rigs.imaging()) or len(rigs.all))
    settings = config.section("allsky")
    per_field = allsky.visit_seconds(goal, overheads,
                                     float(settings.get("visitMinutes") or 0.0),
                                     scopes)

    start = (entry.get("startAt") or night_info.get("duskAstronomical")
             or night_info.get("windowStart") or 0.0)
    end = (entry.get("endAt") or night_info.get("dawnAstronomical")
           or night_info.get("windowEnd") or 0.0)
    available = max(0.0, float(end) - float(start))

    tonight: dict[str, Any] = {"fields": []}
    if goal and available > 0:
        with contextlib.suppress(Exception):
            tonight = allsky.night_plan(
                _allsky_grid(target), progress.survey(target["id"]), goal,
                latitude, longitude, float(start), float(end), per_field,
                settings, width_hint=parameters["fieldWidth"],
                height_hint=parameters["fieldHeight"])

    fields = tonight.get("fields") or []
    summary = allsky.survey_summary(
        _allsky_grid(target), progress.survey(target["id"]), goal, latitude,
        float(config.get("allsky", "minAltitude", 40.0)), per_field * scopes)

    return {
        **entry,
        "kind": "allsky",
        "target": {k: v for k, v in target.items() if k != "panels"},
        "panels": max(1, len(fields)),
        "fieldsTonight": len(fields),
        "tonight": fields[:400],
        "flips": tonight.get("flips"),
        "patches": tonight.get("patches"),
        "contiguous": tonight.get("contiguous"),
        "detail": tonight.get("detail") or "",
        "goal": goal,
        "perFieldSeconds": round(per_field, 1),
        "telescopes": scopes,
        "summary": summary,
        "frames": len(fields) * sum(int(g.get("count") or 0) for g in goal),
        "usedSeconds": round(sum(f["seconds"] for f in fields), 1),
        "availableSeconds": round(available, 1),
        "window": None,
    }


@app.get("/api/schedule/night")
def get_night(date: str | None = Query(default=None)) -> dict[str, Any]:
    info = _night_for(date)
    return {"night": info, "site": effective_site(config, manager),
            "minAltitude": config.get("schedule", "minAltitude", 30.0)}


@app.get("/api/plan")
def get_plan(date: str | None = Query(default=None)) -> dict[str, Any]:
    """The plan, tonight, and how each target sits in it.

    Always tonight.  The Earth goes round and the sky with it, so a plan pinned
    to a date is a plan that is quietly wrong from the following evening: the
    transit curves, the windows and the running order all belong to a night that
    has gone.  Working out the night afresh on every request is a few
    milliseconds and it is always right.
    """
    stored = plan.raw()
    known = {t["id"]: t for t in targets.listing()}
    plan.prune(set(known), {r["id"] for r in recipes.listing()})
    plan.prune_rigs({rig.id for rig in rigs.all})
    stored = plan.raw()

    telescopes = _telescope_summary()
    master_id = rigs.master.id
    rig_ids = [rig["id"] for rig in telescopes]

    by_recipe = {r["id"]: r for r in recipes.listing()}

    site = effective_site(config, rigs.master.manager)
    if site.get("latitude") is None:
        # No site means no windows and no altitude curves — but a calibration
        # task needs neither, and an afternoon of darks with the dome shut is a
        # perfectly good use of a plan.
        return {"plan": stored, "night": None, "site": site,
                "filters": _available_filters(), "telescopes": telescopes,
                "masterRig": master_id, "overheads": _overheads(),
                "entries": [_calibration_entry(e, by_recipe)
                            for e in stored["entries"] if plans.is_calibration(e)],
                "minAltitude": config.get("schedule", "minAltitude", 30.0),
                "detail": "the observing site is not set"}

    latitude, longitude = float(site["latitude"]), float(site["longitude"])
    minimum_altitude = float(config.get("schedule", "minAltitude", 30.0))
    night_info = _night_for(date)
    overheads = _overheads()
    moon = schedule.moon_track(latitude, longitude, night_info)

    # Last night's times are absolute moments, so by this evening they are
    # eighteen hours in the past. What was chosen was a time of night, though,
    # not an instant — so they move forward to tonight at the same clock time
    # rather than being thrown away and typed in again every evening.
    moved = plan.roll_times(night_info.get("windowStart"),
                            night_info.get("windowEnd"))
    if moved:
        manager.log(f"A new night: carried {moved} slot time(s) over from the "
                    "previous one, at the same time of night.")
        stored = plan.raw()

    entries = []
    for entry in stored["entries"]:
        if plans.is_calibration(entry):
            entries.append(_calibration_entry(entry, by_recipe))
            continue
        target = known.get(entry.get("targetId"))
        if target is None:
            continue
        if target.get("type") == "allsky":
            entries.append(_allsky_entry(entry, target, night_info,
                                         latitude, longitude, overheads))
            continue
        info = _target_schedule(target, night_info, latitude, longitude,
                                minimum_altitude, entry)
        repaired = _repair_collab_allocation(entry, target)
        if repaired is not entry:
            # The raw plan in this same response was captured before the
            # repair; refresh it so the first read is already consistent.
            entry = repaired
            stored = plan.raw()
        # The telescopes shoot together, so an entry costs the night whatever
        # its slowest telescope's allocation costs — not the sum of them.
        used = plans.entry_seconds(entry, rig_ids, master_id, info["panels"],
                                   overheads)
        allocations = {rig_id: plans.allocation_for(entry, rig_id, master_id)
                       for rig_id in rig_ids}
        options = plans.options_for(entry)
        # What will actually be shot before the window closes, as against what
        # was asked for. On a mosaic bigger than one night the two are different
        # numbers, and only one of them is a plan.
        #
        # Walked in the order the run walks it — the tile order worked out from
        # the sky, filtered by any hand-picked panels — so the panels named here
        # are the panels that get shot.
        walk = list(info["tileOrder"].get("order") or []) or [
            p.get("index") for p in (target.get("panels") or [])] or [1]
        picked = [int(index) for index in (options.get("panels") or [])]
        if picked and len(walk) > 1:
            narrowed = [index for index in walk if index in picked]
            if narrowed:
                walk = narrowed
        entries.append({
            **entry,
            "target": target,
            **info,
            # Always sent with every default filled in, so the UI never has to
            # know what the defaults are.
            "options": plans.options_for(entry),
            # How near the Moon gets to this target while it is up, and how much
            # of its window that spoils at the limit it has been given.
            "moon": schedule.dark_overlap(
                info["window"]["intervals"], moon,
                astro.normalise_ra_hours(float(target.get("ra") or 0.0)),
                float(target.get("dec") or 0.0),
                float(plans.options_for(entry).get("moonAvoidance") or 0.0) or 1e-9),
            "allocations": allocations,
            # What it costs the night and what it will get through - per
            # telescope, because they may carry different filters and so get
            # through different amounts of the same window. For a collaboration
            # chunk the night is charged for what the run will reach, not for
            # the whole share; see `_cost_tonight`.
            **_cost_tonight(target, used, allocations, info, walk, overheads,
                            options),
            # What a collaboration chunk needs that an ordinary target does
            # not: what the project is asking of everybody, what everybody has
            # collected so far, and the sub lengths this rig is not allowed to
            # change.
            "collab": _collab_entry(target),
            "rigSeconds": {
                rig_id: round(schedule.plan_seconds(rows, info["panels"], overheads), 1)
                for rig_id, rows in allocations.items()},
        })

    return {
        "plan": stored,
        "night": night_info,
        "site": site,
        "minAltitude": minimum_altitude,
        "filters": _available_filters(),
        "telescopes": telescopes,
        "masterRig": master_id,
        "overheads": overheads,
        # What the camera covers now. Only used to draw a framing for a target
        # that was saved without one — everything saved from the Planner carries
        # the field it was framed with, and that is what gets drawn.
        "field": _camera_field(),
        # Whether the camera can be turned. A collaboration target's "turn to
        # the project's angle" choice is only offered when it can.
        "rotator": collabclient.has_rotator(rigs.master),
        # The Moon across the night, for the composite graph. The single most
        # useful thing to draw after the targets themselves.
        "moon": moon,
        "sky": autoplan.conditions(moon, config.section("autoplan")),
        "entries": entries,
    }


@app.post("/api/plan")
def set_plan_meta(body: PlanMetaRequest) -> dict[str, Any]:
    _guard(plan.set_meta, body.name, body.date, body.clearDate)
    return {"plan": plan.raw()}


@app.post("/api/plan/entries")
def add_plan_entry(body: PlanEntryRequest) -> dict[str, Any]:
    target = _guard(targets.get, body.targetId)
    entry = _guard(plan.add, target["id"], target["name"])

    # A collaboration chunk arrives knowing what it is for. Dragging it into
    # the night should not then mean typing its filters and counts in by hand
    # off another tab — the task said what it wants, and the allocation is a
    # fact about the task rather than a preference about the evening.
    clamped = False
    stamp = target.get("collab") or {}
    if stamp.get("task"):
        task = next((entry_ for entry_ in collab_client.tasks()
                     if entry_.get("id") == stamp["task"]), None)
        if task:
            _plan_share(entry["id"], target)
            _apply_project_rules(entry["id"], task)
            clamped = _apply_task_allocation(entry["id"], task, target)
            entry = next((e for e in plan.raw()["entries"]
                          if e["id"] == entry["id"]), entry)

    manager.log(f"Added {target['name']} to the plan")
    return {"entry": entry, "clamped": clamped}


@app.post("/api/plan/calibration")
def add_plan_calibration(body: PlanCalibrationRequest) -> dict[str, Any]:
    """Drop a calibration recipe into the plan as a task of its own."""
    recipe = _guard(recipes.get, body.recipeId)
    scope = (body.rigId or "").strip()
    if scope:
        _rig(scope)                     # 400s now rather than at two in the morning
    entry = _guard(plan.add_calibration, recipe["id"],
                   body.name or recipe["name"], scope or None)
    manager.log(f"Added the calibration task {entry['name']} to the plan")
    return {"entry": entry}


def _entry_budget(entry_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    stored = plan.raw()
    entry = next((e for e in stored["entries"] if e["id"] == entry_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="unknown plan entry")
    if plans.is_calibration(entry):
        raise HTTPException(
            status_code=400,
            detail="a calibration task shoots what its recipe says; edit the "
                   "recipe on the Calibrate tab")
    target = _guard(targets.get, entry["targetId"])
    if target.get("type") == "allsky":
        raise HTTPException(
            status_code=400,
            detail="an all-sky survey shoots the same filters on every field; "
                   "set them on the All-Sky tab")

    latitude, longitude = _require_site()
    minimum_altitude = float(config.get("schedule", "minAltitude", 30.0))
    night_info = _night_for(stored.get("date"))
    return entry, _target_schedule(target, night_info, latitude, longitude,
                                   minimum_altitude, entry)


@app.post("/api/plan/entries/{entry_id}/filters")
def set_plan_filters(entry_id: str, body: PlanFiltersRequest,
                     rig: str | None = None) -> dict[str, Any]:
    """Set one telescope's allocation for a target.

    Each telescope is trimmed against the same window, not a share of it: they
    shoot at the same time, so what limits them is the same stretch of night.
    """
    _entry, info = _entry_budget(entry_id)
    target = _rig(rig)
    rig_id = None if target.is_master else target.id

    result = _guard(plan.set_filters, entry_id,
                    [f.model_dump() for f in body.filters],
                    info["panels"], info["availableSeconds"], _overheads(),
                    rig_id)
    return {**result, "availableSeconds": info["availableSeconds"],
            "panels": info["panels"], "rig": target.id}


@app.delete("/api/plan/entries/{entry_id}/filters")
def clear_plan_filters(entry_id: str, rig: str | None = None) -> dict[str, Any]:
    """Put a telescope back onto mirroring the master's allocation."""
    target = _rig(rig)
    if target.is_master:
        raise HTTPException(
            status_code=400,
            detail="the master's allocation is the one everything else follows; "
                   "set its frame counts to zero instead")
    return {"entry": _guard(plan.clear_rig_filters, entry_id, target.id)}


@app.post("/api/plan/entries/{entry_id}/notes")
def set_plan_notes(entry_id: str, body: PlanNotesRequest) -> dict[str, Any]:
    return {"entry": _guard(plan.set_notes, entry_id, body.notes)}


@app.delete("/api/plan/entries/{entry_id}")
def delete_plan_entry(entry_id: str) -> dict[str, Any]:
    _guard(plan.remove, entry_id)
    return {"deleted": True}


@app.post("/api/plan/order")
def reorder_plan(body: PlanOrderRequest) -> dict[str, Any]:
    return {"plan": _guard(plan.reorder, body.entryIds)}


@app.post("/api/plan/entries/{entry_id}/times")
def set_plan_times(entry_id: str, body: PlanTimesRequest) -> dict[str, Any]:
    return {"entry": _guard(plan.set_times, entry_id, body.startAt, body.endAt,
                            body.clearStart, body.clearEnd)}


@app.get("/api/sequences")
def list_sequences() -> dict[str, Any]:
    """Every saved sequence, newest first."""
    return {"sequences": sequences.listing()}


@app.post("/api/sequences")
def save_sequence(body: SequenceSaveRequest) -> dict[str, Any]:
    """Snapshot the plan under a name, to bring back another night."""
    saved = _guard(sequences.save_as, body.name, plan.raw(), body.id)
    manager.log(f"Saved the sequence '{saved['name']}'", "success")
    return {"sequence": saved, "sequences": sequences.listing()}


@app.post("/api/sequences/{sequence_id}/load")
def load_sequence(sequence_id: str) -> dict[str, Any]:
    """Replace the plan with a saved sequence.

    Its times are deliberately not restored: they belonged to the night it was
    worked out for. The shape of the night comes back and Auto-arrange places
    it in tonight.
    """
    if sequencer.running:
        raise HTTPException(
            status_code=409,
            detail="a sequence is running; stop it before loading another plan")
    stored = _guard(sequences.get, sequence_id)
    _guard(plan.replace_entries, stored.get("entries") or [], stored.get("name"))
    manager.log(f"Loaded the sequence '{stored['name']}' "
                f"({len(stored.get('entries') or [])} target(s)); "
                "run Auto-arrange to give it tonight's times", "success")
    return {"plan": plan.raw()}


@app.delete("/api/sequences/{sequence_id}")
def delete_sequence(sequence_id: str) -> dict[str, Any]:
    _guard(sequences.delete, sequence_id)
    manager.log("Deleted a saved sequence")
    return {"sequences": sequences.listing()}


@app.post("/api/plan/entries/{entry_id}/options")
def set_plan_options(entry_id: str, body: PlanOptionsRequest) -> dict[str, Any]:
    """One target's own sequence settings: its floor, its Moon limit, its goal."""
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")
    entry = _guard(plan.set_options, entry_id, values)
    # Turning the rotator to the project's angle, or not, changes what the
    # mosaic *is* - its angle, its alignment, its grid - so the target is laid
    # out again to match, and the server told, the moment the choice is made.
    if "collabMatchRotation" in values and entry.get("targetId"):
        with contextlib.suppress(Exception):
            target = targets.get(entry["targetId"])
            if _reframe_collab_target(target, match_rotation=values["collabMatchRotation"],
                                      why="the rotator choice changed"):
                with contextlib.suppress(Exception):
                    collab_client.poll()
                entry = next((e for e in plan.raw()["entries"] if e["id"] == entry_id),
                             entry)
    return {"entry": entry}


@app.post("/api/plan/arrange")
def arrange_plan(body: ArrangeRequest | None = None) -> dict[str, Any]:
    """Order the night, hand every target a slot, and fill the slots.

    Two passes, because the second depends on the first.  The order and the
    times come from how long each target's *current* allocation would take, the
    way they always have.  Then, if the operator wants it, each target's slot is
    filled afresh — which filters, how long a sub, and how many — from the Moon,
    from what that target already has in the bank, and from its goal.

    Nothing is shot here.  Every number it picks lands on the plan where it can
    be seen and changed before the night starts, and the reasoning comes back
    with it so it does not have to be checked by hand.
    """
    choose = config.get("autoplan", "chooseExposures", True)
    if body is not None and body.chooseExposures is not None:
        choose = body.chooseExposures

    latitude, longitude = _require_site()
    stored = plan.raw()
    known = {t["id"]: t for t in targets.listing()}
    minimum_altitude = float(config.get("schedule", "minAltitude", 30.0))
    night_info = _night_for(stored.get("date"))
    overheads = _overheads()
    autoplan_settings = config.section("autoplan")

    moon = schedule.moon_track(latitude, longitude, night_info)
    sky = autoplan.conditions(moon, autoplan_settings)

    rig_ids = [rig.id for rig in rigs.all]
    master_id = rigs.master.id

    arrangeable: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for entry in stored["entries"]:
        # Calibration tasks are not arranged: nothing about the sky says when to
        # take darks, so where they sit in the night is the operator's choice.
        if plans.is_calibration(entry):
            continue
        target = known.get(entry.get("targetId"))
        if target is None:
            continue
        # An all-sky survey has no window to arrange around — something in it is
        # always up — and it fills whatever slot it is given, so where that slot
        # sits is the operator's decision rather than the arranger's.
        if target.get("type") == "allsky":
            continue
        # A target switched off in the plan is not given a slot, so the night is
        # shared out between the ones that are actually going to run.
        if not plans.options_for(entry).get("enabled", True):
            continue
        # The window is clipped to the entry's times only when the operator
        # pinned them.  Times the *arranger* wrote last time are its own to
        # replace, and clipping to them would shrink the target's capacity to
        # whatever it was last given — so arranging twice would hand out less
        # than the night each time and leave the tail of the night empty.
        info = _target_schedule(target, night_info, latitude, longitude,
                                minimum_altitude,
                                entry if entry.get("timesPinned") else None)
        arrangeable.append((entry, target, info))

    # A target the operator pinned to a time is not a request, it is a fact:
    # its slot is reserved and everything else is arranged around it.
    pinned: list[dict[str, Any]] = []
    reserved: list[tuple[float, float]] = []
    loose: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    dusk = (night_info or {}).get("duskAstronomical")
    dawn = (night_info or {}).get("dawnAstronomical")
    for entry, target, info in arrangeable:
        if not entry.get("timesPinned") or not dusk or not dawn:
            loose.append((entry, target, info))
            continue
        start = float(entry.get("startAt") or dusk)
        end = float(entry.get("endAt") or dawn)
        if end <= start:
            loose.append((entry, target, info))
            continue
        reserved.append((start, end))
        pinned.append({"id": entry["id"], "start": round(start, 1),
                       "end": round(end, 1), "seconds": round(end - start, 1),
                       "shortSeconds": 0.0, "pinned": True})

    # How long each unpinned target should get.
    #
    # When the arranger is choosing the exposures too, what a target "wants" is
    # its whole window — so asking for that hands the night to whichever target
    # is first and tells the rest it ran out. The night is shared out instead,
    # weighted by priority and capped by how long each target is actually up,
    # so every observable target gets time before any gets a second helping.
    shares: dict[str, float] = {}
    if choose and loose and dusk and dawn:
        spare = sum(b - a for a, b in schedule.free_spans(dusk, dawn, reserved))
        shares = schedule.fair_shares(
            [{"id": entry["id"],
              "capacity": float(info["window"].get("totalMinutes") or 0.0) * 60.0,
              "priority": int(plans.options_for(entry).get("priority", 5))}
             for entry, _t, info in loose], spare)

    requests = []
    for entry, _target, info in loose:
        wanted = plans.entry_seconds(entry, rig_ids, master_id, info["panels"],
                                     overheads)
        if choose:
            # The share is what it should get; what it currently holds is about
            # to be replaced anyway.
            wanted = shares.get(entry["id"], wanted)
        requests.append({
            "id": entry["id"],
            "seconds": wanted,
            "intervals": info["window"]["intervals"],
            "priority": int(plans.options_for(entry).get("priority", 5)),
        })

    result = schedule.arrange(requests, night_info, reserved)
    # Pinned slots take part in the running order even though their times were
    # not the arranger's to set, so the plan still reads down the page in the
    # order the night actually happens.
    result["order"] = sorted(result["order"] + pinned, key=lambda item: item["start"])
    _guard(plan.apply_arrangement, result["order"])

    # -- filling the slots ------------------------------------------------
    chosen: list[dict[str, Any]] = []
    if choose:
        by_id = {entry["id"]: (entry, target, info)
                 for entry, target, info in arrangeable}
        result["usedSeconds"] = round(
            sum(item["seconds"] for item in result["order"]), 1)
        for slot in result["order"]:
            found = by_id.get(slot["id"])
            if found is None:
                continue
            entry, target, info = found
            options = plans.options_for(entry)
            separation = schedule.dark_overlap(
                info["window"]["intervals"], moon,
                astro.normalise_ra_hours(float(target.get("ra") or 0.0)),
                float(target.get("dec") or 0.0),
                float(options.get("moonAvoidance") or 0.0) or 1e-9).get("closest")

            # One panel's worth: a mosaic's slot is shared between its panels,
            # so what each panel gets is the slot divided by how many there are.
            panels = max(1, int(info["panels"]))
            picked = autoplan.choose(
                target, slot["seconds"] / panels, _available_filters(), moon, sky,
                overheads, autoplan_settings,
                goal_hours=float(options.get("goalHours") or 0.0),
                moon_separation=separation)
            if not picked["filters"]:
                chosen.append({"id": entry["id"], "name": entry["name"],
                               "filters": [], "notes": picked["notes"]})
                continue
            saved = _guard(plan.set_filters, entry["id"], picked["filters"],
                           info["panels"], info["availableSeconds"], overheads,
                           None)
            chosen.append({
                "id": entry["id"],
                "name": entry["name"],
                "filters": saved["entry"].get("filters") or [],
                "notes": picked["notes"],
                "moonSeparation": separation,
            })

    names = {e["id"]: e["name"] for e in stored["entries"]}
    manager.log(
        f"Plan arranged: {len(result['order'])} target(s), "
        f"{result['usedSeconds'] / 3600:.1f}h imaging, "
        f"{result['idleSeconds'] / 3600:.1f}h idle"
        + (f", {len(result['unplaced'])} could not be placed"
           if result["unplaced"] else "")
        + (f". {sky['summary']}" if choose else ""), "success")
    return {
        **result,
        "chooseExposures": bool(choose),
        "sky": sky,
        "chosen": chosen,
        "unplaced": [{**item, "name": names.get(item["id"], "?")}
                     for item in result["unplaced"]],
    }


# ---------------------------------------------------------------------------
# The all-sky survey
# ---------------------------------------------------------------------------

def _allsky_target(target_id: str) -> dict[str, Any]:
    target = _guard(targets.get, target_id)
    if target.get("type") != "allsky":
        raise HTTPException(status_code=400,
                            detail=f"{target['name']} is not an all-sky survey")
    return target


def _allsky_grid(target: dict[str, Any]) -> list[dict[str, Any]]:
    parameters = target["allsky"]
    return _guard(allsky.grid, parameters["fieldWidth"], parameters["fieldHeight"],
                  parameters.get("overlap", 0.2), parameters.get("decMin", -90.0),
                  parameters.get("decMax", 90.0), parameters.get("stagger", True))


def _allsky_field_size(width: float | None, height: float | None,
                       rotation: float | None = None) -> dict[str, Any]:
    """The field the grid is built on, allowing for how the camera sits.

    The smallest usable field across the telescopes on the mount, because that
    is the only one every one of them can actually deliver — a grid built on the
    widest would leave gaps in whatever the narrowest shot.  Then shrunk to the
    largest north-up rectangle that fits inside it at the camera's angle, since
    the grid tiles in right ascension and declination and a tilted sensor does
    not cover a north-up rectangle the size of itself.
    """
    if width and height:
        raw = {"width": float(width), "height": float(height), "limitedBy": ""}
    else:
        raw = _survey_field()
        if not raw.get("width") or not raw.get("height"):
            raise HTTPException(
                status_code=400,
                detail="the telescope's field of view is not known — set the "
                       "focal length and sensor size in Site & Optics first")

    angle = (float(rotation) if rotation is not None
             else float(rigs.master.config.get("optics", "rotation", 0.0) or 0.0))
    effective = allsky.effective_field(float(raw["width"]), float(raw["height"]),
                                       angle)
    return {**effective, "sensorWidth": float(raw["width"]),
            "sensorHeight": float(raw["height"]),
            "limitedBy": raw.get("limitedBy", "")}


@app.get("/api/allsky")
def list_allsky() -> dict[str, Any]:
    """Every all-sky survey, with how far each has got."""
    site = effective_site(config, manager)
    latitude = site.get("latitude")
    settings = config.section("allsky")
    overheads = _overheads()

    surveys = []
    for target in targets.listing():
        if target.get("type") != "allsky":
            continue
        parameters = target["allsky"]
        goal = parameters.get("goal") or []
        fields = _guard(allsky.grid, parameters["fieldWidth"],
                        parameters["fieldHeight"], parameters.get("overlap", 0.2),
                        parameters.get("decMin", -90.0),
                        parameters.get("decMax", 90.0),
                        parameters.get("stagger", True))
        surveys.append({
            "id": target["id"], "name": target["name"],
            "allsky": parameters,
            "summary": allsky.survey_summary(
                fields, progress.survey(target["id"]), goal,
                None if latitude is None else float(latitude),
                float(settings.get("minAltitude", 40.0)),
                allsky.goal_seconds(goal, overheads)),
        })
    return {
        "surveys": surveys,
        "settings": settings,
        "site": site,
        "filters": _available_filters(),
        "telescopes": _telescope_summary(),
        "field": _survey_field(),
        "overheads": overheads,
    }


@app.post("/api/allsky/preview")
def preview_allsky(body: AllSkyPreviewRequest) -> dict[str, Any]:
    """What a grid with these numbers would come to, before committing to it."""
    field = _allsky_field_size(body.fieldWidth, body.fieldHeight)
    width, height = field["width"], field["height"]
    fields = _guard(allsky.grid, width, height, body.overlap,
                    body.decMin, body.decMax)
    shape = allsky.summarise_grid(fields, width, height)
    goal = [item.model_dump() for item in body.goal]
    per_field = allsky.goal_seconds(goal, _overheads()) if goal else 0.0

    site = effective_site(config, manager)
    latitude = site.get("latitude")
    within = (sum(allsky.reachable(fields, float(latitude),
                                   float(config.get("allsky", "minAltitude", 40.0))))
              if latitude is not None else len(fields))
    scopes = max(1, len(rigs.imaging()) or len(rigs.all))
    return {
        **shape,
        "camera": field,
        "reachable": within,
        "unreachable": len(fields) - within,
        "perFieldSeconds": round(per_field, 1),
        "telescopes": scopes,
        # Every telescope shoots the same field at once, so the rig gets through
        # a field in a fraction of the time one of them would take.
        "perFieldWithRig": round(per_field / scopes, 1),
        "totalSeconds": round(within * per_field / scopes, 1),
    }


@app.post("/api/allsky")
def create_allsky(body: AllSkyCreateRequest) -> dict[str, Any]:
    """Create an all-sky survey, which then appears in the target list."""
    if body.decMax <= body.decMin:
        raise HTTPException(status_code=400,
                            detail="the declination range is empty")
    field = _allsky_field_size(body.fieldWidth, body.fieldHeight)
    width, height = field["width"], field["height"]
    parameters = {
        "fieldWidth": width, "fieldHeight": height, "overlap": body.overlap,
        "decMin": body.decMin, "decMax": body.decMax, "stagger": body.stagger,
        # The camera angle the grid was laid out for. Frozen with everything
        # else: the tiling only holds while the sensor sits the way it did.
        "rotation": field["rotation"],
        "sensorWidth": field["sensorWidth"],
        "sensorHeight": field["sensorHeight"],
    }
    fields = _guard(allsky.grid, width, height, body.overlap,
                    body.decMin, body.decMax, body.stagger)
    shape = allsky.summarise_grid(fields, width, height)
    target = _guard(targets.create_allsky, body.name, parameters,
                    [item.model_dump() for item in body.goal],
                    {**shape, "camera": field})
    manager.log(f"All-sky survey {target['name']}: {shape['fields']:,} fields "
                f"in {shape['rings']} rings at a camera angle of "
                f"{field['rotation']:.2f}°", "success")
    return {"target": target, "shape": shape, "camera": field}


@app.post("/api/allsky/{target_id}/goal")
def set_allsky_goal(target_id: str, body: AllSkyGoalRequest) -> dict[str, Any]:
    _allsky_target(target_id)
    target = _guard(targets.set_allsky_goal, target_id,
                    [item.model_dump() for item in body.goal])
    return {"target": target}


@app.get("/api/allsky/{target_id}/grid")
def get_allsky_grid(target_id: str) -> dict[str, Any]:
    """The grid and its progress, as parallel arrays.

    Fourteen thousand fields as fourteen thousand JSON objects is a couple of
    megabytes of mostly repeated key names; as columns it is a tenth of that and
    the browser can draw straight from it.
    """
    target = _allsky_target(target_id)
    fields = _allsky_grid(target)
    goal = target["allsky"].get("goal") or []
    done = progress.survey(target_id)

    site = effective_site(config, manager)
    latitude = site.get("latitude")
    floor = float(config.get("allsky", "minAltitude", 40.0))

    ra: list[float] = []
    dec: list[float] = []
    ring: list[int] = []
    fraction: list[float] = []
    within: list[int] = []
    ids: list[str] = []
    for field in fields:
        state = allsky.field_state(done.get(field["id"], {}), goal)
        ids.append(field["id"])
        ra.append(field["ra"])
        dec.append(field["dec"])
        ring.append(field["ring"])
        fraction.append(state["fraction"])
        within.append(1 if (latitude is None
                            or allsky.transit_altitude(field["dec"],
                                                       float(latitude)) >= floor)
                      else 0)

    # The tiling only holds at the angle it was laid out for. If the camera has
    # been turned since, the fields no longer abut and the survey has gaps in
    # it — worth saying loudly rather than discovering while stacking.
    built_at = float(target["allsky"].get("rotation") or 0.0)
    now_at = float(rigs.master.config.get("optics", "rotation", 0.0) or 0.0)
    drift = abs(((now_at - built_at + 180.0) % 360.0) - 180.0)
    warning = ""
    if drift > 1.0:
        warning = (f"the camera is at {now_at:.2f}° and this grid was laid out "
                   f"for {built_at:.2f}° — {drift:.1f}° out, so the fields no "
                   "longer line up. Put it back, or start a new survey.")

    return {
        "id": target_id,
        "name": target["name"],
        "allsky": target["allsky"],
        "cameraAngle": round(now_at, 3),
        "builtAtAngle": round(built_at, 3),
        "angleWarning": warning,
        "ids": ids,
        "ra": ra,
        "dec": dec,
        "ring": ring,
        "fraction": fraction,
        "reachable": within,
        "fieldWidth": target["allsky"]["fieldWidth"],
        "fieldHeight": target["allsky"]["fieldHeight"],
        "summary": allsky.survey_summary(
            fields, done, goal, None if latitude is None else float(latitude),
            floor, allsky.goal_seconds(goal, _overheads())),
        "site": site,
        "minAltitude": floor,
    }


@app.get("/api/allsky/{target_id}/field/{field_id}")
def get_allsky_field(target_id: str, field_id: str) -> dict[str, Any]:
    """One field: where it is, what it has, and when it can next be shot."""
    target = _allsky_target(target_id)
    fields = _allsky_grid(target)
    found = next((f for f in fields if f["id"] == field_id), None)
    if found is None:
        raise HTTPException(status_code=404,
                            detail=f"no field {field_id!r} in {target['name']}")
    goal = target["allsky"].get("goal") or []
    done = progress.field(target_id, field_id)

    site = effective_site(config, manager)
    detail: dict[str, Any] = {}
    if site.get("latitude") is not None:
        latitude = float(site["latitude"])
        longitude = float(site["longitude"])
        night_info = _night_for(None)
        detail = _guard(schedule.rise_set_transit, found["ra"], found["dec"],
                        latitude, longitude, night_info,
                        float(config.get("allsky", "minAltitude", 40.0)))
        detail["transitAltitude"] = round(
            allsky.transit_altitude(found["dec"], latitude), 2)
    return {
        "field": found,
        "state": allsky.field_state(done, goal),
        "done": done,
        "goal": goal,
        "window": detail,
        "folder": str(capture.root_dir / clean_target(target["name"]) / field_id),
    }


@app.get("/api/allsky/{target_id}/tonight")
def allsky_tonight(target_id: str,
                   date: str | None = Query(default=None)) -> dict[str, Any]:
    """Which fields this survey would shoot tonight, and why those.

    The same call the sequencer makes, so what the tab draws is what will
    actually happen rather than a second opinion about it.
    """
    target = _allsky_target(target_id)
    latitude, longitude = _require_site()
    night_info = _night_for(date)
    goal = target["allsky"].get("goal") or []
    overheads = _overheads()

    entry = next((e for e in plan.raw()["entries"]
                  if e.get("targetId") == target_id), None)
    start = ((entry or {}).get("startAt") or night_info.get("duskAstronomical")
             or night_info.get("windowStart"))
    end = ((entry or {}).get("endAt") or night_info.get("dawnAstronomical")
           or night_info.get("windowEnd"))
    if not start or not end:
        raise HTTPException(status_code=400,
                            detail="there is no dark window on that date")

    scopes = max(1, len(rigs.imaging()) or len(rigs.all))
    settings = config.section("allsky")
    per_field = allsky.visit_seconds(goal, overheads,
                                     float(settings.get("visitMinutes") or 0.0),
                                     scopes)
    parameters = target["allsky"]
    # Where the mount is, if this would be starting about now — the sequencer
    # takes it into account when it chooses, so a preview that ignored it would
    # be showing a different night from the one that happens. For a window
    # hours away it is left out: the mount will be somewhere else by then.
    where = None
    if float(start) - time.time() < 600:
        mount = rigs.master.manager.get("mount")
        if mount is not None and mount.connected:
            with contextlib.suppress(Exception):
                where = (mount.ra, mount.dec)

    result = _guard(allsky.night_plan, _allsky_grid(target),
                    progress.survey(target_id), goal, latitude, longitude,
                    float(start), float(end), per_field, settings, where, None,
                    parameters["fieldWidth"], parameters["fieldHeight"])
    return {
        **result,
        "night": night_info,
        "windowStart": float(start),
        "windowEnd": float(end),
        "scheduled": entry is not None,
        "telescopes": scopes,
        "visitMinutes": round(per_field / 60.0, 1),
        "goalMinutes": round(allsky.goal_seconds(goal, overheads) / scopes / 60.0, 1),
    }


@app.post("/api/allsky/settings")
def set_allsky_settings(body: AllSkySettingsRequest) -> dict[str, Any]:
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")
    return {"allsky": config.update("allsky", values)}


@app.delete("/api/allsky/{target_id}")
def delete_allsky(target_id: str) -> dict[str, Any]:
    """Delete a survey and everything recorded against it."""
    target = _allsky_target(target_id)
    removed = progress.forget(target_id)
    _guard(targets.delete, target_id)
    plan.prune({t["id"] for t in targets.listing()},
               {r["id"] for r in recipes.listing()})
    manager.log(f"Deleted the all-sky survey {target['name']} and its record of "
                f"{removed} field(s)", "warn")
    return {"deleted": True, "fields": removed}


# ---------------------------------------------------------------------------
# Calibration: the library, the recipes, and running them
# ---------------------------------------------------------------------------

@app.get("/api/calibration")
def get_calibration() -> dict[str, Any]:
    """The library, the saved recipes and whatever run is going on."""
    return {
        "library": _guard(library.summary),
        "recipes": recipes.listing(),
        "run": calibrator.status(),
        "telescopes": _telescope_summary(),
        "frameTypes": list(calibrating.SET_TYPES),
        "filters": _available_filters(),
    }


@app.post("/api/calibration/settings")
def set_calibration_settings(body: CalibrationSettingsRequest) -> dict[str, Any]:
    values = body.model_dump(exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")

    folder = values.get("libraryDirectory")
    if folder is not None:
        text = folder.strip()
        values["libraryDirectory"] = text
        if text:
            path = Path(text).expanduser()
            try:
                (path / "masters").mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(status_code=400,
                                    detail=f"cannot use {path}: {exc}") from exc
            if not os.access(path, os.W_OK):
                raise HTTPException(status_code=400,
                                    detail=f"{path} is not writable")

    saved = config.update("calibration", values)
    # The folder may have moved, and the matching rules may have changed; either
    # way what was held in memory is no longer the answer.
    library.forget()
    manager.log(f"Calibration library: {library.root}")
    return {"calibration": saved, "library": _guard(library.summary)}


@app.get("/api/calibration/library")
def get_library() -> dict[str, Any]:
    return _guard(library.summary)


def _calibration_needs(target) -> list[dict[str, Any]]:
    """What tonight will ask of the library, for one telescope.

    A bias; a dark at every default exposure the filters are shot at, at
    the camera's setpoint; a flat through every filter. Gain and offset are
    the camera's settings whether or not it is connected, so the answer is
    the same in the afternoon as it is at night.
    """
    camera_settings = target.config.section("camera")
    camera = target.manager.get("camera")
    connected = camera is not None and camera.connected
    gain = camera.gain if connected else camera_settings.get("gain")
    offset = camera.offset if connected else camera_settings.get("offset")
    temperature = (camera_settings.get("setpoint")
                   if camera_settings.get("coolAtStart", True)
                   else (camera.temperature if connected else None))
    binning = int(camera_settings.get("binning", 1) or 1)
    base = {
        "binning": binning,
        "gain": gain,
        "offset": offset,
        "temperature": temperature,
        "telescope": target.name,
        "camera": camera.name if connected else "",
    }
    if connected and camera.sensor_width:
        base["width"] = camera.sensor_width // max(1, binning)
        base["height"] = camera.sensor_height // max(1, binning)

    needs = [{"kind": "bias", "label": "bias", "want": {**base, "exposure": 0.0}}]
    exposures = calibrating.default_exposures(target, config)
    for exposure in sorted(set(exposures.values())):
        through = ", ".join(name for name, value in exposures.items() if value == exposure)
        needs.append({"kind": "dark", "label": f"dark {exposure:g}s ({through})",
                      "want": {**base, "exposure": exposure}})
    for name in _available_filters(target):
        needs.append({"kind": "flat", "label": f"flat {name}",
                      "want": {**base, "exposure": 0.0, "filter": name}})
    return needs


@app.get("/api/calibration/coverage")
def calibration_coverage(rig: str | None = None) -> dict[str, Any]:
    """Whether the library holds a valid, current set of masters for tonight."""
    target = _rig(rig)
    needs = _calibration_needs(target)
    found = _guard(library.coverage, needs)
    return {**found, "rig": target.id, "telescope": target.name,
            "exposures": calibrating.default_exposures(target, config)}


class MasterInspectRequest(BaseModel):
    path: str = Field(min_length=1, max_length=400)


class MasterImportRequest(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    type: str | None = Field(default=None, pattern="^(bias|dark|darkflat|flat)$")
    filter: str | None = Field(default=None, max_length=24)
    exposure: float | None = Field(default=None, ge=0, le=3600)
    temperature: float | None = Field(default=None, ge=-60, le=60)
    gain: int | None = Field(default=None, ge=0, le=100000)
    offset: int | None = Field(default=None, ge=0, le=100000)
    binning: int | None = Field(default=None, ge=1, le=8)
    telescope: str | None = Field(default=None, max_length=60)


@app.post("/api/calibration/library/inspect")
def inspect_master(body: MasterInspectRequest) -> dict[str, Any]:
    """What a FITS or XISF file says it is, before it is brought in."""
    return _guard(calibration.inspect_master_file, body.path)


@app.post("/api/calibration/library/import")
def import_master(body: MasterImportRequest) -> dict[str, Any]:
    """Bring a master built elsewhere - PixInsight, Siril - into the library."""
    overrides = body.model_dump(exclude_none=True)
    path = overrides.pop("path")
    if "filter" in overrides:
        overrides["filter"] = filters.canonical(overrides["filter"]) or overrides["filter"]
    master = _guard(library.import_master, path, overrides)
    manager.log(f"Master brought in: {Path(master['path']).name}", "success")
    return {"master": master, "library": _guard(library.summary)}


@app.delete("/api/calibration/library/{master_id}")
def delete_master(master_id: str) -> dict[str, Any]:
    master = _guard(library.remove, master_id)
    manager.log(f"Deleted master {Path(master['path']).name}", "warn")
    return {"deleted": True, "master": master}


@app.get("/api/calibration/match")
def match_masters(rig: str | None = None,
                  exposure: float = Query(default=0.0, ge=0, le=3600),
                  binning: int = Query(default=0, ge=0, le=8),
                  filter: str = Query(default="", max_length=24)) -> dict[str, Any]:
    """What would be applied to a frame like this, and why.

    The point is the "why": "no master flat" and "the only master flat is
    fourteen months old" are both refusals and they need different answers.
    """
    target = _rig(rig)
    camera = target.manager.get("camera")
    want = {
        "exposure": exposure,
        "binning": binning or (camera.binning if camera is not None else 1),
        "gain": camera.gain if camera is not None else None,
        "offset": camera.offset if camera is not None else None,
        "temperature": camera.temperature if camera is not None else None,
        "telescope": target.name,
        "camera": camera.name if camera is not None else "",
        "filter": filter,
    }
    wheel = target.manager.get("filterwheel")
    if not filter and wheel is not None and wheel.connected:
        names, position = wheel.names, wheel.position
        if 0 <= position < len(names):
            want["filter"] = names[position]
    if camera is not None and camera.connected and camera.sensor_width:
        want["width"] = camera.sensor_width // max(1, want["binning"])
        want["height"] = camera.sensor_height // max(1, want["binning"])

    found = _guard(library.plan_for, want)
    return {
        "want": want,
        "rig": target.id,
        "telescope": target.name,
        "reasons": found["reasons"],
        "masters": {kind: (m and {"id": m["id"], "name": Path(m["path"]).name,
                                  "frames": m["frames"],
                                  "temperature": m["temperature"],
                                  "created": m["created"]})
                    for kind, m in found["masters"].items()},
        "usable": found["usable"],
    }


@app.get("/api/calibration/flatspot")
def flat_spot() -> dict[str, Any]:
    """Where sky flats would point right now, and how the twilight is going."""
    latitude, longitude = _require_site()
    spot = _guard(calibrator.flat_spot, latitude, longitude)
    now = time.time()
    return {
        **spot,
        "sunAltitude": round(astro.sun_altitude(now, latitude, longitude), 2),
        "mount": (rigs.master.manager.get("mount") is not None
                  and rigs.master.manager.get("mount").connected),
    }


@app.get("/api/calibration/recipes")
def list_recipes() -> dict[str, Any]:
    return {"recipes": recipes.listing()}


@app.get("/api/calibration/recipes/suggested")
def suggest_recipe() -> dict[str, Any]:
    """A starting recipe built from what is actually connected."""
    return _guard(calibrating.suggested_recipe, rigs, config)


@app.post("/api/calibration/recipes")
def save_recipe(body: CalibrationRecipeRequest) -> dict[str, Any]:
    recipe = _guard(recipes.save_recipe, body.name,
                    [s.model_dump() for s in body.sets], body.id)
    manager.log(f"Saved the calibration recipe {recipe['name']}")
    return {"recipe": recipe}


@app.delete("/api/calibration/recipes/{recipe_id}")
def delete_recipe(recipe_id: str) -> dict[str, Any]:
    _guard(recipes.remove, recipe_id)
    return {"deleted": True}


def _recipe_from(body: CalibrationRunRequest) -> dict[str, Any]:
    if body.recipeId:
        return _guard(recipes.get, body.recipeId)
    if body.sets:
        return {"name": body.name or "Calibration",
                "sets": [s.model_dump() for s in body.sets]}
    raise HTTPException(status_code=400,
                        detail="say which recipe to run, or give the sets to run")


@app.post("/api/calibration/run")
def run_calibration(body: CalibrationRunRequest) -> dict[str, Any]:
    """Shoot a recipe now, from the Calibrate tab."""
    if sequencer.owns_the_cameras:
        raise HTTPException(
            status_code=400,
            detail="the sequence is using the cameras; pause or stop it first")
    # A warm-down is not a reason to refuse; it is a thing to stop. Darks shot
    # up a temperature ramp match nothing in the library, so the cameras go back
    # on their setpoint first.
    warming = sequencer.cancel_warming("a calibration run was started")
    recipe = _recipe_from(body)
    _guard(calibrator.start, recipe, body.rigId or None)
    return {"started": True, "run": calibrator.status(), "cancelledWarming": warming}


@app.post("/api/calibration/abort")
def abort_calibration() -> dict[str, Any]:
    _guard(calibrator.abort)
    return {"aborted": True}


@app.get("/api/calibration/run")
def calibration_run_status() -> dict[str, Any]:
    return calibrator.status()


# ---------------------------------------------------------------------------
# The sequencer
# ---------------------------------------------------------------------------

@app.get("/api/sequence")
def sequence_status() -> dict[str, Any]:
    return sequencer.status()


@app.post("/api/sequence/start")
def sequence_start() -> dict[str, Any]:
    _guard(sequencer.start)
    return {"started": True}


@app.post("/api/sequence/loop")
def sequence_loop() -> dict[str, Any]:
    """Run the plan tonight and every night after, unattended.

    Each night opens at dusk with the mount released and homed and the
    cover open, and closes at dawn or when the plan runs out with the cover
    shut, the mount homed and parked, the roof closed and the cameras warm.
    Stop ends it; so does Abort & park.
    """
    _guard(sequencer.start, loop=True)
    manager.log("Sequence started on loop: night after night until stopped")
    return {"started": True, "loop": True}


@app.post("/api/sequence/pause")
def sequence_pause() -> dict[str, Any]:
    sequencer.pause()
    return {"paused": True}


@app.post("/api/sequence/resume")
def sequence_resume() -> dict[str, Any]:
    sequencer.resume()
    return {"resumed": True}


@app.post("/api/sequence/skip")
def sequence_skip() -> dict[str, Any]:
    sequencer.skip()
    return {"skipped": True}


@app.post("/api/sequence/stop")
def sequence_stop() -> dict[str, Any]:
    sequencer.stop()
    return {"stopped": True}


#: The shutdown in progress, if any. Like the guider's, it takes minutes — a
#: park can be a slew across the sky — so it is started and then watched.
shutdown_task: dict[str, Any] = {"busy": False, "since": 0.0,
                                 "done": [], "failed": {}}


@app.post("/api/sequence/shutdown")
def sequence_shutdown() -> dict[str, Any]:
    """Stop, park, close the cover and warm the cameras.

    Separate from Stop, which ends the run and leaves the rig where it is — that
    is right for "enough for tonight", when you may well want to look at
    something by hand afterwards. This is the other one: put the observatory to
    bed without anybody else touching it.

    Returns at once; the outcome arrives on the status socket, and each step is
    attempted whatever the one before it did.
    """
    if shutdown_task["busy"]:
        raise HTTPException(status_code=409, detail="already shutting down")
    shutdown_task.update({"busy": True, "since": time.time(),
                          "done": [], "failed": {}})

    def run() -> None:
        try:
            result = sequencer.shut_down()
            shutdown_task.update(result)
        except Exception as exc:                   # noqa: BLE001 - never fatal
            shutdown_task["failed"] = {"shut down": str(exc)}
            rigs.log(f"Shutdown failed: {exc}", "error")
        finally:
            shutdown_task["busy"] = False

    threading.Thread(target=run, daemon=True, name="shutdown").start()
    return {"started": True}


@app.post("/api/focus/run")
def focus_run(rig: str | None = None, all: bool = False) -> dict[str, Any]:
    """Focus once, now.  Refused while the sequencer owns the cameras.

    Every telescope has its own focuser and its own sweep.  `all=true` runs them
    together the way the sequencer does, which is what you want before a run:
    while one telescope is deliberately defocused nothing else on the mount is
    taking a usable frame anyway.
    """
    # Only refuse while the sequencer is actually taking frames. Pausing or
    # stopping it frees the cameras immediately, even though its thread stays
    # alive afterwards to warm them down.
    if sequencer.owns_the_cameras:
        raise HTTPException(status_code=400,
                            detail="the sequencer is imaging; pause it first")
    sequencer.cancel_warming("a focus run was started")

    chosen = rigs.all if all else [_rig(rig)]
    wanted = []
    # Why each telescope was left out.  "Focus all" used to skip an unready
    # telescope in silence, so two of three starting looked like a bug in the
    # focusing rather than a focuser that was never connected.  Nothing is
    # dropped without saying so.
    skipped: list[dict[str, str]] = []

    def skip(telescope, reason: str) -> None:
        skipped.append({"rig": telescope.id, "name": telescope.name,
                        "reason": reason})
        telescope.manager.log(f"Autofocus skipped: {reason}", "warn")

    for telescope in chosen:
        focuser = telescope.manager.get("focuser")
        if focuser is None or not focuser.connected:
            if not all:
                raise HTTPException(status_code=400,
                                    detail=f"{telescope.name} has no focuser connected")
            skip(telescope, f"{telescope.name} has no focuser connected")
            continue
        camera = telescope.manager.get("camera")
        if camera is None or not camera.connected:
            if not all:
                raise HTTPException(status_code=400,
                                    detail=f"{telescope.name} has no camera connected")
            # A sweep measures stars, so it needs the camera as much as the
            # focuser.  Worth its own message: the two are connected separately
            # and it is the camera that is usually the one missing.
            skip(telescope, f"{telescope.name} has no camera connected")
            continue
        if telescope.focuser.running:
            if not all:
                raise HTTPException(status_code=400,
                                    detail=f"{telescope.name} is already focusing")
            # One telescope still sweeping is no reason to refuse the others.
            skip(telescope, f"{telescope.name} is already focusing")
            continue
        wanted.append(telescope)

    if not wanted:
        detail = ("; ".join(entry["reason"] for entry in skipped)
                  or "no telescope has a focuser connected")
        raise HTTPException(status_code=400, detail=detail)

    def job(telescope) -> None:
        # A frame that was already being exposed when the sequence was stopped
        # takes a moment to unwind. Waiting a few seconds for it is far better
        # than refusing: the operator asked to focus, and by the time they could
        # press the button again the camera would be free anyway.
        deadline = time.monotonic() + 30.0
        if telescope.capture.busy:
            telescope.manager.log("Waiting for the current exposure to finish "
                                  "before focusing")
        while telescope.capture.busy and time.monotonic() < deadline:
            time.sleep(0.25)
        if telescope.capture.busy:
            telescope.manager.log(
                "Autofocus gave up waiting for the camera; abort the exposure "
                "and try again", "error")
            return
        try:
            telescope.focuser.run(report=lambda text: telescope.manager.log(text))
        except Exception as exc:                # noqa: BLE001 - reported in the log
            telescope.manager.log(f"Autofocus failed: {exc}", "error")

    for telescope in wanted:
        threading.Thread(target=job, args=(telescope,), daemon=True,
                         name=f"focus-{telescope.id}").start()
    if skipped:
        rigs.log("Autofocus started on "
                 + ", ".join(t.name for t in wanted)
                 + " — left out: "
                 + "; ".join(entry["reason"] for entry in skipped), "warn")
    return {"started": True, "telescopes": [t.id for t in wanted],
            "names": [t.name for t in wanted], "skipped": skipped}


@app.post("/api/focus/offsets/run")
def focus_offsets_run(body: OffsetRunRequest, rig: str | None = None) -> dict[str, Any]:
    """Measure how far apart the filters focus, and keep the answer.

    A sweep per filter per pass, so it costs real time — but it buys every
    filter change for the rest of the season, because a change becomes a focuser
    move of known size instead of another sweep.
    """
    if sequencer.owns_the_cameras:
        raise HTTPException(status_code=400,
                            detail="the sequencer is imaging; pause it first")
    sequencer.cancel_warming("a filter offset run was started")
    target = _rig(rig)
    _guard(offsets_run.start, target, body.filters, body.passes, body.reference)
    return {"started": True, "run": offsets_run.status()}


@app.post("/api/focus/offsets/abort")
def focus_offsets_abort() -> dict[str, Any]:
    offsets_run.abort()
    return {"aborted": True}


@app.get("/api/focus/offsets")
def focus_offsets_status() -> dict[str, Any]:
    return offsets_run.status()


@app.post("/api/focus/abort")
def focus_abort(rig: str | None = None, all: bool = False) -> dict[str, Any]:
    """Stop a focus sweep. The focuser goes back where it started."""
    chosen = rigs.all if all else [_rig(rig)]
    stopped = []
    for telescope in chosen:
        if telescope.focuser.running:
            telescope.focuser.abort()
            telescope.manager.log("Autofocus aborted", "warn")
            stopped.append(telescope.id)
    return {"aborted": stopped}


@app.get("/api/focus")
def focus_status(rig: str | None = None) -> dict[str, Any]:
    return _rig(rig).focuser.status()


# ---------------------------------------------------------------------------
# Telescopes and equipment profiles
# ---------------------------------------------------------------------------

def _shared_settings() -> dict[str, dict[str, Any]]:
    """The settings that belong to the observatory, not to one telescope."""
    return {name: config.section(name) for name in SHARED_SECTIONS}


def _equipment_payload() -> dict[str, Any]:
    return {
        "telescopes": [rig.definition() for rig in rigs.all],
        "masterRig": rigs.master.id,
        "profiles": equipment.profiles(),
        "activeProfile": equipment.active(),
        "rigKinds": {rig.id: list(rig.kinds) for rig in rigs.all},
    }


@app.get("/api/equipment")
def get_equipment() -> dict[str, Any]:
    return _equipment_payload()


# ---------------------------------------------------------------------------
# Coming over from N.I.N.A.
# ---------------------------------------------------------------------------

class NinaImportRequest(BaseModel):
    profile: str = Field(min_length=1, max_length=64)
    # Which sections to write. Empty means every one the profile offers.
    sections: list[str] = Field(default_factory=list, max_length=20)
    devices: bool = True


def _nina_profile_path(profile_id: str) -> Path:
    for row in ninaimport.profiles():
        if row["id"] == profile_id:
            return Path(row["path"])
    raise HTTPException(status_code=404, detail="no such N.I.N.A. profile")


@app.get("/api/nina/profiles")
def nina_profiles() -> dict[str, Any]:
    """Every N.I.N.A. profile on this machine, most recently used first."""
    rows = ninaimport.profiles()
    for row in rows:
        row["lastUsedText"] = ninaimport.when(row["lastUsed"])
    return {"profiles": rows, "folder": str(ninaimport.PROFILE_DIR),
            "found": bool(rows)}


@app.get("/api/nina/preview")
def nina_preview(profile: str = Query(min_length=1, max_length=64),
                 rig: str | None = None) -> dict[str, Any]:
    """What importing this profile would set, before anything is written."""
    target = _rig(rig)
    try:
        plan = ninaimport.read(_nina_profile_path(profile))
    except ninaimport.NinaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Beside each value, what this telescope has now, so the dialog can say
    # what changes rather than only what arrives.
    current: dict[str, dict[str, Any]] = {}
    for section, values in plan["settings"].items():
        have = target.config.section(section)
        current[section] = {key: have.get(key) for key in values}
    # A credential is shown as present, never as itself.
    if (plan["settings"].get("solver") or {}).get("astrometryKey"):
        plan["settings"]["solver"]["astrometryKey"] = "***"
    plan["current"] = current
    plan["rig"] = target.id
    plan["rigName"] = target.name
    return plan


@app.post("/api/nina/import")
def nina_import(body: NinaImportRequest, rig: str | None = None) -> dict[str, Any]:
    """Bring a N.I.N.A. profile's settings and device slots across.

    Devices are remembered, not connected: the slots are set the way choosing
    each driver in the dialog sets them, and Connect brings them up when the
    person is ready. Nothing here touches hardware.
    """
    if sequencer.running:
        raise HTTPException(status_code=400,
                            detail="the sequence is running; stop it first")
    target = _rig(rig)
    try:
        plan = ninaimport.read(_nina_profile_path(body.profile))
        done = ninaimport.apply(plan, config, target, equipment,
                                body.sections or None, body.devices)
    except ninaimport.NinaError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rigs.sync()
    collab_client.start()
    names = (plan["settings"].get("camera") or {}).get("filterNames")
    rigs.log(f"Imported the N.I.N.A. profile {plan['profile']['name']!r} onto "
             f"{target.name}: {sum(len(v) for v in done['settings'].values())} "
             f"settings"
             + (f", filters {', '.join(names)}" if names else "")
             + (f", drivers for {', '.join(done['devices'])}" if done["devices"] else ""),
             "success")
    return {**done, "profile": plan["profile"], "leftOut": plan["leftOut"],
            "equipment": _equipment_payload()}


@app.post("/api/equipment/telescopes")
def add_telescope(body: TelescopeRequest) -> dict[str, Any]:
    rig = _guard(equipment.add_rig, body.name)
    rigs.sync()
    rigs.log(f"Telescope added: {rig['name']}", "success")
    return _equipment_payload()


@app.post("/api/equipment/telescopes/{rig_id}")
def edit_telescope(rig_id: str, body: TelescopeEditRequest) -> dict[str, Any]:
    if body.name is not None:
        _guard(equipment.rename_rig, rig_id, body.name)
    if body.master:
        if sequencer.running:
            raise HTTPException(
                status_code=400,
                detail="the sequence is running; stop it before moving the mount "
                       "to another telescope")
        _guard(equipment.set_master, rig_id)
    rigs.sync()
    if body.master:
        rigs.log(f"{rigs.master.name} is now the master telescope", "success")
    return _equipment_payload()


@app.delete("/api/equipment/telescopes/{rig_id}")
def remove_telescope(rig_id: str) -> dict[str, Any]:
    if sequencer.running:
        raise HTTPException(status_code=400,
                            detail="the sequence is running; stop it first")
    name = _guard(rigs.get, rig_id).name
    _guard(equipment.remove_rig, rig_id)
    rigs.sync()                             # closes the connections it had open
    rigs.log(f"Telescope removed: {name}", "warn")
    return _equipment_payload()


@app.post("/api/equipment/telescopes/{rig_id}/devices/{kind}")
def remember_device(rig_id: str, kind: str,
                    body: RememberDeviceRequest) -> dict[str, Any]:
    """Say what a slot connects to, without connecting it now.

    Which device a telescope wants is worth settling in the afternoon with
    everything switched off — and when three telescopes share a driver, each
    one's device id has to be recorded before any of them comes up.
    """
    target = _rig(rig_id)
    _guard(equipment.set_device, target.id, kind,
           {"backend": body.backend, "driverId": body.driverId,
            "name": body.name or body.driverId,
            "options": body.options or {}})
    pinned = ", ".join(f"{key}={value}" for key, value
                       in sorted((body.options or {}).items()))
    target.manager.log(f"{kind}: will use {body.driverId}"
                       + (f" with {pinned}" if pinned else ""))
    return _equipment_payload()


@app.delete("/api/equipment/telescopes/{rig_id}/devices/{kind}")
def forget_device(rig_id: str, kind: str) -> dict[str, Any]:
    """Leave a slot empty.

    A telescope with no rotator should not have one remembered: Connect all
    would report it as a failure every night, and a profile would carry it to
    every rig it was ever loaded on.
    """
    target = _rig(rig_id)
    device = target.manager.get(kind)
    if device is not None and device.connected:
        _guard(target.manager.disconnect, kind)
    _guard(equipment.set_device, target.id, kind, None)
    return _equipment_payload()


@app.post("/api/equipment/telescopes/{rig_id}/connect")
def connect_telescope(rig_id: str) -> dict[str, Any]:
    """Connect everything this telescope remembers, in one go.

    What a profile is really for: at the start of a night the whole rig comes up
    from what it was last connected to, rather than eight dropdowns.
    """
    rig = _rig(rig_id)
    outcome = _guard(rigs.connect_remembered, rig)
    for message in outcome["failed"]:
        rig.manager.log(message, "error")
    if outcome["connected"]:
        rig.manager.log(f"Connected {', '.join(outcome['connected'])}", "success")
    return {**outcome, "rig": rig.id}


@app.post("/api/equipment/connect")
def connect_everything() -> dict[str, Any]:
    """Connect every telescope's remembered drivers.

    The whole observatory from one button at the start of the night, which is
    what the remembered drivers are for.
    """
    results = {}
    connected = 0
    failed = 0
    for rig in rigs.all:
        outcome = rigs.connect_remembered(rig)
        for message in outcome["failed"]:
            rig.manager.log(message, "error")
        if outcome["connected"]:
            rig.manager.log(f"Connected {', '.join(outcome['connected'])}", "success")
        connected += len(outcome["connected"])
        failed += len(outcome["failed"])
        results[rig.id] = {**outcome, "name": rig.name}
    if not connected and not failed:
        rigs.log("Nothing is remembered yet — connect each device once and it "
                 "will come back on its own next time", "warn")
    return {"telescopes": results, "connected": connected, "failed": failed}


@app.post("/api/equipment/disconnect")
def disconnect_everything() -> dict[str, Any]:
    rigs.disconnect_all()
    return {"disconnected": True}


@app.post("/api/equipment/profiles")
def save_profile(body: ProfileRequest) -> dict[str, Any]:
    saved = _guard(equipment.save_profile, body.name, _shared_settings(), body.id)
    rigs.log(f"Equipment profile saved: {saved['name']}", "success")
    return {"profile": {"id": saved["id"], "name": saved["name"]},
            **_equipment_payload()}


@app.post("/api/equipment/profiles/{profile_id}/load")
def load_profile(profile_id: str, connect: bool = False) -> dict[str, Any]:
    """Adopt a profile: its telescopes, their drivers and its settings.

    Everything is disconnected first.  Bringing a profile up on top of live
    connections would leave a driver connected to a telescope the profile does
    not have, which is the one state nothing else in here can describe.
    """
    if sequencer.running:
        raise HTTPException(status_code=400,
                            detail="the sequence is running; stop it first")
    stored = _guard(equipment.profile, profile_id)
    rigs.disconnect_all()

    for section, values in (stored.get("shared") or {}).items():
        if section in SHARED_SECTIONS and isinstance(values, dict):
            with contextlib.suppress(Exception):
                config.update(section, values)
    _guard(equipment.adopt_profile, profile_id)
    rigs.sync()
    # The profile may name a different calibration folder, so what was held in
    # memory describes the wrong library.
    library.forget()
    plan.prune_rigs({rig.id for rig in rigs.all})
    rigs.log(f"Equipment profile loaded: {stored.get('name')}  "
             f"({len(rigs.all)} telescope(s))", "success")

    if connect:
        for rig in rigs.all:
            outcome = rigs.connect_remembered(rig)
            for message in outcome["failed"]:
                rig.manager.log(message, "error")
    return _equipment_payload()


@app.delete("/api/equipment/profiles/{profile_id}")
def delete_profile(profile_id: str) -> dict[str, Any]:
    _guard(equipment.delete_profile, profile_id)
    return _equipment_payload()


# ---------------------------------------------------------------------------
# Live status
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def status_socket(socket: WebSocket) -> None:
    await socket.accept()
    try:
        while True:
            payload = await asyncio.to_thread(_snapshot)
            await socket.send_json(payload)
            await asyncio.sleep(0.4)
    except WebSocketDisconnect:
        pass
    except Exception:
        with contextlib.suppress(Exception):
            await socket.close()


@app.on_event("shutdown")
def _shutdown() -> None:
    with contextlib.suppress(Exception):
        safety_watcher.stop()
    with contextlib.suppress(Exception):
        pier.stop()
    with contextlib.suppress(Exception):
        sequencer.stop()
    for rig in rigs.all:
        with contextlib.suppress(Exception):
            rig.solver.abort()
        with contextlib.suppress(Exception):
            rig.capture.abort()
    rigs.disconnect_all()


class _RevalidatingStatics(StaticFiles):
    """Serve the interface with revalidation forced.

    Starlette sends only ETag and Last-Modified for static files, which leaves
    the browser free to apply heuristic freshness and go on serving an old
    app.js or sky.js long after an update.  In a desktop application that ships
    its own front end that is never what you want: the embedded WebView will
    happily pair a new index.html with a stale script and produce failures that
    exist nowhere on disk.  `no-cache` still permits 304s, so revalidation over
    loopback costs nothing.
    """

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/", _RevalidatingStatics(directory=str(WEB_DIR), html=True), name="web")
