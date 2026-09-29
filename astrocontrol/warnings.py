"""Everything that could quietly go wrong, said out loud.

A rig fails in two ways. Loudly - an exception, a device that refuses - which
the sequencer already reports. And quietly: the cooler that never reached
its setpoint, the mount whose site is a hundred miles from the observatory,
the cover that stayed shut, the disk with two frames' worth of room, the
frames that have had no stars in them since the cloud came over. Nothing
throws, the night runs to the end, and the morning finds it was wasted.

This is the board those go on. A watchdog looks at the whole rig every few
seconds and keeps a live list: each thing wrong is one entry with a level, a
plain sentence saying what, and where possible a sentence saying what to do.
Entries clear themselves when the condition goes away, so the list is always
what is wrong *now*. The strip at the top of the window shows it; anything
critical also goes out as a notification, once, so somebody not in the room
hears about it while there is still night left.

The checks are plain functions over a `Context` - the live objects the
program holds - and each is wrapped so that a check that breaks can never
take the watchdog with it.
"""

from __future__ import annotations

import contextlib
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import filters as filternames

LEVELS = ("critical", "warning", "notice")
RANK = {level: index for index, level in enumerate(LEVELS)}

#: How often the watchdog looks.
INTERVAL = 15.0
#: A critical warning is sent as a notification when it first appears, and
#: not again for this long even if it clears and comes back.
NOTIFY_AGAIN_SECONDS = 3600.0


@dataclass
class Warning:
    key: str
    level: str
    title: str
    detail: str = ""
    fix: str = ""
    since: float = field(default_factory=time.time)
    last: float = field(default_factory=time.time)
    sticky: bool = False
    acknowledged: bool = False

    def payload(self) -> dict[str, Any]:
        return {"key": self.key, "level": self.level, "title": self.title,
                "detail": self.detail, "fix": self.fix, "since": self.since,
                "ageSeconds": round(time.time() - self.since),
                "sticky": self.sticky, "acknowledged": self.acknowledged}


class WarningBoard:
    """The live list of what is wrong, and who has been told."""

    def __init__(self, log: Any = None, notifier: Any = None) -> None:
        self._log = log
        self.notifier = notifier
        self._lock = threading.RLock()
        self._active: dict[str, Warning] = {}
        self._raised_this_pass: set[str] = set()
        self._notified: dict[str, float] = {}

    # -- a pass ------------------------------------------------------------
    def begin(self) -> None:
        with self._lock:
            self._raised_this_pass = set()

    def raise_(self, key: str, level: str, title: str, detail: str = "",
               fix: str = "", sticky: bool = False) -> None:
        """Put a condition on the board, or refresh it if it is there.

        A warning that changes level is told again; one that merely persists
        is not. Sticky warnings stay until cleared by name: they mark an
        event (a flip that did not flip) rather than a state the watchdog
        can see for itself.
        """
        if level not in RANK:
            level = "warning"
        with self._lock:
            self._raised_this_pass.add(key)
            current = self._active.get(key)
            if current is None:
                entry = Warning(key=key, level=level, title=title, detail=detail,
                                fix=fix, sticky=sticky)
                self._active[key] = entry
                self._say(entry, "new")
                self._notify(entry)
                return
            changed = current.level != level
            current.level = level
            current.title = title
            current.detail = detail
            current.fix = fix
            current.last = time.time()
            current.sticky = sticky or current.sticky
            if changed:
                current.acknowledged = False
                current.since = time.time()
                self._say(current, "changed")
                self._notify(current)

    def end(self) -> None:
        """Clear everything the pass did not raise: the condition has gone."""
        with self._lock:
            for key in list(self._active):
                entry = self._active[key]
                if entry.sticky or key in self._raised_this_pass:
                    continue
                del self._active[key]
                if self._log is not None:
                    self._log(f"Cleared: {entry.title}", "info")

    def clear(self, key: str) -> bool:
        with self._lock:
            entry = self._active.pop(key, None)
        if entry is not None and self._log is not None:
            self._log(f"Cleared: {entry.title}", "info")
        return entry is not None

    def clear_sticky(self) -> None:
        """Forget the event warnings, for a run that is starting afresh."""
        with self._lock:
            for key in [k for k, v in self._active.items() if v.sticky]:
                del self._active[key]

    def acknowledge(self, key: str) -> bool:
        """Somebody has seen it: keep it on the list, off the strip."""
        with self._lock:
            entry = self._active.get(key)
            if entry is None:
                return False
            entry.acknowledged = True
            return True

    def age(self, key: str) -> float:
        """How long a warning has been on the board, 0 if it is not."""
        with self._lock:
            entry = self._active.get(key)
            return (time.time() - entry.since) if entry else 0.0

    # -- reading it --------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = sorted(self._active.values(),
                            key=lambda w: (RANK[w.level], w.since))
            counts = {level: 0 for level in LEVELS}
            unacknowledged = {level: 0 for level in LEVELS}
            for entry in active:
                counts[entry.level] += 1
                if not entry.acknowledged:
                    unacknowledged[entry.level] += 1
            return {"active": [w.payload() for w in active],
                    "counts": counts, "unacknowledged": unacknowledged,
                    "worst": active[0].level if active else None}

    # -- telling people ----------------------------------------------------
    def _say(self, entry: Warning, how: str) -> None:
        if self._log is None:
            return
        level = "error" if entry.level == "critical" else (
            "warn" if entry.level == "warning" else "info")
        self._log(f"{entry.title}" + (f" — {entry.detail}" if entry.detail else ""),
                  level)

    def _notify(self, entry: Warning) -> None:
        if self.notifier is None or entry.level != "critical":
            return
        last = self._notified.get(entry.key, 0.0)
        if time.time() - last < NOTIFY_AGAIN_SECONDS:
            return
        self._notified[entry.key] = time.time()
        with contextlib.suppress(Exception):
            self.notifier.send("warning", entry.title,
                               (entry.detail + ("\n\n" + entry.fix if entry.fix else "")).strip())


