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

    @property
    def guiding(self):
        return self.state == "Guiding"

    def status(self):
        return {"starLost": "low SNR"}

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

# 2. It does not, so guiding is restarted — and recalibrates on the third try.
guider = Guider()
guider.state = "LostLock"
guider.recover_after = 3
seq, rig, config = build({"guider": guider})
config.update("recovery", {"guideGraceSeconds": 0.0, "guideRestartAttempts": 4,
                           "recalibrateAfterAttempts": 2, "cooldownSeconds": 0.0})
config.update("guiding", {"settleTimeoutSeconds": 2.0})
seq._guiding_wanted = True
seq._check_guiding()
recal = [bool(s.get("recalibrate")) for s in guider.starts]
results.append(case("guiding is restarted, recalibrating only on the later tries",
                    guider.guiding and recal == [False, False, True],
                    f"recalibrate flags={recal}"))
results.append(case("the rescue is recorded",
                    seq._rescues == 1 and seq._recoveries[-1]["ok"],
                    f"rescues={seq._rescues}"))

# 3. It never comes back: the night carries on unguided rather than stopping.
guider = Guider()
guider.state = "LostLock"
seq, rig, config = build({"guider": guider})
config.update("recovery", {"guideGraceSeconds": 0.0, "guideRestartAttempts": 2,
                           "cooldownSeconds": 0.0, "maxPerTarget": 0})
config.update("guiding", {"settleTimeoutSeconds": 2.0})
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

# 8b. Guiding that is running but rough — a held star chased at hundreds of
# arcseconds by a reversed calibration — is a fault, not a notice. The night
# of the pier side the TheSky driver never reported: 564" RMS, "Guiding" all
# the way, every sub trailed, nothing restarted.
class RoughGuider(Guider):
    def __init__(self, rms_until_start):
        super().__init__()
        self.rms_until_start = rms_until_start     # rough until this many starts

    def status(self):
        rough = len(self.starts) < self.rms_until_start
        return {"rmsTotal": 564.5 if rough else 0.6, "samples": 50}

    def start_guiding(self, **kw):
        self.starts.append(kw)
        self.state = "Guiding"


guider = RoughGuider(rms_until_start=2)
seq, rig, config = build({"guider": guider})
config.update("recovery", {"guideGraceSeconds": 25.0, "guideRestartAttempts": 3,
                           "recalibrateAfterAttempts": 2, "cooldownSeconds": 0.0,
                           "guideRmsFaultArcsec": 5.0})
seq._guiding_wanted = True
started = time.monotonic()
seq._check_guiding()                 # slot 1: rough -> fresh star
first = [bool(s.get("recalibrate")) for s in guider.starts]
seq._check_guiding()                 # slot 2: still rough -> recalibrate
recal = [bool(s.get("recalibrate")) for s in guider.starts]
seq._check_guiding()                 # slot 3: fine -> nothing, counter reset
results.append(case("rough guiding on a held star is restarted on a fresh star, then recalibrated",
                    first == [False] and recal == [False, True]
                    and len(guider.starts) == 2 and seq._recoveries[-1]["ok"]
                    and seq._guiding_wanted and seq._rough_restarts == 0,
                    f"recalibrate flags={recal}, restarts={seq._rough_restarts}"))
results.append(case("...with no grace period: the RMS is already a minute of frames",
                    time.monotonic() - started < 8.0,
                    f"took {time.monotonic() - started:.1f}s"))

# 8c. The roughness check needs a window of frames, and can be turned off.
guider = RoughGuider(rms_until_start=99)
guider.status = lambda: {"rmsTotal": 564.5, "samples": 3}
seq, rig, config = build({"guider": guider})
config.update("recovery", {"guideGraceSeconds": 0.0, "cooldownSeconds": 0.0})
seq._guiding_wanted = True
seq._check_guiding()
results.append(case("three rough frames after a dither are not yet a verdict",
                    guider.starts == []))
guider = RoughGuider(rms_until_start=99)
seq, rig, config = build({"guider": guider})
config.update("recovery", {"guideGraceSeconds": 0.0, "cooldownSeconds": 0.0,
                           "guideRmsFaultArcsec": 0.0})
seq._guiding_wanted = True
seq._check_guiding()
results.append(case("the roughness check switched off leaves rough guiding alone",
                    guider.starts == []))


