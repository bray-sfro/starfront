"""Exercise the pier camera's auto-exposure and auto-gain.

    python tools/check_piercam.py

The control loop is the whole feature.  A pier cam has to cope with afternoon
sun through to a pitch-dark dome on the same night, across something like
fourteen stops, with nobody awake to adjust it — and the failure modes are all
quiet ones: a loop that oscillates looks like a flickering feed, a loop that
runs the gain up looks like a noisy one, and a loop that gives up looks like a
black rectangle nobody notices until the telescope has been pointing at the
inside of a closed roof for three hours.

So what is checked here is convergence: from any starting brightness, does it
reach the target, does it stay there, and does it come back off the gain when
the light returns.

No test framework, for the same reason as the other checks here.
"""
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["ASTRO_DATA_DIR"] = tempfile.mkdtemp()

from astrocontrol import piercam                               # noqa: E402
from astrocontrol.config import Config                         # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Camera:
    """A camera in front of a scene of a given brightness.

    The scene's brightness is expressed as the level a one-second exposure at
    gain zero would produce, so a sunny afternoon is a huge number and a dark
    dome is a tiny one. Gain is treated the way a real camera's is — a dB-ish
    ladder where a fixed number of steps doubles the signal.
    """

    def __init__(self, scene, gain_max=500, gain_per_double=100.0):
        self.name = "Test pier cam"
        self.connected = True
        self.gain_min = 0
        self.gain_max = gain_max
        self.gain = 0
        self.scene = scene
        self.gain_per_double = gain_per_double

    def set_settings(self, binning=None, gain=None, offset=None):
        if gain is not None:
            self.gain = int(np.clip(gain, self.gain_min, self.gain_max))

    def level_for(self, exposure):
        """What `_level_of` would measure for this exposure and gain, 0..1."""
        signal = self.scene * exposure * (2.0 ** (self.gain / self.gain_per_double))
        return float(min(1.0, signal))

    # The rest of the camera protocol, so `_grab` can be exercised as it really
    # runs rather than only through the control loop.
    def start_exposure(self, seconds, light=True):
        self._pending = self.level_for(float(seconds))

    @property
    def image_ready(self):
        return True

    def get_image(self):
        value = int(np.clip(self._pending, 0.0, 1.0) * 65535)
        frame = np.full((120, 160), value, dtype=np.uint16)
        # A brighter patch, so the frame is not perfectly flat.
        frame[40:80, 60:100] = min(65535, int(value * 1.4))
        return frame


class Rigs:
    def __init__(self, camera):
        self.camera = camera

    @property
    def master(self):
        return self

    @property
    def manager(self):
        return self

    def get(self, kind):
        return self.camera if kind == "piercam" else None


def service(camera, **overrides):
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    if overrides:
        config.update("piercam", overrides)
    return piercam.PierCamService(Rigs(camera), config)


def settle(svc, camera, frames=60):
    """Run the loop `frames` times against the scene and report where it got to."""
    settings = svc.settings()
    for _ in range(frames):
        exposure, gain = svc._current(camera, settings)
        camera.set_settings(gain=gain)
        level = camera.level_for(exposure)
        svc._correct(camera, settings, level)
    exposure, gain = svc._current(camera, settings)
    camera.set_settings(gain=gain)
    return camera.level_for(exposure), exposure, gain


TARGET = 0.35

# --------------------------------------------------------------- convergence
#
# Fourteen stops of scene brightness, which is roughly noon to a dark dome.
for label, scene in [("bright afternoon", 2000.0),
                     ("sunset", 300.0),
                     ("dusk", 12.0),
                     ("moonlit dome", 0.4),
                     ("dark dome", 0.02)]:
    camera = Camera(scene)
    svc = service(camera)
    level, exposure, gain = settle(svc, camera)
    case(f"converges on target in a {label}",
         abs(level - TARGET) <= 0.35 * TARGET,
         f"level {level:.3f}, {exposure:.5f}s, gain {gain}")