# ---------------------------------------------------------------------------
# What the checks look at
# ---------------------------------------------------------------------------

@dataclass
class Context:
    """The live program, as the checks see it. Everything optional, so a
    check written against a full rig degrades to silence on a bare one."""

    config: Any
    rigs: Any = None
    sequencer: Any = None
    plan: Any = None
    targets: Any = None
    library: Any = None
    calibrator: Any = None
    collab: Any = None
    safety: Any = None
    notifier: Any = None
    #: A callable returning the calibration coverage rows for the master, or
    #: None: `main` knows how to build the needs, this module does not.
    coverage: Callable[[], dict[str, Any]] | None = None
    #: A callable returning, for a plan entry, minutes it is observable
    #: tonight (None when unknown).
    observable_minutes: Callable[[dict[str, Any]], float | None] | None = None
    #: How dark it is: a callable returning (dusk, dawn) timestamps for the
    #: night that counts, or None.
    night: Callable[[], tuple[float | None, float | None] | None] | None = None

    def device(self, kind: str, rig: Any = None) -> Any:
        rig = rig or (self.rigs.master if self.rigs is not None else None)
        if rig is None:
            return None
        with contextlib.suppress(Exception):
            device = rig.manager.get(kind)
            if device is not None and getattr(device, "connected", False):
                return device
        return None

    def section(self, name: str) -> dict[str, Any]:
        with contextlib.suppress(Exception):
            return self.config.section(name)
        return {}

    def setting(self, section: str, key: str, default: Any = None) -> Any:
        with contextlib.suppress(Exception):
            return self.config.get(section, key, default)
        return default

    def run(self) -> dict[str, Any]:
        with contextlib.suppress(Exception):
            return self.sequencer.status() if self.sequencer is not None else {}
        return {}

    def entries(self) -> list[dict[str, Any]]:
        with contextlib.suppress(Exception):
            return list(self.plan.raw().get("entries") or []) if self.plan else []
        return []

    def light_entries(self) -> list[dict[str, Any]]:
        return [e for e in self.entries() if (e.get("kind") or "target") != "calibration"]

    def live(self) -> bool:
        """Whether the night is on, or about to be: the sequence running, or
        a plan with targets and dark within the hour. Device checks that
        would nag all afternoon only speak when it is."""
        run = self.run()
        if run.get("running"):
            return True
        if not self.light_entries():
            return False
        if self.night is None:
            return False
        with contextlib.suppress(Exception):
            found = self.night()
            if found:
                dusk, dawn = found
                now = time.time()
                if dusk and dawn and dusk - 3600.0 <= now <= dawn:
                    return True
        return False

    def imaging(self) -> bool:
        run = self.run()
        return bool(run.get("running")) and run.get("state") in ("imaging", "exposing")


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

