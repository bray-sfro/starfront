"""The whole pipeline, from a raw sub on disk to a shared picture on a server.

    python tools/check_livestack.py

Over real HTTP against a real server on a real port, like `check_server.py`,
and for the same reason: the thing being checked is two machines agreeing on
a grid and a picture, and the parts most likely to be wrong — that both sides
build the identical canvas from the same handful of numbers, that a tile
sent twice is counted once, that a frame nobody can place is refused rather
than smeared across everybody's image — are exactly the parts a shortcut
around the network would skip.

What it walks through:

  * two invented observatories with different focal lengths and different
    sensors, both enrolled on one collaboration;
  * raw subs written to disk with the headers a real program writes, dust and
    an offset baked in, plus the masters that correct them;
  * the calibration library matching those masters and applying them;
  * the pipeline placing each calibrated frame, resampling it onto the
    project's canvas and folding it into a local stack;
  * the tile going up, the server folding it into the community stack, and
    a PNG of the result coming back to anybody who asks.

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
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = Path(tempfile.mkdtemp(prefix="check-livestack-server-"))
HOME = Path(tempfile.mkdtemp(prefix="check-livestack-rig-"))
ADMIN = "test-admin-token"
os.environ["ASTROCOLLAB_DATA"] = str(DATA)
os.environ["ASTROCOLLAB_ADMIN_TOKEN"] = ADMIN
os.environ.setdefault("ASTRO_DATA_DIR", str(HOME))

from astrocontrol import calibration, collab, livestack, pipeline   # noqa: E402
from astrocontrol.config import Config                              # noqa: E402
from astrocontrol.imaging import align, fits, stars, wcs            # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


# ---------------------------------------------------------------- the server
def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"

import uvicorn                                                      # noqa: E402

from server.app import app                                          # noqa: E402

server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT,
                                       log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    try:
        urllib.request.urlopen(f"{BASE}/api/v1/health", timeout=1).read()
        break
    except Exception:                                               # noqa: BLE001
        time.sleep(0.1)
else:
    print("FAIL  the server never came up")
    sys.exit(1)


def call(method, path, body=None, token=ADMIN, raw=None, content=None):
    data = raw if raw is not None else (
        None if body is None else json.dumps(body).encode())
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": content or "application/json"}
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                     headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    return json.loads(payload) if payload else {}


health = call("GET", "/api/v1/health", token="")
case("the server says it can live stack", health.get("liveStacking") is True)

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
print("\n== a collaboration ==")
agents = {}
for rig in (ALPHA, BETA):
    made = call("POST", "/api/v1/agents", {"name": rig.name, "owner": "check"})
    agents[rig.name] = made["token"]
    call("POST", "/api/v1/agent/hello", {
        "protocol": collab.PROTOCOL,
        "profile": collab.RigProfile(
            name=rig.name, focalLength=rig.focal, pixelSize=rig.pixel,
            sensorWidth=rig.width, sensorHeight=rig.height, binning=1,
            filters={"L": None}, exposures={"L": 300.0}).payload(),
    }, token=made["token"])

project = call("POST", "/api/v1/projects", {
    "name": "NGC 9999 live", "region": {"ra": TRUE_RA, "dec": TRUE_DEC,
                                        "width": 1.6, "height": 1.2},
    "kind": "single", "requirements": {"filters": {"L": None}},
    "goals": {"L": 4.0}})["project"]
for rig in (ALPHA, BETA):
    call("POST", f"/api/v1/agent/projects/{project['id']}/join",
         {"hours": 2.0, "exposures": {"L": 300.0}}, token=agents[rig.name])
case("both telescopes joined the project", True, project["id"])

plans = {rig.name: call("GET", f"/api/v1/agent/stack/plan?project={project['id']}",
                        token=agents[rig.name])["plan"]
         for rig in (ALPHA, BETA)}
case("both telescopes are given the same canvas",
     plans["alpha"] == plans["beta"],
     f"{plans['alpha']['pixelWidth']}x{plans['alpha']['pixelHeight']} at "
     f"{plans['alpha']['actualScale']:.3f}\"/px")

plan = livestack.Plan.read(plans["alpha"])
canvas_here = plan.canvas()
canvas_there = livestack.Plan.read(plans["beta"]).canvas()
case("the canvas is identical on both machines, to the pixel",
     canvas_here.payload() == canvas_there.payload())
case("the canvas covers the project's region",
     canvas_here.field[0] >= 1.6 - 1e-6 and canvas_here.field[1] >= 1.2 - 1e-6,
     f"{canvas_here.field[0]:.3f}x{canvas_here.field[1]:.3f} deg")

# --------------------------------------------------------------- the pipeline
print("\n== the pipeline ==")
sent = []


def send(tile, job):
    answer = call("POST", f"/api/v1/agent/stack/tile?project={job.project}",
                  raw=tile.encode(), token=agents[job.agent],
                  content="application/octet-stream")
    sent.append(answer)
    return bool(answer.get("added"))


config.update("livestack", {"enabled": True, "maxPixels": plan.maxPixels})
stacker = pipeline.StackPipeline(config, root=HOME / "stacks", send=send)

stars.MAX_STARS = 400
frames = 0
for index in range(3):
    for rig in (ALPHA, BETA):
        path = rig.expose(index, seed=100 + index * 7 + len(rig.name),
                          project=project["id"])
        want = {"width": rig.width, "height": rig.height, "binning": 1,
                "gain": 100, "offset": 50, "temperature": -10.0, "filter": "L",
                "telescope": rig.name, "camera": f"{rig.name} camera",
                "exposure": 300.0}
        result = library.calibrate_file(path, want)
        if not result.get("calibrated"):
            case(f"{rig.name} sub {index} calibrates", False, result["reasons"])
            continue
        job = pipeline.Job(Path(result["path"]), project["id"], "L", plan,
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

local = stacker.stacks().get(f"{project['id']}/L")
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

# --------------------------------------------------------------- the server
print("\n== the community stack ==")
case("every tile was taken by the server",
     all(row.get("added") for row in sent) and len(sent) == frames,
     f"{sum(1 for row in sent if row.get('added'))} of {len(sent)} added")

community = call("GET", f"/api/v1/stacks/{project['id']}/L", token="")["stack"]
case("the community stack holds every telescope's frames",
     community["frames"] == frames and len(community["agents"]) == 2,
     f"{community['frames']} frames, {community['seconds']:.0f}s, "
     f"{community['covered'] * 100:.0f}% covered")
case("the community stack is on the project's canvas",
     community["plan"]["pixelWidth"] == plans["alpha"]["pixelWidth"])
case("the community stack measured its own depth",
     community["maxFramesDeep"] >= 2 and community["deepestSeconds"] > 300,
     f"{community['maxFramesDeep']} frames deep, "
     f"{community['deepestSeconds']:.0f}s at the deepest")

listed = call("GET", f"/api/v1/stacks?project={project['id']}", token="")["stacks"]
case("the stack is listed for anybody to find", len(listed) == 1
     and listed[0]["filter"] == "L", listed and listed[0]["id"])

request = urllib.request.Request(
    f"{BASE}/api/v1/stacks/{project['id']}/L/preview.png?width=500")
with urllib.request.urlopen(request, timeout=30) as response:
    image = response.read()
    kind = response.headers.get("Content-Type")
case("anybody can watch the picture", image[:8] == b"\x89PNG\r\n\x1a\n"
     and kind == "image/png", f"{len(image)} bytes of PNG")

# Sending the same tile again must not count it twice. A retry after a
# dropped connection is the normal case rather than an error, and a ledger
# that counts the same hour twice is the one way it silently becomes wrong.
repeat_frame, repeat_header = fits.read(calibrated["alpha"])
repeat_tile = livestack.make_tile(
    repeat_frame, wcs.from_header(repeat_header), canvas_here,
    "alpha:repeat", seconds=300, agent="alpha", filter_name="L")
first = call("POST", f"/api/v1/agent/stack/tile?project={project['id']}",
             raw=repeat_tile.encode(), token=agents["alpha"],
             content="application/octet-stream")
again = call("POST", f"/api/v1/agent/stack/tile?project={project['id']}",
             raw=repeat_tile.encode(), token=agents["alpha"],
             content="application/octet-stream")
case("the same tile twice is counted once",
     bool(first.get("added")) and not again.get("added")
     and again["stack"]["frames"] == first["stack"]["frames"]
     and again["stack"]["seconds"] == first["stack"]["seconds"],
     f"{first['stack']['frames']} frames and "
     f"{first['stack']['seconds']:.0f}s after both")

# --------------------------------------------------------------- refusals
print("\n== refusals ==")
elsewhere = Observatory("gamma", focal=530.0, pixel=3.76, width=1600,
                        height=1200, gain=1.0, sky=760.0, noise=11.0,
                        rotation=12.0, offset=(28.0, 14.0))
elsewhere.write_masters(library)
stray = elsewhere.expose(0, seed=999, project=project["id"])
result = library.calibrate_file(stray, {
    "width": 1600, "height": 1200, "binning": 1, "gain": 100, "offset": 50,
    "temperature": -10.0, "filter": "L", "telescope": "gamma",
    "camera": "gamma camera", "exposure": 300.0})
made = call("POST", "/api/v1/agents", {"name": "gamma", "owner": "check"})
agents["gamma"] = made["token"]
frame, header = fits.read(Path(result["path"]))
solution = wcs.from_header(header)
try:
    tile = livestack.make_tile(frame, solution, canvas_here, "gamma:1",
                               seconds=300, agent="gamma", filter_name="L")
    refused = call("POST", f"/api/v1/agent/stack/tile?project={project['id']}",
                   raw=tile.encode(), token=agents["gamma"],
                   content="application/octet-stream")
    case("a frame of the wrong sky is refused rather than stacked",
         not refused.get("added"), refused.get("detail", "")[:90])
except livestack.StackError as exc:
    case("a frame of the wrong sky is refused rather than stacked", True,
         str(exc)[:90])

try:
    call("POST", f"/api/v1/agent/stack/tile?project={project['id']}",
         raw=b"not a tile at all", token=agents["alpha"],
         content="application/octet-stream")
    case("rubbish is refused", False, "it was accepted")
except urllib.error.HTTPError as exc:
    case("rubbish is refused with a reason", exc.code == 400,
         json.loads(exc.read()).get("detail", "")[:70])

try:
    call("GET", "/api/v1/agent/stack/plan?project=nonsense",
         token=agents["alpha"])
    case("an unknown project has no canvas", False, "it was given one")
except urllib.error.HTTPError as exc:
    case("an unknown project has no canvas", exc.code == 404)

try:
    call("POST", f"/api/v1/agent/stack/tile?project={project['id']}",
         raw=b"anything", token="not-a-real-token",
         content="application/octet-stream")
    case("an unknown telescope cannot contribute", False, "it was accepted")
except urllib.error.HTTPError as exc:
    case("an unknown telescope cannot contribute", exc.code == 401)

# --------------------------------------------------------------- keeping it
print("\n== keeping it ==")
stacker.save_all()
reopened = livestack.LiveStack.open(
    livestack.stack_root(HOME / "stacks", project["id"], "L"))
case("a stack survives being closed and reopened",
     reopened.frames == local.frames
     and float(np.max(np.abs(reopened.mean() - local.mean()))) < 1e-9,
     f"{reopened.frames} frames")
stored = livestack.stored_plan(
    livestack.stack_root(HOME / "stacks", project["id"], "L"))
case("the canvas a stack was built on is remembered",
     stored is not None and stored.payload() == plan.payload())

server.should_exit = True
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
