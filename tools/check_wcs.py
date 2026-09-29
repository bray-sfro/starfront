"""Where a frame is on the sky: reading it, inverting it, and the grid.

    python tools/check_wcs.py

  * Every dialect a real program writes: a CD matrix, a PC matrix with
    CDELT, CDELT with CROTA2, and a bare pointing with an angle and a scale.
    They are made to describe the *same* frame and are then required to agree,
    which is a far stronger test than each one parsing without an exception.
  * The transform round-trips: pixels to sky to pixels, over the whole frame
    including its corners, to a small fraction of a pixel.
  * Scale, rotation, handedness and the footprint come out of the matrix
    correctly, including for a mirrored frame and one at high declination
    where a degree of sky is not a degree of right ascension.
  * A canvas honours its pixel cap by coarsening rather than by cropping, and
    a frame's bounding box on one is found.
  * A projection this cannot do is refused by name rather than treated as a
    tangent plane.

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

from astrocontrol.imaging import wcs                              # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


def close(a, b, tolerance=1e-6):
    return abs(float(a) - float(b)) <= tolerance


# ---------------------------------------------------------------------------
# One frame, described four ways
# ---------------------------------------------------------------------------
WIDTH, HEIGHT = 4000, 3000
RA, DEC = 343.25, 57.9
SCALE = 1.75 / 3600.0            # degrees per pixel
ANGLE = 23.5                     # position angle of +y

reference = wcs.Wcs(
    crval1=RA, crval2=DEC, crpix1=(WIDTH + 1) / 2, crpix2=(HEIGHT + 1) / 2,
    cd=wcs._cd_from_angle(-SCALE, SCALE, ANGLE), width=WIDTH, height=HEIGHT)

cd11, cd12, cd21, cd22 = reference.cd
common = {"NAXIS1": WIDTH, "NAXIS2": HEIGHT, "CTYPE1": "RA---TAN",
          "CTYPE2": "DEC--TAN", "CRVAL1": RA, "CRVAL2": DEC,
          "CRPIX1": (WIDTH + 1) / 2, "CRPIX2": (HEIGHT + 1) / 2}

dialects = {
    "CD matrix": {**common, "CD1_1": cd11, "CD1_2": cd12,
                  "CD2_1": cd21, "CD2_2": cd22},
    "PC matrix + CDELT": {
        **common, "CDELT1": -SCALE, "CDELT2": SCALE,
        "PC1_1": math.cos(math.radians(ANGLE)),
        "PC1_2": math.sin(math.radians(ANGLE)),
        "PC2_1": -math.sin(math.radians(ANGLE)),
        "PC2_2": math.cos(math.radians(ANGLE))},
    "CDELT + CROTA2": {**common, "CDELT1": -SCALE, "CDELT2": SCALE,
                       "CROTA2": ANGLE},
    "pointing + angle + scale": {
        "NAXIS1": WIDTH, "NAXIS2": HEIGHT, "OBJCTRA": "22 53 00",
        "OBJCTDEC": "+57 54 00", "OBJCTROT": ANGLE,
        "PIXSCALE": SCALE * 3600.0},
    "pointing + angle + optics": {
        "NAXIS1": WIDTH, "NAXIS2": HEIGHT, "CRVAL1": RA, "CRVAL2": DEC,
        "ANGLE": ANGLE, "FOCALLEN": 206.265 * 3.76 / (SCALE * 3600.0),
        "XPIXSZ": 3.76, "XBINNING": 1},
}

print("== reading the dialects ==")
read = {}
for name, header in dialects.items():
    try:
        read[name] = wcs.from_header(header)
        case(f"{name} reads", True, read[name].describe())
    except wcs.WcsError as exc:
        case(f"{name} reads", False, str(exc))

for name in ("CD matrix", "PC matrix + CDELT", "CDELT + CROTA2"):
    solution = read.get(name)
    if solution is None:
        continue
    case(f"{name} agrees on scale", close(solution.scale, SCALE * 3600.0, 1e-6),
         f'{solution.scale:.6f}" vs {SCALE * 3600.0:.6f}"')
    case(f"{name} agrees on rotation", close(solution.rotation, ANGLE, 1e-6),
         f"{solution.rotation:.6f} vs {ANGLE}")
    case(f"{name} is not mirrored", not solution.mirrored)
    centre = solution.centre
    case(f"{name} agrees on the centre",
         close(centre[0], RA, 1e-6) and close(centre[1], DEC, 1e-6),
         f"{centre[0]:.6f} {centre[1]:.6f}")

derived = read.get("pointing + angle + scale")
if derived is not None:
    case("a derived solution is marked derived", derived.source == "derived",
         derived.source)
    case("a derived solution says what it assumed",
         bool(derived.assumptions), "; ".join(derived.assumptions)[:90])
    case("a derived solution gets the scale right",
         close(derived.scale, SCALE * 3600.0, 1e-4),
         f'{derived.scale:.5f}"')

optics = read.get("pointing + angle + optics")
if optics is not None:
    case("a scale worked out from the optics matches one stated",
         close(optics.scale, SCALE * 3600.0, 1e-3), f'{optics.scale:.5f}"')

# ---------------------------------------------------------------------------
# Round-tripping
# ---------------------------------------------------------------------------
print("\n== the transform ==")
xs, ys = np.meshgrid(np.linspace(1, WIDTH, 23), np.linspace(1, HEIGHT, 19))
xs, ys = xs.ravel(), ys.ravel()
ra, dec = reference.to_world(xs, ys)
back_x, back_y, good = reference.to_pixel(ra, dec)
error = float(np.max(np.hypot(back_x - xs, back_y - ys)))
case("pixels round-trip through the sky", bool(good.all()) and error < 1e-6,
     f"worst {error:.2e} pixels over {len(xs)} points")

# A mirrored frame: the determinant flips and nothing else should break.
mirrored = wcs.Wcs(RA, DEC, (WIDTH + 1) / 2, (HEIGHT + 1) / 2,
                   (-cd11, -cd12, cd21, cd22), WIDTH, HEIGHT)
mx, my, mgood = mirrored.to_pixel(*mirrored.to_world(xs, ys))
case("a mirrored frame round-trips",
     bool(mgood.all()) and float(np.max(np.hypot(mx - xs, my - ys))) < 1e-6)
case("a mirrored frame knows it is", mirrored.mirrored and not reference.mirrored)

# The far side of the sky has no image on a tangent plane, and must be told so
# rather than folded back onto the frame.
_, _, behind = reference.to_pixel(np.array([RA + 180.0]), np.array([-DEC]))
case("the far side of the sky is refused rather than folded in",
     not bool(behind[0]))

# ---------------------------------------------------------------------------
# The footprint
# ---------------------------------------------------------------------------
print("\n== the footprint ==")
foot = reference.footprint()
across, down = reference.field
# The bounding box of a rectangle turned 23.5 degrees, in sky degrees.
angle = math.radians(ANGLE)
expected_x = across * abs(math.cos(angle)) + down * abs(math.sin(angle))
expected_y = across * abs(math.sin(angle)) + down * abs(math.cos(angle))
case("the footprint is the turned frame's bounding box",
     close(foot["width"], expected_x, 2e-3) and close(foot["height"], expected_y, 2e-3),
     f"{foot['width']:.4f}x{foot['height']:.4f} vs {expected_x:.4f}x{expected_y:.4f}")
case("the footprint reports sky degrees, not coordinate degrees",
     foot["width"] < abs(reference.corners()[0][0] - reference.corners()[1][0]) * 1.01,
     f"at Dec {DEC} a degree of sky is "
     f"{1 / math.cos(math.radians(DEC)):.2f} degrees of RA")

# The same frame at the equator spans more RA per degree of sky than at +58,
# and the footprint must not change with it.
equator = wcs.Wcs(RA, 0.0, (WIDTH + 1) / 2, (HEIGHT + 1) / 2, reference.cd,
                  WIDTH, HEIGHT)
case("the footprint is the same size at the equator and at +58",
     close(equator.footprint()["width"], foot["width"], 5e-3),
     f"{equator.footprint()['width']:.4f} vs {foot['width']:.4f}")

# ---------------------------------------------------------------------------
# Carrying a solution
# ---------------------------------------------------------------------------
print("\n== carrying it ==")
written = wcs.from_header({**reference.header(), "NAXIS1": WIDTH,
                           "NAXIS2": HEIGHT})
case("a solution written as FITS cards reads back identically",
     all(close(a, b, 1e-9) for a, b in zip(written.cd, reference.cd))
     and close(written.crval1, reference.crval1, 1e-9))
case("a solution survives the wire",
     wcs.Wcs.read(reference.payload()).describe() == reference.describe())
case("a header written here is seen as solved", wcs.solved(reference.header()))
case("a pointing with no angle is not seen as solved",
     not wcs.solved({"OBJCTRA": "22 53 00", "OBJCTDEC": "+57 54 00"}))
case("a program's own keywords are seen as solved",
     wcs.solved(dialects["pointing + angle + scale"]))

# ---------------------------------------------------------------------------
# Angles out of headers
# ---------------------------------------------------------------------------
print("\n== angles ==")
case("sexagesimal hours become degrees",
     close(wcs.sexagesimal("22 52 23", hours=True), 343.0958333, 1e-5))
case("colons work too",
     close(wcs.sexagesimal("22:52:23", hours=True), 343.0958333, 1e-5))
case("a negative declination keeps its sign on every field",
     close(wcs.sexagesimal("-05 30 30", hours=False), -5.508333, 1e-5))
case("a bare number under an RA keyword above 24 is degrees",
     close(wcs.sexagesimal(343.0958, hours=True), 343.0958, 1e-6))
case("a bare number under an RA keyword below 24 is hours",
     close(wcs.sexagesimal(22.8731, hours=True), 343.0965, 1e-3))

# ---------------------------------------------------------------------------
# Canvases
# ---------------------------------------------------------------------------
print("\n== the canvas ==")
canvas = wcs.grid(RA, DEC, 2.0, 1.5, 2.0)
case("a canvas is the size its scale implies",
     canvas.width == math.ceil(2.0 / (2.0 / 3600.0))
     and canvas.height == math.ceil(1.5 / (2.0 / 3600.0)),
     f"{canvas.width}x{canvas.height}")
case("a canvas is north up by default",
     close(canvas.rotation, 0.0, 1e-9) and not canvas.mirrored)

capped = wcs.grid(RA, DEC, 8.0, 6.0, 0.5, max_pixels=2048)
case("a canvas honours its pixel cap by coarsening",
     max(capped.width, capped.height) <= 2048 and capped.scale > 0.5,
     f'{capped.width}x{capped.height} at {capped.scale:.3f}"/px')
case("a capped canvas still covers the sky it was asked for",
     capped.field[0] >= 8.0 - 1e-6 and capped.field[1] >= 6.0 - 1e-6,
     f"{capped.field[0]:.3f}x{capped.field[1]:.3f} deg")

box = wcs.bounds_on(canvas, reference)
case("a frame's box on a canvas is found and is inside it",
     box is not None and box[0] >= 0 and box[1] >= 0
     and box[2] <= canvas.width and box[3] <= canvas.height, str(box))
elsewhere = wcs.Wcs(RA - 40.0, DEC, 1, 1, reference.cd, WIDTH, HEIGHT)
case("a frame on other sky lands nowhere on the canvas",
     wcs.bounds_on(canvas, elsewhere) is None)

# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
print("\n== refusals ==")
try:
    wcs.from_header({**common, "CTYPE1": "RA---ZEA", "CD1_1": cd11,
                     "CD1_2": cd12, "CD2_1": cd21, "CD2_2": cd22})
    case("a projection this cannot do is refused", False, "it was accepted")
except wcs.WcsError as exc:
    case("a projection this cannot do is refused by name", "ZEA" in str(exc),
         str(exc)[:80])

try:
    wcs.from_header({"NAXIS1": WIDTH, "NAXIS2": HEIGHT, "OBJCTRA": "22 53 00",
                     "OBJCTDEC": "+57 54 00", "OBJCTROT": 10.0})
    case("a frame with no scale is refused", False, "it was accepted")
except wcs.WcsError as exc:
    case("a frame with no scale is refused with advice",
         "solve" in str(exc).lower(), str(exc)[:80])

try:
    wcs.from_header({**common, "CD1_1": cd11, "CD1_2": cd12, "CD2_1": cd21,
                     "CD2_2": cd22, "NAXIS1": 0, "NAXIS2": 0})
    case("a frame of unknown size is refused", False, "it was accepted")
except wcs.WcsError:
    case("a frame of unknown size is refused", True)

try:
    wcs.from_header(dialects["pointing + angle + scale"], allow_derived=False)
    case("deriving can be switched off", False, "it derived anyway")
except wcs.WcsError:
    case("deriving can be switched off", True)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
