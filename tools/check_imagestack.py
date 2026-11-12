"""The live stack on the Image tab, from a frame off the camera to a PNG.

    python tools/check_imagestack.py

Over real HTTP against the real application on a free port, because the
part most likely to be wrong is the join: a frame lands in the capture
service, a job goes to the pipeline, the viewer asks the API for the stack
exactly as it asks for a frame, and the same stretch, crop and pixel readout
have to come back. A shortcut around any of those would skip the thing being
checked.

What it walks through:

  * an invented camera with a pedestal and dust, and the masters that take
    them out, in the calibration library;
  * three dithered lights going through the capture service with calibrated
    copies on disk switched *off* - the stack still calibrates them;
  * the stack appearing in the status, flat where the raw frames were
    vignetted, and served to the viewer: stats, render.png at full and crop,
    the pixel readout, a FITS download;
  * restarting a stack, a frame with no matching masters refused when that
    is asked for, a frame shot with saving off still stacked, and a new
    night starting a new stack.

No test framework, for the same reason as the other checks here.
"""

import math
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HOME = Path(tempfile.mkdtemp(prefix="check-imagestack-"))
os.environ["ASTRO_DATA_DIR"] = str(HOME)

from astrocontrol import calibration, main                          # noqa: E402
from astrocontrol.imaging import fits, wcs                          # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


# ---------------------------------------------------------------- the sky
TRUE_RA, TRUE_DEC = 343.25, 58.1
SEEING = 2.6                                   # arcseconds, FWHM-ish
rng = np.random.default_rng(7)
SKY = np.column_stack([
    TRUE_RA + (rng.random(500) - 0.5) * 1.4 / math.cos(math.radians(TRUE_DEC)),
    TRUE_DEC + (rng.random(500) - 0.5) * 1.1,
    rng.pareto(1.6, 500) * 900.0 + 300.0,
])


def render_sky(solution, sky, noise, seed):
    out = np.full((solution.height, solution.width), float(sky))
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
        out[y0:y0 + 2 * reach + 1, x0:x0 + 2 * reach + 1] += (
            SKY[index, 2] * np.exp(-((gx[None, :] - cx) ** 2 + (gy[:, None] - cy) ** 2)
                                   / (2 * sigma * sigma)))
    return out + np.random.default_rng(seed).normal(0.0, noise, out.shape)


# ------------------------------------------------------------- the camera
WIDTH, HEIGHT = 1600, 1200
FOCAL, PIXEL = 530.0, 3.76
SCALE = 206.265 * PIXEL / FOCAL
ROTATION = 12.0


class Camera:
    name = "Invented camera"
    connected = True
    binning = 1
    gain = 100
    offset = 50
    pixel_size_um = PIXEL
    bayer_pattern = None
    temperature = -10.0
    setpoint = -10.0


