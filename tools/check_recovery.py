"""Exercise the sequencer's recovery paths against stub devices.

    python tools/check_recovery.py

Recovery is the one part of the program that only ever runs when nobody is
watching it — the guide star goes at three in the morning or it does not go at
all — so it is the part least likely to be noticed when it breaks and the part
worst to discover broken in the morning.  Stub devices stand in for the mount,
the guider, the focuser and the camera, each told to fail in a particular way,
and each recovery is checked for doing the right thing *and* for stopping.

No test framework: the project has no dependency on one, and this runs from a
cold checkout with nothing installed but what Starfront already needs.
"""
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol.config import Config
from astrocontrol.sequencer import Sequencer, _Reframed, _Skipped, _Stopped
from astrocontrol.devices.base import DeviceError

LOG = []


class Guider:
    def __init__(self):
        self.connected = True
        self.state = "Guiding"
        self.settling = False
        self.stopped = 0
        self.starts = []
        self.recover_after = None      # start_guiding succeeds on this attempt
        self.rms = 0.8                 # arcsec, what status() reports
        self.samples = 40

    @property
    def guiding(self):
        return self.state == "Guiding"

    def status(self):
        return {"starLost": "low SNR", "rmsTotal": self.rms, "samples": self.samples}

    def stop_guiding(self):
        self.stopped += 1
        self.state = "Stopped"

    def start_guiding(self, **kw):
        self.starts.append(kw)
        if self.recover_after is not None and len(self.starts) >= self.recover_after:
            self.state = "Guiding"
        else:
            self.state = "LostLock"

    def set_paused(self, on):
        pass


class Focuser:
    def __init__(self):
        self.connected = True
        self.position = 30000
        self.moving = False

    def move_relative(self, delta):
        self.position += int(delta)


class Wheel:
    def __init__(self, names):
        self.connected = True
        self.names = names
        self.position = 0
        self.moving = False

    def set_position(self, index):
        self.position = index


class Manager:
    def __init__(self, devices):
        self.devices = devices

    def get(self, kind):
        return self.devices.get(kind)

    def require(self, kind):
        device = self.devices.get(kind)
        if device is None:
            raise DeviceError(f"{kind} not connected")
        return device

    def log(self, message, level="info"):
        LOG.append((level, message))

    def disconnect(self, kind):
        LOG.append(("info", f"disconnect {kind}"))


class Capture:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = 0

    def capture_blocking(self, exposure, frame_type="light"):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise DeviceError("the camera did not answer")
        return types.SimpleNamespace(id=f"img{self.calls}", exposure=exposure,
                                     filename="x.fits", path=None,
                                     timestamp=time.time())

    def abort(self):
        pass


class Rig:
    def __init__(self, config, devices, fail_times=0):
        self.id = "rig1"
        self.name = "Telescope 1"
        self.role = "master"
        self.config = config
        self.manager = Manager(devices)
        self.capture = Capture(fail_times)
        self.solver = None
        self.focuser = None


class Rigs:
    def __init__(self, rig):
        self.master = rig
        self.all = [rig]
        self.reconnects = 0

    def imaging(self):
        return self.all

    def get(self, rig_id):
        return self.master

    def connect_remembered(self, rig, kinds=None):
        self.reconnects += 1
        return {"connected": list(kinds or []), "failed": []}


def build(devices, fail_times=0):
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    rig = Rig(config, devices, fail_times)
    sequencer = Sequencer(Rigs(rig), config, None, None)
    return sequencer, rig, config


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


results = []

# 1. The star comes back on its own inside the grace period.
guider = Guider()
seq, rig, config = build({"guider": guider})
config.update("recovery", {"guideGraceSeconds": 3.0})
seq._guiding_wanted = True
guider.state = "LostLock"


def revive():
    time.sleep(1.0)
    guider.state = "Guiding"


threading.Thread(target=revive, daemon=True).start()
seq._check_guiding()
results.append(case("guide star returns inside the grace period",
                    guider.starts == [] and seq._guide_lost_since is None,
                    f"restarts={len(guider.starts)}"))

