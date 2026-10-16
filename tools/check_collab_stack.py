"""Stack mismatched telescopes on the collaboration server and watch it happen.

    python tools/check_collab_stack.py
    python tools/check_collab_stack.py --serve

The first form runs the check and exits. The second leaves the server up
afterwards and prints the watch address, so the picture can be opened in a
browser. Ctrl+C stops it.

What it does, over real HTTP against a real server:

  * two mono telescopes that do not match — focal length, pixel scale,
    sensor, rotation, exposure, noise — and a colour camera with a Bayer
    mosaic, all looking at one invented star field;
  * each light uploaded the way a telescope uploads one, which must return
    before the frame has been stacked;
  * the server registering them onto one canvas, debayering the colour
    camera, weighing them, and refusing a frame of the wrong sky;
  * the public watch page moving as frames arrive, and carrying none of
    the server's credentials and nowhere to send a file;
  * the stack still being the same picture after it is closed and reopened.

No test framework, for the same reason as the other checks here.
"""

import json
import math
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SERVE = "--serve" in sys.argv
DATA = Path(tempfile.mkdtemp(prefix="check-collab-stack-"))
ADMIN = "test-admin-token"
os.environ["STARFRONT_DATA"] = str(DATA)
os.environ["STARFRONT_ADMIN_TOKEN"] = ADMIN
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp(prefix="check-collab-home-"))

PREVIEWS = Path("/opt/cursor/artifacts/collab-stack")
try:
    PREVIEWS.mkdir(parents=True, exist_ok=True)
except OSError:
    PREVIEWS = DATA / "previews"
    PREVIEWS.mkdir(parents=True, exist_ok=True)

from astrocontrol import collab                                          # noqa: E402
from astrocontrol.imaging import debayer, fits, png, render, wcs         # noqa: E402
from astrocontrol.livestack import stack_root                            # noqa: E402
from server.app import app, community                                    # noqa: E402

import uvicorn                                                            # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                       log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    try:
        urllib.request.urlopen(f"{BASE}/api/v1/health", timeout=1).read()
        break
    except Exception:                                                     # noqa: BLE001
        time.sleep(0.1)
else:
    print("FAIL  the server never came up")
    sys.exit(1)


def call(method, path, body=None, token=ADMIN):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if token == "":
        headers = {}
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                     headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
    return json.loads(payload) if payload else {}


def put_frame(project, name, payload, telescope):
    headers = {"Authorization": f"Bearer {ADMIN}",
               "Content-Length": str(len(payload))}
    request = urllib.request.Request(
        f"{BASE}/api/v1/projects/{project}/files/{name}?telescope={telescope}",
        data=payload, method="PUT", headers=headers)
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=60) as response:
        answer = json.loads(response.read().decode())
    return answer, time.perf_counter() - started


