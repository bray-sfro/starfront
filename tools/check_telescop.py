"""TELESCOP names the real telescope; OBSERVAT, RIGNAME and RIGID (0.2.53a).

    python tools/check_telescop.py

2026-11-04: every rig nobody renamed wrote TELESCOP = 'Telescope 1', so
processing could not tell the FSQ106 from vsd100 from EAGLE. Now TELESCOP is
the telescope's configured name (optics.telescopeName), else its Starfront
name unless that is the generic 'Telescope N', else this PC's name (fleet
nodeName, else the hostname) with '-N' when the PC carries several; plus
OBSERVAT, RIGNAME (the PC) and RIGID (<PC>-<telescope index>). TELESCOP is
still there, the cards downstream code reads are untouched, and calibration
made before (TELESCOP 'Telescope 1') still matches the same telescope.

No test framework, like the other checks here.
"""
import os
import socket
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, str(ROOT / "tools"))

from astrocontrol import calibration                               # noqa: E402
from astrocontrol import capture as capture_module                 # noqa: E402
from astrocontrol.config import Config                             # noqa: E402
from astrocontrol.imaging import fits                              # noqa: E402

results = []
HOST = socket.gethostname()


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Camera:
    name = "ZWO ASI2600MM Pro"
    connected = True
    binning = 1
    gain = 100
    offset = 50
    pixel_size_um = 3.76
    sensor_width = 160
    sensor_height = 120
    bayer_pattern = None
    temperature = -10.0
    setpoint = -10.0
    has_gain = True
    has_offset = True

    def set_settings(self, **kw):
        for key in ("gain", "offset", "binning"):
            if kw.get(key) is not None:
                setattr(self, key, kw[key])

    def start_exposure(self, seconds, light=True):
        pass

    @property
    def image_ready(self):
        return True

    def get_image(self):
        return np.full((120, 160), 1200, dtype=np.uint16)


class Manager:
    def __init__(self, devices):
        self.devices = devices

    def get(self, kind):
        return self.devices.get(kind)

    def require(self, kind):
        return self.devices[kind]

    def log(self, message, level="info"):
        pass


def service(name="Telescope 1", index=1, count=1, node="", scope_name="", observatory=None):
    root = Path(tempfile.mkdtemp())
    config = Config(root / "settings.json")
    config.update("capture", {"rootDirectory": str(root / "captures")})
    config.update("camera", {"gain": 100, "offset": 50})
    config.update("server", {"nodeName": node})
    if scope_name:
        config.update("optics", {"telescopeName": scope_name})
    if observatory is not None:
        config.update("site", {"observatory": observatory})
    svc = capture_module.CaptureService(Manager({"camera": Camera()}), config)
    svc.telescope = name
    svc.rig_index, svc.rig_count = index, count
    return svc


def shoot(svc, kind="light"):
    svc.set_output(target="M42", object_name="M42")
    record = svc.capture_blocking(1.0, kind)
    return record, fits.read_header(record.path)


# -- the generic name is replaced -------------------------------------------------
svc = service()
record, h = shoot(svc)
case("generic 'Telescope 1' on a one-scope PC -> TELESCOP is the PC name",
     h.get("TELESCOP") == HOST, f"{h.get('TELESCOP')!r}")
case("TELESCOP is never a generic 'Telescope N'",
     not capture_module.is_generic_telescope(str(h.get("TELESCOP"))))
case("OBSERVAT defaults to Starfront Observatories",
     h.get("OBSERVAT") == "Starfront Observatories")
case("RIGNAME is the PC and RIGID is <PC>-<index>",
     h.get("RIGNAME") == HOST and h.get("RIGID") == f"{HOST}-1",
     f"{h.get('RIGNAME')} / {h.get('RIGID')}")
case("the in-memory record keeps the Starfront name (folders, uploads unchanged)",
     record.telescope == "Telescope 1" and svc.telescope == "Telescope 1")

# -- keys downstream code reads are all still there --------------------------------
needed = ("IMAGETYP", "EXPTIME", "EXPOSURE", "GAIN", "OFFSET", "XBINNING", "YBINNING",
          "INSTRUME", "CCD-TEMP", "DATE-OBS", "OBJECT", "TELESCOP", "SWCREATE",
          "SWVER", "NIGHT", "FRAMENO", "XPIXSZ")
missing = [k for k in needed if k not in h]
case("every card the uploader, filter ID and the library read is still written",
     not missing, f"missing {missing}" if missing else "")
case("GAIN/OFFSET unchanged by this", h.get("GAIN") == 100 and h.get("OFFSET") == 50)

