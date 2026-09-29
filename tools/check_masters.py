"""The calibration library as a whole: what tonight needs, what is there, and
bringing in masters built elsewhere.

    python tools/check_masters.py

  * The suggested recipe: a dark at every default exposure the filters are
    shot at, flats that measure themselves, no dark flats.
  * Coverage: each thing the night needs is ok, out of date, or missing -
    three words, because those are the three things a person can do.
  * Import: a PixInsight XISF master (float 0-1, zlib, byte-shuffled) and a
    float FITS master both land in the library as 16-bit ADU with the right
    header, and are then matched like any other.

No test framework, for the same reason as the other checks here.
"""
import os
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from astrocontrol import calibrating, calibration                  # noqa: E402
from astrocontrol.config import Config                           # noqa: E402
from astrocontrol.devices.base import DeviceError                # noqa: E402
from astrocontrol.imaging import fits, xisf                      # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


class Manager:
    def __init__(self, devices=None):
        self.devices = devices or {}

    def get(self, kind):
        return self.devices.get(kind)


class Rig:
    def __init__(self, config):
        self.id = "main"
        self.name = "Telescope 1"
        self.config = config
        self.manager = Manager()


class Rigs:
    def __init__(self, rig):
        self.master = rig


def fresh():
    root = Path(tempfile.mkdtemp())
    config = Config(root / "settings.json")
    config.update("calibration", {"libraryDirectory": str(root / "lib")})
    config.update("camera", {"filterNames": ["L", "R", "G", "B", "H"],
                             "setpoint": -10.0, "gain": 100, "offset": 30})
    config.update("autoplan", {"filterExposures": {"L": 120.0, "R": 180.0, "G": 180.0,
                                                   "B": 180.0, "H": 600.0}})
    return config, Rig(config), calibration.Library(config)


# ------------------------------------------------------ the suggested recipe
config, rig, library = fresh()
exposures = calibrating.default_exposures(rig, config)
case("the default exposures are read per filter, in the one spelling",
     exposures == {"L": 120.0, "R": 180.0, "G": 180.0, "B": 180.0, "H": 600.0},
     str(exposures))

recipe = calibrating.suggested_recipe(Rigs(rig), config)
kinds = [s["frameType"] for s in recipe["sets"]]
darks = sorted(s["exposure"] for s in recipe["sets"] if s["frameType"] == "dark")
case("the suggestion has a dark at every default exposure, and no others",
     darks == [120.0, 180.0, 600.0], str(darks))
case("and no dark flats", "darkflat" not in kinds, str(kinds))
case("and one line of flats for every filter, measured",
     any(s["frameType"] == "flat" and s["allFilters"] and s["autoExposure"]
         for s in recipe["sets"]))
case("and bias frames", "bias" in kinds)

# A flat can no longer be told its exposure.
flat = calibrating._clean_set({"frameType": "flat", "count": 10, "exposure": 2.0,
                               "autoExposure": False}, 0)
case("a flat measures its own exposure whatever the recipe says",
     flat["autoExposure"] is True)

# ---------------------------------------------------------------- coverage
def needs_for(rig, config):
    base = {"binning": 1, "gain": 100, "offset": 30, "temperature": -10.0,
            "telescope": rig.name, "camera": ""}
    needs = [{"kind": "bias", "label": "bias", "want": {**base, "exposure": 0.0}}]
    for exposure in sorted(set(calibrating.default_exposures(rig, config).values())):
        needs.append({"kind": "dark", "label": f"dark {exposure:g}s",
                      "want": {**base, "exposure": exposure}})
    for name in ["L", "R", "G", "B", "H"]:
        needs.append({"kind": "flat", "label": f"flat {name}",
                      "want": {**base, "exposure": 0.0, "filter": name}})
    return needs