Check = Callable[[WarningBoard, Context], None]
CHECKS: list[Check] = []


def check(fn: Check) -> Check:
    CHECKS.append(fn)
    return fn


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


# -- the site and the plan -------------------------------------------------

@check
def site_unset(board: WarningBoard, ctx: Context) -> None:
    if not ctx.light_entries():
        return
    site = ctx.section("site")
    if site.get("latitude") is None:
        board.raise_("site.unset", "critical", "The observing site is not set",
                     "Without it nothing can be planned: no rise, no set, no dark.",
                     "Open Site & Optics and set the latitude and longitude, or "
                     "connect a mount that knows them.")


@check
def mount_site_disagrees(board: WarningBoard, ctx: Context) -> None:
    mount = ctx.device("mount")
    if mount is None:
        return
    site = ctx.section("site")
    if site.get("latitude") is None or not site.get("useMount", True):
        return
    with contextlib.suppress(Exception):
        reported = mount.site
        if not reported or reported.get("latitude") is None:
            return
        dlat = abs(float(reported["latitude"]) - float(site["latitude"]))
        dlon = abs(float(reported.get("longitude") or 0.0) - float(site.get("longitude") or 0.0))
        if dlat > 0.5 or dlon > 0.5:
            board.raise_("mount.site", "critical",
                         "The mount's site is not where the observatory is",
                         f"The mount says {reported['latitude']:.2f}°, "
                         f"{reported.get('longitude', 0):.2f}° and Site & Optics says "
                         f"{float(site['latitude']):.2f}°, {float(site.get('longitude') or 0):.2f}°. "
                         "Every slew and every plan is wrong by the difference.",
                         "Set the mount's location in its driver, or untick "
                         "'take the site from the mount' in Site & Optics.")


@check
def plan_tonight(board: WarningBoard, ctx: Context) -> None:
    run = ctx.run()
    entries = ctx.light_entries()
    if run.get("loop") and not ctx.entries():
        board.raise_("plan.empty", "warning", "The plan is empty",
                     "Run on loop will open the night and find nothing to shoot.",
                     "Add targets on the Plan tab.")
        return
    if not entries or not ctx.live():
        return
    usable = 0
    for entry in entries:
        options = entry.get("options") or {}
        if options.get("enabled") is False:
            continue
        if not any(int(f.get("count") or 0) > 0 for f in (entry.get("filters") or [])):
            continue
        if ctx.observable_minutes is not None:
            with contextlib.suppress(Exception):
                minutes = ctx.observable_minutes(entry)
                if minutes is not None and minutes <= 0:
                    continue
        usable += 1
    if usable == 0:
        board.raise_("plan.nothing", "warning", "Nothing on the plan can be shot tonight",
                     "Every target is skipped, has no frames, or is not up during the dark.",
                     "Check the Plan tab: frames on each target, and the night graph.")


