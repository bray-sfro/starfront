"""Registering frames by plate solution, and stacking what comes out.

    python tools/check_register.py
    python tools/check_register.py "F:\\Images\\for claude"

Two halves.

**Synthetic**, always run: a star field is invented, then photographed twice
by two made-up telescopes at different scales, rotations and handedness, with
different sky levels, different sensitivities and different noise. The truth
is known exactly, so every stage can be held to it — the gradient fit must
recover a planted ramp it was never told about, the alignment must recover
the rotation to a hundredth of a degree, the resampling must put the stars
back where they started, the photometric match must recover the sensitivity
ratio, and the rejection must eat a satellite trail without eating the
stars.

**Real**, run when a folder of frames is given: the same pipeline over actual
subs and actual masters from actual telescopes, which is where the header
dialects, the vignetting and the true awkwardness live. The folder is
expected to hold one directory per telescope, each with a light and its
`masterBias`/`masterFlat`.

No test framework, for the same reason as the other checks here.
"""
import math
import os
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from pathlib import Path                                          # noqa: E402

from astrocontrol import calibration, livestack                   # noqa: E402
from astrocontrol.imaging import (align, fits, gradient, reproject,  # noqa: E402
                                  stars, wcs, xisf)

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


# ---------------------------------------------------------------------------
# An invented sky, and two telescopes that photograph it
# ---------------------------------------------------------------------------

TRUE_RA, TRUE_DEC = 84.0, -6.5
RANDOM = np.random.default_rng(20260924)

#: Stars scattered over a couple of degrees, with a wide range of brightness
#: so that two telescopes of different aperture see overlapping but not
#: identical sets — which is the case the matcher actually has to survive.
STAR_COUNT = 1400
SKY_STARS = np.column_stack([
    TRUE_RA + (RANDOM.random(STAR_COUNT) - 0.5) * 2.6 / math.cos(math.radians(TRUE_DEC)),
    TRUE_DEC + (RANDOM.random(STAR_COUNT) - 0.5) * 2.0,
    120.0 * 10.0 ** (RANDOM.random(STAR_COUNT) * 1.9),  # relative brightness
])


#: Seeing, in arcseconds, and the same for both invented telescopes. In
#: *arcseconds*, deliberately, not in pixels: a star is a certain size on the
#: sky, and two telescopes at different scales record it across different
#: numbers of pixels. Inventing it in pixels instead would give the two
#: frames different angular star sizes, and the single number relating them
#: photometrically would then not exist — the fit would return the slope that
#: best relates two different shapes, which is a real thing this has to cope
#: with but not a thing to measure a known answer against.
SEEING = 3.4


def photograph(solution, gain, sky, noise, seed, trail=False):
    """Render the invented sky through a given plate solution.

    Gaussian stars of a fixed angular size, on a sky of the given level with
    the given read noise. `gain` is the telescope's sensitivity, which is
    what the photometric match has to recover without being told: with the
    star size fixed on the sky, two frames resampled onto one canvas differ
    by exactly this factor and nothing else.
    """
    rng = np.random.default_rng(seed)
    frame = np.full((solution.height, solution.width), float(sky))
    x, y, ahead = solution.to_pixel(SKY_STARS[:, 0], SKY_STARS[:, 1])
    sigma = SEEING / solution.scale
    reach = int(math.ceil(sigma * 3.5))
    for index in np.nonzero(ahead)[0]:
        cx, cy = float(x[index]) - 1.0, float(y[index]) - 1.0
        # Room for the whole blob, counting the rounding: a star one pixel
        # inside the guard still writes a patch that runs off the array.
        if not (reach + 1 < cx < solution.width - reach - 2
                and reach + 1 < cy < solution.height - reach - 2):
            continue
        brightness = SKY_STARS[index, 2] * gain
        x0, y0 = int(round(cx)) - reach, int(round(cy)) - reach
        gx = np.arange(x0, x0 + 2 * reach + 1)
        gy = np.arange(y0, y0 + 2 * reach + 1)
        blob = (brightness
                * np.exp(-((gx[None, :] - cx) ** 2 + (gy[:, None] - cy) ** 2)
                         / (2 * sigma * sigma)))
        frame[y0:y0 + 2 * reach + 1, x0:x0 + 2 * reach + 1] += blob
    if trail:
        # A satellite: a bright diagonal line across the whole frame.
        step = np.arange(0, min(solution.width, solution.height))
        frame[step, step] += 9000.0
    frame += rng.normal(0.0, noise, frame.shape)
    return np.clip(frame, 0, 65535).astype(np.uint16)


