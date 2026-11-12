"""Stopping a sequence does not warm the camera; the end of the night does (0.2.52d).

    python tools/check_stop_warm.py

2026-11-04 00:16, Bray: "when i stop a sequence i dont need the camera to warm
up". FSQ106's log: 'Sequence finished' -> 'warming the camera from -10 C at
3 C/min' after a Stop, and after a run that ended in its first second because
M42 was still under its 30 degree floor. Each cost a ten-minute warm and then
another cool-down.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol.config import Config                          # noqa: E402
from astrocontrol.sequencer import Sequencer                    # noqa: E402
from astrocontrol.devices.base import DeviceError               # noqa: E402

LOG = []
results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Camera:
    def __init__(self, temperature=-10.0):
        self.connected = True
        self.can_cool = True
        self._temperature = temperature
        self.setpoints = []
        self.cooler = True

    @property
    def temperature(self):
        return self._temperature

    def set_setpoint(self, value):
        self.setpoints.append(value)

    def set_cooler(self, on):
        self.cooler = bool(on)


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


class Capture:
    def abort(self):
        pass

    def clear_context(self):
        pass


class Rig:
    def __init__(self, rid, name, config, devices):
        self.id = rid
        self.name = name
        self.role = "master"
        self.config = config
        self.manager = Manager(devices)
        self.capture = Capture()
        self.solver = None
        self.focuser = None


class Rigs:
    def __init__(self, rigs):
        self.master = rigs[0]
        self.all = list(rigs)

    def imaging(self):
        return self.all

    def get(self, rig_id):
        return self.master


def build(site=True, cameras=1):
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    if site:
        config.update("site", {"latitude": 31.9, "longitude": -99.1, "useMount": False})
    rigs = [Rig(f"r{i}", f"Telescope {i + 1}", config, {"camera": Camera()})
            for i in range(cameras)]
    seq = Sequencer(Rigs(rigs), config, None, None)
    return seq, rigs, config


def end(seq, night, dawn_in=5 * 3600.0, frames=0):
    """Run `_run` with a night that does `night`, dawn `dawn_in` seconds away."""
    seq._loop = False
    now = time.time()
    seq._dawn = lambda: now + dawn_in if dawn_in is not None else None
    seq._next_dawn = lambda: now + dawn_in if dawn_in is not None else None

    def body():
        if frames:
            seq._done = {"x": frames}
        night()

    seq._night = body
    del LOG[:]
    seq._run()


def said(text):
    return any(text in m for _, m in LOG)


def tidy(seq):
    seq._stop_warming()


# ---------------------------------------------------------------- Stop
seq, rigs, config = build()
end(seq, lambda: seq.stop(), frames=12)
cam = rigs[0].manager.get("camera")
case("Stop does not warm the camera", not seq.warming and cam.setpoints == []
     and cam.cooler is True, f"warming={seq.warming} setpoints={cam.setpoints}")
case("...and says it is staying cold, and why",
     said("stays cooled at -10.0 C (stopped by hand)"), str(LOG[-3:]))
case("...with a warm-up booked for dawn instead", seq.warm_pending
     and seq._warm_later_at is not None)
case("a new sequence cancels the dawn warm-up", (tidy(seq), not seq.warm_pending)[1])

seq, rigs, config = build()
config.update("camera", {"warmOnStop": True})
end(seq, lambda: seq.stop())
case("warmOnStop brings the old warm-up after Stop back", seq.warming)
tidy(seq)

seq, rigs, config = build()
config.update("camera", {"warmAtDawnIfCold": False})
end(seq, lambda: seq.stop())
case("warmAtDawnIfCold off: Stop leaves it cold with nothing booked",
     not seq.warming and not seq.warm_pending)

seq, rigs, config = build()
rigs[0].manager.get("camera").cooler_on = False   # already warm: a loop put it to bed
end(seq, lambda: seq.stop())
case("Stop with the cooler already off books nothing and claims nothing",
     not seq.warm_pending and not said("stays cooled"))

# ------------------------------------------ ended at once: nothing was up yet
seq, rigs, config = build()
end(seq, lambda: None, frames=0)            # "M42 is below its 30° floor" -> finished
case("a run that ends at once (target under its floor) does not warm",
     not seq.warming and rigs[0].manager.get("camera").setpoints == [])
case("...because nothing was shot", said("(nothing was shot)"), str(LOG[-2:]))
tidy(seq)

seq, rigs, config = build()
end(seq, lambda: None, frames=40)
case("a plan that runs out at 1 AM keeps the camera cold too",
     not seq.warming and said("the plan ran out before the end of the night"))
case("...and books it for dawn", seq.warm_pending)
tidy(seq)

# ------------------------------------------------ the end of the night warms
seq, rigs, config = build()


def dawn_reached():
    seq._night_over = True                  # what _past_deadline sets at dawn


end(seq, dawn_reached, frames=40)
case("a night that ran into dawn warms the camera at once", seq.warming)
tidy(seq)

seq, rigs, config = build()
end(seq, lambda: None, frames=40, dawn_in=10 * 60.0)
case("a plan that runs out within half an hour of dawn counts as the end of the night",
     seq.warming)
tidy(seq)

seq, rigs, config = build(site=False)
seq._dawn = lambda: None
seq._loop = False
seq._done = {}
seq._night = lambda: setattr(seq, "_done", {"x": 3})
seq._run()
case("no site: a plan shot through to its end warms, as it always did", seq.warming)
tidy(seq)

# -------------------------------------------- failures closed out still warm
seq, rigs, config = build()


def failed():
    seq._error = "the camera fell off"
    seq._close_out = "the sequence failed: the camera fell off"


end(seq, failed, frames=5)
case("a failed night being closed out still warms the camera", seq.warming)
tidy(seq)

seq, rigs, config = build()
config.update("recovery", {"closeOutOnFailure": "none"})
end(seq, failed, frames=5)
case("a failure with close-out off leaves the camera as it is",
     not seq.warming and said("(the run failed)"))
tidy(seq)

# ------------------------------------------------- off means off
seq, rigs, config = build()
config.update("camera", {"warmAtEnd": False})
end(seq, dawn_reached, frames=40)
case("warmAtEnd off: not even dawn warms it", not seq.warming and not seq.warm_pending)
seq._start_warming()
case("...nor Shut down / Abort & park", not seq.warming)

# ------------------------------------------- the dawn warm-up itself
seq, rigs, config = build()
seq._warm_later(rigs, time.time() + 0.3)
time.sleep(0.8)
cam = rigs[0].manager.get("camera")
case("the booked warm-up starts when its time comes",
     seq.warming or cam.setpoints, f"setpoints={cam.setpoints}")
tidy(seq)

seq, rigs, config = build()
seq._thread = __import__("threading").Thread(target=lambda: time.sleep(1.5), daemon=True)
seq._thread.start()
seq._warm_later(rigs, time.time() + 0.2)
time.sleep(0.6)
case("...but not under a sequence that has started meanwhile",
     not seq.warming and rigs[0].manager.get("camera").setpoints == [])

# --------------------------------------------- two telescopes, one setting each
seq, rigs, config = build(cameras=2)
rigs[1].config = Config(Path(tempfile.mkdtemp()) / "s2.json")
rigs[1].config.update("camera", {"warmOnStop": True})
end(seq, lambda: seq.stop())
case("per telescope: the one with warmOnStop warms, the other stays cold",
     rigs[1].manager.get("camera").setpoints and not rigs[0].manager.get("camera").setpoints,
     f"{rigs[0].manager.get('camera').setpoints} {rigs[1].manager.get('camera').setpoints}")
tidy(seq)

# ------------------------------------------------- when "dawn" is in daylight
import astrocontrol.schedule as schedule                       # noqa: E402
seq, rigs, config = build()
now = time.time()
real = schedule.night
schedule.night = lambda lat, lon, date=None: {"duskAstronomical": now + 3 * 3600,
                                              "dawnAstronomical": now + 13 * 3600}
at = seq._next_dawn()
case("stopped in daylight (before dusk): warmed an hour later, not after the night",
     abs(at - (now + 3600)) < 5, f"{(at - now) / 3600:.2f} h")
schedule.night = lambda lat, lon, date=None: {"duskAstronomical": now - 3 * 3600,
                                              "dawnAstronomical": now + 4 * 3600}
at = seq._next_dawn()
case("stopped in the night: warmed at that night's dawn", abs(at - (now + 4 * 3600)) < 5)
schedule.night = real

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
