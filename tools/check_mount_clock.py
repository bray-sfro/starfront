"""The mount clock against this PC. Read-only.

    python3 tools/check_mount_clock.py

A connected mount whose UTC or local sidereal time is more than a minute
from this PC raises a warning on the board that status already publishes.
A clock inside that minute does not. No mount, a mount that is
disconnected, or a driver that does not answer is "clock check skipped".
The driver is read once per connection. Nothing is synced or slewed.

No test framework, for the same reason as the other checks here.
"""
import ast
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from astrocontrol import __version__, astro, mountclock, warnings          # noqa: E402
from astrocontrol.config import Config                                      # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


NOW = datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc)
mountclock.clock = lambda: NOW

WRITES = ("sync_to", "slew_to", "set_tracking", "park", "unpark", "abort_slew")


def pc_lst(longitude=0.0):
    return astro.local_sidereal_hours(longitude, NOW)


class Manager:
    def __init__(self, devices):
        self.devices = devices

    def get(self, kind):
        return self.devices.get(kind)


class Rig:
    def __init__(self, devices, name="FSQ106"):
        self.id = name
        self.name = name
        self.manager = Manager(devices)


class Rigs:
    def __init__(self, rig, log):
        self.master = rig
        self.all = [rig]
        self.log = log


class ClockMount:
    """A mount that reports a clock and records anything that would move it."""

    def __init__(self, utc=None, lst=None, boom=False):
        self.connected = True
        self.tracking = True
        self.slewing = False
        self.site = {"latitude": 31.9, "longitude": 0.0, "elevation": 1000.0}
        self.calls = []
        self.utc = utc
        self.lst = lst
        self.boom = boom

    def reported_clock(self):
        self.calls.append("reported_clock")
        if self.boom:
            raise RuntimeError("driver busy")
        return {"utc": self.utc, "lst": self.lst}

    def sync_to(self, *args, **kwargs):
        self.calls.append("sync_to")

    def slew_to(self, *args, **kwargs):
        self.calls.append("slew_to")

    def set_tracking(self, *args, **kwargs):
        self.calls.append("set_tracking")

    def park(self, *args, **kwargs):
        self.calls.append("park")

    def unpark(self, *args, **kwargs):
        self.calls.append("unpark")

    def abort_slew(self, *args, **kwargs):
        self.calls.append("abort_slew")


def build(devices, longitude=0.0):
    mountclock.reset()
    config = Config(Path(tempfile.mkdtemp()) / "settings.json")
    config.update("site", {"latitude": 31.9, "longitude": longitude, "useMount": True})
    # A roomy disk, so the disk check stays quiet on this machine.
    config.update("warnings", {"diskCriticalGb": 0.0, "diskWarnGb": 0.0})
    logs = []
    rig = Rig(devices)
    board = warnings.WarningBoard(log=lambda message, level="info": logs.append((level, message)))
    ctx = warnings.Context(
        config=config, rigs=Rigs(rig, lambda message, level="info": logs.append((level, message))),
        night=lambda: None)
    dog = warnings.Watchdog(board, ctx, interval=999,
                            log=lambda message, level="info": logs.append((level, message)))
    return board, dog, logs


def active(board):
    return {item["key"]: item for item in board.snapshot()["active"]}


def method_body(path, name):
    lines = Path(path).read_text().splitlines()
    start = next(index for index, line in enumerate(lines) if f"def {name}(" in line)
    body = []
    for line in lines[start + 1:]:
        if line.startswith("    def ") or line.startswith("class "):
            break
        body.append(line)
    return "\n".join(body)


# ---------------------------------------------------------------- the wiring
case("version is 0.2.53h", __version__ == "0.2.53h", __version__)
case("the clock check is on the watchdog",
     any(fn.__name__ == "mount_clock" for fn in warnings.CHECKS))
main_text = (ROOT / "astrocontrol/main.py").read_text()
case("status publishes the verdict without asking the mount again",
     'payload["mountClock"] = mountclock.latest()' in main_text)

ascom_body = method_body(ROOT / "astrocontrol/devices/ascom.py", "reported_clock")
case("ASCOM clock read does not write",
     "_set" not in ascom_body and "_invoke" not in ascom_body
     and "setattr" not in ascom_body and "Sync" not in ascom_body
     and "Slew" not in ascom_body and "UTCDate" in ascom_body
     and "SiderealTime" in ascom_body)
alpaca_body = method_body(ROOT / "astrocontrol/devices/alpaca.py", "reported_clock")
case("Alpaca clock read is GET only",
     ".put(" not in alpaca_body and "utcdate" in alpaca_body and "siderealtime" in alpaca_body)
clock_source = (ROOT / "astrocontrol/mountclock.py").read_text()
case("the comparison itself never commands the mount",
     not any(name in clock_source for name in ("sync_to", "slew_to", "set_tracking", ".put(")))