# 2. It does not, so guiding is restarted on a fresh star. A lost star never
#    recalibrates (0.2.49, FSQ106 0003): PHD2 cannot calibrate on a star it
#    cannot see, and the calibration it writes replaces the good one.
guider = Guider()
guider.state = "LostLock"
guider.recover_after = 3
seq, rig, config = build({"guider": guider})
config.update("recovery", {"guideGraceSeconds": 0.0, "guideRestartAttempts": 4,
                           "recalibrateAfterAttempts": 2, "cooldownSeconds": 0.0})
seq._guiding_wanted = True
seq._check_guiding()
recal = [bool(s.get("recalibrate")) for s in guider.starts]
results.append(case("guiding is restarted, never recalibrating on a lost star",
                    guider.guiding and recal == [False, False, False],
                    f"recalibrate flags={recal}"))
results.append(case("the rescue is recorded",
                    seq._rescues == 1 and seq._recoveries[-1]["ok"],
                    f"rescues={seq._rescues}"))

# 2b. Rough guiding on a well-seen star: a fresh star first, the calibration on
#     the rough restart the setting names - unless the star is too faint.
class SeenGuider(Guider):
    snr = 25.0

    def status(self):
        return {"guiding": self.guiding, "starLost": None, "snr": self.snr,
                "rmsTotal": self.rms, "samples": self.samples}


def rough_run(snr):
    g = SeenGuider()
    g.snr = snr
    g.rms = 4.0
    g.recover_after = 1
    s, _, c = build({"guider": g})
    c.update("recovery", {"guideGraceSeconds": 0.0, "guideRestartAttempts": 2,
                          "recalibrateAfterAttempts": 2, "cooldownSeconds": 0.0,
                          "roughGuidingArcsec": 2.0, "roughGuidingFrames": 1,
                          "roughGuidingRetryMinutes": 0})
    s._guiding_wanted = True
    s._check_guiding()
    s._check_guiding()
    return [bool(x.get("recalibrate")) for x in g.starts]


flags = rough_run(25.0)
results.append(case("rough guiding on a bright star recalibrates on rough restart 2",
                    flags == [False, True], f"recalibrate flags={flags}"))
flags = rough_run(4.0)
results.append(case("...but not when the star is too faint to calibrate on",
                    flags == [False, False], f"recalibrate flags={flags}"))


# 2c. A calibration far off square is refused: guiding stops, the night goes on.
class SkewGuider(Guider):
    def __init__(self, ortho):
        super().__init__()
        self.ortho = ortho

    def calibration_data(self):
        return {"calibrated": True, "xAngle": 10.0, "yAngle": 10.0 + 90.0 + self.ortho,
                "orthoError": self.ortho}


for ortho, sound in ((53.0, False), (4.0, True)):
    g = SkewGuider(ortho)
    s, _, _ = build({"guider": g})
    ok = s._calibration_sound(g)
    results.append(case(f"a calibration {ortho:g} deg off square is "
                        + ("guided on" if sound else "refused and guiding stopped"),
                        ok is sound and (sound or (g.stopped == 1 and not s._guiding_wanted)),
                        f"sound={ok} stopped={g.stopped}"))

# 3. It never comes back: the night carries on unguided rather than stopping.
guider = Guider()
guider.state = "LostLock"
seq, rig, config = build({"guider": guider})
config.update("recovery", {"guideGraceSeconds": 0.0, "guideRestartAttempts": 2,
                           "cooldownSeconds": 0.0, "maxPerTarget": 0})
seq._guiding_wanted = True
seq._check_guiding()
results.append(case("a guider that will not come back leaves the run going",
                    not seq._guiding_wanted and not seq._recoveries[-1]["ok"],
                    f"guiding_wanted={seq._guiding_wanted}"))

# 4. The per-target budget is enforced, and `onGiveUp` decides what that means.
guider = Guider()
guider.state = "LostLock"
seq, rig, config = build({"guider": guider})
config.update("recovery", {"guideGraceSeconds": 0.0, "guideRestartAttempts": 3,
                           "cooldownSeconds": 0.0, "maxPerTarget": 1,
                           "onGiveUp": "next"})
seq._guiding_wanted = True
seq._target_rescues = 1
try:
    seq._check_guiding()
    skipped = False
except _Skipped:
    skipped = True