# Two telescopes: different focal length, rotated 137 degrees apart, and the
# second mirrored — every axis of disagreement at once.
ALPHA = wcs.Wcs(TRUE_RA, TRUE_DEC, 700.5, 500.5,
                wcs._cd_from_angle(-2.0 / 3600, 2.0 / 3600, 18.0),
                1400, 1000)
BETA_TRUE = wcs.Wcs(TRUE_RA + 0.09, TRUE_DEC - 0.06, 600.5, 450.5,
                    wcs._cd_from_angle(2.6 / 3600, 2.6 / 3600, 155.0),
                    1200, 900)

print("== two invented telescopes ==")
print(f"   alpha {ALPHA.describe()}")
print(f"   beta  {BETA_TRUE.describe()}")

frame_a = photograph(ALPHA, gain=1.0, sky=800.0, noise=12.0, seed=1)
frame_b = photograph(BETA_TRUE, gain=2.4, sky=2100.0, noise=30.0, seed=2)
case("both invented frames have stars in them",
     len(stars.detect(frame_a)) > 50 and len(stars.detect(frame_b)) > 50,
     f"{len(stars.detect(frame_a))} and {len(stars.detect(frame_b))}")

# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------
print("\n== alignment ==")
# Beta's header is wrong the way a real one is: four degrees of rotator slop,
# half a per cent of scale error, and an arcminute of pointing error.
BETA_GUESS = wcs.Wcs(BETA_TRUE.crval1 + 0.012, BETA_TRUE.crval2 - 0.009,
                     BETA_TRUE.crpix1, BETA_TRUE.crpix2,
                     wcs._cd_from_angle(2.6 * 1.005 / 3600, 2.6 * 1.005 / 3600,
                                        159.0),
                     1200, 900, source="derived")

stars.MAX_STARS = 400
reference = align.star_sky(stars.detect(frame_a), ALPHA)
try:
    fixed, note = align.refine(BETA_GUESS, align.star_pixels(stars.detect(frame_b)),
                               reference, reference=ALPHA)
    case("a wrong header is corrected against the stars", True, note["detail"])
    case("the corrected rotation matches the truth",
         abs(((fixed.rotation - BETA_TRUE.rotation + 180) % 360) - 180) < 0.05,
         f"{fixed.rotation:.4f} vs {BETA_TRUE.rotation:.4f}")
    case("the corrected scale matches the truth",
         abs(fixed.scale - BETA_TRUE.scale) < 0.005,
         f'{fixed.scale:.5f}" vs {BETA_TRUE.scale:.5f}"')
    case("the corrected handedness matches the truth",
         fixed.mirrored == BETA_TRUE.mirrored)
    centre = fixed.centre
    truth = BETA_TRUE.centre
    gap = math.hypot((centre[0] - truth[0]) * math.cos(math.radians(truth[1])),
                     centre[1] - truth[1]) * 3600.0
    case("the corrected pointing matches the truth", gap < 3.0,
         f'{gap:.2f}" from the truth')
except align.AlignError as exc:
    case("a wrong header is corrected against the stars", False, str(exc))
    fixed = BETA_TRUE

# Half a turn out: the commonest real dialect difference, and it must be
# found without anybody configuring a per-program quirk.
turned = wcs.Wcs(BETA_GUESS.crval1, BETA_GUESS.crval2, BETA_GUESS.crpix1,
                 BETA_GUESS.crpix2, tuple(-v for v in BETA_GUESS.cd),
                 1200, 900, source="derived")
try:
    half, note = align.refine(turned, align.star_pixels(stars.detect(frame_b)),
                              reference, reference=ALPHA)
    case("a header half a turn out is still matched",
         abs(((half.rotation - BETA_TRUE.rotation + 180) % 360) - 180) < 0.05,
         note["detail"][:70])
except align.AlignError as exc:
    case("a header half a turn out is still matched", False, str(exc))

# A frame of somewhere else must be refused, not forced.
elsewhere = wcs.Wcs(TRUE_RA + 30.0, TRUE_DEC, 600.5, 450.5, BETA_TRUE.cd,
                    1200, 900, source="derived")
