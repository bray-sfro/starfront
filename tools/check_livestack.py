"""The desktop live stacker, from a raw sub on disk to a local stack.

    python tools/check_livestack.py

The thing being checked is the single-rig preview in `astrocontrol/livestack.py`
and `pipeline.py`: that every machine builds the identical canvas from the same
handful of numbers, that a tile added twice is counted once, that a frame
nobody can place is refused rather than smeared across the image.

It once also drove `/api/v1/agent/stack/*` on the server. Those routes lived in
`server/stacks.py`, which was never mounted and is gone (the distributed
stacking plan moves the shared stack to the Collab engine on a hub), so the
network half of this check went with it.

What it walks through:

  * two invented observatories with different focal lengths and different
    sensors;
  * raw subs written to disk with the headers a real program writes, dust and
    an offset baked in, plus the masters that correct them;
  * the calibration library matching those masters and applying them;
  * the pipeline placing each calibrated frame, resampling it onto the
    canvas and folding it into a local stack;
  * tiles added twice, tiles of the wrong sky and rubbish refused.

No test framework, for the same reason as the other checks here.
"""

import math
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HOME = Path(tempfile.mkdtemp(prefix="check-livestack-rig-"))
os.environ.setdefault("ASTRO_DATA_DIR", str(HOME))

from astrocontrol import calibration, collab, livestack, pipeline   # noqa: E402
from astrocontrol.config import Config                              # noqa: E402
from astrocontrol.imaging import align, fits, stars, wcs            # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


# --------------------------------------------------------------- the sky
TRUE_RA, TRUE_DEC = 84.05, -6.4
RANDOM = np.random.default_rng(7)
STAR_COUNT = 1600
SKY = np.column_stack([
    TRUE_RA + (RANDOM.random(STAR_COUNT) - 0.5) * 2.4 / math.cos(math.radians(TRUE_DEC)),
    TRUE_DEC + (RANDOM.random(STAR_COUNT) - 0.5) * 1.8,
    140.0 * 10.0 ** (RANDOM.random(STAR_COUNT) * 1.9),
])
SEEING = 3.2


def render_sky(solution, gain, sky, noise, seed):
    """The invented sky through one telescope, before anything is done to it."""
    rng = np.random.default_rng(seed)
    frame = np.full((solution.height, solution.width), float(sky))
    x, y, ahead = solution.to_pixel(SKY[:, 0], SKY[:, 1])
    sigma = SEEING / solution.scale
    reach = int(math.ceil(sigma * 3.5))
    for index in np.nonzero(ahead)[0]:
        cx, cy = float(x[index]) - 1.0, float(y[index]) - 1.0
        if not (reach + 1 < cx < solution.width - reach - 2
                and reach + 1 < cy < solution.height - reach - 2):
            continue
        x0, y0 = int(round(cx)) - reach, int(round(cy)) - reach
        gx = np.arange(x0, x0 + 2 * reach + 1)
        gy = np.arange(y0, y0 + 2 * reach + 1)
        frame[y0:y0 + 2 * reach + 1, x0:x0 + 2 * reach + 1] += (
            SKY[index, 2] * gain
            * np.exp(-((gx[None, :] - cx) ** 2 + (gy[:, None] - cy) ** 2)
                     / (2 * sigma * sigma)))
    return frame + rng.normal(0.0, noise, frame.shape)