results.append(case("out of budget, the target is abandoned rather than looped on",
                    skipped))

# 5. A camera that fails twice and then answers.
seq, rig, config = build({}, fail_times=2)
config.update("recovery", {"frameRetryAttempts": 3, "frameRetrySeconds": 0.0,
                           "reconnectDevices": True})
outcome = seq._expose_slot([rig], {rig.id: {"exposure": 60.0, "filter": "L"}})
results.append(case("a frame that fails twice is retried until it lands",
                    not outcome["errors"] and rig.capture.calls == 3,
                    f"calls={rig.capture.calls}, errors={outcome['errors']}"))
results.append(case("the camera is reconnected before the later retry",
                    seq.rigs.reconnects == 1, f"reconnects={seq.rigs.reconnects}"))

# 6. A camera that never answers gives up after the configured number of tries.
seq, rig, config = build({}, fail_times=99)
config.update("recovery", {"frameRetryAttempts": 2, "frameRetrySeconds": 0.0,
                           "maxPerTarget": 0})
outcome = seq._expose_slot([rig], {rig.id: {"exposure": 1.0, "filter": "L"}})
results.append(case("a dead camera stops being retried",
                    bool(outcome["errors"]) and rig.capture.calls == 3,
                    f"calls={rig.capture.calls}"))

# 7. Filter offsets move by the difference, not the absolute.
focuser = Focuser()
wheel = Wheel(["L", "Ha"])
seq, rig, config = build({"focuser": focuser, "filterwheel": wheel})
config.update("sequencer", {"filterOffsets": {"L": 0, "Ha": -180},
                            "useFilterOffsets": True})
moved = seq._select_filter(rig, "Ha")
first = focuser.position
seq._select_filter(rig, "L")
results.append(case("a filter change moves the focuser by its offset",
                    first == 30000 - 180 and focuser.position == 30000,
                    f"Ha={first}, back to L={focuser.position}"))
results.append(case("an offset already applied is not a reason to sweep",
                    moved is False, f"_select_filter returned {moved}"))

# 8. Offsets off means the focuser is left alone.
focuser = Focuser()
wheel = Wheel(["L", "Ha"])
seq, rig, config = build({"focuser": focuser, "filterwheel": wheel})
config.update("sequencer", {"filterOffsets": {"Ha": -180}, "useFilterOffsets": False})
seq._select_filter(rig, "Ha")
results.append(case("offsets switched off leave the focuser where it is",
                    focuser.position == 30000, f"position={focuser.position}"))

# 9. Recovery switched off entirely does nothing at all.
guider = Guider()
guider.state = "LostLock"
seq, rig, config = build({"guider": guider})
config.update("recovery", {"enabled": False})
seq._guiding_wanted = True
seq._check_guiding()
results.append(case("recovery switched off does nothing",
                    guider.starts == [] and seq._rescues == 0))


# 10. Pointing recovery slews back, and puts guiding back afterwards.
class Mount:
    def __init__(self):
        self.connected = True
        self.slewing = False
        self.side_of_pier = "east"
        self.slews = []
        self.parked = False
        self.tracking = True

    def slew_to(self, ra, dec):
        self.slews.append((round(ra, 4), round(dec, 4)))

    def park(self):
        self.parked = True

    def set_tracking(self, on):
        self.tracking = on


class Solver:
    def executable(self):
        return None


mount = Mount()
guider = Guider()
seq, rig, config = build({"mount": mount, "guider": guider})
rig.solver = Solver()
seq.rigs.master.solver = Solver()
config.update("recovery", {"recentreArcmin": 10.0, "recentreAttempts": 2})
config.update("sequencer", {"settleSeconds": 0.0})
config.update("guiding", {"startWithSequence": True, "settleTimeoutSeconds": 2.0})
guider.recover_after = 1
seq._recentre_request = "14.2' off target"
panel = {"index": 1, "ra": 0.712, "dec": 41.27, "rotation": None}
seq._check_recentre(panel, "M31")
results.append(case("a drifting target is slewed back to and re-acquired",
                    mount.slews == [(0.712, 41.27)] and guider.stopped == 1
                    and guider.guiding,
                    f"slews={mount.slews}, guiding={guider.guiding}"))