try:
    align.refine(elsewhere, align.star_pixels(stars.detect(frame_b)),
                 reference, reference=ALPHA)
    case("a frame of other sky is refused", False, "it was aligned anyway")
except align.AlignError:
    case("a frame of other sky is refused", True)

# ---------------------------------------------------------------------------
# Gradients
# ---------------------------------------------------------------------------
# A ramp is planted at a slope the fit is never told, and has to come back.
# The hard case is a ramp with a bright object sitting on one side of it: an
# object only ever adds light, so a fit that rejects symmetrically leans into
# it and tilts the answer in the one direction nobody would notice.
print("\n== gradients ==")


def ramped(slope_x, slope_y, sky=800.0, nebula=0.0, noise=12.0, seed=31):
    """A star field on a tilted sky, optionally with a nebula on one side."""
    rng = np.random.default_rng(seed)
    height, width = 1000, 1400
    rows, columns = np.mgrid[0:height, 0:width]
    frame = (sky + slope_x * columns + slope_y * rows
             + rng.normal(0.0, noise, (height, width)))
    for _ in range(500):
        cx, cy = rng.random(2) * [width - 24, height - 24] + 12
        peak = 200.0 * 10.0 ** (rng.random() * 1.8)
        gx = np.arange(int(cx) - 5, int(cx) + 6)
        gy = np.arange(int(cy) - 5, int(cy) + 6)
        frame[int(cy) - 5:int(cy) + 6, int(cx) - 5:int(cx) + 6] += peak * np.exp(
            -((gx[None, :] - cx) ** 2 + (gy[:, None] - cy) ** 2) / (2 * 1.8 ** 2))
    if nebula:
        frame += nebula * np.exp(
            -(((columns - width * 0.72) / 230.0) ** 2
              + ((rows - height * 0.33) / 180.0) ** 2))
    return frame


for label, sx, sy, nebula, tolerance in (
        ("a flat sky is left flat", 0.0, 0.0, 0.0, 0.002),
        ("a gentle ramp is recovered", 0.025, 0.0, 0.0, 0.03),
        ("a steep ramp is recovered", 0.15, 0.10, 0.0, 0.03),
        ("a ramp under a nebula is recovered", 0.12, -0.08, 900.0, 0.05),
        ("...and under a very bright one", 0.12, -0.08, 4000.0, 0.05)):
    surface = gradient.measure(ramped(sx, sy, nebula=nebula), degree=1)

    def off(fitted, wanted):
        """How far out a slope is: relatively where there is one to be
        relative to, absolutely where the truth is flat. A percentage of
        zero is not a measure of anything."""
        if abs(wanted) < 1e-9:
            return abs(fitted) < 0.002, f"{fitted:+.5f} vs flat"
        error = abs(fitted - wanted) / abs(wanted)
        return error < tolerance, f"{error * 100:.1f}% out"

    ok_x, said_x = off(surface.a, sx)
    ok_y, said_y = off(surface.b, sy)
    case(label, ok_x and ok_y,
         f"slope {surface.a:+.5f}/{surface.b:+.5f} vs {sx:+.5f}/{sy:+.5f} "
         f"({said_x}, {said_y})")

# The sky level has to survive too, or every frame arrives on a different
# zero and the photometric match spends its overlap measuring the offset.
surface = gradient.measure(ramped(0.05, 0.03), degree=1)
expected = 800.0 + 0.05 * (1400 - 1) / 2.0 + 0.03 * (1000 - 1) / 2.0
case("the sky level at the middle of the frame is right",
     abs(surface.level - expected) < 8.0,
     f"{surface.level:.1f} vs {expected:.1f} ADU")

# What matters in the end: after removal, is it actually flat?
flattened, surface = gradient.remove(ramped(0.12, -0.08, nebula=900.0), degree=1)
left = gradient.measure(flattened, degree=1)
case("what is left after removal has no ramp in it",
     abs(left.a) < 0.004 and abs(left.b) < 0.004,
     f"residual slope {left.a:+.5f}/{left.b:+.5f}")
case("the corners of a flattened frame agree",
     abs(float(np.median(flattened[:80, :80]))
         - float(np.median(flattened[-80:, -80:]))) < 6.0,
     f"{float(np.median(flattened[:80, :80])):+.1f} vs "
     f"{float(np.median(flattened[-80:, -80:])):+.1f} ADU")
