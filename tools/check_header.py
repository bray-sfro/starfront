"""The FITS header on a light frame: everything a pipeline needs, and nothing
that is not true.

    python tools/check_header.py

A frame is taken through the real capture service against stub devices and
read back off disk. The checks are the cards a stacker groups by, the ones a
grader sorts by, the ones that say which panel of which mosaic for which
collaboration, and the timing - DATE-OBS is the shutter opening, not the file
being written.

No test framework, for the same reason as the other checks here.
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from astrocontrol import capture as capture_module                 # noqa: E402
from astrocontrol.config import Config                             # noqa: E402
from astrocontrol.imaging import fits                              # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Camera:
    name = "Test camera"
    connected = True
    binning = 1
    gain = 100
    offset = 30
    pixel_size_um = 3.76
    sensor_width = 160
    sensor_height = 120
    bayer_pattern = None
    temperature = -10.2
    setpoint = -10.0

    def set_settings(self, **kw):
        pass

    def start_exposure(self, seconds, light=True):
        time.sleep(0.2)

    @property
    def image_ready(self):
        return True

    def get_image(self):
        return np.full((120, 160), 1200, dtype=np.uint16)


class Mount:
    connected = True
    ra = 5.5883                      # hours, roughly M42
    dec = -5.391
    side_of_pier = "west"


class Guider:
    connected = True

    def status(self):
        return {"state": "Guiding", "rmsTotal": 0.62, "rmsRa": 0.41, "rmsDec": 0.47}


class Manager:
    def __init__(self, devices):
        self.devices = devices
        self.lines = []

    def get(self, kind):
        return self.devices.get(kind)

    def require(self, kind):
        device = self.devices.get(kind)
        if device is None:
            raise RuntimeError(f"{kind} not connected")
        return device

    def log(self, message, level="info"):
        self.lines.append(message)


def service(devices):
    root = Path(tempfile.mkdtemp())
    config = Config(root / "settings.json")
    config.update("site", {"latitude": 31.9, "longitude": -99.1, "elevation": 400.0,
                           "useMount": False})
    config.update("optics", {"focalLength": 389.0, "rotation": 268.5})
    config.update("capture", {"rootDirectory": str(root / "captures")})
    config.update("collab", {"user": {"id": "1", "name": "astrofalls"}})
    svc = capture_module.CaptureService(Manager(devices), config)
    svc.telescope = "Telescope 1"
    return svc


# ------------------------------------------------- a light on a mosaic panel
svc = service({"camera": Camera(), "mount": Mount(), "guider": Guider()})
svc.set_output(target="Orion mosaic", object_name="Orion mosaic - Panel 5", panel="P5")
svc.set_context(target="Orion mosaic", targetId="t-123", entryId="e-9", mosaic=True,
                panel=5, panels=12, panelRa=5.6, panelDec=-5.4, panelAngle=268.5,
                collabProject="p-77", collabProjectName="Orion constellation collab",
                collabTask="task-1")
svc.mark_dithered()
before = time.time()
record = svc.capture_blocking(2.0, "light")
after = time.time()
header = fits.read_header(record.path)

case("the frame is filed under target, night and panel",
     record.filename.startswith("Orion_mosaic_P5_") and record.filename.endswith("_2s_-10C_0001.fits"),
     record.filename)

# Grouping cards a stacker reads.
case("OBJECT names the panel", header.get("OBJECT") == "Orion mosaic - Panel 5")
case("IMAGETYP, EXPTIME, GAIN, OFFSET, XBINNING, CCD-TEMP, FILTER-less mono",
     header.get("IMAGETYP") == "Light" and header.get("EXPTIME") == 2.0
     and header.get("GAIN") == 100 and header.get("OFFSET") == 30
     and header.get("XBINNING") == 1 and abs(header.get("CCD-TEMP") - -10.2) < 1e-6)
case("TELESCOP says which scope", header.get("TELESCOP") == "Telescope 1")

# Timing: the shutter, not the save.
stamp = datetime.strptime(header["DATE-OBS"], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
case("DATE-OBS is when the shutter opened",
     before - 0.01 <= stamp.timestamp() <= before + 0.5, header.get("DATE-OBS"))
ended = datetime.strptime(header["DATE-END"], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
case("DATE-END is the shutter plus the exposure",
     abs((ended - stamp).total_seconds() - 2.0) < 0.01)
case("JD is mid-exposure and MJD-OBS the start",
     abs(header["JD"] - (stamp.timestamp() + 1.0) / 86400.0 - 2440587.5) < 1e-5
     and abs(header["MJD-OBS"] - (stamp.timestamp() / 86400.0 + 40587.0)) < 1e-5)
case("NIGHT and FRAMENO are there",
     len(str(header.get("NIGHT"))) == 10 and header.get("FRAMENO") == 1)

# Pointing.
case("OBJCTRA/OBJCTDEC are the panel's own coordinates, sexagesimal",
     header.get("OBJCTRA") == "05 36 00.00" and header.get("OBJCTDEC") == "-05 24 00.0",
     f"{header.get('OBJCTRA')} / {header.get('OBJCTDEC')}")
case("RA/DEC are the mount's, in degrees",
     abs(header["RA"] - 5.5883 * 15.0) < 1e-3 and abs(header["DEC"] + 5.391) < 1e-6)
case("PIERSIDE, CENTALT, CENTAZ, AIRMASS and HA are worked out",
     header.get("PIERSIDE") == "WEST" and "CENTALT" in header and "CENTAZ" in header
     and "HA" in header and ("AIRMASS" in header or header["CENTALT"] <= 0),
     f"alt {header.get('CENTALT')} az {header.get('CENTAZ')} ha {header.get('HA')}")
case("the Sun and Moon are recorded",
     "SUNALT" in header and "MOONALT" in header and 0.0 <= header["MOONILLU"] <= 1.0
     and 0.0 <= header["MOONSEP"] <= 180.0)

# Optics.
case("FOCALLEN, PIXSCALE and POSANGLE",
     header.get("FOCALLEN") == 389.0 and abs(header["PIXSCALE"] - 206.265 * 3.76 / 389.0) < 1e-3
     and header.get("POSANGLE") == 268.5)
case("the site, with elevation", header.get("SITELAT") == 31.9 and header.get("SITEELEV") == 400.0)
case("OBSERVER from the Discord sign-in", header.get("OBSERVER") == "astrofalls")
case("SWCREATE and SWVER", header.get("SWCREATE") == "Starfront" and header.get("SWVER"))

# Guiding and dither.
case("guiding state and RMS at the start of the frame",
     header.get("GUIDESTA") == "Guiding" and header.get("GUIDING") is True
     and header.get("GUIDERMS") == 0.62 and header.get("GUIDRMSR") == 0.41)
case("the first frame after a dither says so", header.get("DITHERED") is True)

# The planner's own cards.
case("TARGET, TARGETID, MOSAIC, PANEL and NPANELS",
     header.get("TARGET") == "Orion mosaic" and header.get("TARGETID") == "t-123"
     and header.get("MOSAIC") is True and header.get("PANEL") == 5
     and header.get("NPANELS") == 12 and header.get("PANELPA") == 268.5)
case("the collaboration it was shot for",
     header.get("PROJECT") == "Orion constellation collab" and header.get("PROJID") == "p-77"
     and header.get("COLTASK") == "task-1")

# The second frame is not "after a dither".
record2 = svc.capture_blocking(2.0, "light")
header2 = fits.read_header(record2.path)
case("the next frame is not marked dithered, and counts up",
     header2.get("DITHERED") is False and header2.get("FRAMENO") == 2)

# ----------------------------------------------- a dark carries no target
svc.set_output(target="library", object_name="", panel="")
record3 = svc.capture_blocking(2.0, "dark")
header3 = fits.read_header(record3.path)
case("a dark carries none of the target or guiding cards",
     "PANEL" not in header3 and "PROJECT" not in header3 and "GUIDERMS" not in header3
     and "DITHERED" not in header3 and header3.get("IMAGETYP") == "Dark")

# ------------------------------------- a frame by hand, after a sequence
svc.set_output(target="M31")
record4 = svc.capture_blocking(2.0, "light")
header4 = fits.read_header(record4.path)
case("a target typed by hand forgets the planner's context",
     "PANEL" not in header4 and header4.get("OBJECT") == "M31")

# ------------------------------------------------- nothing but a camera
bare = service({"camera": Camera()})
bare.set_output(target="Bare")
record5 = bare.capture_blocking(1.0, "light")
header5 = fits.read_header(record5.path)
case("with no mount there are no pointing cards, and no lies",
     "RA" not in header5 and "OBJCTRA" not in header5 and "CENTALT" not in header5
     and "PIERSIDE" not in header5 and header5.get("GUIDING") is None
     and "None" not in open(record5.path, "rb").read(2880 * 4).decode("ascii", "replace"))

# The sexagesimal writers at the edges.
case("RA rounds up cleanly", capture_module._hms(23.999999) == "00 00 00.00"
     and capture_module._hms(0.712306) == "00 42 44.30")
case("Dec keeps its sign", capture_module._dms(-5.391111) == "-05 23 28.0"
     and capture_module._dms(41.269) == "+41 16 08.4")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