results.append(case("the re-centre is counted against the target's budget",
                    seq._target_rescues == 1, f"budget used={seq._target_rescues}"))

# 11. No request pending means no slew at all.
mount = Mount()
seq, rig, config = build({"mount": mount})
seq._check_recentre(panel, "M31")
results.append(case("no drift means no slew", mount.slews == []))

# 12. Giving up with `park` stops the run and makes the mount safe.
mount = Mount()
guider = Guider()
seq, rig, config = build({"mount": mount, "guider": guider})
config.update("recovery", {"onGiveUp": "park"})
try:
    seq._give_up("the guider keeps losing its star")
    stopped = False
except _Stopped:
    stopped = True
results.append(case("giving up with 'park' parks the mount and stops the run",
                    stopped and mount.parked and guider.stopped == 1
                    and seq._abort.is_set()))

# 13. End of run: parking is opt-in, and stopping tracking is separate.
mount = Mount()
seq, rig, config = build({"mount": mount})
seq._make_safe(park=False, stop_tracking=True)
results.append(case("stopping tracking at the end does not park",
                    not mount.parked and mount.tracking is False))


# 13b. Rough guiding: still "guiding", but every sub trailed. Past the limit
# for the set number of frames in a row the star is dropped and a fresh one
# picked; one rough frame is only noted; a second fresh star is not asked
# for until the spacing has passed.
guider = Guider()
guider.rms = 5.2
seq, rig, config = build({"guider": guider})
config.update("recovery", {"roughGuidingArcsec": 3.0, "roughGuidingFrames": 2,
                           "roughGuidingRetryMinutes": 10.0,
                           "guideRestartAttempts": 2, "cooldownSeconds": 0.0})
config.update("guiding", {"settleTimeoutSeconds": 2.0})
seq._guiding_wanted = True
guider.recover_after = 1
seq._check_guiding()
results.append(case("one rough frame is noted, not acted on",
                    guider.starts == [] and seq._rough_runs == 1))
seq._check_guiding()
results.append(case("two rough frames running: guiding restarts on a fresh star",
                    len(guider.starts) == 1 and guider.starts[0].get("fresh_star") is True
                    and guider.guiding and seq._recoveries[-1]["ok"],
                    f"starts={guider.starts}"))
seq._check_guiding(); seq._check_guiding()
results.append(case("...and not again inside the spacing",
                    len(guider.starts) == 1, f"starts={len(guider.starts)}"))
seq._rough_last -= 601.0
seq._check_guiding(); seq._check_guiding()
results.append(case("...but again once the spacing has passed",
                    len(guider.starts) == 2, f"starts={len(guider.starts)}"))
guider = Guider()
guider.rms = 5.2
guider.samples = 4
seq, rig, config = build({"guider": guider})
config.update("recovery", {"roughGuidingArcsec": 3.0, "roughGuidingFrames": 1})
seq._guiding_wanted = True
seq._check_guiding()
results.append(case("too few guide steps to judge: no restart",
                    guider.starts == []))
config.update("recovery", {"roughGuidingArcsec": 0})
guider.samples = 40
seq._check_guiding()
results.append(case("0 arcsec turns the rough-guiding restart off",
                    guider.starts == [] and not seq._guide_lost_in_slot))

# 14. Going to a panel with a plate solver present, and no rotator. The
# solve is asked to hold the framing angle only when a rotator is turning;
# the flag that says so was once used before it was set, and every slew of
# every night failed with a NameError the moment ASTAP was installed.
class SolvingSolver:
    def __init__(self):
        self.calls = []
        self.busy = False

    def executable(self):
        return "astap"

    def start(self, mode, image_id, ra, dec, rotation=None):
        self.calls.append((mode, round(ra, 3), round(dec, 2), rotation))

    def status(self):
        return {"error": None}


mount = Mount()
seq, rig, config = build({"mount": mount})
solver = SolvingSolver()
rig.solver = solver
seq.rigs.master.solver = solver
config.update("sequencer", {"settleSeconds": 0.0, "homeAtStart": False})
try:
    seq._goto({"index": 1, "ra": 0.712, "dec": 41.27, "rotation": 35.0}, "M31")
    went = True
    why = ""