# The *sky* lands on zero, not the median of the whole frame: a frame with
# five hundred stars and a nebula in it has a median above its own sky, and
# that is the signal, which is the entire point of keeping it.
case("removal leaves the sky on zero, negatives and all",
     abs(left.level) < 3.0 and float(flattened.min()) < 0,
     f"sky {left.level:+.2f} ADU, median {float(np.median(flattened)):+.2f} "
     f"(the signal), minimum {float(flattened.min()):.0f}")

# The rail: an object filling the frame leaves no sky to fit, and a plane
# fitted to it would carve a wedge out of the picture.
rows, columns = np.mgrid[0:1000, 0:1400]
filled = (800.0 + 5000.0 * np.exp(-(((columns - 700) / 640.0) ** 2
                                    + ((rows - 500) / 460.0) ** 2))
          + np.random.default_rng(3).normal(0.0, 12.0, (1000, 1400)))
surface = gradient.measure(filled, degree=1)
case("an object filling the frame does not become a gradient",
     abs(surface.a) < 0.02 and abs(surface.b) < 0.02,
     f"slope {surface.a:+.5f}/{surface.b:+.5f}, {surface.detail[:44]}")

# And a frame where a plane would run the sky negative falls back to a level.
steep = (200.0 + 0.9 * columns
         + np.random.default_rng(4).normal(0.0, 8.0, (1000, 1400)))
surface = gradient.measure(steep, degree=1)
case("a plane that would run the sky to nothing is refused",
     surface.degree == 0, surface.detail[:76])

case("degree zero is a plain level", gradient.measure(
    ramped(0.12, -0.08), degree=0).degree == 0)

# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------
print("\n== resampling ==")
canvas = wcs.grid(TRUE_RA, TRUE_DEC, 1.4, 1.1, 2.2)
values, covered, note = reproject.resample(frame_a, ALPHA, canvas)
case("a frame resamples onto a canvas", covered.any(),
     f"{note['coverage']:.2f} of the box covered, averaged by "
     f"{note['averagedBy']}")
case("uncovered canvas is masked rather than zero-filled",
     not covered.all() or note["coverage"] == 1.0,
     "the mask and the values are separate")

# The stars have to land where the sky says they should, not merely somewhere.
x0, y0 = note["box"][0], note["box"][1]
placed = np.zeros((canvas.height, canvas.width), dtype=np.float32)
placed[y0:y0 + values.shape[0], x0:x0 + values.shape[1]] = values
found = stars.detect(np.clip(placed, 0, 65535).astype(np.uint16))
if found:
    detected = align.star_pixels(found)
    want_x, want_y, ahead = canvas.to_pixel(SKY_STARS[:, 0], SKY_STARS[:, 1])
    wanted = np.column_stack([want_x[ahead], want_y[ahead]])
    distance = np.hypot(detected[:, None, 0] - wanted[None, :, 0],
                        detected[:, None, 1] - wanted[None, :, 1])
    nearest = distance.min(axis=1)
    hit = nearest < 2.0
    case("resampled stars land where the sky says they should",
         hit.mean() > 0.8 and float(np.median(nearest[hit])) < 0.7,
         f"{hit.sum()}/{len(nearest)} within 2 px, median "
         f"{float(np.median(nearest[hit])):.2f} px")
else:
    case("resampled stars land where the sky says they should", False,
         "no stars were found in the resampled frame")

# Block averaging must not move anything by half a pixel.
shrunk = reproject.block_average(frame_a.astype(np.float32), 2)
shrunk_wcs = reproject.shrink_wcs(ALPHA, 2)
sx, sy, _ = shrunk_wcs.to_pixel(SKY_STARS[:1, 0], SKY_STARS[:1, 1])
fx, fy, _ = ALPHA.to_pixel(SKY_STARS[:1, 0], SKY_STARS[:1, 1])
case("block averaging keeps the solution aligned",
     abs(float(sx[0]) - (float(fx[0]) + 0.5) / 2.0) < 1e-6,
     f"{float(sx[0]):.6f} vs {(float(fx[0]) + 0.5) / 2.0:.6f}")
case("block averaging keeps the flux scale",
     abs(float(shrunk.mean()) - float(frame_a.mean())) < 2.0,
     f"{shrunk.mean():.1f} vs {frame_a.mean():.1f}")