@check
def plan_filters_known(board: WarningBoard, ctx: Context) -> None:
    if not ctx.live():
        return
    known: set[str] = set()
    wheel = ctx.device("filterwheel")
    if wheel is not None:
        with contextlib.suppress(Exception):
            known = {filternames.canonical(n) for n in (wheel.names or []) if str(n).strip()}
    if not known:
        with contextlib.suppress(Exception):
            master = ctx.rigs.master
            known = {filternames.canonical(n)
                     for n in (master.config.get("camera", "filterNames", []) or [])
                     if str(n).strip()}
            fixed = str(master.config.get("camera", "fixedFilter", "") or "").strip()
            if fixed:
                known.add(filternames.canonical(fixed))
    if not known:
        return
    missing: set[str] = set()
    for entry in ctx.light_entries():
        for item in entry.get("filters") or []:
            if int(item.get("count") or 0) <= 0:
                continue
            name = filternames.canonical(item.get("name") or "")
            if name and name not in known:
                missing.add(item.get("name") or name)
    if missing:
        board.raise_("plan.filters", "critical",
                     "The plan asks for a filter this telescope does not have",
                     f"{', '.join(sorted(missing))} is on the plan; the wheel carries "
                     f"{', '.join(sorted(known))}.",
                     "Change the plan's filters, or name the wheel's slots under Equipment.")


# -- the camera --------------------------------------------------------------

@check
def camera_present(board: WarningBoard, ctx: Context) -> None:
    if not ctx.live() or not ctx.entries():
        return
    if ctx.device("camera") is None:
        board.raise_("camera.missing", "critical", "No camera is connected",
                     "The night is on and there is nothing to take frames with.",
                     "Open Equipment and connect the camera.")


@check
def camera_cooling(board: WarningBoard, ctx: Context) -> None:
    camera = ctx.device("camera")
    if camera is None or not getattr(camera, "can_cool", False):
        return
    with contextlib.suppress(Exception):
        if not camera.cooler_on:
            return
        setpoint = _num(camera.setpoint)
        temperature = _num(camera.temperature)
        if setpoint is None or temperature is None:
            return
        tolerance = float(ctx.setting("camera", "coolToleranceC", 1.0) or 1.0)
        gap = temperature - setpoint
        power = _num(getattr(camera, "cooler_power", None))
        if gap > tolerance + 2.0:
            minutes = float(ctx.setting("warnings", "coolingMinutes", 15.0) or 15.0)
            key = "camera.cooling"
            waited = board.age(key) / 60.0
            hard = power is not None and power >= 95.0 and waited >= minutes
            level = "warning" if hard or waited >= minutes else "notice"
            board.raise_(key, level,
                         "The camera is not at its setpoint"
                         if not hard else "The camera cannot reach its setpoint",
                         f"{temperature:.1f} C against a setpoint of {setpoint:.0f} C"
                         + (f", cooler at {power:.0f}%" if power is not None else "")
                         + (f", for {waited:.0f} minutes" if waited >= 1 else "") + ".",
                         "Raise the setpoint a few degrees so the darks match, or wait; "
                         "a cooler at full power on a warm night will never get there."
                         if hard else "")


@check
def camera_darks_match(board: WarningBoard, ctx: Context) -> None:
    if not ctx.live() or ctx.coverage is None:
        return
    if str(ctx.setting("calibration", "applyTo", "survey") or "off").lower() == "off":
        return
    with contextlib.suppress(Exception):
        found = ctx.coverage()
        rows = found.get("rows") or []
        dark_missing = [r for r in rows if r["kind"] == "dark" and r["state"] != "ok"]
        if dark_missing and rows:
            what = "; ".join(f"{r['label']}: {r['state']}" for r in dark_missing[:3])
            board.raise_("library.darks", "warning",
                         "Tonight's frames have no matching dark",
                         f"{what}. Frames will be filed uncalibrated.",
                         "Shoot the darks on the Calibrate tab, or check the camera's "
                         "temperature against the library's.")


@check
def frames_not_saved(board: WarningBoard, ctx: Context) -> None:
    run = ctx.run()
    if not run.get("running") or ctx.rigs is None:
        return
    with contextlib.suppress(Exception):
        capture = ctx.rigs.master.capture
        if run.get("state") in ("imaging", "exposing") and not capture.save_enabled:
            board.raise_("capture.unsaved", "critical", "Frames are not being saved",
                         "The sequence is exposing and saving is switched off; "
                         "every frame is lost as it lands.",
                         "Turn saving back on in the camera panel.")