except Exception as exc:                          # noqa: BLE001
    went, why = False, f"{type(exc).__name__}: {exc}"
results.append(case("a slew with a solver present centres without a rotator",
                    went and mount.slews == [(0.712, 41.27)]
                    and solver.calls == [("center", 0.712, 41.27, None)],
                    why or f"slews={mount.slews}, solver={solver.calls}"))


# 14b. A rotator is turned to the panel's angle - unless the entry is a
# collaboration joined with "turn the rotator to the project's angle" off,
# in which case it stays exactly where it is and the solve is not asked to
# correct the angle either. And guiding stops before the slew: a guider
# pulsing the mount through a slew and a centring fights every correction.
class TurningRotator:
    def __init__(self):
        self.connected = True
        self.position = 120.0
        self.moving = False
        self.moves = []

    def move_absolute(self, angle):
        self.moves.append(round(angle, 2))
        self.position = angle


def goto_case(rotate, guiding):
    mount = Mount()
    rotator = TurningRotator()
    guider = Guider()
    guider.state = "Guiding" if guiding else "Stopped"
    seq, rig, config = build({"mount": mount, "rotator": rotator, "guider": guider})
    solver = SolvingSolver()
    rig.solver = solver
    seq.rigs.master.solver = solver
    config.update("sequencer", {"settleSeconds": 0.0, "homeAtStart": False})
    seq._goto({"index": 1, "ra": 0.712, "dec": 41.27, "rotation": 35.0}, "M31", rotate=rotate)
    return rotator, solver, guider


rotator, solver, guider = goto_case(rotate=True, guiding=True)
results.append(case("a rotator is turned to the panel's angle and the solve holds it",
                    rotator.moves == [35.0] and solver.calls[0][3] == 35.0,
                    f"moves={rotator.moves}, solver={solver.calls}"))
results.append(case("...and a guider that was guiding is stopped before the slew",
                    guider.stopped == 1 and not guider.guiding, f"stopped {guider.stopped}"))
rotator, solver, guider = goto_case(rotate=False, guiding=False)
results.append(case("a collaboration shot at the camera's own angle leaves the rotator alone",
                    rotator.moves == [] and rotator.position == 120.0
                    and solver.calls[0][3] is None,
                    f"moves={rotator.moves}, solver={solver.calls}"))
results.append(case("...and a guider that was not guiding is not stopped",
                    guider.stopped == 0))
seq, rig, config = build({"mount": Mount()})
own_entry = {"id": "e", "name": "Mine", "targetId": "t"}
collab_off = {"id": "e2", "name": "Theirs", "targetId": "t2", "options": {"collabMatchRotation": False}}
collab_on = {"id": "e3", "name": "Theirs too", "targetId": "t3"}
results.append(case("the rotator rule: your own targets turn, a collaboration turns only when asked",
                    seq._rotate_for(own_entry, {"id": "t"}) is True
                    and seq._rotate_for(collab_off, {"id": "t2", "collab": {"task": "x"}}) is False
                    and seq._rotate_for(collab_on, {"id": "t3", "collab": {"task": "y"}}) is True))


# 15. The camera on a rig with no rotator is where the panels assume it is,
# or it is not. The first plate solve of a collaboration measures it; a
# disagreement beyond the tolerance lays the mosaic out again through the
# hook and starts the target over, and anything inside it is left alone.
class MeasuringSolver(SolvingSolver):
    def __init__(self, rotation):
        super().__init__()
        self.rotation = rotation

    def status(self):
        return {"error": None, "result": {"rotation": self.rotation}}


def angle_case(measured, target, devices=None):
    mount = Mount()
    seq, rig, config = build({"mount": mount, **(devices or {})})
    solver = MeasuringSolver(measured)
    rig.solver = solver
    seq.rigs.master.solver = solver
    laid = []
    seq.on_camera_angle = lambda target_id, angle: laid.append((target_id, angle))
    entry = {"id": "e1", "name": "Orion", "targetId": target["id"]}
    try:
        seq._check_camera_angle(entry, target, target.get("panels", [{}])[0])
        return laid, False
    except _Reframed:
        return laid, True