# ---------------------------------------------------------------------------
# The photometric match
# ---------------------------------------------------------------------------
print("\n== putting two telescopes on one scale ==")
a_values, a_mask, _ = reproject.resample(frame_a, ALPHA, canvas,
                                         (0, 0, canvas.width, canvas.height))
b_values, b_mask, _ = reproject.resample(frame_b, fixed, canvas,
                                         (0, 0, canvas.width, canvas.height))
a_sky, _ = reproject.background(a_values, a_mask)
b_sky, _ = reproject.background(b_values, b_mask)
gain, offset, match = reproject.scale_to(a_values - a_sky, b_values - b_sky,
                                         a_mask & b_mask)
case("two telescopes are matched on their overlap", match["matched"],
     match["detail"])
case("the measured gain is the true sensitivity ratio",
     match["matched"] and abs(gain - 2.4) / 2.4 < 0.08,
     f"{gain:.4f} vs 2.4 true")
scaled = (b_values - b_sky - offset) / max(gain, 1e-9)
overlap = a_mask & b_mask
difference = (scaled - (a_values - a_sky))[overlap]
signal = float(np.std((a_values - a_sky)[overlap]))
case("applying the gain makes the two frames agree",
     float(np.std(difference)) < signal * 0.45,
     f"residual {float(np.std(difference)):.1f} against a signal of {signal:.1f}")
case("the match is believed only when it correlates",
     match.get("correlation", 0) > 0.8, str(match.get("correlation")))

scrambled = np.array(b_values - b_sky)
RANDOM.shuffle(scrambled)
_, _, nonsense = reproject.scale_to(a_values - a_sky, scrambled, a_mask & b_mask)
case("an uncorrelated frame is not matched", not nonsense["matched"],
     nonsense["detail"][:70])

# ---------------------------------------------------------------------------
# Stacking
# ---------------------------------------------------------------------------
print("\n== the stack ==")
workspace = Path(tempfile.mkdtemp(prefix="check-register-"))
plan = livestack.Plan(ra=TRUE_RA, dec=TRUE_DEC, width=1.4, height=1.1,
                      scale=2.2)
stack = livestack.LiveStack(workspace / "stack", plan)

tile_a = livestack.make_tile(frame_a, ALPHA, stack.canvas, "alpha:1",
                             seconds=300, agent="alpha", filter_name="L")
report_a = stack.add(tile_a)
case("the first frame anchors the stack", report_a["added"], report_a["detail"])
stack.remember_stars(align.star_sky(stars.detect(frame_a), ALPHA))
case("the anchor's stars become the reference",
     len(stack.reference) > 50, f"{len(stack.reference)} reference stars")

tile_b = livestack.make_tile(frame_b, fixed, stack.canvas, "beta:1",
                             seconds=300, agent="beta", filter_name="L")
report_b = stack.add(tile_b)
case("a second telescope joins the stack", report_b["added"], report_b["detail"])
case("the second telescope was brought onto the first's scale",
     report_b["match"]["matched"] and 0.2 < report_b["gain"] < 5.0,
     f"gain {report_b['gain']}")

case("the same frame twice is counted once",
     not stack.add(tile_b)["added"] and stack.frames == 2,
     f"{stack.frames} frames")

# What a tile carries about what was taken off it, and the two normalisations.
background = tile_a.note.get("background") or {}
case("a tile records the background that was removed",
     background.get("degree") == 1 and background.get("level", 0) > 0,
     background.get("detail", "")[:76])
case("a tile is normalised to ADU per second",
     tile_a.note.get("perSecond") is True and tile_a.seconds == 300)

# The same sky at twice the exposure must land on the same scale, and must
# be weighed twice as heavily. That is the whole reason the exposure is
# divided out rather than rediscovered from an overlap.
#
# A real 600-second sub has twice the signal and only **root two** times the
# noise. Doubling both — the obvious way to fake one — makes a frame whose
# noise per second is identical to the 300-second version's, so it earns the
# same weight and the check passes for the wrong reason, or fails for one.
doubled = livestack.make_tile(
    photograph(ALPHA, gain=2.0, sky=1600.0, noise=12.0 * math.sqrt(2.0),
               seed=1),
    ALPHA, stack.canvas, "alpha:600s", seconds=600, filter_name="L")