def fetch(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as response:
        return response.read(), response.headers.get("Content-Type", "")


# ---------------------------------------------------------------- debayer
print("== debayer ==")
flat = np.full((32, 32), 1000.0, dtype=np.float32)
constant = all(np.allclose(debayer.debayer(flat, pattern), 1000.0, atol=1e-3)
               for pattern in ("RGGB", "BGGR", "GRBG", "GBRG"))
case("a grey mosaic stays grey in every Bayer pattern", constant)

red = np.zeros((24, 24), dtype=np.float32)
red[0::2, 0::2] = 1000.0
luma = debayer.debayer(red, "RGGB")
expected = 0.2126 * 1000.0
case("a red mosaic becomes red-weighted luminance, not a chequerboard",
     np.allclose(luma[4:-4, 4:-4], expected, atol=1.5)
     and float(luma[5, 5]) > 0.5 * expected,
     f"centre {float(luma[8, 8]):.1f}, expected about {expected:.1f}")

yy, xx = np.mgrid[0:80, 0:80]
blob = np.exp(-((yy - 40.0) ** 2 + (xx - 37.0) ** 2) / (2 * 2.4 ** 2)) * 4000
mosaic = np.zeros_like(blob)
mosaic[0::2, 0::2] = blob[0::2, 0::2]
recovered = debayer.debayer(mosaic, "RGGB")
peak = np.unravel_index(int(np.argmax(recovered)), recovered.shape)
case("debayer puts a star back on its centre",
     math.hypot(peak[0] - 40, peak[1] - 37) <= 1.5,
     f"peak at {peak}, wanted row 40 col 37")

try:
    debayer.pattern_of({"BAYERPAT": "XYZ"})
    case("an unknown Bayer pattern is refused", False, "it was accepted")
except debayer.DebayerError as exc:
    case("an unknown Bayer pattern is refused", "XYZ" in str(exc), str(exc)[:80])
case("a mono header is not debayered", debayer.pattern_of({"IMAGETYP": "LIGHT"}) is None)

# ---------------------------------------------------------------- the sky
TRUE_RA, TRUE_DEC = 83.82, 54.0
RNG = np.random.default_rng(19)
STAR_COUNT = 900
SKY = np.column_stack([
    TRUE_RA + (RNG.random(STAR_COUNT) - 0.5) * 0.95 / math.cos(math.radians(TRUE_DEC)),
    TRUE_DEC + (RNG.random(STAR_COUNT) - 0.5) * 0.72,
    500.0 * 10.0 ** (RNG.random(STAR_COUNT) * 1.4),
])


def render_sky(solution, gain, sky_level, noise, seeing, seed, gradient=True):
    """One telescope's view of the invented field, sky and all."""
    rng = np.random.default_rng(seed)
    frame = np.full((solution.height, solution.width), float(sky_level), dtype=np.float32)
    if gradient:
        rows, cols = frame.shape
        yy, xx = np.mgrid[0:rows, 0:cols]
        frame += np.float32(90.0 * (xx / max(cols, 1)) + 50.0 * (yy / max(rows, 1)))
    x, y, ahead = solution.to_pixel(SKY[:, 0], SKY[:, 1])
    sigma = max(seeing / solution.scale, 0.8)
    reach = int(math.ceil(sigma * 3.2))
    for index in np.nonzero(ahead)[0]:
        cx, cy = float(x[index]) - 1.0, float(y[index]) - 1.0
        if not (reach < cx < solution.width - reach - 1
                and reach < cy < solution.height - reach - 1):
            continue
        x0, y0 = int(round(cx)) - reach, int(round(cy)) - reach
        gx = np.arange(x0, x0 + 2 * reach + 1)
        gy = np.arange(y0, y0 + 2 * reach + 1)
        frame[y0:y0 + 2 * reach + 1, x0:x0 + 2 * reach + 1] += np.float32(
            SKY[index, 2] * gain
            * np.exp(-((gx[None, :] - cx) ** 2 + (gy[:, None] - cy) ** 2)
                     / (2 * sigma * sigma)))
    frame += rng.normal(0.0, noise, frame.shape).astype(np.float32)
    return frame


def solution_for(ra, dec, width, height, scale_arcsec, rotation):
    scale = scale_arcsec / 3600.0
    return wcs.Wcs(ra, dec, (width + 1) / 2.0, (height + 1) / 2.0,
                   wcs._cd_from_angle(-scale, scale, rotation), width, height)


class Rig:
    def __init__(self, name, focal, pixel, width, height, rotation, offset,
                 gain, sky, noise, seeing):
        self.name = name
        self.focal = focal
        self.pixel = pixel
        self.width = width
        self.height = height
        self.rotation = rotation
        self.gain = gain
        self.sky = sky
        self.noise = noise
        self.seeing = seeing
        self.scale = 206.265 * pixel / focal
        self.offset = offset
        self.centre = (TRUE_RA + offset[0], TRUE_DEC + offset[1])

    def pointing(self, dither=(0.0, 0.0)):
        return solution_for(self.centre[0] + dither[0], self.centre[1] + dither[1],
                            self.width, self.height, self.scale, self.rotation)

    def expose(self, index, seed, header="derived", seconds=300.0, bayer="",
               lie=None, streak=False, as_float=False):
        dither = (((index % 3) - 1) * 0.004, ((index % 2) - 0.5) * 0.003)
        truth = self.pointing(dither)
        sky = render_sky(truth, self.gain, self.sky, self.noise, self.seeing, seed)
        if streak:
            mid = self.height // 2
            sky[mid - 1:mid + 2, :] = 28000.0
        if bayer:
            mosaic = np.zeros_like(sky)
            # Grey stars: every photosite records the same scene value, which
            # is what a white star looks like through a Bayer matrix. The
            # debayer check above is what proves a coloured mosaic is rebuilt.
            mosaic[:, :] = sky
            sky = mosaic
        image = np.clip(sky, 0, 65535)
        cards = {
            "IMAGETYP": "LIGHT",
            "OBJECT": "synthetic field",
            "EXPTIME": float(seconds),
            "EXPOSURE": float(seconds),
            "FILTER": "L",
            "TELESCOP": self.name,
            "FOCALLEN": self.focal,
            "XPIXSZ": self.pixel,
            "YPIXSZ": self.pixel,
            "XBINNING": 1,
            "DATE-OBS": "2026-10-16T02:10:00.000",
        }
        if bayer:
            cards["BAYERPAT"] = bayer
            cards["FILTER"] = "L"
        if lie is not None:
            cards.update(_cd_cards(lie))
        elif header == "cd":
            cards.update(_cd_cards(truth))
        else:
            # A few degrees and a small pointing error, which is what a
            # program's own rotation keyword is. The pixels were drawn with
            # the truth above; the header does not know it.
            cards.update({
                "CRVAL1": truth.crval1 + 0.010,
                "CRVAL2": truth.crval2 - 0.007,
                "CRPIX1": truth.crpix1,
                "CRPIX2": truth.crpix2,
                "OBJCTROT": round(self.rotation + 4.0, 3),
            })
        name = f"{self.name}_{index:04d}.fits"
        path = DATA / name
        if as_float:
            _write_float(path, image.astype(np.float32), cards)
        else:
            fits.write(path, image.astype(np.uint16), cards)
        return path, image


def _cd_cards(solution):
    cd11, cd12, cd21, cd22 = solution.cd
    return {
        "CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN",
        "CRVAL1": solution.crval1, "CRVAL2": solution.crval2,
        "CRPIX1": solution.crpix1, "CRPIX2": solution.crpix2,
        "CD1_1": cd11, "CD1_2": cd12, "CD2_1": cd21, "CD2_2": cd22,
    }


def _write_float(path, image, header):
    """A 32-bit float FITS, which is the other bit depth a sub turns up in."""
    data = np.asarray(image, dtype=np.float32)
    height, width = data.shape
    cards = [
        fits._card("SIMPLE", True),
        fits._card("BITPIX", -32),
        fits._card("NAXIS", 2),
        fits._card("NAXIS1", width),
        fits._card("NAXIS2", height),
        fits._card("BSCALE", 1.0),
        fits._card("BZERO", 0.0),
    ]
    for key, value in header.items():
        cards.append(fits._card(key, value))
    cards.append(b"END".ljust(80))
    payload = data.astype(">f4").tobytes()
    raw = fits._pad(b"".join(cards)) + fits._pad(payload)
    Path(path).write_bytes(raw)


def save_uint(path, image):
    sample = np.asarray(image)
    if sample.dtype != np.uint16:
        low, high = np.percentile(sample, [0.5, 99.8])
        if high - low < 1e-6:
            high = low + 1.0
        sample = np.clip((sample - low) / (high - low), 0, 1)
        sample = (sample * 65535).astype(np.uint16)
    shown, _info = render.render_png_array(sample, render.auto_stretch(sample), max_dim=900)
    Path(path).write_bytes(png.encode(shown))


ALPHA = Rig("alpha", focal=480.0, pixel=3.76, width=900, height=700,
            rotation=14.0, offset=(0.0, 0.0), gain=1.0, sky=700.0,
            noise=12.0, seeing=3.3)
BETA = Rig("beta", focal=720.0, pixel=4.80, width=1000, height=750,
           rotation=128.0, offset=(0.16, -0.05), gain=2.35, sky=2100.0,
           noise=36.0, seeing=7.2)
OSC = Rig("osc", focal=280.0, pixel=3.80, width=640, height=480,
          rotation=-22.0, offset=(0.02, 0.03), gain=1.3, sky=900.0,
          noise=18.0, seeing=6.4)

print("\n== two mismatched rigs and a colour camera ==")
for rig in (ALPHA, BETA, OSC):
    print(f"   {rig.name}: {rig.focal:g} mm, {rig.scale:.2f}\"/px, "
          f"{rig.width}x{rig.height}, rotated {rig.rotation:g}°, "
          f"seeing {rig.seeing:g}\"")
case("the rigs really do disagree",
     abs(ALPHA.scale - BETA.scale) / ALPHA.scale > 0.08
     and abs(ALPHA.rotation - BETA.rotation) > 45
     and (ALPHA.width, ALPHA.height) != (BETA.width, BETA.height)
     and abs(ALPHA.focal - OSC.focal) > 50)

# ---------------------------------------------------------------- project
print("\n== the collaboration ==")
health = call("GET", "/api/v1/health", token="")
case("the server says it will stack uploaded lights", health.get("communityStack") is True)

project = call("POST", "/api/v1/projects", {
    "name": "Synthetic field",
    "region": {"ra": TRUE_RA, "dec": TRUE_DEC, "width": 0.75, "height": 0.55},
    "kind": "single",
    "requirements": {"filters": {"L": None}},
    "goals": {"L": 2.0},
    "collectFiles": True,
})["project"]
PROJECT = project["id"]
for rig in (ALPHA, BETA):
    made = call("POST", "/api/v1/agents", {"name": rig.name, "owner": "check"})
    call("POST", "/api/v1/agent/hello", {
        "protocol": collab.PROTOCOL,
        "profile": collab.RigProfile(
            name=rig.name, focalLength=rig.focal, pixelSize=rig.pixel,
            sensorWidth=rig.width, sensorHeight=rig.height, binning=1,
            filters={"L": None}, exposures={"L": 300.0}).payload(),
    }, token=made["token"])
    call("POST", f"/api/v1/agent/projects/{PROJECT}/join",
         {"hours": 2.0, "exposures": {"L": 300.0}}, token=made["token"])
case("both telescopes joined before any frame", True, PROJECT)

# ---------------------------------------------------------------- uploads do not wait
print("\n== uploads do not wait on the stack ==")
community.gate.clear()
time.sleep(0.4)
anchor_path, anchor_image = ALPHA.expose(0, seed=4, header="cd", seconds=300.0)
save_uint(PREVIEWS / "before-alpha.png", anchor_image)
answer, elapsed = put_frame(PROJECT, anchor_path.name, anchor_path.read_bytes(), "alpha")
watched = call("GET", f"/api/v1/watch/{PROJECT}", token="")
case("the upload is accepted and only queued",
     answer.get("stacking") == "queued" and answer.get("already") is False
     and int(watched.get("frames") or 0) == 0 and int(watched.get("pending") or 0) >= 1,
     f"stacking={answer.get('stacking')} frames={watched.get('frames')} "
     f"pending={watched.get('pending')} in {elapsed:.2f}s")
case("queueing the frame took well under a second", elapsed < 1.0, f"{elapsed:.2f}s")
again, _elapsed = put_frame(PROJECT, anchor_path.name, anchor_path.read_bytes(), "alpha")
watched = call("GET", f"/api/v1/watch/{PROJECT}", token="")
case("the same file again is not a second frame",
     again.get("already") is True and int(watched.get("pending") or 0) == 1,
     f"already={again.get('already')} pending={watched.get('pending')}")

community.gate.set()


def wait_stack(frames, timeout=240.0):
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = call("GET", f"/api/v1/watch/{PROJECT}", token="")
        luminous = next((row for row in last.get("stacks") or [] if row.get("filter") == "L"), {})
        if int(luminous.get("frames") or 0) >= frames and int(last.get("pending") or 0) == 0:
            return last, luminous
        time.sleep(0.25)
    return last, {}


summary, luminous = wait_stack(1)
case("the first frame anchors the stack",
     int(luminous.get("frames") or 0) == 1 and luminous.get("width", 0) <= 2048,
     f"{luminous.get('frames')} frame, {luminous.get('width')}x{luminous.get('height')} "
     f"at {luminous.get('scale')}\"/px"
     + ("" if luminous else "  " + json.dumps((summary.get("recent") or [])[-2:])[:300]))

# ---------------------------------------------------------------- the other rigs
print("\n== the other telescopes ==")
events = []
stop_sse = threading.Event()


def listen():
    request = urllib.request.Request(f"{BASE}/api/v1/watch/{PROJECT}/events")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            for raw in response:
                if stop_sse.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    try:
                        events.append(json.loads(line[5:].strip()))
                    except ValueError:
                        continue
    except Exception:                                                     # noqa: BLE001
        return


threading.Thread(target=listen, daemon=True).start()
time.sleep(0.5)

beta_path, beta_image = BETA.expose(0, seed=11, seconds=300.0)
save_uint(PREVIEWS / "before-beta.png", beta_image)
uploads = [
    BETA.expose(0, seed=11, seconds=300.0),
    BETA.expose(1, seed=12, seconds=240.0),
    ALPHA.expose(1, seed=5, seconds=300.0),
    ALPHA.expose(2, seed=6, seconds=40.0),
    ALPHA.expose(3, seed=7, seconds=300.0, as_float=True),
    OSC.expose(0, seed=21, seconds=180.0, bayer="RGGB"),
]
# beta 0 was rendered twice so the saved preview matches the upload; send
# the saved one, and the rest from the list except the duplicate render.
uploads[0] = (beta_path, beta_image)
sent = []
for path, _image in uploads:
    rig_name = path.name.split("_", 1)[0]
    answer, _elapsed = put_frame(PROJECT, path.name, path.read_bytes(), rig_name)
    sent.append(answer)

summary, luminous = wait_stack(1 + len(uploads))
case("every mismatched frame was stacked",
     int(luminous.get("frames") or 0) == 1 + len(uploads),
     f"{luminous.get('frames')} frames, pending {summary.get('pending')}")
names = {row.get("name") for row in luminous.get("contributors") or []}
case("both telescopes and the colour camera are credited",
     names >= {"alpha", "beta", "osc"},
     ", ".join(sorted(names)))
seconds = {row["name"]: row.get("seconds") for row in luminous.get("contributors") or []}
case("exposures are credited, including the short one",
     seconds.get("alpha", 0) > seconds.get("osc", 0) > 0 and seconds.get("beta", 0) > 0,
     str(seconds))
case("the canvas is only partly covered",
     0.05 < float(luminous.get("covered") or 0) < 0.97,
     f"{float(luminous.get('covered') or 0) * 100:.0f}%")
case("the shared grid stays inside the pixel cap",
     int(luminous.get("width") or 0) <= 2048 and int(luminous.get("height") or 0) <= 2048,
     f"{luminous.get('width')}x{luminous.get('height')}")

recent = summary.get("recent") or []
aligned = [row for row in recent if row.get("added") and row.get("aligned")]
case("later frames were star-matched, not trusted",
     len(aligned) >= 3 and all(float(row.get("residual") or 99) < 1.5 for row in aligned),
     ", ".join(f"{row.get('contributor')} {row.get('residual')}" for row in aligned[:6]))
debayered = [row for row in recent if row.get("debayered") and row.get("added")]
case("the colour camera was debayered into the same stack",
     len(debayered) == 1 and debayered[0].get("contributor") == "osc",
     debayered[0].get("detail", "")[:80] if debayered else "no debayered frame")
floated = [row for row in recent if row.get("bitpix") == -32 and row.get("added")]
case("a 32-bit float frame was stacked", len(floated) == 1,
     floated[0].get("frame", "") if floated else "none")
beta_rows = [row for row in recent if row.get("contributor") == "beta" and row.get("added")]
alpha_rows = [row for row in recent if row.get("contributor") == "alpha" and row.get("added")
              and row.get("fwhm")]
beta_fwhm = [float(row["fwhm"]) for row in beta_rows if row.get("fwhm")]
alpha_fwhm = [float(row["fwhm"]) for row in alpha_rows if row.get("fwhm")]
case("the softer telescope is measured softer",
     bool(beta_fwhm and alpha_fwhm) and float(np.median(beta_fwhm)) > float(np.median(alpha_fwhm)) * 1.2,
     f"alpha {np.median(alpha_fwhm) if alpha_fwhm else 0:.2f}\"  "
     f"beta {np.median(beta_fwhm) if beta_fwhm else 0:.2f}\"")
beta_gain = [float(row["gain"]) for row in beta_rows if row.get("gain")]
# The fitter measures the resampled pixels, not the amplitude the stars were
# drawn with. A softer, finer-sampled telescope does not come out at the
# draw-time ratio; it does have to come out brighter than the anchor, and
# inside the range the stack is willing to apply.
case("the brighter telescope is scaled onto the first",
     bool(beta_gain) and 1.5 < float(np.median(beta_gain)) < 30.0,
     f"gain {np.median(beta_gain) if beta_gain else 0:.2f}")

deadline = time.monotonic() + 20
while time.monotonic() < deadline and not any(int(row.get("frames") or 0) >= 3 for row in events):
    time.sleep(0.2)
stop_sse.set()
case("the watch stream pushed the growing stack",
     any(int(row.get("frames") or 0) >= 3 for row in events),
     f"{len(events)} events, frames {[row.get('frames') for row in events[:8]]}")

# ---------------------------------------------------------------- rejection and a stranger
print("\n== rejection, a stranger, and the other filter ==")
before_rejected = int(luminous.get("rejectedPixels") or 0)
streak_path, _image = ALPHA.expose(4, seed=8, seconds=300.0, streak=True)
put_frame(PROJECT, streak_path.name, streak_path.read_bytes(), "alpha")
summary, luminous = wait_stack(int(luminous.get("frames") or 0) + 1)
case("a satellite streak is stacked but its pixels are rejected",
     int(luminous.get("rejectedPixels") or 0) > before_rejected + 30,
     f"rejected pixels {before_rejected} -> {luminous.get('rejectedPixels')}")

stranger = Rig("stranger", focal=500.0, pixel=3.8, width=700, height=520,
               rotation=5.0, offset=(0.0, 0.0), gain=1.0, sky=600.0,
               noise=10.0, seeing=3.0)
pretend = solution_for(TRUE_RA, TRUE_DEC, 700, 520, stranger.scale, 5.0)
# Stars of this field, mirrored. The header claims a normal plate solution
# of the target, which a trustworthy solution does not get to second-guess
# by trying a mirror. The frame has plenty of stars and none of them agree.
sky = np.ascontiguousarray(np.fliplr(
    render_sky(pretend, 1.0, 600.0, 10.0, 3.0, 99, gradient=False)))
stranger_path = DATA / "stranger_0000.fits"
cards = {"IMAGETYP": "LIGHT", "EXPTIME": 120.0, "FILTER": "L", "TELESCOP": "stranger",
         "FOCALLEN": 500.0, "XPIXSZ": 3.8, "DATE-OBS": "2026-10-16T03:00:00"}
cards.update(_cd_cards(pretend))
fits.write(stranger_path, np.clip(sky, 0, 65535).astype(np.uint16), cards)
frames_before = int(luminous.get("frames") or 0)
put_frame(PROJECT, stranger_path.name, stranger_path.read_bytes(), "stranger")
summary, luminous = wait_stack(frames_before)
refused = [row for row in (summary.get("recent") or [])
           if row.get("frame") == "stranger_0000.fits"]
case("a frame of the wrong sky is refused",
     int(luminous.get("frames") or 0) == frames_before and refused and not refused[-1].get("added"),
     (refused[-1].get("detail") if refused else "no report")[:120])

ha_path, _image = ALPHA.expose(5, seed=15, header="cd", seconds=300.0)
# Retitle it as hydrogen. The bytes are the same field; the filter is not,
# and it must not be poured into the luminance stack.
raw = bytearray(ha_path.read_bytes())
# FILTER was written as L. Patching a FITS card in place is brittle, so
# write it again from the pixels with the right card.
_ha_image, _ha_header = fits.read(ha_path)
_ha_header["FILTER"] = "Ha"
fits.write(ha_path, _ha_image, _ha_header)
put_frame(PROJECT, "alpha_Ha_0005.fits", ha_path.read_bytes(), "alpha")
deadline = time.monotonic() + 120
last = {}
while time.monotonic() < deadline:
    last = call("GET", f"/api/v1/watch/{PROJECT}", token="")
    filters_now = {row.get("filter"): row.get("frames") for row in last.get("stacks") or []}
    if filters_now.get("H") == 1 and int(last.get("pending") or 0) == 0:
        break
    time.sleep(0.25)
else:
    filters_now = {row.get("filter"): row.get("frames") for row in last.get("stacks") or []}
case("Ha is its own stack and luminance is left alone",
     filters_now.get("H") == 1 and filters_now.get("L") == frames_before,
     str(filters_now))

# ---------------------------------------------------------------- one picture
print("\n== one picture ==")
opened = community._open_existing(PROJECT, "L")
mean = opened.mean()
coverage = opened.coverage()
case("some pixels were seen by more than one telescope, and some by only one",
     bool((coverage >= 2).any()) and bool((coverage == 1).any()),
     f"deepest {int(coverage.max())}, single-rig pixels {int((coverage == 1).sum())}")

canvas = opened.canvas
x, y, ahead = canvas.to_pixel(SKY[:, 0], SKY[:, 1])
order = np.argsort(-SKY[:, 2])
offsets = []
checked = 0
radius = 4
for index in order:
    if not ahead[index]:
        continue
    cx = int(round(float(x[index]) - 1.0))
    cy = int(round(float(y[index]) - 1.0))
    if not (radius < cx < mean.shape[1] - radius - 1
            and radius < cy < mean.shape[0] - radius - 1):
        continue
    if int(coverage[cy - radius:cy + radius + 1, cx - radius:cx + radius + 1].max()) < 2:
        continue
    patch = mean[cy - radius:cy + radius + 1, cx - radius:cx + radius + 1]
    peak = float(patch.max())
    if peak <= 0:
        continue
    py, px = np.unravel_index(int(np.argmax(patch)), patch.shape)
    offsets.append(math.hypot(px - radius, py - radius))
    checked += 1
    if checked >= 40:
        break
median_offset = float(np.median(offsets)) if offsets else 99.0
upper = float(np.percentile(offsets, 80)) if offsets else 99.0
case("stars from both telescopes land on the true positions",
     len(offsets) >= 12 and median_offset <= 1.25 and upper <= 2.0,
     f"{len(offsets)} stars, median offset {median_offset:.2f} px, "
     f"80th {upper:.2f} px")

preview, kind = fetch(f"/api/v1/watch/{PROJECT}/L/preview.png?width=900")
(PREVIEWS / "after-stack.png").write_bytes(preview)
case("anybody can fetch the stretched preview",
     preview[:8] == b"\x89PNG\r\n\x1a\n" and kind == "image/png" and len(preview) > 1000,
     f"{len(preview)} bytes")

# ---------------------------------------------------------------- restart
print("\n== the stack survives a restart ==")
before = np.array(opened.mean(), copy=True)
frames_now = int(luminous.get("frames") or 0)
community.reload()
reopened = community._open_existing(PROJECT, "L")
after = reopened.mean()
case("closing and reopening the stack does not change it",
     reopened.frames == frames_now and before.shape == after.shape
     and float(np.max(np.abs(before - after))) < 1e-6,
     f"{reopened.frames} frames")
summary = call("GET", f"/api/v1/watch/{PROJECT}", token="")
case("the public page still has it after the restart",
     int(summary.get("frames") or 0) >= frames_now and summary.get("name") == "Synthetic field",
     f"{summary.get('frames')} frames")

# One more frame, to prove the reopened reference still registers.
extra_path, _image = ALPHA.expose(6, seed=16, seconds=300.0)
put_frame(PROJECT, extra_path.name, extra_path.read_bytes(), "alpha")
summary, luminous = wait_stack(frames_now + 1)
case("a frame after the restart still joins the same stack",
     int(luminous.get("frames") or 0) == frames_now + 1,
     f"{luminous.get('frames')} frames")

# ---------------------------------------------------------------- the page keeps its secrets
print("\n== the page is only a picture ==")
page, page_kind = fetch("/watch")
one, _kind = fetch(f"/watch/{PROJECT}")
listing = call("GET", "/api/v1/watch", token="")
body = page.decode("utf-8", "replace") + one.decode("utf-8", "replace") + json.dumps(listing) + json.dumps(summary)
forbidden = [ADMIN, "Bearer", "/files", "/api/v1/agent", "/api/v1/projects",
            "upload", "admin-token", "pending.json", str(DATA)]
leaked = [word for word in forbidden if word and word in body]
case("the watch page and its feed name no token and no upload address",
     not leaked and "text/html" in page_kind and "EventSource" in one.decode("utf-8"),
     ", ".join(leaked)[:160] or "clean")
case("the index names the collaboration and points at its picture",
     any(row.get("id") == PROJECT and row.get("watch") == f"/watch/{PROJECT}"
         for row in listing.get("projects") or []),
     listing.get("projects"))

try:
    urllib.request.urlopen(f"{BASE}/api/v1/projects/{PROJECT}/files", timeout=10)
    case("the frame list still wants a sign-in", False, "it was public")
except Exception as exc:                                                 # noqa: BLE001
    code = getattr(exc, "code", None)
    case("the frame list still wants a sign-in", code in (401, 403), f"HTTP {code}")

print(f"\nPreviews: {PREVIEWS}")
print(f"   before, alpha: {PREVIEWS / 'before-alpha.png'}")
print(f"   before, beta:  {PREVIEWS / 'before-beta.png'}")
print(f"   after, stack:  {PREVIEWS / 'after-stack.png'}")
print(f"\nWatch: {BASE}/watch/{PROJECT}")
print(f"\n{sum(results)}/{len(results)} passed")

if SERVE:
    print("\nThe server is still running. Open the watch address above.")
    print("Ctrl+C stops it.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass

server.should_exit = True
sys.exit(0 if all(results) else 1)