COLLAB = {"id": "t1", "align": "fixed", "rotation": 268.0,
          "collab": {"task": "abc", "project": "p"},
          "panels": [{"index": 1, "rotation": 268.0}]}
laid, restarted = angle_case(271.5, COLLAB)
results.append(case("a camera measured 3.5 degrees off its panels lays the mosaic "
                    "out again and starts the target over",
                    laid == [("t1", 271.5)] and restarted, f"laid={laid}"))
laid, restarted = angle_case(269.0, COLLAB)
results.append(case("...one degree off is inside the tolerance and nothing moves",
                    laid == [] and not restarted))
laid, restarted = angle_case(88.0, {**COLLAB, "align": "aligned"})
results.append(case("...a mosaic laid along the project's grid is not checked",
                    laid == [] and not restarted))
laid, restarted = angle_case(88.0, {**COLLAB, "collab": {}})
results.append(case("...nor a mosaic of your own", laid == [] and not restarted))


class Rotator:
    connected = True


laid, restarted = angle_case(88.0, COLLAB, {"rotator": Rotator()})
results.append(case("...nor anything on a rig with a rotator, which commands its angle",
                    laid == [] and not restarted))
laid, restarted = angle_case(-90.0, COLLAB)      # -90 is 270: 2 degrees off
results.append(case("...and the gap is measured round the circle",
                    laid == [] and not restarted))
# The night this pins down: the solver said 95.5 for a camera laid at 268.
# That is a half-turn plus seven and a half degrees, and a rectangle turned
# half a circle is the same rectangle - so the real disagreement is 7.5
# degrees, and the mosaic is re-laid at 275.5, keeping its panel numbers
# where they were, rather than at 95.5 with every number on new sky.
laid, restarted = angle_case(95.5, COLLAB)
results.append(case("a solver reporting the other half-turn is read as the same rectangle",
                    laid == [("t1", 275.5)] and restarted, f"laid={laid}"))
laid, restarted = angle_case(88.0, COLLAB)       # 268 - 180: the same footprint
results.append(case("...and exactly half a turn away is no turn at all",
                    laid == [] and not restarted, f"laid={laid}"))

# 2026-10-19 20:47: OnStep refused the slew to the next panel with "Already in
# motion" 38 ms after PHD2's last guide pulse, and that one refusal parked
# the mount and ended the night.  A refused slew is now asked for again.
print("-- a slew refused while a guide pulse is still being worked off --")


class Mount:
    def __init__(self, refusals=0, pulse_for=0.0):
        self.connected = True
        self.refusals = refusals
        self.slews = []
        self.pulse_until = time.monotonic() + pulse_for
        self.slewing = False
        self.tracking = True

    @property
    def pulse_guiding(self):
        return time.monotonic() < self.pulse_until

    def slew_to(self, ra, dec):
        self.slews.append((ra, dec, time.monotonic()))
        if len(self.slews) <= self.refusals:
            raise DeviceError("SlewToCoordinatesAsync: SlewError: Already in motion")


mount = Mount(refusals=2)
seq, rig, config = build({"mount": mount})
seq.SLEW_RETRY_SECONDS = 0.05
config.update("sequencer", {"slewRetrySeconds": 5.0})
started = time.monotonic()
seq._slew(mount, 20.5, 37.0, "panel 6")
results.append(case("a slew refused twice is asked for until the mount takes it",
                    len(mount.slews) == 3 and mount.slews[-1][:2] == (20.5, 37.0),
                    f"slews={len(mount.slews)}"))

mount = Mount(refusals=0, pulse_for=0.4)
seq, rig, config = build({"mount": mount})
seq.SLEW_RETRY_SECONDS = 0.05
seq._slew(mount, 1.0, 2.0, "x")
results.append(case("a guide pulse the driver reports is waited out before the slew",
                    len(mount.slews) == 1 and mount.slews[0][2] >= mount.pulse_until,
                    f"slews={len(mount.slews)}"))

mount = Mount(refusals=99)
seq, rig, config = build({"mount": mount})
seq.SLEW_RETRY_SECONDS = 0.05
config.update("sequencer", {"slewRetrySeconds": 0.3})
try:
    seq._slew(mount, 1.0, 2.0, "x")
    refused = ""