one = tile_a.values[tile_a.weight > 0]
two = doubled.values[doubled.weight > 0]
ratio = float(np.std(two)) / max(float(np.std(one)), 1e-9)
case("a 600s sub and a 300s sub of the same sky land on one scale",
     0.9 < ratio < 1.1, f"spread ratio {ratio:.3f}, wanted 1")
heavier = (float(np.median(doubled.weight[doubled.weight > 0]))
           / float(np.median(tile_a.weight[tile_a.weight > 0])))
case("...and the longer one is weighed twice as heavily",
     1.7 < heavier < 2.3, f"{heavier:.2f}x the weight, wanted 2")

# A gradient across one frame must not survive into the stack.
tilted_rows, tilted_columns = np.mgrid[0:ALPHA.height, 0:ALPHA.width]
tilted = np.clip(frame_a.astype(np.float64) + 0.25 * tilted_columns,
                 0, 65535).astype(np.uint16)
ramp_stack = livestack.LiveStack(workspace / "ramp", plan)
ramp_stack.add(livestack.make_tile(tilted, ALPHA, ramp_stack.canvas, "tilted",
                                   seconds=300, filter_name="L"))
picture = ramp_stack.mean()
seen = ramp_stack.coverage() > 0
left = gradient.measure(np.where(seen, picture, np.nan), degree=1)
case("a frame's own gradient does not reach the stack",
     abs(left.a) < 0.01 and abs(left.b) < 0.01,
     f"residual slope {left.a:+.5f}/{left.b:+.5f} from a planted +0.25/px")

# Registration quality: stacking two telescopes must not blur the stars.
def sharpness(which):
    scratch = livestack.LiveStack(workspace / f"s{which}", plan)
    for name, frame, solution in which:
        scratch.add(livestack.make_tile(frame, solution, scratch.canvas, name,
                                        seconds=300, filter_name="L"))
    mean = scratch.mean()
    seen = scratch.coverage() > 0
    if not seen.any():
        return None
    low, high = np.percentile(mean[seen], [1, 99.99])
    image = (np.clip((mean - low) / max(high - low, 1e-9), 0, 1) * 65535).astype(np.uint16)
    return stars.measure(image)["hfr"]

alone = sharpness([("a", frame_a, ALPHA)])
together = sharpness([("a", frame_a, ALPHA), ("b", frame_b, fixed)])
case("stacking two telescopes does not blur the stars",
     alone and together and together < alone * 1.25,
     f"HFR {alone:.3f} px alone, {together:.3f} px together")

# Rejection: a satellite through one frame must not reach the stack.
print("\n== rejection ==")
deep = livestack.LiveStack(workspace / "deep", plan)
for index in range(6):
    clean = photograph(ALPHA, gain=1.0, sky=800.0, noise=12.0, seed=100 + index)
    deep.add(livestack.make_tile(clean, ALPHA, deep.canvas, f"clean:{index}",
                                 seconds=300, filter_name="L"))
before = deep.mean().copy()
streaked = photograph(ALPHA, gain=1.0, sky=800.0, noise=12.0, seed=200,
                      trail=True)
streak_report = deep.add(livestack.make_tile(streaked, ALPHA, deep.canvas,
                                             "streak", seconds=300,
                                             filter_name="L"))
case("the streaked frame is still accepted", streak_report["added"],
     streak_report["detail"])
case("the satellite's pixels are rejected", streak_report["rejected"] > 200,
     f"{streak_report['rejected']} pixels rejected")
after = deep.mean()
moved = float(np.max(np.abs(after - before)))
case("the satellite does not reach the stacked picture", moved < 400.0,
     f"the worst pixel moved by {moved:.1f}")

# ---------------------------------------------------------------------------
# Tiles on the wire, and the stack on disk
# ---------------------------------------------------------------------------
print("\n== carrying it ==")
raw = tile_a.encode()
again = livestack.Tile.decode(raw)
case("a tile survives the wire",
     again.id == tile_a.id and again.box == tile_a.box
     and again.values.shape == tile_a.values.shape,
     f"{len(raw) / 1e6:.2f} MB for {tile_a.values.size} pixels")
inside = tile_a.weight > 0
error = float(np.max(np.abs(again.values[inside] - tile_a.values[inside])))
span = float(np.max(np.abs(tile_a.values[inside])))
case("half precision keeps the tile to a part in a thousand",
     error / max(span, 1e-9) < 2e-3,
     f"worst {error:.3f} on a range of {span:.0f}")