@check
def camera_hung(board: WarningBoard, ctx: Context) -> None:
    if ctx.rigs is None:
        return
    with contextlib.suppress(Exception):
        capture = ctx.rigs.master.capture
        status = capture.status()
        started = capture._exposure_started
        if not started:
            return
        expected = float(status.get("exposureSeconds") or 0.0)
        over = time.time() - started - expected
        if status.get("state") == "exposing" and over > 300.0:
            board.raise_("camera.hung", "critical", "The camera has not finished its exposure",
                         f"An exposure of {expected:g}s started {over / 60 + expected / 60:.0f} "
                         "minutes ago and the camera has not said it is done.",
                         "The driver has probably wedged: Abort, then power-cycle the camera.")
        elif status.get("state") == "downloading" and over > 300.0:
            board.raise_("camera.hung", "critical", "The camera is stuck downloading",
                         f"A frame has been downloading for {over / 60:.0f} minutes.",
                         "The USB link has probably dropped: Abort, then reconnect the camera.")


# -- the mount ---------------------------------------------------------------

@check
def mount_present(board: WarningBoard, ctx: Context) -> None:
    if not ctx.live() or not ctx.light_entries():
        return
    if ctx.device("mount") is None:
        board.raise_("mount.missing", "critical", "No mount is connected",
                     "The plan has targets and nothing to point at them with.",
                     "Open Equipment and connect the mount.")


@check
def mount_tracking(board: WarningBoard, ctx: Context) -> None:
    if not ctx.imaging():
        return
    mount = ctx.device("mount")
    if mount is None:
        return
    with contextlib.suppress(Exception):
        if not mount.tracking and not mount.slewing:
            board.raise_("mount.tracking", "critical", "The mount is not tracking",
                         "A light frame is being taken with the mount stopped; "
                         "every frame is trailed.",
                         "Switch tracking on in the mount panel.")


# -- the focuser -------------------------------------------------------------

@check
def focuser_wanted(board: WarningBoard, ctx: Context) -> None:
    if not ctx.live():
        return
    seq = ctx.section("sequencer")
    wants = (seq.get("autofocusOnStart", True) or seq.get("autofocusOnFilterChange", True)
             or float(seq.get("autofocusIntervalMinutes") or 0) > 0)
    focuser = ctx.device("focuser")
    if wants and focuser is None:
        board.raise_("focuser.missing", "warning", "Autofocus is on but no focuser is connected",
                     "The night will run at whatever focus the telescope was left at.",
                     "Connect the focuser, or turn autofocus off under Equipment → Autofocus.")
        return
    if focuser is None:
        return
    with contextlib.suppress(Exception):
        if float(seq.get("autofocusTemperatureDelta") or 0) > 0 and focuser.temperature is None:
            board.raise_("focuser.temperature", "notice",
                         "The focuser reports no temperature",
                         "Refocusing on temperature change cannot happen; the clock "
                         "and star-size triggers still can.")
    with contextlib.suppress(Exception):
        limit = int(getattr(focuser, "max_step", 0) or 0)
        position = int(focuser.position)
        if limit and (position <= 0 or position >= limit):
            board.raise_("focuser.limit", "warning", "The focuser is at the end of its travel",
                         f"Position {position} of 0–{limit}: a focus sweep cannot move "
                         "past it one way.",
                         "Move the focuser toward the middle of its range and refocus.")


# -- the guider --------------------------------------------------------------