camera = Camera()
pedestal = 480.0 + rng.normal(0.0, 3.0, (HEIGHT, WIDTH))
ys, xs = np.mgrid[0:HEIGHT, 0:WIDTH]
radius = np.hypot((xs - WIDTH / 2) / (WIDTH / 2), (ys - HEIGHT / 2) / (HEIGHT / 2))
vignette = 1.0 - 0.28 * radius ** 2
for _ in range(12):
    cx, cy = rng.random(2) * [WIDTH, HEIGHT]
    vignette -= 0.22 * np.exp(-((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * 26 ** 2))


def expose(index, seed):
    """One sub as the camera would hand it over, and where it was pointed."""
    dither = ((index % 3) - 1) * 12.0 / 3600.0
    truth = wcs.Wcs(TRUE_RA + dither, TRUE_DEC - dither, (WIDTH + 1) / 2, (HEIGHT + 1) / 2,
                    wcs._cd_from_angle(-SCALE / 3600, SCALE / 3600, ROTATION),
                    WIDTH, HEIGHT)
    sky = render_sky(truth, 760.0, 11.0, seed)
    raw = np.clip(sky * vignette + pedestal, 0, 65535).astype(np.uint16)
    centre = truth.centre
    # The mount's pointing and the rotator's angle, a little wrong as they are.
    metadata = {"ra": centre[0] / 15.0 + 0.0004, "dec": centre[1] - 0.004,
                "filter": "L", "positionAngle": ROTATION + 3.0,
                "startedAt": time.time() - 300.0}
    return raw, metadata


def corner_ratio(frame):
    middle = float(np.median(frame[HEIGHT // 2 - 100:HEIGHT // 2 + 100,
                                   WIDTH // 2 - 100:WIDTH // 2 + 100]))
    corner = float(np.median(frame[20:220, 20:220]))
    return corner / max(middle, 1e-9)


# -------------------------------------------------------------- settings
print("== the application ==")
main.config.update("site", {"latitude": 31.9, "longitude": -99.1, "useMount": False})
main.config.update("optics", {"focalLength": FOCAL})
main.config.update("calibration", {"libraryDirectory": str(HOME / "library"),
                                   "applyTo": "off", "maxFlatAgeDays": 0.0,
                                   "maxDarkAgeDays": 0.0})
main.config.update("livestack", {"enabled": True, "maxPixels": 1200,
                                 "stackUncalibrated": True})
main.library.forget()
meta = {"exposure": 0.0, "binning": 1, "gain": 100, "offset": 50,
        "temperature": -10.0, "filter": "", "telescope": "", "camera": camera.name}
main.library.store("bias", calibration.to_uint16(pedestal), meta,
                   {"frames": 50, "method": "sigma clip"})
main.library.store("flat", calibration.to_uint16(vignette * 26000.0 + pedestal),
                   {**meta, "exposure": 2.0, "filter": "L"},
                   {"frames": 25, "method": "sigma clip"})
case("the masters are in the library", len(main.library.masters()) == 2)

rig = main.rigs.master
capture = rig.capture
capture.target_name = "NGC 9999"
capture.telescope = ""
stacker = rig.stacker
case("every telescope has a live stack pipeline",
     stacker is not None and capture.stacker is stacker and stacker.enabled)


def settled(count, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = stacker.status(brief=True)
        done = status["counts"]["stacked"] + status["counts"]["refused"]
        if status["pending"] == 0 and done >= count:
            # One more beat for the last frame's bookkeeping to land.
            time.sleep(0.2)
            return stacker.status()
        time.sleep(0.2)
    return stacker.status()


# ------------------------------------------------------------ three subs
print("\n== three subs ==")
started = time.monotonic()
for index in range(3):
    raw, metadata = expose(index, seed=100 + index)
    capture._store(raw, 300.0, "light", camera, metadata)
status = settled(3)
case("every sub reached the stack",
     status["counts"]["stacked"] == 3 and status["counts"]["refused"] == 0,
     f"{status['counts']['stacked']} stacked, {status['counts']['refused']} refused "
     f"in {time.monotonic() - started:.1f}s; last: "
     f"{(status.get('last') or {}).get('error') or 'ok'}")
case("the frames were calibrated on the way in, with no calibrated copy wanted on disk",
     status["counts"]["calibrated"] == 3
     and not list((capture.session_dir / "calibrated").glob("*")) if
     (capture.session_dir / "calibrated").is_dir() else status["counts"]["calibrated"] == 3,
     f"{status['counts']['calibrated']} calibrated, {status['counts']['raw']} raw")
key = "NGC 9999/L"
case("the stack is named by target and filter and is the current one",
     key in status["stacks"] and status["current"] == key, status.get("current"))
case("the stack lives in tonight's folder",
     status["night"] == capture.night_name() and
     Path(status["root"]).name == capture.night_name(), status["root"])

local = stacker.stacks().get(key)
summary = local.summary()
case("the canvas was cut from the camera's own field, a fifth larger",
     abs(summary["plan"]["width"] / (WIDTH * SCALE / 3600.0) - 1.2) < 0.01
     and summary["plan"]["pixelWidth"] <= 1200,
     f"{summary['plan']['pixelWidth']}x{summary['plan']['pixelHeight']} at "
     f"{summary['plan']['actualScale']:.3f}\"/px, rotated {summary['plan']['rotation']:.1f}")
case("the stack is an hour and a quarter deep where every frame landed",
     summary["maxFramesDeep"] == 3 and abs(summary["deepestSeconds"] - 900.0) < 1,
     f"{summary['maxFramesDeep']} frames, {summary['deepestSeconds']:.0f}s")
placing = (status.get("last") or {}).get("placing") or {}
case("the third frame was aligned on the stars rather than trusted",
     bool(placing.get("aligned")), placing.get("detail", "")[:90])

picture, covered = local.picture()
case("the viewer's picture is a 16-bit frame of the canvas",
     picture.dtype == np.uint16 and picture.shape == (local.canvas.height, local.canvas.width)
     and covered.any())
mean = local.mean()
inside = covered & (local.coverage() == 3)
rows, cols = np.nonzero(inside)
middle = mean[rows.min() + 60:rows.max() - 60, cols.min() + 60:cols.max() - 60]
# The stack is in sky-subtracted ADU per second. A raw sub's corner sits
# 0.28 of the sky below its centre - about 0.7 ADU/s here - and a plane fit
# cannot take a bowl out, so the corner reads well below the centre unless
# the flat was applied first.
corner = float(np.median(middle[:80, :80]))
centre = float(np.median(middle[middle.shape[0] // 2 - 40:middle.shape[0] // 2 + 40,
                                middle.shape[1] // 2 - 40:middle.shape[1] // 2 + 40]))
case("the flat took the vignetting out before the frames were stacked",
     abs(corner - centre) < 0.08,
     f"corner {corner:.3f} vs centre {centre:.3f} ADU/s "
     f"(raw corner/centre {corner_ratio(raw):.2f}, so about "
     f"{(1 - corner_ratio(raw)) * 760 / 300:.2f} ADU/s apart uncalibrated)")

brief = rig.status()["livestack"]
case("the status carries a brief of every stack with no pass over the pixels",
     brief["stacks"][key]["frames"] == 3 and brief["stacks"][key]["calibrated"] == 3
     and "covered" not in brief["stacks"][key], str(brief["stacks"][key])[:100])
snapshot = main._snapshot()
case("the socket payload carries the master's stacks at the top level",
     snapshot["livestack"]["stacks"][key]["frames"] == 3)

# ----------------------------------------------------------------- the API
print("\n== the viewer's side ==")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
import uvicorn                                                      # noqa: E402

server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=PORT,
                                       log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    try:
        urllib.request.urlopen(f"{BASE}/api/livestack", timeout=1).read()
        break
    except Exception:                                               # noqa: BLE001
        time.sleep(0.1)

import json                                                         # noqa: E402


def get(path, raw=False):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as response:
        body = response.read()
        # The headers object looks names up whatever their case; a dict of
        # it would not, and the server sends them in lower case.
        return (body, response.headers) if raw else json.loads(body)


def post(path, body=None):
    request = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body or {}).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


query = f"project={urllib.parse.quote('NGC 9999')}&filter=L"
listing = get("/api/livestack")
case("GET /api/livestack lists tonight's stacks", key in listing["stacks"]
     and listing["stacks"][key]["frames"] == 3)

stats = get(f"/api/livestack/stats?{query}")
case("the stack's stats come in the shape a frame's do",
     stats["image"]["width"] == local.canvas.width and "histogram" in stats["stats"]
     and 0 <= stats["autoStretch"]["black"] < 1 and stats["summary"]["frames"] == 3,
     stats["image"]["filename"])

png, headers = get(f"/api/livestack/render.png?{query}&auto=true&maxDim=800", raw=True)
case("render.png draws the whole stack", png[:8] == b"\x89PNG\r\n\x1a\n"
     and headers.get("X-Stack-Frames") == "3", f"{len(png)} bytes, factor {headers.get('X-Render-Factor')}")
crop, headers = get(f"/api/livestack/render.png?{query}&auto=false&black=0.01&white=0.6"
                    f"&midtone=0.2&region=100,100,300,200&maxDim=0", raw=True)
case("render.png crops a region at 1:1 for the zoomed viewer",
     crop[:8] == b"\x89PNG\r\n\x1a\n" and headers.get("X-Render-Factor") == "1",
     f"{len(crop)} bytes")

y, x = rows[len(rows) // 2], cols[len(cols) // 2]
pixel = get(f"/api/livestack/pixel?{query}&x={x}&y={y}")
case("the pixel readout says how many frames are under the cursor",
     pixel["frames"] == 3 and pixel["value"] == int(picture[y, x]), str(pixel))

fits_bytes, headers = get(f"/api/livestack/download?{query}", raw=True)
out = HOME / "downloaded.fits"
out.write_bytes(fits_bytes)
frame, header = fits.read(out)
case("the download is a FITS of the canvas with its WCS",
     frame.shape == picture.shape and header.get("NFRAMES") == 3
     and abs(float(header["CRVAL1"]) - TRUE_RA) < 0.01 and header.get("CTYPE1") == "RA---TAN",
     f"{frame.shape[1]}x{frame.shape[0]}, CRVAL {header.get('CRVAL1')}, {header.get('CRVAL2')}")

try:
    get(f"/api/livestack/stats?project=Nothing&filter=L")
    case("an unknown stack is a 404", False, "it was served")
except urllib.error.HTTPError as exc:
    case("an unknown stack is a 404", exc.code == 404)

saved = post("/api/settings/livestack", {"maxPixels": 1024, "removeGradient": False})
case("live stack settings are saved", saved["livestack"]["maxPixels"] == 1024
     and saved["livestack"]["removeGradient"] is False)
settings = get("/api/settings")
case("the settings form can read them back", settings["livestack"]["maxPixels"] == 1024)

# ---------------------------------------------------------------- restarting
print("\n== restarting ==")
answer = post(f"/api/livestack/reset?{query}")
case("a stack can be thrown away", answer["reset"] is True
     and key not in answer["livestack"]["stacks"])
raw, metadata = expose(0, seed=500)
capture._store(raw, 300.0, "light", camera, metadata)
status = settled(4)
case("the next frame starts it again on a canvas of the new size",
     status["stacks"].get(key, {}).get("frames") == 1
     and status["stacks"][key]["plan"]["pixelWidth"] <= 1024,
     f"{status['stacks'].get(key, {}).get('frames')} frame, "
     f"{status['stacks'].get(key, {}).get('plan', {}).get('pixelWidth')} px")

# ------------------------------------------------------------- uncalibrated
print("\n== frames with no masters ==")
main.config.update("livestack", {"stackUncalibrated": False})
camera.gain = 300                                  # nothing in the library matches
raw, metadata = expose(1, seed=501)
capture._store(raw, 300.0, "light", camera, metadata)
status = settled(5)
last = status.get("last") or {}
case("with raw frames not wanted, a frame with no matching masters is left out and says why",
     status["counts"]["refused"] == 1 and "no matching masters" in str(last.get("error")),
     str(last.get("error"))[:100])

main.config.update("livestack", {"stackUncalibrated": True})
raw, metadata = expose(2, seed=502)
capture._store(raw, 300.0, "light", camera, metadata)
status = settled(6)
case("with raw frames allowed, it goes in and is counted as raw",
     status["counts"]["raw"] == 1 and status["stacks"][key]["frames"] == 2,
     f"{status['counts']['raw']} raw of {status['stacks'][key]['frames']}")
brief = rig.status()["livestack"]["stacks"][key]
case("the brief says how many of the stack's frames were calibrated",
     brief["frames"] == 2 and brief["calibrated"] == 1)
camera.gain = 100

# ------------------------------------------------------------- saving off
print("\n== saving switched off ==")
capture.save_enabled = False
raw, metadata = expose(0, seed=503)
record = capture._store(raw, 300.0, "light", camera, metadata)
status = settled(7)
case("a frame shot with saving off is still stacked, from memory",
     not record.saved and status["stacks"][key]["frames"] == 3,
     f"{status['stacks'][key]['frames']} frames; last: "
     f"{(status.get('last') or {}).get('error') or (status.get('last') or {}).get('stack', {}).get('detail')}")
capture.save_enabled = True

# -------------------------------------------------------------- a new night
print("\n== a new night ==")
before_root = status["root"]
stacker.begin_night("2031-01-01")
case("a new night saves what was open and starts a fresh folder",
     stacker.status()["stacks"] == {} and stacker.root.name == "2031-01-01"
     and (Path(before_root) / "NGC 9999" / "L" / "stack.json").is_file(),
     str(stacker.root))
stacker.begin_night(capture.night_name())
case("coming back to tonight finds the stack on disk again",
     stacker.stack("NGC 9999", "L") is not None
     and stacker.stack("NGC 9999", "L").frames == 3)

server.should_exit = True
stacker.stop()
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