stack.save()
reopened = livestack.LiveStack.open(workspace / "stack")
case("a stack reopens with everything in it",
     reopened.frames == stack.frames
     and len(reopened.reference) == len(stack.reference)
     and float(np.max(np.abs(reopened.mean() - stack.mean()))) < 1e-9,
     f"{reopened.frames} frames, {len(reopened.reference)} reference stars")

image, info = stack.preview(max_dim=400)
case("a stack renders a preview", image[:8] == b"\x89PNG\r\n\x1a\n"
     and info["width"] <= 400, f"{info['width']}x{info['height']} PNG, "
     f"{len(image)} bytes")

# ---------------------------------------------------------------------------
# Real frames, if a folder was given
# ---------------------------------------------------------------------------
folder = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if folder and folder.is_dir():
    print(f"\n== real frames from {folder} ==")
    rigs = []
    for directory in sorted(p for p in folder.iterdir() if p.is_dir()):
        lights = [p for p in directory.iterdir()
                  if p.suffix.lower() in (".fits", ".fit")
                  and "master" not in p.name.lower()]
        bias = next((p for p in directory.iterdir()
                     if "bias" in p.name.lower()), None)
        flat = next((p for p in directory.iterdir()
                     if "flat" in p.name.lower()), None)
        if not lights or bias is None or flat is None:
            print(f"   skipping {directory.name}: needs a light, a bias and a flat")
            continue
        rigs.append((directory.name, lights[0], bias, flat))

    if len(rigs) < 1:
        print("   nothing usable in that folder")
    prepared = []
    for name, light, bias, flat in rigs:
        frame, header = fits.read(light)
        bias_frame = calibration.to_uint16(
            calibration._as_adu(calibration.read_master_file(bias)[0]))
        flat_frame = calibration.to_uint16(
            calibration._as_adu(calibration.read_master_file(flat)[0]))
        calibrated, steps = calibration.apply_masters(frame, None, flat_frame,
                                                      bias_frame)
        case(f"{name}: calibrates", "bias" in steps and "flat" in steps,
             ", ".join(steps))
        try:
            solution = wcs.from_header(header)
            case(f"{name}: has a usable plate solution", True,
                 solution.describe())
        except wcs.WcsError as exc:
            case(f"{name}: has a usable plate solution", False, str(exc))
            continue
        prepared.append((name, calibrated, solution,
                         str(header.get("FILTER") or "L").strip()))

    if len(prepared) >= 2:
        centres = [item[2].centre for item in prepared]
        real_plan = livestack.Plan.for_region(
            sum(c[0] for c in centres) / len(centres),
            sum(c[1] for c in centres) / len(centres),
            max(item[2].field[0] for item in prepared) * 1.3,
            max(item[2].field[1] for item in prepared) * 1.3,
            [item[2].scale for item in prepared])
        real = livestack.LiveStack(workspace / "real", real_plan)
        print(f"   canvas {real.canvas.width}x{real.canvas.height} at "
              f'{real.canvas.scale:.3f}"/px')
        for index, (name, frame, solution, filter_name) in enumerate(prepared):
            placed = solution
            if index:
                nearby = real.reference_near(solution, 400)
                try:
                    placed, note = align.refine(
                        solution, align.star_pixels(stars.detect(frame)),
                        nearby, reference=real.canvas)
                    case(f"{name}: aligns to the stack", True, note["detail"])
                except align.AlignError as exc:
                    case(f"{name}: aligns to the stack", False, str(exc))
            tile = livestack.make_tile(frame, placed, real.canvas,
                                       f"{name}:1", seconds=600, agent=name,
                                       filter_name=filter_name)
            report = real.add(tile)
            case(f"{name}: goes into the community stack", report["added"],
                 report["detail"])
            real.remember_stars(align.star_sky(stars.detect(frame), placed))
        summary = real.summary()
        case("the real stack holds every telescope",
             summary["frames"] == len(prepared)
             and len(summary["agents"]) == len(prepared),
             f"{summary['frames']} frames from {', '.join(summary['agents'])}, "
             f"{summary['covered'] * 100:.0f}% of the canvas covered")
elif folder:
    print(f"\n   {folder} is not a folder; the real-frame checks were skipped")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