@check
def guider_state(board: WarningBoard, ctx: Context) -> None:
    guider = ctx.device("guider")
    wants = bool(ctx.setting("guiding", "startWithSequence", True))
    if guider is None:
        if wants and ctx.live():
            board.raise_("guider.missing", "warning", "PHD2 is not connected",
                         "The sequence will run unguided.",
                         "Start PHD2 and connect it under Equipment → Guiding.")
        return
    if not ctx.imaging():
        return
    with contextlib.suppress(Exception):
        status = guider.status()
        state = str(status.get("state") or "")
        if state == "LostLock" or status.get("starLost"):
            board.raise_("guider.lost", "warning", "The guide star is lost",
                         str(status.get("starLost") or "PHD2 has no lock on a star."),
                         "Recovery will try to reacquire it; cloud or dew if it keeps happening.")
        elif not guider.guiding and wants:
            board.raise_("guider.idle", "warning", "Not guiding while imaging",
                         f"PHD2 is {state.lower() or 'idle'} and a light frame is running.",
                         "Recovery should start it; if not, press Guide in the guiding panel.")
        else:
            rms = _num(status.get("rmsTotal"))
            limit = float(ctx.setting("warnings", "guideRmsArcsec", 1.5) or 1.5)
            if rms is not None and rms > limit and int(status.get("samples") or 0) >= 20:
                board.raise_("guider.rms", "warning", "Guiding is rough",
                             f'{rms:.2f}" RMS against a limit of {limit:g}".',
                             "Wind, seeing, or a mount that needs its balance or backlash "
                             "looked at. Frames are still taken.")
            snr = _num(status.get("snr"))
            if snr is not None and snr < 8:
                board.raise_("guider.snr", "notice", "The guide star is faint",
                             f"SNR {snr:.0f}; PHD2 may lose it in thin cloud.")


# -- the light path ----------------------------------------------------------

@check
def light_path(board: WarningBoard, ctx: Context) -> None:
    if not ctx.imaging():
        return
    panel = ctx.device("flatpanel")
    if panel is not None:
        with contextlib.suppress(Exception):
            if getattr(panel, "has_cover", False) and panel.cover_state in ("closed", "moving"):
                board.raise_("panel.cover", "critical", "The cover is shut while imaging",
                             f"The flat panel's cover is {panel.cover_state} and a light "
                             "frame is running.",
                             "Open the cover from the flat panel panel; check 'open the "
                             "cover before any light frame' under Calibrate.")
            elif panel.light_on:
                board.raise_("panel.light", "critical", "The flat panel is lit while imaging",
                             "The panel's light is on during a light frame.",
                             "Switch the panel off.")
    dome = ctx.device("dome")
    if dome is not None:
        with contextlib.suppress(Exception):
            if getattr(dome, "can_shutter", False) and dome.shutter_state not in ("open", "notpresent"):
                board.raise_("dome.shutter", "critical", "The roof is not open while imaging",
                             f"The shutter is {dome.shutter_state}.",
                             "Open the roof from the observatory panel.")
            elif getattr(dome, "can_slave", False) and not dome.slaved:
                board.raise_("dome.slave", "notice", "The dome is not following the mount",
                             "Slaving is off; the slit will drift off the telescope.",
                             "Tick 'slave to the mount' in the observatory panel.")


@check
def weather(board: WarningBoard, ctx: Context) -> None:
    if ctx.safety is None:
        return
    with contextlib.suppress(Exception):
        status = ctx.safety.status()
        if status.get("safe") is False:
            board.raise_("safety.unsafe", "critical", "The safety monitor says unsafe",
                         str(status.get("error") or "The sky is not safe to open under."),
                         "Nothing to do but wait; the program acts on it by itself.")
        elif status.get("enabled", True) and not status.get("connected") \
                and ctx.run().get("loop"):
            board.raise_("safety.missing", "warning",
                         "Running on loop with no safety monitor",
                         "Nothing will close the observatory if the weather turns.",
                         "Connect a safety monitor under Equipment, or watch the sky yourself.")


# -- disk and files ------------------------------------------------------------

@check
def disk_space(board: WarningBoard, ctx: Context) -> None:
    if ctx.rigs is None:
        return
    with contextlib.suppress(Exception):
        root = Path(ctx.rigs.master.capture.root_dir)
        probe = root
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        usage = shutil.disk_usage(probe)
        free_gb = usage.free / 1e9
        warn_gb = float(ctx.setting("warnings", "diskWarnGb", 20.0) or 20.0)
        critical_gb = float(ctx.setting("warnings", "diskCriticalGb", 5.0) or 5.0)
        if free_gb < critical_gb:
            level, title = "critical", "The disk is nearly full"
        elif free_gb < warn_gb:
            level, title = "warning", "The disk is getting full"
        else:
            return
        camera = ctx.device("camera")
        per_frame = 0.0
        with contextlib.suppress(Exception):
            per_frame = (camera.sensor_width * camera.sensor_height * 2) / 1e9 if camera else 0.0
        frames = f" — about {int(free_gb / per_frame)} more frames" if per_frame else ""
        board.raise_("disk.space", level, title,
                     f"{free_gb:.1f} GB free on the drive holding {root}{frames}.",
                     "Move last season's frames off, or point the capture folder at a "
                     "bigger drive under Equipment → Files.")