except DeviceError as exc:
    refused = str(exc)
results.append(case("a mount that never takes the slew is given up on, with the count",
                    "refused to slew to x" in refused and len(mount.slews) > 1,
                    refused))

# 2026-10-19 20:14:51: PHD2 still said Stopped one second after `guide`, so
# the panel was shot "unguided" while PHD2 went on to guide it.
print("-- PHD2 reporting Stopped for a moment after guide --")
guider = Guider()
guider.state = "Stopped"
seq, rig, config = build({"guider": guider})


def start_late():
    time.sleep(1.5)
    guider.state = "Guiding"


threading.Thread(target=start_late, daemon=True).start()
results.append(case("a guider that takes a moment to report is waited for",
                    seq._wait_for_guiding(6.0) is True, f"state={guider.state}"))

guider = Guider()
guider.state = "LostLock"
seq, rig, config = build({"guider": guider})
results.append(case("...but a lost lock is still an answer straight away",
                    seq._wait_for_guiding(6.0) is False))

# 2026-10-19 20:38: three cameras reconnected cleanly after a USB dropout and
# shot the rest of the night at +18 C, their coolers off since the reopen.
print("-- the cooler after a reconnect --")
from astrocontrol import rigs as rigs_module                       # noqa: E402


class ColdCamera:
    connected = True
    can_cool = True

    def __init__(self):
        self.gain = None
        self.offset = None
        self.setpoint = None
        self.cooler = False
        self.calls = []

    def set_settings(self, gain=None, offset=None):
        self.gain, self.offset = gain, offset
        self.calls.append(("settings", gain, offset))

    def set_setpoint(self, value):
        self.setpoint = value
        self.calls.append(("setpoint", value))

    def set_cooler(self, on):
        self.cooler = on
        self.calls.append(("cooler", on))


camera = ColdCamera()
config = Config(Path(tempfile.mkdtemp()) / "settings.json")
config.update("camera", {"gain": 100, "offset": 50})
stand_in = types.SimpleNamespace(manager=Manager({"camera": camera}), config=config,
                                 cooler_wanted=None)
rigs_module.Rig.note_cooler(stand_in, on=True, setpoint=-5.0)
done = rigs_module.Rig.restore_camera(stand_in)
results.append(case("a camera back from a reconnect is cooled to what was last asked",
                    camera.cooler is True and camera.setpoint == -5.0
                    and camera.gain == 100 and camera.offset == 50,
                    f"calls={camera.calls}"))
rigs_module.Rig.note_cooler(stand_in, on=False)
camera = ColdCamera()
stand_in.manager = Manager({"camera": camera})
rigs_module.Rig.restore_camera(stand_in)
results.append(case("...and left warm after the warm-down asked for that",
                    camera.cooler is False and camera.setpoint == -5.0,
                    f"calls={camera.calls}"))
camera = ColdCamera()
stand_in.manager = Manager({"camera": camera})
stand_in.cooler_wanted = None
rigs_module.Rig.restore_camera(stand_in)
results.append(case("...and the cooler is not touched when nobody ever asked",
                    not any(c[0] in ("cooler", "setpoint") for c in camera.calls)
                    and camera.gain == 100, f"calls={camera.calls}"))


class RigsWithRestore(Rigs):
    def reconnect_camera(self, rig, stop=None, progress=None):
        self.reconnects += 1
        # The sequence says how to notice a Stop and where to report waits.
        assert callable(stop) and callable(progress)
        LOG.append(("info", "restored"))
        return {"connected": ["camera"], "failed": []}


seq, rig, config = build({}, fail_times=2)
seq.rigs = RigsWithRestore(rig)
config.update("recovery", {"frameRetryAttempts": 3, "frameRetrySeconds": 0.0,
                           "reconnectDevices": True})
outcome = seq._expose_slot([rig], {rig.id: {"exposure": 60.0, "filter": "L"}})
results.append(case("the sequence's own reconnect goes through the restoring path",
                    not outcome["errors"] and seq.rigs.reconnects == 1
                    and ("info", "restored") in LOG, f"errors={outcome['errors']}"))

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