# ---------------------------------------------------------------- a real skew
ahead = ClockMount(utc=NOW + timedelta(hours=2), lst=(pc_lst() + 2.0) % 24.0)
board, dog, logs = build({"mount": ahead})
dog.pass_once()
found = active(board)
warning = found.get("mount.clock")
verdict = mountclock.latest()
case("two hours fast is a warning",
     warning is not None and warning["level"] == "warning"
     and verdict["state"] == "warn"
     and abs(verdict["utcDeltaSeconds"] - 7200) < 1
     and abs(verdict["lstDeltaSeconds"] - 7200) < 1
     and verdict["compared"] == ["utc", "lst"],
     verdict["detail"])
case("...and the warning names the shift and that nothing was changed",
     warning is not None and "2h ahead of this PC" in warning["detail"]
     and "30°" in warning["detail"] and "Nothing was changed." in warning["detail"]
     and "will not sync" in warning["fix"])
case("...and the mount was only read",
     ahead.calls == ["reported_clock"] and not any(name in ahead.calls for name in WRITES))
dog.pass_once()
case("a second pass repeats the warning without reading again",
     "mount.clock" in active(board) and ahead.calls == ["reported_clock"])
case("the warning is on the board status publishes",
     any(item["key"] == "mount.clock" for item in board.snapshot()["active"]))

behind = ClockMount(utc=NOW - timedelta(hours=2))
board, dog, logs = build({"mount": behind})
dog.pass_once()
text = active(board)["mount.clock"]["detail"]
case("two hours slow says behind, and still only reads",
     "2h behind this PC" in text and behind.calls == ["reported_clock"]
     and mountclock.latest()["utcDeltaSeconds"] < 0)


# ---------------------------------------------------------------- no skew
matched = ClockMount(utc=NOW + timedelta(seconds=5), lst=(pc_lst() + 5 / 3600.0) % 24.0)
board, dog, logs = build({"mount": matched})
dog.pass_once()
ok = mountclock.latest()
case("five seconds is not a warning",
     "mount.clock" not in active(board) and ok["state"] == "ok"
     and abs(ok["utcDeltaSeconds"] - 5) < 1 and "matches this PC" in ok["detail"],
     ok["detail"])
case("...and the session log says so once",
     sum(1 for level, message in logs if "matches this PC" in message) == 1)
dog.pass_once()
case("...and a second pass does not read or log it again",
     matched.calls == ["reported_clock"]
     and sum(1 for level, message in logs if "matches this PC" in message) == 1)

on_the_minute = ClockMount(utc=NOW + timedelta(seconds=60))
board, dog, _logs = build({"mount": on_the_minute})
dog.pass_once()
case("exactly one minute is still inside the limit",
     mountclock.latest()["state"] == "ok" and "mount.clock" not in active(board))

over = ClockMount(utc=NOW + timedelta(seconds=61))
board, dog, _logs = build({"mount": over})
dog.pass_once()
case("a second past a minute is a warning",
     "mount.clock" in active(board) and mountclock.latest()["state"] == "warn"
     and "1m 01s ahead of this PC" in active(board)["mount.clock"]["detail"])


# ---------------------------------------------------------------- skipped
board, dog, logs = build({})
dog.pass_once()
skipped = mountclock.latest()
case("no mount skips quietly",
     skipped["state"] == "skipped" and skipped["detail"] == "clock check skipped"
     and "mount.clock" not in active(board))
case("...and says so in the log once",
     sum(1 for _level, message in logs if message == "clock check skipped") == 1)

asleep = ClockMount(utc=NOW + timedelta(hours=2))
asleep.connected = False
board, dog, _logs = build({"mount": asleep})
dog.pass_once()
case("a disconnected mount is a skip and is not asked",
     mountclock.latest()["detail"] == "clock check skipped" and asleep.calls == []
     and "mount.clock" not in active(board))

broken = ClockMount(boom=True)
board, dog, _logs = build({"mount": broken})
dog.pass_once()
silent = mountclock.latest()
case("a driver that throws is a skip, not a crash",
     silent["state"] == "skipped" and "did not report" in silent["detail"]
     and "mount.clock" not in active(board) and broken.calls == ["reported_clock"])
dog.pass_once()
case("...and it is not asked again while it stays connected",
     broken.calls == ["reported_clock"])

mute = ClockMount(utc=None, lst=None)
board, dog, _logs = build({"mount": mute})
dog.pass_once()
case("a connected mount with no time is a skip",
     "did not report" in mountclock.latest()["detail"] and "mount.clock" not in active(board))