# -- the solver ---------------------------------------------------------------

@check
def solver_present(board: WarningBoard, ctx: Context) -> None:
    if not ctx.live() or not ctx.light_entries() or ctx.rigs is None:
        return
    with contextlib.suppress(Exception):
        if ctx.rigs.master.solver.executable() is None:
            board.raise_("solver.missing", "warning", "ASTAP was not found",
                         "Targets will be slewed to but not centred, the meridian flip "
                         "will not re-centre, and the camera angle cannot be checked.",
                         "Install ASTAP with a star database, or set its path in Site & Optics.")


# -- the run as it goes -----------------------------------------------------------

@check
def sequence_health(board: WarningBoard, ctx: Context) -> None:
    run = ctx.run()
    if not run.get("running"):
        return
    rescues = int(run.get("rescues") or 0)
    if rescues >= 3:
        board.raise_("sequence.rescues", "warning", "The run keeps needing to be rescued",
                     f"{rescues} recoveries so far tonight.",
                     "Something is marginal: the guide star, the mount, a cable. "
                     "Worth a look before it gives up.")
    seq = ctx.section("sequencer")
    if not seq.get("stopAtDawn", True) and not run.get("loop") and ctx.night is not None:
        with contextlib.suppress(Exception):
            found = ctx.night()
            if found and found[1] and time.time() > found[1] - 1800:
                board.raise_("sequence.dawn", "warning", "The run will not stop at dawn",
                             "'Stop at dawn' is off and the sky is getting light.",
                             "Stop it by hand, or turn 'stop at dawn' on under Equipment → Sequencer.")

    frames = list(run.get("frames") or [])[-3:]
    if len(frames) >= 3 and seq.get("measureFrames", True):
        if all(f.get("hfr") is None and not f.get("detail") for f in frames):
            board.raise_("frames.nostars", "critical", "No stars in the last frames",
                         f"The last {len(frames)} frames had nothing measurable in them.",
                         "Cloud, dew on the optics, or badly out of focus. The run "
                         "carries on; these frames are probably worthless.")
        limit = float(seq.get("focusWarnPercent") or 25.0)
        drifts = [f.get("driftPercent") for f in frames[-2:]]
        if all(d is not None and d > limit for d in drifts):
            board.raise_("frames.focus", "warning", "Focus has drifted",
                         f"Stars are {drifts[-1]:.0f}% bigger than at the last focus run.",
                         "Autofocus should trigger; if it has failed, refocus by hand.")
        arc = _num(frames[-1].get("errorArcmin"))
        if arc is not None and arc > float(seq.get("pointingWarnArcmin") or 5.0):
            board.raise_("frames.pointing", "warning", "The telescope is off its target",
                         f"{arc:.1f}' from where it should be on the last check.",
                         "Recovery re-centres it; a mount slipping or a poor solve if it repeats.")

    # What the frames themselves look like: black is a cover, white is the day.
    if ctx.rigs is None:
        return
    with contextlib.suppress(Exception):
        recent = [r for r in ctx.rigs.master.capture.images[:3]
                  if r.frame_type == "light" and time.time() - r.timestamp < 1800]
        if len(recent) >= 2:
            offset = float(recent[0].offset or 0)
            medians = [float((r.stats or {}).get("median") or 0.0) for r in recent]
            if all(m <= offset * 10 + 30 for m in medians) and all(m < 300 for m in medians):
                board.raise_("frames.black", "critical", "The frames are black",
                             f"The last {len(recent)} lights have a median of "
                             f"{medians[-1]:.0f} ADU: nothing is reaching the sensor.",
                             "A cover still on, the panel's cover shut, or a dark filter "
                             "slot. Check the light path.")
            elif all(m > 60000 for m in medians):
                board.raise_("frames.white", "critical", "The frames are saturated",
                             f"The last {len(recent)} lights are at {medians[-1]:.0f} ADU.",
                             "Daylight, a light leak, or the flat panel on. Stop and look.")