def master(library, kind, exposure=0.0, filt="", age_days=0.0, level=1000.0):
    frame = np.full((40, 30), level, dtype=np.float64)
    meta = {"exposure": exposure, "binning": 1, "gain": 100, "offset": 30,
            "temperature": -10.0, "filter": filt, "telescope": "Telescope 1"}
    made = library.store(kind, frame, meta, {"frames": 20, "method": "sigma"})
    if age_days:
        stamp = time.time() - age_days * 86400.0
        os.utime(made["path"], (stamp, stamp))
        library.forget()
    return made


config, rig, library = fresh()
empty = library.coverage(needs_for(rig, config))
case("an empty library is incomplete, and says how much is missing",
     empty["state"] == "missing" and empty["missing"] == 9, empty["summary"])

master(library, "bias")
for exposure in (120.0, 180.0, 600.0):
    master(library, "dark", exposure)
for name in ["L", "R", "G", "B", "H"]:
    master(library, "flat", 3.0, name)
full = library.coverage(needs_for(rig, config))
case("with everything shot, the set is complete and current",
     full["state"] == "ok" and full["missing"] == 0 and full["stale"] == 0,
     full["summary"])

# Age one flat past its limit: that is "out of date", not "missing".
config.update("calibration", {"maxFlatAgeDays": 30})
for entry in library.masters():
    if entry["type"] == "flat" and entry["filter"] == "H":
        stamp = time.time() - 45 * 86400.0
        os.utime(entry["path"], (stamp, stamp))
library.forget()
aged = library.coverage(needs_for(rig, config))
row = next(r for r in aged["rows"] if r["label"] == "flat H")
case("an old master is out of date, not missing",
     aged["state"] == "stale" and row["state"] == "stale" and row["ageDays"] > 40,
     f"{aged['summary']} / {row['detail']}")
case("...and the rest are still ok",
     sum(1 for r in aged["rows"] if r["state"] == "ok") == 8)

# Change a default exposure: the dark for it goes missing at once.
config.update("autoplan", {"filterExposures": {"L": 90.0}})
moved = library.coverage(needs_for(rig, config))
case("a new default exposure is a missing dark until it is shot",
     any(r["state"] == "missing" and r["label"] == "dark 90s" for r in moved["rows"]),
     moved["summary"])

# ---------------------------------------------------------- importing
def write_xisf(path, frame, keywords, compress=False, shuffle=False):
    """A monolithic XISF the way PixInsight writes a master: Float32 0-1."""
    data = frame.astype("<f4").tobytes()
    attrs = 'geometry="{w}:{h}:1" sampleFormat="Float32" colorSpace="Gray" ' \
            'byteOrder="little" location="attachment:{pos}:{size}"'
    payload = data
    comp = ""
    if compress:
        raw = data
        if shuffle:
            raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 4).T.reshape(-1).tobytes()
        payload = zlib.compress(raw)
        comp = f' compression="zlib{"+sh" if shuffle else ""}:{len(data)}{":4" if shuffle else ""}"'
    keys = "".join(f'<FITSKeyword name="{k}" value="{v}" comment=""/>'
                   for k, v in keywords.items())
    # The attachment position depends on the header length, which depends on
    # the position's digits: iterate until it settles.
    pos = 0
    for _ in range(4):
        header = ('<?xml version="1.0" encoding="UTF-8"?>'
                  '<xisf version="1.0" xmlns="http://www.pixinsight.com/xisf">'
                  '<Image ' + attrs.format(w=frame.shape[1], h=frame.shape[0],
                                           pos=pos, size=len(payload)) + comp + '>'
                  + keys + '</Image></xisf>').encode()
        if pos == 16 + len(header):
            break
        pos = 16 + len(header)
    with open(path, "wb") as handle:
        handle.write(b"XISF0100" + struct.pack("<I", len(header)) + b"\0\0\0\0")
        handle.write(header)
        handle.write(payload)


config, rig, library = fresh()
scratch = Path(tempfile.mkdtemp())