# ---------------------------------------------------------------- sidereal time
lst_only = ClockMount(utc=None, lst=(pc_lst() + 2.0) % 24.0)
board, dog, _logs = build({"mount": lst_only})
dog.pass_once()
lst_warn = active(board)["mount.clock"]
case("sidereal time two hours off warns when UTC is absent",
     mountclock.latest()["compared"] == ["lst"]
     and abs(mountclock.latest()["lstDeltaSeconds"] - 7200) < 1
     and "local sidereal time" in lst_warn["detail"]
     and "did not report UTC" in lst_warn["detail"]
     and lst_only.calls == ["reported_clock"])

lst_ok = ClockMount(utc=None, lst=pc_lst())
board, dog, _logs = build({"mount": lst_ok})
dog.pass_once()
case("sidereal time that matches is not a warning",
     mountclock.latest()["state"] == "ok" and "mount.clock" not in active(board))

long_way = ClockMount(utc=None, lst=(pc_lst() + 23.0) % 24.0)
board, dog, _logs = build({"mount": long_way})
dog.pass_once()
wrapped = mountclock.latest()
case("twenty-three hours ahead is one hour behind, the short way",
     wrapped["state"] == "warn" and wrapped["lstDeltaSeconds"] < 0
     and abs(wrapped["lstDeltaSeconds"] + 3600) < 2
     and "behind this PC" in active(board)["mount.clock"]["detail"],
     str(wrapped["lstDeltaSeconds"]))

utc_ok_lst_bad = ClockMount(utc=NOW + timedelta(seconds=3), lst=(pc_lst() + 2.0) % 24.0)
board, dog, _logs = build({"mount": utc_ok_lst_bad})
dog.pass_once()
mixed = active(board)["mount.clock"]["detail"]
case("a good UTC with a bad sidereal time still warns",
     "local sidereal time" in mixed and "UTC agrees" in mixed
     and mountclock.latest()["compared"] == ["utc", "lst"])

no_longitude = ClockMount(utc=None, lst=(pc_lst() + 5.0) % 24.0)
board, dog, _logs = build({"mount": no_longitude})
# Site longitude removed: LST alone cannot be compared.
dog.context.config.update("site", {"latitude": None, "longitude": None})
# The mount's own site would still supply one. Clear it.
no_longitude.site = None
dog.pass_once()
case("sidereal time with no longitude to compare is a skip",
     mountclock.latest()["state"] == "skipped" and "mount.clock" not in active(board))


# ---------------------------------------------------------------- what a driver might hand back
iso = mountclock.assess({"utc": "2026-11-06T08:00:00Z", "lst": None},
                        now=NOW, longitude=0.0)
case("an ISO UTC two hours fast is the same warning",
     iso["state"] == "warn" and abs(iso["utcDeltaSeconds"] - 7200) < 1)

naive = mountclock.assess({"utc": datetime(2026, 11, 6, 8, 0), "lst": None},
                          now=NOW, longitude=0.0)
case("a naive datetime is taken as UTC",
     naive["state"] == "warn" and abs(naive["utcDeltaSeconds"] - 7200) < 1)

epoch = datetime(1899, 12, 30, tzinfo=timezone.utc)
ole = (datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc) - epoch).total_seconds() / 86400.0
from_ole = mountclock.assess({"utc": ole, "lst": None}, now=NOW, longitude=0.0)
case("an OLE automation date is accepted",
     from_ole["state"] == "warn" and abs(from_ole["utcDeltaSeconds"] - 7200) < 1)

hours_as_date = mountclock.assess({"utc": 12.5, "lst": None}, now=NOW, longitude=None)
case("a sidereal-sized number is not read as a calendar date",
     hours_as_date["state"] == "skipped")


# ---------------------------------------------------------------- reconnect
fixed = ClockMount(utc=NOW + timedelta(hours=2))
board, dog, _logs = build({"mount": fixed})
dog.pass_once()
case("the skew is on the board before anyone corrects it", "mount.clock" in active(board))
fixed.connected = False
dog.pass_once()
case("disconnecting clears the warning and skips",
     "mount.clock" not in active(board)
     and mountclock.latest()["detail"] == "clock check skipped")
fixed.connected = True
fixed.utc = NOW + timedelta(seconds=1)
dog.pass_once()
case("reconnecting reads again, and a corrected clock is quiet",
     fixed.calls.count("reported_clock") == 2
     and "mount.clock" not in active(board)
     and mountclock.latest()["state"] == "ok"
     and not any(name in fixed.calls for name in WRITES))


# The module parses. A syntax error in the drivers would still import, because
# those bodies are only compiled with the file; this catches a broken tree.
for relative in ("astrocontrol/mountclock.py", "astrocontrol/warnings.py",
                 "astrocontrol/devices/base.py", "astrocontrol/devices/ascom.py",
                 "astrocontrol/devices/alpaca.py", "astrocontrol/main.py"):
    ast.parse((ROOT / relative).read_text())
case("the touched files parse", True)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