# -- the fleet node name, a configured scope name, named rigs -----------------------
_, h = shoot(service(node="EAGLE4PRO0258"))
case("fleet nodeName wins over the hostname", h.get("TELESCOP") == "EAGLE4PRO0258"
     and h.get("RIGID") == "EAGLE4PRO0258-1" and h.get("RIGNAME") == "EAGLE4PRO0258")
_, h = shoot(service(node="EAGLE4PRO0258", scope_name="Twin FSQ"))
case("optics.telescopeName is written as is", h.get("TELESCOP") == "Twin FSQ")
_, h = shoot(service(name="FSQ106", node="WIN-Q9DV8QD504V"))
case("a telescope already named keeps its name", h.get("TELESCOP") == "FSQ106"
     and h.get("RIGID") == "WIN-Q9DV8QD504V-1")
for i, n in enumerate(["RASA1", "RASA2", "RASA3"], start=1):
    _, h = shoot(service(name=n, index=i, count=3, node="DESKTOP-4PILSFA"))
    case(f"multiscope {n}: own name, RIGID ...-{i}",
         h.get("TELESCOP") == n and h.get("RIGID") == f"DESKTOP-4PILSFA-{i}"
         and h.get("RIGNAME") == "DESKTOP-4PILSFA")
_, h = shoot(service(name="Telescope 2", index=2, count=2, node="NAMIBIA-PC"))
case("generic second scope on a two-camera PC -> <PC>-2, not 'Telescope 2'",
     h.get("TELESCOP") == "NAMIBIA-PC-2", f"{h.get('TELESCOP')!r}")
_, h = shoot(service(observatory="Hakos"))
case("OBSERVAT follows site.observatory", h.get("OBSERVAT") == "Hakos")
_, h = shoot(service(node="EAGLE4PRO0258"), kind="flat")
case("calibration frames carry the same cards", h.get("TELESCOP") == "EAGLE4PRO0258"
     and h.get("RIGID") == "EAGLE4PRO0258-1" and h.get("OBSERVAT"))
for generic in ("Telescope 1", "telescope", "Telescope12", " Telescope 3 "):
    case(f"{generic!r} counts as generic", capture_module.is_generic_telescope(generic))
case("'Telescope Peak' and 'RASA1' are not generic",
     not capture_module.is_generic_telescope("Telescope Peak")
     and not capture_module.is_generic_telescope("RASA1"))

# -- calibration made before still matches; another scope's still does not ----------
root = Path(tempfile.mkdtemp())
config = Config(root / "settings.json")
config.update("calibration", {"libraryDirectory": str(root / "lib")})
library = calibration.Library(config)


def flat(telescope):
    meta = {"exposure": 2.0, "binning": 1, "gain": 100, "offset": 50,
            "temperature": -10.0, "filter": "G", "telescope": telescope}
    return library.store("flat", np.full((120, 160), 20000.0), meta,
                         {"frames": 20, "method": "sigma"})


svc = service(node="EAGLE4PRO0258")
want = {"width": 160, "height": 120, "binning": 1, "gain": 100, "offset": 50,
        "temperature": -10.0, "filter": "G", "telescope": svc.telescope,
        "telescopes": svc.telescope_names(), "exposure": 300.0}
case("the want lists the new TELESCOP and the old name",
     svc.telescope_names() == ["EAGLE4PRO0258", "Telescope 1"], str(svc.telescope_names()))
flat("Telescope 1")
found, why = library.match("flat", want, ignore_age=True)
case("a flat made before 0.2.53a ('Telescope 1') still calibrates this telescope",
     found is not None and found["telescope"] == "Telescope 1", why)
library.forget()
for path in (root / "lib").rglob("*.fit*"):
    path.unlink()
library.forget()
flat("EAGLE4PRO0258")
found, why = library.match("flat", want, ignore_age=True)
case("a flat made from 0.2.53a frames (TELESCOP EAGLE4PRO0258) matches too",
     found is not None and found["telescope"] == "EAGLE4PRO0258", why)
for path in (root / "lib").rglob("*.fit*"):
    path.unlink()
library.forget()
flat("RASA3")
found, why = library.match("flat", want, ignore_age=True)
case("another telescope's flat still never matches", found is None, why)
old_want = {k: v for k, v in want.items() if k != "telescopes"}
found, _ = library.match("flat", {**old_want, "telescope": "RASA3"}, ignore_age=True)
case("a want without 'telescopes' (older callers) works as before", found is not None)

failed = results.count(False)
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)