@check
def slow_download(board: WarningBoard, ctx: Context) -> None:
    if ctx.rigs is None or not ctx.run().get("running"):
        return
    with contextlib.suppress(Exception):
        overheads = ctx.rigs.master.capture.overheads
        if overheads is None:
            return
        row = (overheads.summary() or {}).get("download") or {}
        seconds = _num(row.get("seconds"))
        if seconds is not None and seconds > 60 and int(row.get("samples") or 0) >= 3:
            board.raise_("camera.download", "notice", "Frames are slow to download",
                         f"{seconds:.0f} s per frame on average.",
                         "A USB 2 link or a hub; a fifth of the night goes to waiting.")


# -- the collaboration ----------------------------------------------------------

@check
def collaboration(board: WarningBoard, ctx: Context) -> None:
    if ctx.collab is None:
        return
    with contextlib.suppress(Exception):
        status = ctx.collab.status()
        if not status.get("configured"):
            return
        error = str(status.get("error") or "")
        if "401" in error:
            board.raise_("collab.token", "critical", "The collaboration server rejects this telescope",
                         "Its token is not known there; nothing will be dealt or reported.",
                         "Join with Discord again on the Collab tab.")
            return
        reached = float(status.get("reachedAt") or 0.0)
        every = float(ctx.setting("collab", "pollMinutes", 10) or 10) * 60.0
        if error and reached and time.time() - reached > 3 * every:
            board.raise_("collab.unreachable", "warning", "The collaboration server cannot be reached",
                         f"{error}. Frames are kept and reported when it is back.",
                         "Check the connection; nothing is lost.")
        fit = status.get("compatibility") or {}
        if fit and fit.get("ok") is False:
            board.raise_("collab.fit", "warning", "This telescope no longer suits its collaboration",
                         str(fit.get("summary") or ""),
                         "Its frames will be refused. Check the filters and exposures.")
        skew = _num(status.get("clockSkew"))
        if skew is not None and abs(skew) > 120:
            board.raise_("clock.skew", "warning", "This PC's clock is wrong",
                         f"It differs from the server's by {abs(skew) / 60:.0f} minutes; "
                         "every timestamp and every plan time is off by that.",
                         "Turn on automatic time in Windows settings.")


@check
def notifications(board: WarningBoard, ctx: Context) -> None:
    if ctx.notifier is None:
        return
    with contextlib.suppress(Exception):
        status = ctx.notifier.status()
        if status.get("enabled") and status.get("lastError"):
            board.raise_("notify.failing", "warning", "Notifications are failing",
                         str(status["lastError"]),
                         "Check the webhook URL under Equipment → Notifications and "
                         "send a test message.")


# ---------------------------------------------------------------------------
# The watchdog
# ---------------------------------------------------------------------------

class Watchdog:
    """Runs every check on a timer, each one on its own so none can stop
    the rest, and clears what no longer holds."""

    def __init__(self, board: WarningBoard, context: Context,
                 interval: float = INTERVAL, log: Any = None) -> None:
        self.board = board
        self.context = context
        self.interval = interval
        self._log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._broken: dict[str, str] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="warnings")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # The first pass waits a little: devices are still connecting when
        # the program starts, and a burst of "nothing is connected" on the
        # first screen is noise.
        self._stop.wait(5.0)
        while not self._stop.is_set():
            self.pass_once()
            self._stop.wait(self.interval)

    def pass_once(self) -> None:
        """One look at everything. Public so a test can drive it."""
        self.board.begin()
        for fn in CHECKS:
            try:
                fn(self.board, self.context)
                self._broken.pop(fn.__name__, None)
            except Exception as exc:                 # noqa: BLE001 - one check must not stop the rest
                text = str(exc)
                if self._broken.get(fn.__name__) != text and self._log is not None:
                    self._log(f"Warning check {fn.__name__} failed: {text}", "warn")
                self._broken[fn.__name__] = text
        self.board.end()