# --------------------------------------------------------------- two rigs
class Observatory:
    """An invented telescope, its masters, and the frames it has taken."""

    def __init__(self, name, focal, pixel, width, height, gain, sky, noise,
                 rotation, offset):
        self.name = name
        self.focal, self.pixel = focal, pixel
        self.width, self.height = width, height
        self.gain, self.sky, self.noise = gain, sky, noise
        self.scale = 206.265 * pixel / focal
        self.rotation = rotation
        self.folder = HOME / "captures" / name
        self.folder.mkdir(parents=True, exist_ok=True)
        # Where this telescope really points, which is where its subs come
        # from and what the pipeline has to rediscover.
        self.truth = wcs.Wcs(
            TRUE_RA + offset[0], TRUE_DEC + offset[1],
            (width + 1) / 2, (height + 1) / 2,
            wcs._cd_from_angle(-self.scale / 3600, self.scale / 3600, rotation),
            width, height)
        # A bias pedestal and a dust-and-vignetting pattern, which is what
        # the masters exist to take back out.
        rng = np.random.default_rng(abs(hash(name)) % 10000)
        self.bias = 480.0 + rng.normal(0.0, 3.0, (height, width))
        ys, xs = np.mgrid[0:height, 0:width]
        radius = np.hypot((xs - width / 2) / (width / 2),
                          (ys - height / 2) / (height / 2))
        self.vignette = 1.0 - 0.28 * radius ** 2
        for _ in range(12):                       # dust motes
            cx, cy = rng.random(2) * [width, height]
            spot = np.exp(-((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * 26 ** 2))
            self.vignette -= 0.22 * spot

    def write_masters(self, library):
        """Put this telescope's bias and flat into the calibration library."""
        meta = {"exposure": 0.0, "binning": 1, "gain": 100, "offset": 50,
                "temperature": -10.0, "filter": "", "telescope": self.name,
                "camera": f"{self.name} camera"}
        library.store("bias", calibration.to_uint16(self.bias), meta,
                      {"frames": 50, "method": "sigma clip"})
        flat = self.vignette * 26000.0 + self.bias
        library.store("flat", calibration.to_uint16(flat),
                      {**meta, "exposure": 2.0, "filter": "L"},
                      {"frames": 25, "method": "sigma clip"})

    def expose(self, index, seed, project=""):
        """Take one sub and write it exactly as a real program would.

        Deliberately *without* a CD matrix: the frame carries a pointing, an
        angle and a scale in a program's own keywords, which is the awkward
        case the whole registrar exists for. The angle is a little wrong, as
        a real rotator's is.
        """
        dither = ((index % 3) - 1) * 12.0 / 3600.0
        pointing = wcs.Wcs(self.truth.crval1 + dither,
                           self.truth.crval2 - dither, self.truth.crpix1,
                           self.truth.crpix2, self.truth.cd,
                           self.width, self.height)
        sky = render_sky(pointing, self.gain, self.sky, self.noise, seed)
        raw = np.clip(sky * self.vignette + self.bias, 0, 65535).astype(np.uint16)
        centre = pointing.centre
        header = {
            "IMAGETYP": ("LIGHT", ""),
            "OBJECT": ("NGC 9999", ""),
            "EXPTIME": (300.0, "seconds"), "EXPOSURE": (300.0, "seconds"),
            "FILTER": ("L", ""), "TELESCOP": (self.name, ""),
            "INSTRUME": (f"{self.name} camera", ""),
            "XBINNING": (1, ""), "YBINNING": (1, ""),
            "GAIN": (100, ""), "OFFSET": (50, ""), "CCD-TEMP": (-10.0, ""),
            "FOCALLEN": (self.focal, ""), "XPIXSZ": (self.pixel, ""),
            "YPIXSZ": (self.pixel, ""),
            "OBJCTRA": (_hms(centre[0] / 15.0), "RA in hours"),
            "OBJCTDEC": (_dms(centre[1]), "Dec in degrees"),
            # Three degrees out, which is what a real rotator keyword is.
            "OBJCTROT": (round((self.rotation + 3.0) % 360.0, 3), ""),
            "PROJID": (project or None, "collaboration id"),
            "DATE-OBS": (fits.utc_now(), ""),
        }
        path = self.folder / f"{self.name}_L_{index:04d}.fits"
        fits.write(path, raw, header)
        return path


def _hms(hours):
    hours = hours % 24.0
    h = int(hours)
    m = int((hours - h) * 60)
    s = (hours - h - m / 60.0) * 3600.0
    return f"{h:02d} {m:02d} {s:06.3f}"


def _dms(degrees):
    sign = "-" if degrees < 0 else "+"
    degrees = abs(degrees)
    d = int(degrees)
    m = int((degrees - d) * 60)
    s = (degrees - d - m / 60.0) * 3600.0
    return f"{sign}{d:02d} {m:02d} {s:05.2f}"


ALPHA = Observatory("alpha", focal=530.0, pixel=3.76, width=1600, height=1200,
                    gain=1.0, sky=760.0, noise=11.0, rotation=12.0,
                    offset=(0.0, 0.0))
BETA = Observatory("beta", focal=380.0, pixel=4.63, width=1400, height=1050,
                   gain=2.1, sky=1900.0, noise=26.0, rotation=201.0,
                   offset=(0.05, -0.04))

print("== two invented observatories ==")
for rig in (ALPHA, BETA):
    print(f'   {rig.name}: {rig.focal:g} mm, {rig.scale:.3f}"/px, '
          f"rotated {rig.rotation:g} deg")

# --------------------------------------------------------------- calibration
print("\n== calibration ==")
config = Config()
config.update("calibration", {"libraryDirectory": str(HOME / "library"),
                              "applyTo": "all", "maxFlatAgeDays": 0.0,
                              "maxDarkAgeDays": 0.0})
library = calibration.Library(config)
for rig in (ALPHA, BETA):
    rig.write_masters(library)
case("both telescopes' masters are in the library",
     len(library.masters()) == 4,
     ", ".join(sorted(m["type"] + "/" + (m["telescope"] or "?")
                      for m in library.masters())))

calibrated = {}
for rig in (ALPHA, BETA):
    path = rig.expose(0, seed=11, project="")
    want = {"width": rig.width, "height": rig.height, "binning": 1,
            "gain": 100, "offset": 50, "temperature": -10.0, "filter": "L",
            "telescope": rig.name, "camera": f"{rig.name} camera",
            "exposure": 300.0}
    result = library.calibrate_file(path, want)
    case(f"{rig.name}: its own masters are matched and applied",
         result.get("calibrated") and set(result["steps"]) == {"bias", "flat"},
         result.get("steps") or result.get("reasons"))
    calibrated[rig.name] = Path(result["path"])

# The flat has to have actually done something: a calibrated frame should be
# flat across the field where the raw one was vignetted by nearly a third.
raw_frame, _ = fits.read(ALPHA.folder / "alpha_L_0000.fits")
cal_frame, cal_header = fits.read(calibrated["alpha"])


def corner_ratio(frame):
    middle = float(np.median(frame[500:700, 700:900]))
    corner = float(np.median(frame[20:220, 20:220]))
    return corner / max(middle, 1e-9)


case("the flat takes the vignetting out",
     abs(corner_ratio(cal_frame) - 1.0) < abs(corner_ratio(raw_frame) - 1.0) / 3,
     f"corner/centre {corner_ratio(raw_frame):.3f} raw, "
     f"{corner_ratio(cal_frame):.3f} calibrated")
case("the calibrated frame says what was done to it",
     str(cal_header.get("CALSTAT") or "") == "BF",
     f"CALSTAT={cal_header.get('CALSTAT')!r}")
case("the raw frame is untouched",
     (ALPHA.folder / "alpha_L_0000.fits").is_file()
     and abs(corner_ratio(raw_frame) - 1.0) > 0.2)

# --------------------------------------------------------------- a project
print("\n== a canvas ==")
PROJECT = "prj_check_live"
# What the hub's engine now does for a collaboration, done here for the one
# rig-local preview: one canvas from the region and the median scale.
plan = livestack.Plan.for_region(TRUE_RA, TRUE_DEC, 1.6, 1.2,
                                 scales=[ALPHA.scale, BETA.scale])
payload = plan.payload()
canvas_here = plan.canvas()
canvas_there = livestack.Plan.read(payload).canvas()
case("the canvas is identical when rebuilt from its numbers, to the pixel",
     canvas_here.payload() == canvas_there.payload(),
     f"{payload['pixelWidth']}x{payload['pixelHeight']} at "
     f"{payload['actualScale']:.3f}\"/px")
case("the canvas covers the project's region",
     canvas_here.field[0] >= 1.6 - 1e-6 and canvas_here.field[1] >= 1.2 - 1e-6,
     f"{canvas_here.field[0]:.3f}x{canvas_here.field[1]:.3f} deg")

# --------------------------------------------------------------- the pipeline
print("\n== the pipeline ==")
sent = []


def send(tile, job):
    sent.append({"added": True, "tile": tile.id})
    return True


config.update("livestack", {"enabled": True, "maxPixels": plan.maxPixels})
stacker = pipeline.StackPipeline(config, root=HOME / "stacks", send=send)

stars.MAX_STARS = 400
frames = 0
for index in range(3):
    for rig in (ALPHA, BETA):
        path = rig.expose(index, seed=100 + index * 7 + len(rig.name),
                          project=PROJECT)
        want = {"width": rig.width, "height": rig.height, "binning": 1,
                "gain": 100, "offset": 50, "temperature": -10.0, "filter": "L",
                "telescope": rig.name, "camera": f"{rig.name} camera",
                "exposure": 300.0}
        result = library.calibrate_file(path, want)
        if not result.get("calibrated"):
            case(f"{rig.name} sub {index} calibrates", False, result["reasons"])
            continue
        job = pipeline.Job(Path(result["path"]), PROJECT, "L", plan,
                           night="2026-09-24", agent=rig.name, seconds=300.0,
                           telescope=rig.name)
        stacker.submit(job)
        frames += 1

# Waited out on the *sent* count as well as the stacked one. A frame is
# counted as stacked before its tile is handed over, so a wait that watched
# only the stacking would let this read `sent` while the last upload was
# still in flight — and then report that the server had missed a tile it had
# in fact already taken.
deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    status = stacker.status()
    counts = status["counts"]
    done = counts["stacked"] + counts["refused"]
    delivered = counts["sent"] + counts["spooled"]
    if status["pending"] == 0 and done >= frames and delivered >= frames:
        break
    time.sleep(0.3)
stacker.stop()

status = stacker.status()
case("every sub reached the local stack",
     status["counts"]["stacked"] == frames,
     f"{status['counts']['stacked']} of {frames} stacked, "
     f"{status['counts']['refused']} refused")
case("the pipeline reports what it did to the last frame",
     bool((status.get("last") or {}).get("placing")),
     (status.get("last") or {}).get("placing", {}).get("detail", "")[:80])

local = stacker.stacks().get(f"{PROJECT}/L")
case("the local stack holds both telescopes",
     local is not None and len(local.summary()["agents"]) == 2,
     local and f"{local.summary()['frames']} frames from "
               f"{', '.join(local.summary()['agents'])}")
case("the local stack built an astrometric reference",
     local is not None and len(local.reference) > 100,
     local and f"{len(local.reference)} stars")

# Every header here says an angle three degrees from the truth, so a pipeline
# that took them at their word would smear the stack. The last frame in must
# therefore have been *aligned*, not trusted — that is the whole exercise.
placing = (status.get("last") or {}).get("placing") or {}
case("the last frame was aligned rather than trusted",
     bool(placing.get("aligned")) and placing.get("residual", 99) < 1.5,
     placing.get("detail", "")[:90])

# --------------------------------------------------------------- tiles
print("\n== tiles ==")
case("every tile was handed on", len(sent) == frames,
     f"{len(sent)} of {frames}")

# Adding the same tile again must not count it twice. A retry is the normal
# case rather than an error, and a ledger that counts the same hour twice is
# the one way it silently becomes wrong.
repeat_frame, repeat_header = fits.read(calibrated["alpha"])
repeat_tile = livestack.make_tile(
    repeat_frame, wcs.from_header(repeat_header), canvas_here,
    "alpha:repeat", seconds=300, agent="alpha", filter_name="L")
first = local.add(repeat_tile)
again = local.add(repeat_tile)
case("the same tile twice is counted once",
     bool(first.get("added")) and not again.get("added")
     and again.get("duplicate") is True,
     f"{local.summary()['frames']} frames after both")

# --------------------------------------------------------------- refusals
print("\n== refusals ==")
elsewhere = Observatory("gamma", focal=530.0, pixel=3.76, width=1600,
                        height=1200, gain=1.0, sky=760.0, noise=11.0,
                        rotation=12.0, offset=(28.0, 14.0))
elsewhere.write_masters(library)
stray = elsewhere.expose(0, seed=999, project=PROJECT)
result = library.calibrate_file(stray, {
    "width": 1600, "height": 1200, "binning": 1, "gain": 100, "offset": 50,
    "temperature": -10.0, "filter": "L", "telescope": "gamma",
    "camera": "gamma camera", "exposure": 300.0})
frame, header = fits.read(Path(result["path"]))
solution = wcs.from_header(header)
try:
    tile = livestack.make_tile(frame, solution, canvas_here, "gamma:1",
                               seconds=300, agent="gamma", filter_name="L")
    refused = local.add(tile)
    case("a frame of the wrong sky is refused rather than stacked",
         not refused.get("added"), str(refused.get("detail", ""))[:90])
except livestack.StackError as exc:
    case("a frame of the wrong sky is refused rather than stacked", True,
         str(exc)[:90])

try:
    livestack.Tile.decode(b"not a tile at all")
    case("rubbish is refused", False, "it was accepted")
except livestack.StackError as exc:
    case("rubbish is refused with a reason", True, str(exc)[:70])

# --------------------------------------------------------------- keeping it
print("\n== keeping it ==")
stacker.save_all()
reopened = livestack.LiveStack.open(
    livestack.stack_root(HOME / "stacks", PROJECT, "L"))
case("a stack survives being closed and reopened",
     reopened.frames == local.frames
     and float(np.max(np.abs(reopened.mean() - local.mean()))) < 1e-9,
     f"{reopened.frames} frames")
stored = livestack.stored_plan(
    livestack.stack_root(HOME / "stacks", PROJECT, "L"))
case("the canvas a stack was built on is remembered",
     stored is not None and stored.payload() == plan.payload())

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
