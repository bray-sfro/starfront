"""Why Starfront will not start on this machine.

The application runs as a window with no console behind it, so a missing
dependency looks exactly like a missing Python looks exactly like a driver
that will not load: nothing happens at all.  This script is the opposite of
that.  It uses nothing but the standard library, so it still runs on a machine
where none of the requirements are installed, and it says in plain words what
is wrong and what to type to fix it.

Run it with any Python:

    python tools\\doctor.py

or double-click `Check Starfront.cmd` in the folder above, which finds a
Python for you and keeps the window open afterwards.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent

#: Python version the code is written against.  Lower than this and the
#: application does not fail at the point of use, it fails on import with a
#: syntax error, which is far more confusing than being told.
MINIMUM_PYTHON = (3, 10)

problems: list[str] = []
warnings: list[str] = []


def say(line: str = "") -> None:
    print(line)


def good(label: str, detail: str = "") -> None:
    say(f"  OK    {label}" + (f"   {detail}" if detail else ""))


def bad(label: str, detail: str, fix: str) -> None:
    say(f"  WRONG {label}   {detail}")
    say(f"        fix: {fix}")
    problems.append(f"{label}: {detail}  ->  {fix}")


def warn(label: str, detail: str) -> None:
    say(f"  note  {label}   {detail}")
    warnings.append(f"{label}: {detail}")


def installer() -> str:
    """The exact pip line to type, using the Python that is running this."""
    return f'"{sys.executable}" -m pip install -r "{PROJECT / "requirements.txt"}"'


# ---------------------------------------------------------------- the checks

def check_python() -> None:
    say("Python")
    version = sys.version_info[:3]
    where = sys.executable or "?"
    if version < MINIMUM_PYTHON:
        bad("version", f"{'.'.join(map(str, version))} at {where}",
            f"install Python {'.'.join(map(str, MINIMUM_PYTHON))} or newer from "
            "python.org and run this again with it")
    else:
        good("version", f"{'.'.join(map(str, version))}  ({where})")
    good("machine", f"{platform.system()} {platform.release()} "
                    f"({platform.machine()})")


def check_project() -> None:
    say("\nThe program itself")
    required = [
        PROJECT / "astrocontrol" / "main.py",
        PROJECT / "astrocontrol" / "web" / "index.html",
        PROJECT / "astrocontrol" / "web" / "app.js",
        PROJECT / "requirements.txt",
    ]
    missing = [path for path in required if not path.exists()]
    if missing:
        bad("files", f"{len(missing)} missing, e.g. "
                     f"{missing[0].relative_to(PROJECT)}",
            "copy the whole project folder again — this one is incomplete")
    else:
        good("files", str(PROJECT))

    # A folder synced while the application was running, or copied halfway,
    # shows up here rather than as a blank window twenty minutes later.
    catalog = PROJECT / "astrocontrol" / "data"
    if catalog.exists() and not any(catalog.iterdir()):
        warn("star catalog", f"{catalog} is empty — the planetarium will be bare")


def check_packages() -> list[str]:
    say("\nWhat it needs installed")
    wanted = [
        ("fastapi", "fastapi", "the web application underneath the window"),
        ("uvicorn", "uvicorn", "the server that runs it"),
        ("numpy", "numpy", "all the image arithmetic"),
        ("webview", "pywebview", "the application window itself"),
    ]
    if os.name == "nt":
        wanted.append(("win32com", "pywin32", "ASCOM drivers"))

    absent = []
    for module, package, why in wanted:
        try:
            found = importlib.util.find_spec(module)
        except (ImportError, ValueError):
            found = None
        if found is None:
            absent.append(package)
            say(f"  WRONG {package}   not installed — {why}")
        else:
            good(package, why)
    # One problem and one command, however many packages are behind it: five
    # copies of the same pip line is a worse answer than one.
    if absent:
        say(f"\n        fix: {installer()}")
        problems.append(f"{len(absent)} package(s) missing "
                        f"({', '.join(absent)})  ->  {installer()}")
    return absent


def check_import(absent: list[str]) -> None:
    """The real test: does the application import at all?"""
    say("\nStarting it up")
    if absent:
        say("  ----  skipped: install the packages above first, then run this "
            "again")
        return
    sys.path.insert(0, str(PROJECT))
    try:
        from astrocontrol import main            # noqa: F401 - importing is the test
    except BaseException as exc:                 # noqa: BLE001 - reporting it
        bad("import", f"{type(exc).__name__}: {exc}",
            "the lines below say where; if it names a package, install it")
        say("")
        for line in traceback.format_exc().splitlines()[-12:]:
            say(f"        {line}")
        return
    good("import", "the application loads")

    try:
        from astrocontrol import desktop         # noqa: F401
        good("window", "pywebview and the window code load")
    except BaseException as exc:                 # noqa: BLE001 - reporting it
        bad("window", f"{type(exc).__name__}: {exc}",
            'run "python run.py --browser" instead, which needs no window')


def check_data_dir() -> None:
    say("\nWhere it keeps your settings")
    root = Path(os.environ.get("ASTRO_DATA_DIR") or (Path.home() / "Starfront"))
    try:
        (root / "logs").mkdir(parents=True, exist_ok=True)
        probe = root / "logs" / ".doctor"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        good("data folder", str(root))
    except OSError as exc:
        bad("data folder", f"{root} cannot be written to ({exc})",
            "set ASTRO_DATA_DIR to a folder you can write to")
        return

    log = root / "logs" / "astrocontrol.log"
    if log.exists() and log.stat().st_size:
        say(f"\n  The last few lines it wrote ({log}):")
        try:
            lines = log.read_text("utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines[-12:]:
            say(f"        {line}")
        if not lines:
            say("        (empty)")
    else:
        warn("log", "nothing has been written yet — it has not got far enough "
                    "to log, which usually means Python or a package is missing")


def check_webview2() -> None:
    """The window is Edge WebView2. Windows 11 has it; some Windows 10 does not."""
    if os.name != "nt":
        return
    say("\nThe window's browser engine")
    try:
        import winreg
    except ImportError:
        return
    key = (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
           r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}")
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, key) as handle:
                version, _ = winreg.QueryValueEx(handle, "pv")
                good("WebView2 runtime", str(version))
                return
        except OSError:
            continue
    warn("WebView2 runtime",
         "not found — the window may never appear. Install the Evergreen "
         "WebView2 Runtime from Microsoft, or use "
         '"python run.py --browser" instead')


def check_ascom() -> None:
    if os.name != "nt":
        return
    say("\nASCOM")
    if importlib.util.find_spec("win32com") is None:
        say("  ----  skipped: pywin32 is not installed yet")
        return
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        profile = win32com.client.Dispatch("ASCOM.Utilities.Profile")
        profile.DeviceType = "Camera"
        cameras = [str(entry.Key) for entry in profile.RegisteredDevices("Camera")]
        good("Platform", f"{len(cameras)} camera driver(s) registered")
        for prog_id in cameras[:8]:
            say(f"        {prog_id}")
    except BaseException as exc:                 # noqa: BLE001 - reporting it
        warn("Platform", f"not usable here ({type(exc).__name__}: {exc}) — "
                         "install the ASCOM Platform if this machine drives the gear")


def check_zwo() -> None:
    """ZWO's own SDK, which reaches devices ASCOM cannot."""
    if os.name != "nt":
        return
    say("\nZWO direct")
    sys.path.insert(0, str(PROJECT))
    try:
        from astrocontrol.devices import zwo
    except BaseException as exc:              # noqa: BLE001 - reporting it
        warn("SDK", f"could not be loaded ({type(exc).__name__}: {exc})")
        return
    if not zwo.available():
        warn("SDK", "not installed — install ZWO's ASCOM drivers (the SDK "
                    "ships with them) to reach cameras and focusers directly")
        return
    good("SDK", ", ".join(f"{kind}: {os.path.basename(path)}"
                          for kind, path in zwo.sdk_paths().items() if path))
    for kind in ("camera", "focuser"):
        try:
            found = zwo.list_devices(kind)
        except BaseException as exc:          # noqa: BLE001
            warn(kind, f"could not be listed ({exc})")
            continue
        good(f"{kind}s", f"{len(found)} connected")
        for entry in found:
            say(f"        {entry['name']}")


def main() -> int:
    say("Starfront — what is stopping it starting")
    say("=" * 60)
    check_python()
    check_project()
    absent = check_packages()
    check_import(absent)
    check_data_dir()
    check_webview2()
    check_ascom()
    check_zwo()

    say("\n" + "=" * 60)
    if problems:
        say(f"{len(problems)} thing(s) to fix, most important first:\n")
        for index, line in enumerate(problems, 1):
            say(f"  {index}. {line}")
        say("")
        return 1
    if warnings:
        say("Nothing is broken. Worth knowing:\n")
        for line in warnings:
            say(f"  - {line}")
        say("")
        return 0
    say("Everything checks out. If the window still does not appear, run")
    say(f'  "{sys.executable}" "{PROJECT / "run.py"}" --browser')
    say("which opens the same interface in a normal browser and prints any")
    say("error to this window.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
