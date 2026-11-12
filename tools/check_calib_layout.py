"""calib-layout patch: Dropbox per-telescope layout for the calibration library.

    python tools/check_calib_layout.py

Temp dirs only; nothing touches a real Dropbox or data folder.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="calib-layout-"))
DATA = TMP / "AstroControl"                       # stands in for C:\Users\Bray\AstroControl
BOX = TMP / "Dropbox"                             # stands in for C:\Users\Bray\Dropbox
APP = TMP / "LocalAppData"
os.environ["ASTRO_DATA_DIR"] = str(DATA)
os.environ["LOCALAPPDATA"] = str(APP)
os.environ.pop("APPDATA", None)
# dropbox_root() falls back to ~/Dropbox, so on a PC with a real Dropbox the
# "no Dropbox" case found it. Home is a temp folder too (HOME on POSIX,
# USERPROFILE on Windows: both are what Path.home() reads).
HOME = TMP / "home"
HOME.mkdir(parents=True)
os.environ["HOME"] = str(HOME)
os.environ["USERPROFILE"] = str(HOME)
for _var in ("HOMEDRIVE", "HOMEPATH"):
    os.environ.pop(_var, None)
DATA.mkdir(parents=True)

from astrocontrol import calibration                       # noqa: E402
from astrocontrol import capture as capture_mod            # noqa: E402
from astrocontrol.config import Config                     # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))


def meta(tel, temp=-5.0, exposure=0.0):
    return {"exposure": exposure, "binning": 1, "gain": 100, "offset": 50,
            "temperature": temp, "filter": "", "telescope": tel,
            "camera": f"ZWO ASI2600MM Pro ({tel})"}


def want(tel, exposure=0.0, temp=-5.0):
    return {"exposure": exposure, "binning": 1, "gain": 100, "offset": 50,
            "temperature": temp, "filter": "", "telescope": tel,
            "camera": "", "width": 30, "height": 40}


config = Config(DATA / "settings.json")

# 1. no Dropbox at all -> legacy root
case("isolation: no Dropbox visible (info.json and ~/Dropbox are temp-only)",
     calibration.dropbox_root() is None, str(calibration.dropbox_root()))
lib = calibration.Library(config)
case("no Dropbox: library stays at <data>/calibration",
     lib.root == DATA / "calibration", str(lib.root))

# Legacy masters, built by the old code (flat masters/ folder), before Dropbox exists.
legacy_lib = calibration.Library(config)
old = {}
for tel in ("rasa1", "rasa2", "rasa3"):
    frame = np.full((40, 30), 1000.0)
    m = legacy_lib.store("bias", frame, meta(tel), {"frames": 50, "method": "sigma"})
    # move it to where the OLD code put it, to mimic the 10/30 masters
    target = DATA / "calibration" / "masters" / Path(m["path"]).name
    target.parent.mkdir(parents=True, exist_ok=True)
    Path(m["path"]).replace(target)
    old[tel] = target
    m = legacy_lib.store("dark", frame, meta(tel, exposure=300.0), {"frames": 25, "method": "sigma"})
    target = DATA / "calibration" / "masters" / Path(m["path"]).name
    Path(m["path"]).replace(target)
# tidy the empty new-layout folders that store() made under the legacy root
import shutil  # noqa: E402
for tel in ("RASA1", "RASA2", "RASA3"):
    shutil.rmtree(DATA / "calibration" / tel, ignore_errors=True)
lib.forget()

# 2. Dropbox present (via info.json) with RASA Rig\Calibration -> that is the root
(BOX / "RASA Rig" / "Calibration").mkdir(parents=True)
(APP / "Dropbox").mkdir(parents=True)
(APP / "Dropbox" / "info.json").write_text(json.dumps({"personal": {"path": str(BOX)}}))
lib = calibration.Library(config)
case("Dropbox found from info.json, root = <Dropbox>/RASA Rig/Calibration",
     lib.root == BOX / "RASA Rig" / "Calibration", str(lib.root))

# 3. libraryDirectory wins when set
config.update("calibration", {"libraryDirectory": str(TMP / "custom")})
case("calibration.libraryDirectory overrides the default",
     calibration.Library(config).root == TMP / "custom")
config.update("calibration", {"libraryDirectory": ""})
lib = calibration.Library(config)

# 4. paths for each telescope
night = "2026-10-30"
print("\nGenerated paths (root = <Dropbox>/RASA Rig/Calibration):")
ok = True
for tel, folder in (("rasa1", "RASA1"), ("rasa2", "RASA2"), ("rasa3", "RASA3")):
    b = lib.sub_folder(tel, "bias", night)
    d = lib.sub_folder(tel, "dark", night)
    f = lib.sub_folder(tel, "flat", night)
    s = lib.sub_folder(tel, "skyflat", night)
    mf = lib.master_folder(tel, night)
    for label, p in (("bias  ", b), ("dark  ", d), ("flat  ", f), ("skyflat", s), ("master", mf)):
        print(f"   {tel} {label} -> {p.relative_to(BOX)}")
    r = lib.root / folder
    ok &= (b == r / "Bias" / night and d == r / "Darks" / night
           and f == r / "Flats" / night and s == r / "Flats" / night / "sky"
           and mf == r / "Masters" / night)
case("subs go to RASA#/{Bias,Darks,Flats}/<night>, masters to RASA#/Masters/<night>", ok)

# 5. the night rule (noon cut) used for master folders
t = time.mktime((2026, 10, 31, 1, 30, 0, 0, 0, -1))
case("a master built at 01:30 on 10/31 files under the 10/30 night",
     calibration.night_of(t) == "2026-10-30", calibration.night_of(t))

# 6. old masters still discovered, flagged legacy, and match
found = lib.masters()
legacy = [m for m in found if m.get("legacy")]
case("the 6 old masters in <data>/calibration/masters are still found",
     len(legacy) == 6, f"{len(found)} found")
hit, why = lib.match("bias", want("rasa2"))
case("an old rasa2 bias still matches a rasa2 frame",
     hit is not None and hit["telescope"] == "rasa2" and hit.get("legacy"), why)
hit, why = lib.match("dark", want("rasa3", 300.0))
case("an old rasa3 300s dark still matches", hit is not None and hit["telescope"] == "rasa3", why)

# 7. old masters are read-only
try:
    lib.remove(legacy[0]["id"])
    case("removing a legacy master is refused", False)
except Exception as exc:  # DeviceError
    case("removing a legacy master is refused", Path(legacy[0]["path"]).exists(), str(exc))

# 8. a new master lands in the new layout and is preferred / de-duplicated
frame = np.full((40, 30), 1100.0)
new = lib.store("bias", frame, meta("rasa1", temp=-5.0), {"frames": 50, "method": "sigma"})
p = Path(new["path"])
case("a new rasa1 bias master is written to RASA1/Masters/<tonight>",
     p.parent.parent == lib.root / "RASA1" / "Masters", str(p.relative_to(BOX)))
case("no stray old-style masters/ or subs/ folder appears in Dropbox",
     not (lib.root / "masters").exists() and not (lib.root / "subs").exists())
# copy an old master into the new layout (what dropbox organizer did) -> seen once, as the new copy
dst = lib.root / "RASA2" / "Masters" / night / old["rasa2"].name
dst.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(old["rasa2"], dst)
lib.forget()
same = [m for m in lib.masters() if m["id"] == old["rasa2"].stem]
case("a master present in both folders is listed once, from the new layout",
     len(same) == 1 and not same[0]["legacy"], same[0]["path"] if same else "")
hit, _ = lib.match("bias", want("rasa1"))
case("rasa1 still matches (now to the newest master)", hit is not None and hit["telescope"] == "rasa1")

# 9. raw subs in Bias/<night> are NOT mistaken for masters
sub = lib.sub_folder("rasa1", "bias", night)
sub.mkdir(parents=True, exist_ok=True)
from astrocontrol.imaging import fits  # noqa: E402
fits.write(sub / "cal_BIAS_R_0s_-4C_0001.fits", np.zeros((40, 30), np.uint16),
           {"IMAGETYP": ("Bias", ""), "TELESCOP": ("rasa1", "")})
lib.forget()
case("a raw bias sub in RASA1/Bias/<night> is not read as a master",
     all("cal_BIAS" not in m["id"] for m in lib.masters()))

# 10. capture: exact override does not get the rig subfolder appended (the doubling fix)
cap = object.__new__(capture_mod.Capture) if hasattr(capture_mod, "Capture") else None
cls = next(v for v in vars(capture_mod).values()
           if isinstance(v, type) and "session_dir" in vars(v))
cap = object.__new__(cls)
cap._directory_override = sub
cap._directory_exact = True
cap.subfolder = "rasa1"
case("calibration subs: session_dir is exactly RASA1/Bias/<night> (no trailing rasa1)",
     cap.session_dir == sub, str(cap.session_dir.relative_to(BOX)))
cap._directory_exact = False
case("a hand-set folder on a multi-scope rig still gets its rig subfolder",
     cap.session_dir == sub / "rasa1")
summary = lib.summary()
case("summary reports the new root", summary["root"] == str(lib.root))

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
