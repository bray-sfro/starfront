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

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