# A PixInsight master dark: float, normalised, with its keywords.
dark = (np.full((40, 30), 700.0) + np.arange(30)[None, :]) / 65535.0
xisf_path = scratch / "masterDark_300s.xisf"
write_xisf(xisf_path, dark, {"IMAGETYP": "'Master Dark'", "EXPTIME": "300.0",
                             "CCD-TEMP": "-10.0", "GAIN": "100", "OFFSET": "30",
                             "XBINNING": "1", "NCOMBINE": "31"})
case("an XISF file is recognised", xisf.is_xisf(xisf_path))
seen = calibration.inspect_master_file(xisf_path)
case("...and read for what it says it is",
     seen["type"] == "dark" and seen["exposure"] == 300.0 and seen["temperature"] == -10.0
     and seen["width"] == 30 and seen["frames"] == 31, str(seen))
brought = library.import_master(xisf_path)
frame, _ = fits.read(brought["path"])
case("a float 0-1 master comes in as 16-bit ADU",
     brought["type"] == "dark" and brought["exposure"] == 300.0
     and abs(float(frame[0, 0]) - 700.0) <= 1.0 and abs(float(frame[0, 29]) - 729.0) <= 1.0,
     f"{brought['type']} {brought['exposure']} {frame[0, 0]} {frame[0, 29]}")
case("and the library lists it", any(m["id"] == brought["id"] for m in library.masters()))

# The same, zlib-compressed and byte-shuffled, which PixInsight does by default.
packed = scratch / "masterDark_packed.xisf"
write_xisf(packed, dark, {"IMAGETYP": "'Master Dark'", "EXPTIME": "300.0"},
           compress=True, shuffle=True)
plain, _ = xisf.read(packed)
case("a zlib+shuffle XISF reads back exactly",
     np.allclose(plain, dark.astype(np.float32)), f"{plain[0, :3]}")

# A float FITS flat from elsewhere, with no filter card: the operator names it.
flat = np.full((40, 30), 0.9, dtype=np.float32)
flat_path = scratch / "masterFlat.fits"
with open(flat_path, "wb") as handle:
    cards = [("SIMPLE", "T"), ("BITPIX", "-32"), ("NAXIS", "2"), ("NAXIS1", "30"),
             ("NAXIS2", "40"), ("IMAGETYP", "'Flat'"), ("EXPTIME", "2.5"),
             ("NCOMBINE", "25")]
    text = "".join(f"{k:<8}= {v:>20}".ljust(80) for k, v in cards) + "END".ljust(80)
    handle.write(text.ljust(2880).encode("ascii"))
    handle.write(flat.astype(">f4").tobytes())
try:
    library.import_master(flat_path)
    case("a flat with no filter is refused until one is named", False)
except DeviceError as exc:
    case("a flat with no filter is refused until one is named", "filter" in str(exc), str(exc))
named = library.import_master(flat_path, {"filter": "Ha", "telescope": "Telescope 1",
                                          "temperature": -10.0})
case("...and comes in once named, in the one spelling",
     named["type"] == "flat" and named["filter"] == "H" and named["exposure"] == 2.5,
     f"{named['type']} {named['filter']} {named['exposure']}")
frame, _ = fits.read(named["path"])
case("a normalised flat is scaled into 16-bit range",
     abs(float(frame[5, 5]) - round(0.9 * 65535)) <= 1.0, str(frame[5, 5]))

# Once in, an imported master is matched like any other.
found, why = library.match("dark", {"exposure": 300.0, "binning": 1, "gain": 100,
                                    "offset": 30, "temperature": -9.0,
                                    "telescope": "Telescope 1"})
case("the imported dark matches a 300 s light at -9 C", found is not None
     and found["id"] == brought["id"], why)

# Something that is neither format is refused with a reason.
junk = scratch / "notes.txt"
junk.write_text("hello", "utf-8")
try:
    calibration.inspect_master_file(junk)
    case("a file that is not a master is refused", False)
except DeviceError as exc:
    case("a file that is not a master is refused", "not a FITS or XISF" in str(exc), str(exc))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