# Brighter than the camera can answer even wide open at its shortest exposure.
# It cannot win, but it must lose quietly: pinned at the floor, at minimum gain,
# not oscillating between extremes frame after frame.
camera = Camera(1e6)
svc = service(camera)
settle(svc, camera)
settings = svc.settings()
seen = []
for _ in range(20):
    exposure, gain = svc._current(camera, settings)
    camera.set_settings(gain=gain)
    seen.append((exposure, gain))
    svc._correct(camera, settings, camera.level_for(exposure))
case("a scene it cannot expose for pins at the floor without hunting",
     len(set(seen)) == 1 and seen[0][1] == 0, f"{seen[0][0]:.6f}s, gain {seen[0][1]}")

# ------------------------------------------------------------------ stability
#
# Once settled it must sit still: a feed that breathes all night is worse to
# watch than one slightly off target.
camera = Camera(12.0)
svc = service(camera)
settle(svc, camera)
settings = svc.settings()
seen = []
for _ in range(30):
    exposure, gain = svc._current(camera, settings)
    camera.set_settings(gain=gain)
    seen.append(exposure)
    svc._correct(camera, settings, camera.level_for(exposure))
spread = max(seen) / min(seen)
case("it sits still once it has settled", spread < 1.05, f"spread x{spread:.3f}")

# ---------------------------------------------------------------- the deadband
camera = Camera(12.0)
svc = service(camera)
settle(svc, camera)
before = svc._exposure
# A nudge well inside the deadband must move nothing at all.
svc._correct(camera, svc.settings(), TARGET * 1.04)
case("a small error inside the deadband moves nothing", svc._exposure == before)

svc._correct(camera, svc.settings(), TARGET * 2.0)
case("a large error does move it", svc._exposure != before)

# ------------------------------------------------------------- gain discipline
#
# Gain is the noisy way to get brightness, so the loop should reach for exposure
# first and only use gain once the exposure has run out of room.
camera = Camera(50.0)                       # easy scene, well inside the range
svc = service(camera)
_, exposure, gain = settle(svc, camera)
case("an easy scene is handled on exposure alone, at zero gain", gain == 0,
     f"gain {gain}, {exposure:.4f}s")

camera = Camera(0.002)                      # darker than the longest exposure
svc = service(camera)
_, exposure, gain = settle(svc, camera)
case("a scene too dark for the longest exposure raises the gain", gain > 0,
     f"gain {gain}, {exposure:.3f}s")
case("and it does not exceed the camera's maximum", gain <= camera.gain_max)

# Dawn: the light comes back, and the gain must come back down with it.
camera.scene = 50.0
_, exposure, gain = settle(svc, camera)
case("gain comes back down when the light returns", gain == 0,
     f"gain {gain}, {exposure:.4f}s")

# A ceiling in the settings is respected even though the camera allows more.
camera = Camera(0.0005)
svc = service(camera, maxGain=120)
_, exposure, gain = settle(svc, camera)
case("a gain ceiling in the settings is respected", gain <= 120, f"gain {gain}")

# ------------------------------------------------------------ exposure limits
camera = Camera(1e9)                        # absurdly bright
svc = service(camera, minExposure=0.0005)
_, exposure, gain = settle(svc, camera)
case("exposure never goes below the floor", exposure >= 0.0005 - 1e-12,
     f"{exposure:.6f}s")

camera = Camera(1e-9)                       # absurdly dark
svc = service(camera, maxExposure=4.0)
_, exposure, gain = settle(svc, camera)
case("and never above the ceiling, so the feed stays live",
     exposure <= 4.0 + 1e-9, f"{exposure:.3f}s")

# A blown-out or black frame must not divide by zero or run away.
camera = Camera(12.0)
svc = service(camera)
svc._correct(camera, svc.settings(), 0.0)
case("a completely black frame is handled", svc._exposure is not None
     and svc._exposure > 0)
