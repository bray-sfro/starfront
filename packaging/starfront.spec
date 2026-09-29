# -*- mode: python ; coding: utf-8 -*-
"""How Starfront is built into a folder somebody can download and run.

    pyinstaller packaging/starfront.spec

Two programs from one analysis: `Starfront.exe`, the window, with no console
behind it; and `Starfront Console.exe`, the same program with a console and
the command-line switches (--browser, --server, --port), for a headless rig
or for seeing what a window that will not open is saying. Everything the two
need sits beside them in `_internal`, including the web files and the
WebView2 loader pywebview carries.

Nothing about a person's observatory is in here: settings, targets and the
plan live under their home folder, and are untouched by an upgrade that
replaces this folder.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
ICON = str(ROOT / "packaging" / "build" / "starfront.ico")

datas = [(str(ROOT / "astrocontrol" / "web"), "astrocontrol/web"),
         (str(ROOT / "README.md"), ".")]
binaries = []
hiddenimports = [
    "astrocontrol.main",
    # COM for ASCOM: pywin32's pieces that are found by name at run time.
    "win32com", "win32com.client", "pythoncom", "pywintypes", "win32timezone",
    "win32api", "win32con",
]
hiddenimports += collect_submodules("astrocontrol")

# Libraries that find their own parts at run time and need them all present.
for package in ("webview", "uvicorn", "pydantic", "pydantic_core", "starlette",
                "fastapi", "anyio", "clr_loader", "pythonnet"):
    try:
        d, b, h = collect_all(package)
    except Exception:                              # noqa: BLE001 - optional ones
        continue
    datas += d
    binaries += b
    hiddenimports += h

block_cipher = None

a = Analysis(
    [str(ROOT / "Starfront.pyw")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
              "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

window = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Starfront",
    icon=ICON,
    console=False,
    disable_windowed_traceback=False,
)

console_a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
              "IPython", "pytest"],
    noarchive=False,
)
console_pyz = PYZ(console_a.pure)
console = EXE(
    console_pyz, console_a.scripts, [],
    exclude_binaries=True,
    name="Starfront Console",
    icon=ICON,
    console=True,
)

COLLECT(
    window, a.binaries, a.datas,
    console, console_a.binaries, console_a.datas,
    strip=False, upx=False,
    name="Starfront",
)