# 8d. The guider fails to settle when the target starts. That used to be
# "carrying on unguided" fifteen seconds into the night with recovery
# switched off for good; now it is the same rescue as a lost star.
class SettlingGuider(Guider):
    def __init__(self, fail_starts):
        super().__init__()
        self.state = "Stopped"
        self.fail_starts = fail_starts
        self.settle_error = None

    def start_guiding(self, **kw):
        self.starts.append(kw)
        self.state = "Guiding"
        self.settle_error = ("timed-out waiting for guider to settle"
                             if len(self.starts) <= self.fail_starts else None)


guider = SettlingGuider(fail_starts=1)
seq, rig, config = build({"guider": guider})
config.update("guiding", {"startWithSequence": True, "settleTimeoutSeconds": 3.0})
config.update("recovery", {"guideGraceSeconds": 0.0, "guideRestartAttempts": 3,
                           "recalibrateAfterAttempts": 2, "cooldownSeconds": 0.0})
seq._start_guiding()
results.append(case("a guider that fails to settle at the start is restarted rather than abandoned",
                    len(guider.starts) == 2 and seq._guiding_wanted
                    and seq._recoveries[-1]["ok"],
                    f"starts={len(guider.starts)}, wanted={seq._guiding_wanted}"))

guider = SettlingGuider(fail_starts=99)
seq, rig, config = build({"guider": guider})
config.update("guiding", {"startWithSequence": True, "settleTimeoutSeconds": 3.0})
config.update("recovery", {"guideGraceSeconds": 0.0, "guideRestartAttempts": 2,
                           "cooldownSeconds": 0.0, "maxPerTarget": 0})
seq._start_guiding()
results.append(case("...and one that never settles leaves the night running unguided after its tries",
                    len(guider.starts) == 3 and not seq._guiding_wanted,
                    f"starts={len(guider.starts)}, wanted={seq._guiding_wanted}"))

guider = SettlingGuider(fail_starts=99)
seq, rig, config = build({"guider": guider})
config.update("guiding", {"startWithSequence": True, "settleTimeoutSeconds": 3.0})
config.update("recovery", {"guidingRecovery": False})
seq._start_guiding()
results.append(case("...with guiding recovery off it is told once and left alone",
                    len(guider.starts) == 1 and not seq._guiding_wanted))


# 8e. A star dropped for a frame while settling is not a failure. PHD2 says
# LostLock on one low-SNR frame and is guiding on the next; giving up on the
# first one is how a night went unguided fifteen seconds in.
class FlickeringGuider(Guider):
    def __init__(self):
        super().__init__()
        self.settle_error = None
        self._t0 = time.monotonic()

    @property
    def state(self):
        return "LostLock" if time.monotonic() - self._t0 < 1.5 else "Guiding"

    @state.setter
    def state(self, value):
        pass


guider = FlickeringGuider()
seq, rig, config = build({"guider": guider})
results.append(case("a star lost for a frame during the settle is waited out",
                    seq._wait_for_guiding(5.0) is True))


class SettleThenFailGuider(FlickeringGuider):
    @property
    def settling(self):
        return time.monotonic() - self._t0 < 1.5

    @settling.setter
    def settling(self, value):
        pass

    @property
    def settle_error(self):
        return None if self.settling else "timed-out waiting for guider to settle"

    @settle_error.setter
    def settle_error(self, value):
        pass


guider = SettleThenFailGuider()
seq, rig, config = build({"guider": guider})
started = time.monotonic()
results.append(case("PHD2's own verdict that the settle failed ends the wait at once",
                    seq._wait_for_guiding(30.0) is False
                    and time.monotonic() - started < 5.0))


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

# 14. Giving up on a target is one of the ends of a run: the busy file goes.
from astrocontrol.sequencer import _busy_path                   # noqa: E402

if _busy_path().exists():
    _busy_path().unlink()
mount = Mount()
seq, rig, config = build({"mount": mount})
config.update("recovery", {"onGiveUp": "park"})
during = {}
seq._night = lambda: (during.update(text=_busy_path().read_text(encoding="utf-8")),
                      seq._give_up("the guider keeps losing its star"))
try:
    seq._run()
except _Stopped:
    pass
results.append(case("the busy file exists while recovery is trying",
                    "pid=" in during.get("text", "")))
results.append(case("and is gone once recovery gives up and the run ends",
                    not _busy_path().exists()))

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