svc._correct(camera, svc.settings(), 1.0)
case("a completely white frame is handled", svc._exposure > 0)

# ------------------------------------------------------------- manual override
camera = Camera(12.0)
svc = service(camera, auto=False, exposure=0.5, gain=77)
exposure, gain = svc._current(camera, svc.settings())
case("turning auto off uses the fixed exposure", abs(exposure - 0.5) < 1e-9)
case("and the fixed gain", gain == 77)

# ---------------------------------------------------------------- the metering
#
# Mostly-dark frame with a bright telescope in it: metering on the mean would
# expose for the wall and blow out the thing you want to look at.
frame = np.full((200, 200), 200, dtype=np.uint16)
frame[50:150, 50:150] = 40000           # the telescope, a quarter of the frame
mean_level = float(frame.mean()) / piercam.MAX_ADU
metered = piercam._level_of(frame, 95.0)
case("metering follows the bright subject, not the dark wall",
     metered > mean_level * 2,
     f"{metered:.3f} vs mean {mean_level:.3f}")

# Which is the point: metering on the mean would expose for the wall and clip
# the telescope. The percentile meter asks for a shorter exposure instead.
case("so a mostly-dark frame is not over-exposed", metered > TARGET,
     f"{metered:.3f} against a target of {TARGET}")

# ------------------------------------------------------------------ rendering
frame = np.linspace(0, 65535, 256 * 256, dtype=np.float64).reshape(256, 256)
payload, width, height = piercam._to_png(frame.astype(np.uint16), 128, 2.2)
case("a frame renders to a PNG", payload[:8] == b"\x89PNG\r\n\x1a\n")
case("and is downsampled to fit the wire", max(width, height) <= 128,
     f"{width}x{height}")

payload, width, height = piercam._to_png(frame.astype(np.uint16), 4000, 2.2)
case("a small frame is not blown up", width == 256 and height == 256)

# Gamma has to lift the midtones, or a correctly exposed pier cam still looks
# like a black rectangle on a screen.
ramp = np.full((8, 8), int(0.25 * 65535), dtype=np.uint16)
linear, _, _ = piercam._to_png(ramp, 64, 1.0)
gamma, _, _ = piercam._to_png(ramp, 64, 2.2)
case("gamma lifts the midtones", len(gamma) > 0 and len(linear) > 0)

# ------------------------------------------------- a frame, end to end
#
# Through the real device protocol — start_exposure, image_ready, get_image —
# rather than only through the control loop, because that seam is where a
# driver-shaped mistake would hide.
camera = Camera(12.0)
svc = service(camera)
for _ in range(25):
    svc._grab(camera, svc.settings())
payload, token = svc.frame()
status = svc.status()
case("grabbing produces a PNG", payload[:8] == b"\x89PNG\r\n\x1a\n",
     f"{len(payload)} bytes")
case("and a token that changes with the frame", bool(token))
first = token
svc._grab(camera, svc.settings())
case("a new frame gets a new token", svc.frame()[1] != first)
case("the status says what it is doing",
     status["connected"] and status["hasFrame"] and status["frames"] == 25)
case("and it has exposed itself correctly by now",
     abs(status["level"] - TARGET) <= 0.35 * TARGET,
     f"level {status['level']}, {status['exposure']}s, gain {status['gain']}")
case("the frame is sent no larger than asked for",
     max(status["width"], status["height"]) <= 720,
     f"{status['width']}x{status['height']}")

# Disconnecting must not leave a stale picture on screen looking live.
svc.rigs.camera = None
case("a camera that goes away is reported as disconnected",
     svc.status()["connected"] is False)

# ------------------------------------------------------------ nothing connected
svc = service(None)
svc.rigs.camera = None
status = svc.status()
case("no camera reports disconnected rather than failing",
     status["connected"] is False and status["hasFrame"] is False)

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
