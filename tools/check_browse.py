"""Browse… in a browser: walking the capture PC's folders.

    python tools/check_browse.py

Against a folder tree made here, so the listing is checked without depending
on what happens to be on the machine running the check.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from astrocontrol import browse                                     # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


top = Path(tempfile.mkdtemp())
for folder in ("captures", "Calibration", "darks", ".git"):
    (top / folder).mkdir()
for name in ("M31_L_001.fits", "m31_r_001.FIT", "notes.txt", "phd2.exe"):
    (top / name).write_text("x")

folders = browse.listing(str(top))
case("a folder lists its subfolders, in order whatever the case",
     folders["dirs"] == ["Calibration", "captures", "darks"], str(folders["dirs"]))
case("...hiding the dot-folders", ".git" not in folders["dirs"])
case("...and no files when choosing a folder", folders["files"] == [])
case("...and says where it is and what is above it",
     folders["path"] == str(top) and folders["parent"] == str(top.parent))

fits = browse.listing(str(top), [".fits", ".fit"])
case("files are offered by extension, whatever the case",
     fits["files"] == ["M31_L_001.fits", "m31_r_001.FIT"], str(fits["files"]))
case("'*' offers every file", len(browse.listing(str(top), ["*"])["files"]) == 4)

case("a file opens at its folder",
     browse.listing(str(top / "notes.txt"))["path"] == str(top))
case("a folder not made yet opens at the nearest one that is",
     browse.listing(str(top / "captures" / "2026-10-03" / "M31"))["path"] == str(top / "captures"))
case("nothing typed opens at home", browse.listing("")["path"] == str(Path.home()))
case("a relative path opens at home rather than wherever the server started",
     browse.listing("captures")["path"] == str(Path.home()))

drives = browse.listing(":roots")
case("the drive list is a list of drives, with nowhere above it",
     drives["path"] is None and drives["parent"] is None and drives["dirs"] == browse.roots()
     and drives["dirs"], str(drives["dirs"]))
anchor = Path(top.anchor)
case("the top of a drive goes up to the drive list",
     browse.listing(str(anchor))["parent"] == (":roots" if os.name == "nt" else None))

many = Path(tempfile.mkdtemp())
for i in range(browse.LIMIT + 5):
    (many / f"d{i:05}").mkdir()
big = browse.listing(str(many))
case("a huge folder is cut short, and says so",
     len(big["dirs"]) == browse.LIMIT and big["truncated"])

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
