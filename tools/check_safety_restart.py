"""Roof-status glitches and the auto-restart after a weather shutdown (0.2.53a).

Bray, 2026-11-04: "shut down, but occasionally there is a roof status
glitch! and the scope should restart if conditions become good again".

Everything here runs on a simulated clock: the network source is fed by a
fake server (no sockets), the watcher's and the source's clocks are the same
fake, and the auto-restart's host is a recording fake - except the last part,
which drives the real program (main) for the user-action cancel, the status
and the warnings strip, and the real sequencer's recool / centre / unpark.
"""

from __future__ import annotations

import email.utils
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="saferestart-"))
os.environ.setdefault("ASTRO_DATA_DIR", str(TMP / "data"))
os.environ.setdefault("HOME", str(TMP / "home"))
os.environ.setdefault("LOCALAPPDATA", str(TMP / "lad"))

from astrocontrol import autorestart, netsafety, safety       # noqa: E402
from astrocontrol.config import Config                        # noqa: E402
from astrocontrol.devices.base import DeviceError             # noqa: E402

results: list[bool] = []


def case(name: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return bool(ok)


# ---------------------------------------------------------------------------
# A simulated clock, server and watcher.

class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.base = time.time()

    def mono(self) -> float:
        return self.t

    def wall(self) -> float:
        return self.base + self.t


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Server:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.reset()

    def reset(self) -> None:
        self.safe = True
        self.is_open = True
        self.state = None
        self.status = 200
        self.update_age = 10.0      # state_update this old (s)
        self.date_offset = 0.0      # server clock - PC clock
        self.fail_next = 0          # this many fetches answer 500, then normal
        self.requests = 0

    def fetch(self, url: str, timeout: float):
        self.requests += 1
        now = self.clock.wall() + self.date_offset
        headers = {"date": email.utils.formatdate(now, usegmt=True)}
        if self.fail_next > 0:
            self.fail_next -= 1
            return 500, headers, b"Internal Server Error"
        if self.status != 200:
            return self.status, headers, b"Internal Server Error"
        if "/issafe" in url:
            return 200, headers, json.dumps({"Value": self.safe, "ErrorNumber": 0}).encode()
        building = {"device_number": 2, "name": "Building 2", "is_open": self.is_open,
                    "state_update": iso(now - self.update_age)}
        if self.state:
            building["state"] = self.state
        return 200, headers, json.dumps([building]).encode()


class Rigs:
    master = property(lambda self: self)
    manager = property(lambda self: self)

    def get(self, kind):
        return None


def make(**settings):
    clock = Clock()
    server = Server(clock)
    config = Config(TMP / f"settings-{time.monotonic_ns()}.json")
    base = {"enabled": True, "onUnsafe": "shutdown", "graceSeconds": 15.0,
            "resumeAfterSeconds": 600.0, "source": "alpaca", "alpacaScheme": "https",
            "alpacaHost": "roof.test", "alpacaPort": 443, "alpacaDevice": 2}
    base.update(settings)
    config.update("safety", base)
    calls = {"unsafe": [], "safe": []}
    w = safety.SafetyWatcher(Rigs(), config,
                             on_unsafe=lambda d: calls["unsafe"].append((clock.t, d)),
                             on_safe=lambda: calls["safe"].append(clock.t))
    w._mono = clock.mono
    w._net = netsafety.AlpacaSafetySource(w.settings, fetch=server.fetch,
                                          clock=clock.wall, monotonic=clock.mono)
    return w, server, clock, calls


def run(w, clock, seconds, step=5.0, each=None):
    """The watcher loop, on the fake clock: one poll every 5 s."""
    end = clock.t + seconds
    while clock.t < end - 1e-9:
        clock.t += step
        if each is not None:
            each(clock.t)
        w._poll()
        w.tick_restart()


def first_unsafe(calls):
    return calls["unsafe"][0][0] if calls["unsafe"] else None


# ---------------------------------------------------------------------------

def glitch_cases() -> None:
    # A healthy minute first, so every case starts from a settled "safe".
    w, s, clock, calls = make()
    run(w, clock, 60)
    case("settled: safe, polling at the 30 s pace",
         w.read() is True and not calls["unsafe"] and s.requests <= 2 * 3 + 2,
         f"{s.requests} requests in 60 s")

    # 1. one bad read
    s.fail_next = 1
    run(w, clock, 300)
    case("a single glitch (one HTTP 500) never shuts down", not calls["unsafe"],
         str(calls["unsafe"]))
    case("...and it reads safe again afterwards, with no shutdown logged",
         any(e["kind"] == "unsafe" for e in w._events) is False and w.read() is True)

    # 2. bursts shorter than the debounce, repeatedly
    w, s, clock, calls = make()
    run(w, clock, 60)
    for _ in range(4):
        s.status = 500
        run(w, clock, 60)          # 60 s of errors: under the 75 s debounce
        s.status = 200
        run(w, clock, 30)
    case("glitch bursts of 60 s (x4, 30 s apart) never shut down", not calls["unsafe"],
         str(calls["unsafe"]))
    s.reset()
    s.safe, s.is_open = False, True    # issafe=false the roof report does not back up
    run(w, clock, 50)
    s.reset()
    run(w, clock, 60)
    case("an unconfirmed issafe=false for 50 s does not shut down", not calls["unsafe"])

    # 3. sustained: errors, stale, skew - each after the debounce, not before
    for label, setup, words in (
            ("HTTP 500 errors", lambda s: setattr(s, "status", 500), "HTTP 500"),
            ("stale roof report", lambda s: setattr(s, "update_age", 400.0), "old"),
            ("clock skew 200 s", lambda s: setattr(s, "date_offset", 200.0), "clock"),
            ("unconfirmed issafe=false", lambda s: setattr(s, "safe", False), "disagrees")):
        w, s, clock, calls = make()
        run(w, clock, 60)
        setup(s)
        changed = clock.t
        run(w, clock, 200)
        at = first_unsafe(calls)
        began = None
        # when the watcher first read it unsafe (the poll that saw it)
        detail = calls["unsafe"][0][1] if calls["unsafe"] else ""
        if at is not None:
            began = at - changed
        case(f"sustained {label}: shuts down after the debounce (75-110 s after it began)",
             at is not None and 75.0 <= began <= 110.0 and words in (detail or ""),
             f"after {began} s: {detail}")
        case(f"sustained {label}: exactly one shutdown", len(calls["unsafe"]) == 1)

    # 4. explicit close acts fast
    for label, setup in (
            ("issafe=false confirmed by roof/state closed",
             lambda s: (setattr(s, "safe", False), setattr(s, "is_open", False))),
            ("roof/state is_open=false while issafe still true",
             lambda s: setattr(s, "is_open", False)),
            ("roof/state 'closing'", lambda s: setattr(s, "state", "closing"))):
        w, s, clock, calls = make()
        run(w, clock, 60)
        setup(s)
        changed = clock.t
        seen = {}

        def note(t, w=w, seen=seen):
            if w._unsafe_at is not None and "t" not in seen:
                seen["t"] = t
        run(w, clock, 120, each=note)
        at = first_unsafe(calls)
        lag = None if at is None or "t" not in seen else at - seen["t"]
        case(f"explicit close ({label}): acts on the 2nd confirmed read, "
             "<=10 s after it is first seen and within one poll + 10 s",
             at is not None and lag is not None and lag <= 10.0 and at - changed <= 40.0,
             f"first seen +{seen.get('t', 0) - changed:.0f} s, acted +{(at or 0) - changed:.0f} s")

    # 5. the grace and the debounce do not stack
    w, s, clock, calls = make(graceSeconds=60.0)
    run(w, clock, 60)
    s.safe, s.is_open = False, False
    changed = clock.t
    run(w, clock, 120)
    at = first_unsafe(calls)
    case("a 60 s grace does not delay a confirmed close", at is not None and at - changed <= 40.0,
         f"+{(at or 0) - changed:.0f} s")
    w, s, clock, calls = make(graceSeconds=120.0)
    run(w, clock, 60)
    s.status = 500
    changed = clock.t
    run(w, clock, 300)
    at = first_unsafe(calls)
    case("unconfirmed: the hold is max(grace 120, debounce 75), not the sum (195)",
         at is not None and 120.0 <= at - changed <= 150.0, f"+{(at or 0) - changed:.0f} s")
    st = w.status()
    case("status: holdSeconds and confirmed are reported",
         st.get("holdSeconds") == 120.0 and st.get("confirmed") is False, str(st.get("holdSeconds")))

    # 6. no network source: the old grace, untouched
    clock = Clock()
    config = Config(TMP / "plain.json")
    config.update("safety", {"graceSeconds": 15.0})

    class Slot:
        connected = True
        name = "slot"
        is_safe = False

    class SlotRigs(Rigs):
        def get(self, kind):
            return Slot() if kind == "safetymonitor" else None

    hits = []
    w = safety.SafetyWatcher(SlotRigs(), config, on_unsafe=lambda d: hits.append(clock.t))
    w._mono = clock.mono
    run(w, clock, 30)
    case("no network source: an Equipment monitor still acts after the 15 s grace",
         hits and 15.0 <= hits[0] <= 20.0, str(hits))


# ---------------------------------------------------------------------------
# The auto-restart, against a recording host.

class Host:
    def __init__(self, clock) -> None:
        self.clock = clock
        self.calls: list[str] = []
        self.running = False
        self.dawn = None
        self.end = None
        self.sub = 300.0
        self.night = "2026-11-04"
        self.late: list[str] = []
        self.centre_error: Exception | None = None
        self.during = None

    def weather_deadlines(self, snapshot):
        return {"night": self.night, "dawn": self.dawn, "end": self.end, "sub": self.sub}

    def restart_begin(self):
        self.calls.append("begin")

    def restart_abort(self):
        self.calls.append("abort")

    def restart_recool(self, timeout):
        self.calls.append(f"recool({timeout:.0f})")
        if self.during:
            self.during()
        return list(self.late)

    def restart_unpark(self):
        self.calls.append("unpark")

    def restart_centre(self, snapshot):
        self.calls.append("centre")
        if self.centre_error:
            raise self.centre_error
        return "centred"

    def restart_focus(self):
        self.calls.append("refocus")
        return "temperature moved 2.1 C"

    def restart_resume(self, snapshot):
        self.calls.append(f"resume({snapshot.get('entryId')},{snapshot.get('progress')})")
        self.running = True

    def restart_park(self):
        self.calls.append("park")


def make_restart(**settings):
    clock = Clock()
    host = Host(clock)
    cfg = {"enabled": True, "onUnsafe": "shutdown", "resumeAfterSeconds": 600.0}
    cfg.update(settings)
    alerts: list[str] = []
    logs: list[str] = []
    r = autorestart.AutoRestart(lambda: cfg, host,
                                notify=lambda subject, body: alerts.append(subject),
                                log=lambda m, level: logs.append(m),
                                clock=clock.wall, spawn=lambda fn: fn())
    return r, host, clock, alerts, logs, cfg


SNAP = {"night": "2026-11-04", "entryId": "m31", "panelIndex": 0,
        "progress": {"m31:0:rasa:L": 7}, "finished": [], "loop": False}


def safe_for(r, clock, seconds, step=5.0, safe=True):
    end = clock.t + seconds
    while clock.t < end - 1e-9:
        clock.t += step
        r.tick(safe)


def restart_cases() -> None:
    r, host, clock, alerts, logs, cfg = make_restart()
    r.arm(SNAP)
    case("a weather shutdown of a running sequence arms the restart",
         r.state == "armed" and any("armed" in a for a in alerts), r.state)
    safe_for(r, clock, 595)
    case("not before 600 s of continuous safe", r.state == "armed" and not host.calls)
    safe_for(r, clock, 10)
    case("restart order: recool, unpark, centre, refocus, resume (same entry, same progress)",
         host.calls == ["begin", "recool(1200)", "unpark", "centre", "refocus",
                        "resume(m31,{'m31:0:rasa:L': 7})"], str(host.calls))
    case("restarted: state resumed, 1 of 3 tonight, alerts sent",
         r.state == "resumed" and r.restarts() == 1
         and any("restarting" in a for a in alerts) and any("running again" in a for a in alerts),
         f"{r.state} {alerts}")
    case("the refocus reason is logged", any("temperature moved" in m for m in logs))

    # a glitch inside the safe window starts it again
    r, host, clock, alerts, logs, cfg = make_restart()
    r.arm(SNAP)
    safe_for(r, clock, 500)
    r.tick(False)
    safe_for(r, clock, 500)
    case("one unsafe read in the safe window restarts the 600 s count",
         r.state == "armed" and not host.calls, f"{r.status()}")
    safe_for(r, clock, 110)
    case("...and it restarts 600 s after the glitch", r.state == "resumed")

    # recool timeout: reported, carries on
    r, host, clock, alerts, logs, cfg = make_restart(autoRestartCoolTimeoutMinutes=5)
    host.late = ["RASA"]
    r.arm(SNAP)
    safe_for(r, clock, 610)
    case("recool timeout (5 min): alerted, and the restart carries on to resume",
         any("setpoint" in a for a in alerts) and host.calls[1] == "recool(300)"
         and host.calls[-1].startswith("resume") and r.state == "resumed",
         f"{host.calls} {alerts}")

    # solve failure: park, stay parked, alert
    r, host, clock, alerts, logs, cfg = make_restart()
    host.centre_error = DeviceError("plate solving M31 failed: no stars")
    r.arm(SNAP)
    safe_for(r, clock, 610)
    case("solve failure: parks again, never resumes, alerts, state failed",
         host.calls[-1] == "park" and not any(c.startswith("resume") for c in host.calls)
         and r.state == "failed" and any("could not centre" in a for a in alerts),
         f"{host.calls} {r.state} {alerts}")
    safe_for(r, clock, 3000)
    case("...and stays parked (no second try on its own)", host.calls.count("park") == 1
         and host.calls.count("begin") == 1)

    # flapping: backoff 600, 1200, 2400, then the cap
    r, host, clock, alerts, logs, cfg = make_restart()
    waits = []
    for n in range(3):
        host.running = True
        r.arm(SNAP)
        host.running = False
        start = clock.t
        while r.state == "armed" and clock.t - start < 10000:
            clock.t += 5
            r.tick(True)
        waits.append(clock.t - start)
    case("flapping roof: the safe window doubles - 600, 1200, 2400 s",
         [round(x / 5) * 5 for x in waits] == [605, 1205, 2405] and r.restarts() == 3,
         str(waits))
    r.arm(SNAP)
    safe_for(r, clock, 6000)
    case("flapping roof: the 4th shutdown hits the cap (3/night), says so and stays down",
         r.state == "skipped" and "3 of 3" in r.reason and host.calls.count("begin") == 3
         and any("not restarting" in a for a in alerts), r.reason)
    r, host2, clock2, *_ = make_restart()
    r._count = {"2026-11-04": 3}
    host2.night = "2026-11-05"
    r.arm(dict(SNAP, night="2026-11-05"))
    case("the cap is per night: a new night starts again at 0", r.state == "armed"
         and r.restarts() == 0)

    # cutoffs
    for label, setup, words in (
            ("astronomical dawn has passed", lambda h, c: setattr(h, "dawn", c.wall() + 300),
             "dawn has passed"),
            ("the sequence's end time has passed", lambda h, c: setattr(h, "end", c.wall() + 300),
             "end time has passed"),
            ("less than one sub left before dawn",
             lambda h, c: setattr(h, "dawn", c.wall() + 900), "less than one 300 s sub"),
            ("less than one sub left before the sequence end",
             lambda h, c: setattr(h, "end", c.wall() + 850), "less than one 300 s sub"),
            ("the night it was shut down in is over",
             lambda h, c: setattr(h, "night", "2026-11-05"), "night")):
        r, host, clock, alerts, logs, cfg = make_restart()
        r.arm(SNAP)
        setup(host, clock)
        safe_for(r, clock, 610)
        case(f"cutoff: {label} - no restart, logged and alerted",
             r.state == "skipped" and words in r.reason and not host.calls
             and any("not restarting" in m for m in logs) and alerts[-1].startswith(
                 "Auto-restart: not restarting"), r.reason)
    r, host, clock, alerts, logs, cfg = make_restart()
    host.dawn = clock.wall() + 600 + 5 + 301
    r.arm(SNAP)
    safe_for(r, clock, 610)
    case("enough for one sub before dawn: restarts", r.state == "resumed", r.reason)

    # user action cancels
    r, host, clock, alerts, logs, cfg = make_restart()
    r.arm(SNAP)
    safe_for(r, clock, 300)
    r.cancel("user action: POST /api/sequence/stop")
    safe_for(r, clock, 1000)
    case("a manual stop cancels the armed restart (logged, alerted, nothing runs)",
         r.state == "cancelled" and not host.calls and any("cancelled" in a for a in alerts)
         and any("sequence/stop" in m for m in logs), r.state)
    r, host, clock, alerts, logs, cfg = make_restart()
    host.during = lambda: r.cancel("user action: POST /api/mount/park")
    r.arm(SNAP)
    safe_for(r, clock, 610)
    case("a user action during the restart aborts it before the unpark",
         host.calls[:3] == ["begin", "recool(1200)", "abort"] and "unpark" not in host.calls
         and r.state == "cancelled", str(host.calls))

    # the weather turning again mid-restart re-arms with the same snapshot
    r, host, clock, alerts, logs, cfg = make_restart()
    host.during = lambda: r.arm(None)
    r.arm(SNAP)
    safe_for(r, clock, 610)
    case("unsafe again during the restart: no resume, re-armed with the same run, "
         "and the next window is doubled", r.state == "armed" and r.snapshot["entryId"] == "m31"
         and not any(c.startswith("resume") for c in host.calls) and r.window() == 1200.0,
         f"{r.state} {host.calls}")

    # settings
    r, host, clock, alerts, *_ = make_restart(autoRestart=False)
    r.arm(SNAP)
    case("autoRestart off: a shutdown arms nothing", r.state == "idle" and not alerts)
    r, host, clock, alerts, *_ = make_restart(onUnsafe="pause")
    r.arm(SNAP)
    case("onUnsafe pause: nothing to restart (the pause resumes by itself)", r.state == "idle")
    r, host, clock, alerts, *_ = make_restart()
    r.arm(None)
    case("nothing was running (daytime closed roof): nothing armed", r.state == "idle"
         and not alerts)
    r, host, clock, alerts, *_ = make_restart()
    r.arm(SNAP)
    host.running = True
    safe_for(r, clock, 610)
    case("a sequence already running (started by hand): no restart", r.state == "skipped"
         and not host.calls)


# ---------------------------------------------------------------------------
# The real program.

def program_cases() -> None:
    from fastapi.testclient import TestClient
    from astrocontrol import main, warnings

    http = TestClient(main.app)
    ar = main.auto_restart
    case("the program wires the auto-restart to the watcher and the sequencer",
         main.safety_watcher.auto_restart is ar
         and main.sequencer.on_weather_shutdown == ar.arm)

    ar.arm(dict(SNAP, night=main.sequencer._night_now()))
    st = main.safety_watcher.status()
    case("watcher status carries the auto-restart state", (st.get("autoRestart") or {})
         .get("state") == "armed", str(st.get("autoRestart")))

    board = warnings.WarningBoard()
    board.begin()
    warnings.weather(board, main.warning_context)
    board.end()
    items = {w["key"]: w for w in board.payload()} if hasattr(board, "payload") else {
        k: v.payload() for k, v in board._active.items()}
    case("warnings strip shows 'Auto-restart armed'", "safety.restart" in items
         and "armed" in items["safety.restart"]["title"], str(list(items)))

    r = http.get("/api/sequence")
    r = http.post("/api/sequence/stop")
    case("POST /api/sequence/stop cancels it (user action)", ar.state == "cancelled"
         and "sequence/stop" in ar.reason, f"{r.status_code} {ar.state} {ar.reason}")

    ar.arm(dict(SNAP, night=main.sequencer._night_now()))
    http.get("/api/safety/settings")
    case("a GET does not cancel it", ar.state == "armed")
    http.post("/api/settings/safety", json={"debounceSeconds": 75})
    case("saving settings does not cancel it", ar.state == "armed")
    r = http.post("/api/safety/autorestart/cancel")
    case("POST /api/safety/autorestart/cancel cancels it", r.status_code == 200
         and r.json()["cancelled"] and ar.state == "cancelled")
    r = http.post("/api/settings/safety", json={"autoRestart": True, "autoRestartMaxPerNight": 3,
                                                "autoRestartCoolTimeoutMinutes": 20,
                                                "confirmReads": 2, "debounceSeconds": 90})
    case("the new settings save", r.status_code == 200
         and r.json()["safety"]["debounceSeconds"] == 90, r.text[:200])
    r = http.post("/api/settings/safety", json={"debounceSeconds": 5000})
    case("an out-of-range debounce is refused", r.status_code == 422)

    seq = main.sequencer
    case("weather_snapshot is None with nothing running", seq.weather_snapshot() is None)
    seen = []
    seq.on_weather_shutdown = lambda snap: seen.append(snap)
    seq.shut_down = lambda: {"done": [], "failed": {}}          # no gear here
    seq.weather_unsafe("roof closed")
    seq.on_weather_shutdown = ar.arm
    case("weather_unsafe (shutdown) hands the snapshot to the auto-restart first",
         seen == [None])

    # recool, on the real sequencer, with simulated cameras
    class Cam:
        connected = True
        can_cool = True

        def __init__(self, temps):
            self.temps = list(temps)
            self.setpoint = None
            self.cooler = False

        @property
        def temperature(self):
            return self.temps.pop(0) if len(self.temps) > 1 else self.temps[0]

        def set_setpoint(self, v):
            self.setpoint = v

        def set_cooler(self, on):
            self.cooler = on

    class Section(dict):
        def section(self, name):
            return self

        def get(self, *a, **k):
            return dict.get(self, a[-2] if len(a) > 1 else a[0], k.get("default", a[-1] if len(a) > 1 else None))

    class Rig:
        def __init__(self, name, cam):
            self.name, self.id = name, name
            self.cam = cam
            self.config = type("C", (), {"section": lambda s, n: {"setpoint": -10.0,
                                                                  "coolToleranceC": 1.0}})()
            self.manager = type("M", (), {"get": lambda s, k, cam=cam: cam if k == "camera"
                                          else None})()

    real_rigs = seq.rigs

    class FakeRigs:
        def __init__(self, rigs):
            self._r = rigs
            self.master = real_rigs.master
            self.all = rigs

        def imaging(self):
            return self._r

    seq._abort.clear()
    good = Cam([5.0, -2.0, -9.5])
    seq.rigs = FakeRigs([Rig("RASA", good)])
    t0 = time.monotonic()
    late = seq.restart_recool(30.0)
    case("recool: cooler on, setpoint -10, returns when it gets there",
         late == [] and good.cooler and good.setpoint == -10.0 and time.monotonic() - t0 < 10,
         str(late))
    stuck = Cam([5.0])
    seq.rigs = FakeRigs([Rig("RASA", stuck)])
    t0 = time.monotonic()
    late = seq.restart_recool(1.0)
    case("recool timeout: gives up at the timeout and names the camera",
         late == ["RASA"] and 0.9 <= time.monotonic() - t0 < 4.0, str(late))
    seq.rigs = real_rigs

    # centre: a solve that fails raises (so the restart parks), a good one centres
    class Solver:
        def __init__(self, fail):
            self.fail = fail
            self.busy = False

        def executable(self):
            return "astap"

        def start(self, *a):
            pass

        def status(self):
            return {"error": "no stars found"} if self.fail else {}

        def abort(self):
            pass

    entry = {"id": "m31", "name": "M31", "targetId": "t1", "filters": [
        {"name": "L", "exposure": 300, "count": 20}]}
    seq.plan = type("P", (), {"raw": lambda s: {"entries": [entry]}})()
    seq.targets = type("T", (), {"listing": lambda s: [
        {"id": "t1", "ra": 0.71, "dec": 41.27, "type": "deep"}]})()
    seq._mount_ready = lambda: "mount"
    seq._slew = lambda m, ra, dec, label: m
    seq._wait_for_mount = lambda m: None
    for fail in (True, False):
        type(seq).solver = property(lambda s, f=fail: Solver(f))
        try:
            said = seq.restart_centre(SNAP)
            ok = (not fail) and "centred on M31" in said
            detail = said
        except DeviceError as exc:
            ok = fail and "plate solving M31 failed" in str(exc)
            detail = str(exc)
        case(f"centre on the interrupted target: solve {'fails -> raises' if fail else 'works'}",
             ok, detail)
    d = seq.weather_deadlines(SNAP)
    case("deadlines: one sub is the target's own exposure (300 s), dawn worked out",
         d["sub"] == 300.0 and "night" in d, str(d))

    class Dome:
        connected = True
        can_shutter = True
        shutter_state = "closed"
    real_get = seq.manager.get
    seq.manager.get = lambda kind: Dome() if kind == "dome" else real_get(kind)
    try:
        seq.restart_unpark()
        ok, detail = False, "no error"
    except DeviceError as exc:
        ok, detail = "not reopened automatically" in str(exc), str(exc)
    seq.manager.get = real_get
    case("unpark refuses when a roof/shutter this program closed is shut", ok, detail)


def main_() -> int:
    glitch_cases()
    restart_cases()
    program_cases()
    passed = sum(results)
    print(f"{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    code = main_()
    sys.stdout.flush()
    os._exit(code)
