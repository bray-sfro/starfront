"""Replay the failures from the RASA and FSQ106 logs against the real sequencer.

    python tools/check_recovery_real.py            # every case, one and three rigs
    python tools/check_recovery_real.py hung_download   # just the named case(s)

Each case starts the real program in a child process (its own data folder),
connects fake devices, starts a real sequence and breaks something the way it
broke on a real night (logs of 2026-09-16 .. 2026-11-01):

  camera_drop_1      2026-10-12 20:59, 10-14 20:40, 10-19 23:58, 10-20 00:25:
                     the master camera stops answering and does not come back
                     inside the frame retries; the whole night ended.
  camera_drop_3      the same on three telescopes, with all three cameras
                     failing at once and reconnecting at the same time, which
                     is what gave "ASIOpenCamera failed (invalid id)".  The
                     other two telescopes must keep imaging.
  hung_download      2026-10-14 02:24: a download that never returns; the
                     program sat "running" for 12.5 hours.
  hung_download_3    one of three cameras wedges for good: the other two finish.
  dead_at_dawn       the same, but the camera never comes back: the night must
                     be closed out (mount parked) at dawn, not left tracking.
  stuck_driver       a mount driver call that never returns on the sequence
                     thread itself: the watchdog must notice and close out.
  cooling_stall_3    2026-10-14 20:45: a reconnected camera that does not cool
                     held every telescope for 20 minutes.
  mount_dropped      FSQ 2026-10-20 23:22 "Sequence failed: Mount is not
                     connected" (and the OnStep "Already in motion" refusal of
                     2026-10-19 20:47, fixed before 0.2.50, kept as a guard).
  not_tracking       2026-10-20 21:5x: the mount stopped tracking and the night
                     carried on shooting trails.
  guider_retry       2026-10-14 01:52: guiding gave up after three restarts and
                     was never tried again that night.
  restart_resume     FSQ, 11 runs that ended with the program killed or the PC
                     off; none picked up again.  The program is killed mid-run
                     and started again: it must resume at the frame it was on.

No test framework, like the other checks.  Exit status 0 when every case passes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASE_TIMEOUT = 100.0


# ---------------------------------------------------------------------------
# The child: one case, one program
# ---------------------------------------------------------------------------

def child(case: str, data: str, phase: str) -> None:
    os.environ["ASTRO_DATA_DIR"] = data + "/data"
    os.environ["HOME"] = data + "/home"
    os.environ["LOCALAPPDATA"] = data + "/lad"
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tools"))
    import datetime as dt
    from fastapi.testclient import TestClient
    from astrocontrol import main, astro
    from astrocontrol.devices.base import DeviceError
    from fakes import FakeCamera, FakeMount, FakeWheel, install

    out: dict = {"case": case, "phase": phase, "checks": []}

    def check(name, ok, detail=""):
        out["checks"].append([name, bool(ok), str(detail)[:300]])

    def emit():
        if os.environ.get("RR_DEBUG"):
            for event in reversed(main.rigs.recent_events(400)):
                print("LOG", event.get("time") or event.get("t"), event.get("rig", ""),
                      event.get("level"), event.get("message"))
        print("RESULT " + json.dumps(out), flush=True)

    class Bus:
        """The PC's USB bus: one enumeration at a time or ids get mixed up."""
        def __init__(self):
            self.lock = threading.Lock()
            self.busy = 0
            self.races = 0
            self.opens = 0

    bus = Bus()

    class Camera(FakeCamera):
        dead_until = 0.0        # off the bus until this monotonic time
        dead_forever = False
        fail_next = 0           # this many exposures fail outright
        hang_next = 0           # this many downloads never return
        hang_always = False
        stuck_temp = None       # a cooler that does not cool
        on_expose = None

        def start_exposure(self, seconds, light=True):
            if self.on_expose:
                self.on_expose(self)
            if self.fail_next > 0:
                self.fail_next -= 1
                raise DeviceError("exposure failed")
            if self.dead_forever or time.monotonic() < self.dead_until:
                raise DeviceError("exposure failed")
            super().start_exposure(seconds, light)

        def get_image(self):
            if self.hang_always or self.hang_next > 0:
                self.hang_next = max(0, self.hang_next - 1)
                threading.Event().wait()           # a native call that never returns
            return super().get_image()

        @property
        def temperature(self):
            if self.stuck_temp is not None:
                return self.stuck_temp
            return FakeCamera.temperature.fget(self)

    utc = dt.datetime.now(dt.timezone.utc)
    lon = ((-(utc.hour + utc.minute / 60.0)) * 15.0 + 540.0) % 360.0 - 180.0
    cfg = main.config
    nrigs = 3 if case.endswith("_3") else 1
    if phase != "resume":
        cfg.update("site", {"latitude": 30.0, "longitude": lon, "useMount": False})
        cfg.update("sequencer", {
            "autofocusOnStart": False, "autofocusOnFilterChange": False,
            "autofocusIntervalMinutes": 0, "autofocusTemperatureDelta": 0,
            "autofocusHfrPercent": 0, "homeAtStart": False, "meridianFlipEnabled": False,
            "settleSeconds": 0.1, "slewRetrySeconds": 30.0, "parkAtEnd": True,
            "dawnMarginMinutes": 0.0})
        cfg.update("guiding", {"ditherEnabled": False, "settleTimeoutSeconds": 3.0})
        cfg.update("recovery", {
            "frameRetrySeconds": 1.0, "frameRetryAttempts": 3, "hungSeconds": 2.0,
            "guideGraceSeconds": 0.5, "guideRestartAttempts": 2, "cooldownSeconds": 0.2,
            "maxPerTarget": 0,
            # 0.2.51 settings; an older build ignores them.
            "rigRecoveryAttempts": 6, "rigRecoveryBackoffSeconds": 1.0,
            "rigRecoveryBackoffMaxSeconds": 4.0, "recoveryStepSeconds": 10.0,
            "downloadTimeoutSeconds": 3.0, "slotGraceSeconds": 3.0,
            "watchdogMinutes": 0.1, "coolStallSeconds": 2.0, "guideRetryMinutes": 0.05,
            "mountReconnectAttempts": 3, "resumeAfterRestart": True,
            "resumeDelaySeconds": 0.5, "resumeWaitMinutes": 0.5,
            "closeOutOnFailure": "park", "dawnCloseOutMinutes": 0.0})
        c = TestClient(main.app, raise_server_exceptions=False)
        for i in range(1, nrigs):
            c.post("/api/equipment/telescopes", json={"name": f"RASA {i + 1}"})

    rigs = main.rigs.all
    for rig in rigs:
        rig.config.update("camera", {"coolAtStart": False, "coolAllAtStart": False,
                                     "warmAtEnd": False, "coolTimeoutMinutes": 20.0})
    mount = install(main.rigs.master, "mount", FakeMount("mount"))
    mount.slew_seconds = 0.2
    cams = {}
    for i, rig in enumerate(rigs):
        cams[rig.id] = install(rig, "camera", Camera(f"cam{i + 1}", seed=i))
        install(rig, "filterwheel", FakeWheel(f"wheel{i + 1}"))

    # Reconnecting goes through the program's own path down to the driver
    # open, which is the part a fake has to stand in for.
    real_connect = main.rigs.connect_remembered

    def connect_remembered(rig, kinds=None):
        connected, failed = [], []
        for kind in kinds or ("camera", "mount"):
            if kind == "camera":
                cam = cams.get(rig.id)
                if cam is None:
                    continue
                device = rig.manager.get("camera")
                if device is not None and device.connected:
                    continue
                with bus.lock:
                    bus.busy += 1
                    raced = bus.busy > 1
                time.sleep(0.3)
                with bus.lock:
                    bus.busy -= 1
                if raced:
                    bus.races += 1
                    failed.append("camera: ASIOpenCamera failed (invalid id)")
                    continue
                if cam.dead_forever or time.monotonic() < cam.dead_until:
                    failed.append("camera: ASIOpenCamera failed (invalid id)")
                    continue
                bus.opens += 1
                if not cam.hang_always:
                    cam.hang_next = 0
                install(rig, "camera", cam)
                connected.append("camera")
            elif kind == "mount" and rig is main.rigs.master:
                if mount.connected:
                    continue
                if getattr(mount, "unplugged", False):
                    failed.append("mount: no answer")
                    continue
                install(rig, "mount", mount)
                connected.append("mount")
        return {"connected": connected, "failed": failed}

    main.rigs.connect_remembered = connect_remembered

    lst = astro.local_sidereal_hours(lon)

    def add_target(name, count, exposure=0.3):
        t = c.post("/api/targets", json={"name": name, "ra": (lst - 1.5) % 24,
                                         "dec": 35.0}).json()["target"]
        e = c.post("/api/plan/entries", json={"targetId": t["id"]}).json()["entry"]
        c.post(f"/api/plan/entries/{e['id']}/filters",
               json={"filters": [{"name": "L", "exposure": exposure, "count": count}]})
        return e

    seq = main.sequencer
    samples = []

    def watch(seconds, until=None):
        """Sample the status until the run ends (or `until` says so)."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            s = seq.status()
            samples.append(s)
            if until is not None and until(s):
                return s
            if until is None and not s["running"]:
                return s
            time.sleep(0.25)
        return seq.status()

    def done_for(rig_id, s=None):
        s = s or seq.status()
        return sum(v for k, v in (s.get("framesDone") or {}).items()
                   if k.split(":")[1] == rig_id)

    def saw_rig_recovery(rig_id):
        """Did the status ever say this telescope was recovering, attempt n/m?"""
        for s in samples:
            rec = (s.get("rigRecovery") or {}).get(rig_id) or {}
            tel = (s.get("telescopes") or {}).get(rig_id) or {}
            rec = rec or tel.get("recovery") or {}
            if rec.get("state") == "recovering" and rec.get("attempts") \
                    and rec.get("reason"):
                return True
        return False

    master = main.rigs.master
    mcam = cams[master.id]

    # -- the cases ---------------------------------------------------------
    if case in ("camera_drop_1", "camera_drop_3"):
        add_target("NGC 1499", 6)
        state = {"armed": True}

        def drop(cam):
            # The 3rd frame: every camera on the hub goes at once; the master
            # stays off the bus for 30 seconds (longer than three retries).
            if state["armed"] and cam is mcam and cam.exposures >= 2:
                state["armed"] = False
                mcam.dead_until = time.monotonic() + 30.0
                for other in cams.values():
                    if other is not mcam:
                        other.fail_next = 2
        for cam in cams.values():
            cam.on_expose = drop
        c.post("/api/sequence/start")
        s = watch(80)
        check("the run did not end in failure", s.get("error") is None
              and not str(s.get("message", "")).startswith("error"), s.get("error"))
        for rig in rigs:
            check(f"{rig.name} shot its whole allocation", done_for(rig.id, s) == 6,
                  f"done={done_for(rig.id, s)}")
        check("the status showed the master recovering, attempt n/m, with a reason",
              saw_rig_recovery(master.id))
        if nrigs > 1:
            # While the master was off the bus the others kept going.
            during = [x for x in samples
                      if ((x.get("rigRecovery") or {}).get(master.id) or {}).get("state")
                      == "recovering"]
            others = [r for r in rigs if r is not master]
            progressed = bool(during) and all(
                done_for(r.id, during[-1]) > done_for(r.id, during[0]) for r in others)
            check("the other telescopes kept imaging while the master recovered",
                  progressed, f"{len(during)} samples")
            check("reconnects never raced on the USB bus (no 'invalid id')",
                  bus.races == 0, f"races={bus.races}")

    elif case == "hung_download":
        add_target("M31", 5)
        state = {"armed": True}

        def hang(cam):
            if state["armed"] and cam.exposures >= 2:
                state["armed"] = False
                cam.hang_next = 1
        mcam.on_expose = hang
        c.post("/api/sequence/start")
        s = watch(45)
        check("the run did not sit 'running' on a dead download", not s["running"],
              s.get("message"))
        check("every frame was shot after the hang", done_for(master.id, s) == 5,
              f"done={done_for(master.id, s)}")
        check("no failure", s.get("error") is None, s.get("error"))

    elif case == "hung_download_3":
        # One telescope's camera wedges for good mid-run; the other two must
        # finish their frames and the run must end rather than hang.
        add_target("IC 1805", 5)
        sick = cams[rigs[1].id]
        state = {"armed": True}

        def wedge(cam):
            if state["armed"] and cam.exposures >= 2:
                state["armed"] = False
                cam.hang_always = True
        sick.on_expose = wedge
        cfg.update("recovery", {"rigRecoveryAttempts": 3})
        c.post("/api/sequence/start")
        s = watch(80)
        check("the run ended instead of hanging", not s["running"], s.get("message"))
        for rig in (rigs[0], rigs[2]):
            check(f"{rig.name} shot its whole allocation", done_for(rig.id, s) == 5,
                  f"done={done_for(rig.id, s)}")
        check(f"{rigs[1].name} was recovered then benched, and the status said so",
              saw_rig_recovery(rigs[1].id))

    elif case == "dead_at_dawn":
        add_target("M33", 50)
        state = {"armed": True}

        dawn = {"at": time.time() + 10 ** 6}

        def wedge(cam):
            if state["armed"] and cam.exposures >= 2:
                state["armed"] = False
                cam.hang_always = True
                dawn["at"] = time.time() + 15.0     # and the night runs out
        mcam.on_expose = wedge
        seq._dawn = lambda: dawn["at"]
        c.post("/api/sequence/start")
        s = watch(45, until=lambda s: not s["running"] and mount.at_park)
        check("the run was closed out by dawn rather than left running",
              not s["running"], f"state={s['state']} msg={s.get('message')}")
        check("the mount was parked", mount.at_park)
        check("the status said why while it was trying",
              saw_rig_recovery(master.id))

    elif case == "stuck_driver":
        add_target("M45", 3)
        hang = threading.Event()
        real = type(mount).slewing

        class Stuck(type(mount)):
            @property
            def slewing(self):
                if hang.is_set():
                    threading.Event().wait()
                return real.fget(self)
        mount.__class__ = Stuck
        real_slew = mount.slew_to

        def slew_to(ra, dec):
            real_slew(ra, dec)
            hang.set()                          # the next status read never returns
        mount.slew_to = slew_to
        c.post("/api/sequence/start")
        s = watch(40)
        check("the watchdog ended a run stuck inside a driver call", not s["running"],
              f"state={s['state']} msg={s.get('message')}")
        check("the reason is in the status", "progress" in str(s.get("error") or "")
              or "respond" in str(s.get("error") or ""), s.get("error"))
        stacks = list(Path(os.environ["ASTRO_DATA_DIR"]).rglob("*stack*"))
        check("thread stacks were saved for the morning", bool(stacks),
              [str(p) for p in stacks])

    elif case == "cooling_stall_3":
        add_target("NGC 7000", 2)
        for rig in rigs:
            rig.config.update("camera", {"coolAtStart": True, "setpoint": -5.0})
        stuck = cams[rigs[1].id]
        stuck.stuck_temp = 28.7
        started = time.monotonic()
        c.post("/api/sequence/start")
        s = watch(40, until=lambda s: all(done_for(r.id, s) >= 1 for r in rigs))
        took = time.monotonic() - started
        check("a camera that will not cool does not hold the night for 20 minutes",
              all(done_for(r.id, s) >= 1 for r in rigs), f"{took:.0f}s")
        watch(30)

    elif case == "mount_dropped":
        first = add_target("T1", 2)
        add_target("T2", 2)
        state = {"armed": True, "refused": 0}

        def drop(cam):
            if state["armed"] and cam.exposures >= 2:
                state["armed"] = False
                mount._connected = False            # the cable, or the driver
        mcam.on_expose = drop
        real_slew = mount.slew_to

        def slew_to(ra, dec):
            if not mount.connected:
                raise DeviceError("Mount is not connected")
            if state["refused"] == 0:
                state["refused"] += 1
                raise DeviceError("SlewToCoordinatesAsync: SlewError: Already in motion")
            real_slew(ra, dec)
        mount.slew_to = slew_to
        c.post("/api/sequence/start")
        s = watch(50)
        check("the run did not fail on a dropped mount", s.get("error") is None,
              s.get("error"))
        check("both targets were shot", done_for(master.id, s) == 4,
              f"done={done_for(master.id, s)}")

    elif case == "not_tracking":
        add_target("M42", 5)
        tracked = []

        def note(cam):
            tracked.append(mount.tracking)
            if cam.exposures == 2:
                mount._tracking = False             # stopped, nobody told us
        mcam.on_expose = note
        c.post("/api/sequence/start")
        watch(40)
        check("frames after the mount stopped tracking were not shot untracked",
              tracked[3:] and all(tracked[3:]), f"tracking at each frame: {tracked}")

    elif case == "guider_retry":
        add_target("M81", 12, exposure=0.5)

        class Guider:
            connected = True
            settling = False

            def __init__(self):
                self.state = "Stopped"
                self.refuse_until = 0.0
                self.starts = 0
            guiding = property(lambda self: self.state == "Guiding")

            def status(self):
                return {"starLost": "low SNR" if self.state == "LostLock" else "",
                        "rmsTotal": 0.8, "samples": 40, "guiding": self.guiding}

            def start_guiding(self, **kw):
                self.starts += 1
                self.state = "LostLock" if time.monotonic() < self.refuse_until \
                    else "Guiding"

            def stop_guiding(self):
                self.state = "Stopped"

            def set_paused(self, on):
                pass

            def dither(self, **kw):
                pass

            def connect(self):
                pass
        guider = Guider()
        master.manager._devices["guider"] = guider
        state = {"armed": True}

        def lose(cam):
            if state["armed"] and cam.exposures >= 2:
                state["armed"] = False
                guider.state = "LostLock"           # a cloud, for 8 seconds
                guider.refuse_until = time.monotonic() + 8.0
        states = []

        def lose_and_note(cam):
            lose(cam)
            states.append(guider.state)
        mcam.on_expose = lose_and_note
        c.post("/api/sequence/start")
        s = watch(60)
        check("guiding was tried again later in the night and came back",
              states[-3:] == ["Guiding"] * 3,
              f"guider at each frame: {states}; starts={guider.starts}")

    elif case == "restart_resume":
        if phase == "first":
            add_target("Heart", 4)
            add_target("Soul", 4)
            done_at = {"n": 0}

            def die(cam):
                if cam.exposures >= 6:
                    # The PC loses power in the 2nd frame of the 2nd target.
                    os._exit(3)
            mcam.on_expose = die
            c.post("/api/sequence/start")
            watch(40)
            check("(the first program should have died)", False)
        else:
            with TestClient(main.app, raise_server_exceptions=False) as c2:
                s = watch(40, until=lambda s: s["running"])
                check("the run picked up again by itself after the restart",
                      s["running"], s.get("message"))
                s = watch(40)
                total = sum((s.get("framesDone") or {}).values())
                check("it resumed at the frame it was on (2 more, not 8)",
                      mcam.exposures == 2, f"exposures after restart={mcam.exposures}")
                check("the night's count is the whole allocation", total == 8,
                      f"framesDone={s.get('framesDone')}")
    else:
        check(f"unknown case {case}", False)
    emit()
    os._exit(0)


# ---------------------------------------------------------------------------
# The parent: run every case in its own program
# ---------------------------------------------------------------------------

CASES = ["camera_drop_1", "camera_drop_3", "hung_download", "hung_download_3",
         "dead_at_dawn",
         "stuck_driver", "cooling_stall_3", "mount_dropped", "not_tracking",
         "guider_retry", "restart_resume"]


def run_child(case: str, data: str, phase: str) -> tuple[list, str]:
    try:
        proc = subprocess.run([sys.executable, __file__, "--child", case, data, phase],
                              capture_output=True, text=True, timeout=CASE_TIMEOUT,
                              cwd=str(ROOT))
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) \
            else (exc.stdout or "")
        return [[f"{case} finished in {CASE_TIMEOUT:.0f}s", False, "timed out"]], output
    for line in output.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[7:])["checks"], output
    return [[f"{case}: the program died ({proc.returncode})", False, ""]], output


def run_case(case: str) -> list:
    data = tempfile.mkdtemp(prefix=f"rr-{case}-")
    if case == "restart_resume":
        checks, _out = run_child(case, data, "first")
        died = not any(name.startswith("(the first") for name, _ok, _d in checks)
        result = [["the first program was killed mid-run", died, ""]]
        more, _out = run_child(case, data, "resume")
        return result + more
    checks, _out = run_child(case, data, "run")
    return checks


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(sys.argv[2], sys.argv[3], sys.argv[4])
        return 0
    wanted = sys.argv[1:] or CASES
    results = []
    for case in wanted:
        started = time.monotonic()
        checks = run_case(case)
        ok = all(item[1] for item in checks)
        print(f"{'PASS' if ok else 'FAIL'}  {case}  ({time.monotonic() - started:.0f}s)")
        for name, good, detail in checks:
            print(f"    {'ok ' if good else 'BAD'} {name}" + (f"  [{detail}]" if detail and not good else ""))
        results.append(ok)
    print()
    print(f"{sum(results)}/{len(results)} cases passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
