"""Recording what happened, so a crash is not a mystery in the morning.

The application normally runs as a window with no console behind it.  That means
a Python traceback, a uvicorn error and a hard crash in a driver all go to the
same place: nowhere.  A rig that dies at 2am and leaves nothing behind cannot be
fixed, so everything is teed to a file under the data directory:

  * **Uncaught exceptions**, on the main thread and on every background thread —
    a capture thread dying takes the night with it and Python's default is to
    print to a stderr nobody is reading.
  * **Hard crashes** via `faulthandler` — but only when asked for, and the
    reason is worth writing down.  On Windows faulthandler installs a vectored
    exception handler that fires on *every* structured exception carrying the
    error severity bit, not only on fatal ones.  That includes `0xE0434F4D`, the
    code the CLR raises for an ordinary .NET exception — which is what a
    .NET-based ASCOM driver throws for a perfectly routine error, many times a
    second while its properties are being polled.  `win32com` catches each one
    and turns it into a normal Python exception, but faulthandler has already
    dumped every thread's stack to disk.  Left on, it wrote a 284 MB file and
    stopped the application responding.  So it is off unless `ASTRO_FAULTHANDLER`
    is set, and on by default only where that trap does not exist.
  * **The last thing the app was doing**, because a crash log is far more useful
    with the preceding minute of the session log next to it.

Logs rotate by size and a handful are kept: this runs on an observatory PC that
may go months between visits.
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

# Re-exported so a launcher can log a startup failure without a second import.
__all__ = ["setup", "note", "tail", "crash_report", "log_dir", "logging"]

from .config import data_root

_started = False
_crash_file = None          # kept open for the lifetime of the process


def log_dir() -> Path:
    return data_root() / "logs"


def _want_faulthandler() -> bool:
    """Whether to install the C-level crash dumper.

    Off on Windows unless asked for: there it fires on every handled .NET
    exception an ASCOM driver raises, which is not a crash, happens constantly,
    and buries the machine in stack dumps.  See the module docstring.
    """
    setting = os.environ.get("ASTRO_FAULTHANDLER", "").strip().lower()
    if setting in ("1", "true", "yes", "on"):
        return True
    if setting in ("0", "false", "no", "off"):
        return False
    return os.name != "nt"


def setup(level: int = logging.INFO) -> Path:
    """Start logging to file. Safe to call more than once."""
    global _started, _crash_file
    folder = log_dir()
    path = folder / "astrocontrol.log"
    if _started:
        return path
    _started = True

    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return path                             # a read-only home is not fatal

    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(threadName)-14s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    # Keep the console output when there is a console to keep it on.
    if sys.stderr is not None and sys.stderr.isatty():
        root.addHandler(logging.StreamHandler(sys.stderr))

    _install_hooks()

    # A crash report can only have grown large by catching things that were
    # never crashes, so clear a runaway one however it got there — including a
    # file left behind by a version that had faulthandler on.
    crash = folder / "crash.log"
    try:
        if crash.exists() and crash.stat().st_size > 20_000_000:
            size = crash.stat().st_size
            crash.unlink()
            logging.getLogger("astrocontrol").warning(
                "removed a runaway crash.log (%.0f MB)", size / 1e6)
    except OSError:
        pass                                    # still held open by something

    _start_watchdog(folder)

    if _want_faulthandler():
        # faulthandler needs a real file descriptor that outlives the crash, so
        # this one is deliberately never closed.
        try:
            _crash_file = open(crash, "a", buffering=1, encoding="utf-8")
            faulthandler.enable(file=_crash_file, all_threads=True)
        except OSError:
            pass

    logging.getLogger("astrocontrol").info(
        "Starfront starting (pid %s, python %s)", os.getpid(),
        sys.version.split()[0])
    return path


_watchdog_file = None


def _start_watchdog(folder: Path) -> None:
    """Dump every thread's stack periodically, when asked to.

    For diagnosing a *hang* rather than a crash — the window stops repainting
    and there is no exception to catch, so the only useful question is "what is
    each thread sitting in?".

    `dump_traceback_later` is safe where `faulthandler.enable` is not: it arms a
    timer rather than installing the vectored exception handler that fires on
    every handled .NET exception an ASCOM driver raises.

    Set `ASTRO_STACKDUMP` to a number of seconds to turn it on.
    """
    global _watchdog_file
    raw = os.environ.get("ASTRO_STACKDUMP", "").strip()
    if not raw:
        return
    try:
        interval = float(raw)
    except ValueError:
        return
    if interval <= 0:
        return
    try:
        _watchdog_file = open(folder / "stacks.log", "a", buffering=1,
                              encoding="utf-8")
        _watchdog_file.write(
            f"\n===== watchdog armed, every {interval:g}s, pid {os.getpid()} =====\n")
        faulthandler.dump_traceback_later(interval, repeat=True,
                                          file=_watchdog_file, exit=False)
        logging.getLogger("astrocontrol").warning(
            "stack watchdog on: dumping every thread every %gs to stacks.log",
            interval)
    except (OSError, ValueError):
        pass


def _install_hooks() -> None:
    log = logging.getLogger("astrocontrol.crash")

    previous = sys.excepthook

    def on_exception(kind, value, traceback) -> None:
        # KeyboardInterrupt is a person asking it to stop, not a fault.
        if not issubclass(kind, KeyboardInterrupt):
            log.critical("unhandled exception", exc_info=(kind, value, traceback))
        previous(kind, value, traceback)

    sys.excepthook = on_exception

    def on_thread_exception(args) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        log.critical("unhandled exception in thread %s",
                     getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = on_thread_exception


def note(message: str, *args) -> None:
    logging.getLogger("astrocontrol").info(message, *args)


def tail(lines: int = 200) -> list[str]:
    """The end of the log, for showing in the interface."""
    path = log_dir() / "astrocontrol.log"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()[-lines:]
    except OSError:
        return []


def crash_report() -> str:
    """Anything faulthandler wrote, which only exists after a hard crash."""
    try:
        return (log_dir() / "crash.log").read_text("utf-8", errors="replace")
    except OSError:
        return ""
