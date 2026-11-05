"""The hub supervisor runs a program it does not own, and survives it.

    python tools/check_hub.py

The real `collab-hub` is built in another repository, so this runs against a
fake that speaks the same four commands (`check`, `run`, `token --admin`, and
the HTTP surface `GET /api/hub/v1/status` and `POST /api/hub/v1/enrol-codes`).

  * The TOML written for the hub is the settings, and no more than the settings.
  * `check` runs first and its report is kept; a failing check stops the start.
  * `run` starts, its JSON log reaches the ring buffer (capped), and its status
    route is polled.
  * A crash restarts the hub with a doubling wait, capped at 60 s by default.
  * A stop terminates the child; with a pretend Mac, `caffeinate -w <pid>` is
    run and ended with it. Nothing is left running.
  * Registering sends the right body to the Starfront server and writes the hub
    token and grant secret to a 0600 file - and not to settings.json.
  * An enrol code is asked for with the admin bearer token.
  * The routes exist, never block, and mask the admin token.

Nothing leaves this machine: the hub and the Starfront server are both fakes
on loopback.
"""
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WORK = Path(tempfile.mkdtemp(prefix="check_hub_"))
os.environ["ASTRO_DATA_DIR"] = str(WORK / "data")

from astrocontrol import hub                                     # noqa: E402
from astrocontrol.collabadmin import CollabError                 # noqa: E402
from astrocontrol.config import Config                           # noqa: E402


class _WindowsFakes:
    """Windows cannot execute a script with a shebang (WinError 193), so there the
    fakes are handed to the interpreter. `hub` sees this in place of `subprocess`;
    everything but the fakes' own argv goes through untouched."""

    def __init__(self):
        self._real = subprocess

    def __getattr__(self, name):
        return getattr(self._real, name)

    @staticmethod
    def _argv(args):
        if os.name == "nt" and isinstance(args, (list, tuple)) and args \
                and Path(str(args[0])).name in {"collab-hub", "caffeinate"}:
            return [sys.executable, *map(str, args)]
        return args

    def run(self, args, *a, **kw):
        return self._real.run(self._argv(args), *a, **kw)

    def Popen(self, args, *a, **kw):
        return self._real.Popen(self._argv(args), *a, **kw)


hub.subprocess = _WindowsFakes()

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


def until(predicate, timeout=15.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------- the fakes
FAKE_DIR = WORK / "fake"
FAKE_DIR.mkdir()

FAKE_HUB = textwrap.dedent('''\
    #!%(python)s
    """A stand-in for collab-hub. Control files sit beside this script."""
    import json, sys, time, tomllib, threading, os
    from pathlib import Path
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    here = Path(__file__).resolve().parent
    argv = sys.argv[1:]
    with open(here / "calls.log", "a") as log:
        log.write(json.dumps(argv) + "\\n")

    def config():
        return tomllib.loads(Path(argv[argv.index("--config") + 1]).read_text())

    if argv[:1] == ["check"]:
        if (here / "check_fail").exists():
            print("problem: xpsd_dir does not exist"); sys.exit(1)
        print("bind: " + config()["bind"] + "\\nxpsd_dir: ok"); sys.exit(0)

    if argv[:2] == ["token", "--admin"]:
        print("admin token (shown once):\\nrscu_fakeadmin123"); sys.exit(0)

    if argv[:1] != ["run"]:
        sys.exit(2)

    if (here / "crash").exists():
        print(json.dumps({"level": "ERROR", "message": "fake crash on start"}), flush=True)
        sys.exit(3)

    host, _, port = config()["bind"].rpartition(":")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, body, code=200):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            if self.path == "/api/hub/v1/status":
                with open(here / "polls.log", "a") as f: f.write("x\\n")
                self._send({"ok": True, "version": "0.0.0-fake",
                            "workers": {"online": 2, "total": 3}})
            elif self.path == "/__crash":
                self._send({}); threading.Timer(0.05, lambda: os._exit(7)).start()
            else:
                self._send({}, 404)
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path == "/api/hub/v1/enrol-codes":
                with open(here / "enrol.log", "a") as f:
                    f.write(json.dumps({"auth": self.headers.get("Authorization"),
                                        "body": json.loads(body)}) + "\\n")
                self._send({"code": "ABCD-EFGH", "expires_at": "2099-01-01T00:00:00Z"})
            else:
                self._send({}, 404)

    print(json.dumps({"level": "INFO", "message": "fake hub listening on " + port}), flush=True)
    for i in range(3):
        print("not json: plain line %%d" %% i, flush=True)
    ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler).serve_forever()
''') % {"python": sys.executable}

FAKE_CAFFEINATE = textwrap.dedent('''\
    #!%(python)s
    import sys, time
    from pathlib import Path
    Path(__file__).resolve().parent.joinpath("caffeinate.log").write_text(" ".join(sys.argv[1:]))
    time.sleep(120)
''') % {"python": sys.executable}


def script(name, text):
    path = FAKE_DIR / name
    path.write_text(text)
    path.chmod(0o755)
    return path


fake_hub = script("collab-hub", FAKE_HUB)
fake_caffeinate = script("caffeinate", FAKE_CAFFEINATE)


def calls():
    path = FAKE_DIR / "calls.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def runs():
    return [c for c in calls() if c[:1] == ["run"]]


class FakeStarfront(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        FakeStarfront.received.append((self.path, self.headers.get("Authorization"), body))
        data = json.dumps({"hubId": "hub_FAKE1", "hubToken": "hubtok_SECRET",
                           "grantSecret": "grant_SECRET"}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


starfront = ThreadingHTTPServer(("127.0.0.1", 0), FakeStarfront)
threading.Thread(target=starfront.serve_forever, daemon=True).start()
STARFRONT = f"http://127.0.0.1:{starfront.server_address[1]}"

# --------------------------------------------------------------- the settings
settings_path = WORK / "settings.json"
config = Config(settings_path)
port = free_port()
config_dir = WORK / "hubcfg"
hub_data = WORK / "hubdata"
catalogue = WORK / "catalogue"
catalogue.mkdir()
config.update("hub", {
    "enabled": True, "binary": str(fake_hub), "configDir": str(config_dir),
    "dataRoot": str(hub_data), "catalogueDir": str(catalogue),
    "bind": f"127.0.0.1:{port}", "publicUrl": f"http://hub.example:{port}",
    "adminToken": "", "autoStart": False})
config.update("collab", {"serverUrl": STARFRONT, "userToken": "usr_PERSON"})

sup = hub.HubSupervisor(config, poll_seconds=0.3, backoff_base=0.2, backoff_max=0.8,
                        stable_after=3600, terminate_wait=5)

# ---------------------------------------------------------- the configuration
print("-- the configuration")
written = sup.write_config()
doc = tomllib.loads(written.read_text())
case("the TOML carries bind, public_url, data_root and xpsd_dir from the settings",
     doc.get("bind") == f"127.0.0.1:{port}"
     and doc.get("public_url") == f"http://hub.example:{port}"
     and doc.get("data_root") == str(hub_data)
     and doc.get("xpsd_dir") == str(catalogue), json.dumps(doc))
here_os = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
case("[xpsd_paths] names this machine's OS", doc.get("xpsd_paths") == {here_os: str(catalogue)})
# A mixed network: workers on the other OSes reach the folder through a share,
# and enrolment hands each the path for its OS (RSC hub routes/admin.rs).
shares = {"macos": "/Volumes/XPSD", "windows": "\\\\nas\\XPSD", "linux": "/mnt/xpsd"}
config.update("hub", {"catalogueMacos": shares["macos"], "catalogueWindows": shares["windows"],
                      "catalogueLinux": shares["linux"]})
mixed = tomllib.loads(sup.render_toml()).get("xpsd_paths")
case("[xpsd_paths] carries a path for every OS that has one", mixed == shares, json.dumps(mixed))
config.update("hub", {"catalogueMacos": "", "catalogueWindows": "", "catalogueLinux": ""})
config.update("hub", {"catalogueWindows": shares["windows"]})
partial = tomllib.loads(sup.render_toml()).get("xpsd_paths")
case("this machine's OS falls back to the hub's own folder when its share path is blank",
     partial == {here_os: str(catalogue), "windows": shares["windows"]} if here_os != "windows"
     else partial == {"windows": shares["windows"]}, json.dumps(partial))
config.update("hub", {"catalogueWindows": ""})
case("an unregistered hub has no [starfront] table and TLS is not configured",
     "starfront" not in doc and "tls" not in doc)
case("the hub's data folder was created", hub_data.is_dir())

config.update("hub", {"bind": "127.0.0.1:" + str(port), "catalogueDir": ""})
bare = tomllib.loads(sup.render_toml())
case("an empty catalogue folder writes no xpsd keys", "xpsd_dir" not in bare and "xpsd_paths" not in bare)
config.update("hub", {"catalogueDir": str(catalogue)})

case("back-off is 1, 2, 4 ... 60 by default",
     [hub.HubSupervisor(config).delay_for(n) for n in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60])

# ------------------------------------------------------------------ the check
print("-- check")
(FAKE_DIR / "check_fail").write_text("")
bad = sup.run_check()
case("a failing check reports its output and exit code",
     bad["ok"] is False and bad["exit"] == 1 and "xpsd_dir" in bad["report"], str(bad))
sup.start()
case("a failing check stops the start: state failed, hub never run",
     until(lambda: sup.summary()["state"] == "failed", 5) and not runs()
     and "xpsd_dir" in sup.summary()["lastError"], str(sup.summary()))
(FAKE_DIR / "check_fail").unlink()

ok = sup.run_check()
case("a passing check keeps its report", ok["ok"] and "xpsd_dir: ok" in sup.status()["check"]["report"])
config.update("hub", {"binary": str(WORK / "no-such-hub")})
missing = sup.run_check()
case("a missing binary is reported, not raised", not missing["ok"] and "not found" in missing["report"])
config.update("hub", {"binary": str(fake_hub)})

# ------------------------------------------------- start, poll, crash, restart
print("-- run")
sup._caffeinate = None
real_platform, real_caff = hub._platform, hub.CAFFEINATE
hub._platform = lambda: "darwin"
hub.CAFFEINATE = str(fake_caffeinate)

sup.start()
case("the hub starts and its status route is polled until it reports running",
     until(lambda: sup.summary()["state"] == "running"), str(sup.summary()))
polls = lambda: len((FAKE_DIR / "polls.log").read_text().splitlines()) if (FAKE_DIR / "polls.log").exists() else 0
n = polls()
case("polling goes on", until(lambda: polls() >= n + 2, 5))
case("the worker count comes from the status", sup.summary()["workers"] == 2, str(sup.summary()))
launch = runs()[-1]
case("it was run with --log-format json and the TOML written from settings",
     launch[:3] == ["run", "--log-format", "json"] and launch[-1] == str(written), str(launch))
case("the check ran before the run",
     [c[0] for c in calls() if c[0] in ("check", "run")][-2:] == ["check", "run"])
lines = sup.log_lines()
case("JSON log lines are structured, plain lines are kept as they come",
     any(l.get("level") == "info" and "listening" in l["message"] for l in lines)
     and any("plain line 2" in l["message"] for l in lines))
case("on a (pretend) Mac, caffeinate -w <pid> is running",
     until(lambda: (FAKE_DIR / "caffeinate.log").exists())
     and (FAKE_DIR / "caffeinate.log").read_text() == f"-w {sup.status()['pid']}"
     and sup.status()["keepingAwake"])
caff = sup._caffeinate

for i in range(hub.LOG_LINES + 100):
    sup._ingest(json.dumps({"level": "INFO", "message": f"line {i}"}))
case("the log ring buffer keeps the last 500 lines",
     len(sup.log_lines()) == hub.LOG_LINES and sup.log_lines()[-1]["message"] == f"line {hub.LOG_LINES + 99}")
case("log_lines(n) returns the newest n", len(sup.log_lines(5)) == 5)

pid1 = sup.status()["pid"]
(FAKE_DIR / "crash").write_text("")           # every restart will now die at once
sup._hub_call("GET", "/__crash")
case("a crash is noticed and the state says so",
     until(lambda: sup.summary()["state"] in ("backoff", "starting")
           or sup.status()["restarts"] >= 1, 5), str(sup.summary()))
case("the hub that kept crashing is retried with a doubling wait, capped",
     until(lambda: len(sup.status()["backoff"]) >= 5, 15)
     and sup.status()["backoff"][:5] == [0.2, 0.4, 0.8, 0.8, 0.8], str(sup.status()["backoff"]))
case("the last error says what happened", "exited with code 3" in sup.summary()["lastError"])
case("each retry is a real new process", len(runs()) >= 5)
case("the caffeinate belonging to the dead hub was ended", caff.poll() is not None)

(FAKE_DIR / "crash").unlink()
case("when the fault clears the hub comes back and runs",
     until(lambda: sup.summary()["state"] == "running", 10), str(sup.summary()))
case("with a new pid", sup.status()["pid"] not in (None, pid1))
case("and the old error is cleared once it answers", sup.summary()["lastError"] == "")
caff2 = sup._caffeinate

# ------------------------------------------------------------------ enrol code
print("-- enrol code")
code = sup.new_enrol_code("worker", "kitchen laptop", 600)
enrol = [json.loads(l) for l in (FAKE_DIR / "enrol.log").read_text().splitlines()]
token = config.get("hub", "adminToken")
case("with no admin token yet, one is minted with `token --admin` and kept",
     token == "rscu_fakeadmin123" and ["token", "--admin"] == [c for c in calls() if c[:1] == ["token"]][0][:2])
case("the code and its expiry come back", code == {"code": "ABCD-EFGH", "expires_at": "2099-01-01T00:00:00Z"})
case("the request carried the admin bearer and the body",
     enrol[-1]["auth"] == "Bearer rscu_fakeadmin123"
     and enrol[-1]["body"] == {"kind": "worker", "note": "kitchen laptop", "ttl_s": 600}, str(enrol[-1]))

# -------------------------------------------------------------------- register
print("-- register")
before = settings_path.read_text()
answer = sup.register("Eddie's hub")
path, auth, body = FakeStarfront.received[-1]
case("the server got POST /api/v1/hubs as the signed-in person",
     path == "/api/v1/hubs" and auth == "Bearer usr_PERSON", f"{path} {auth}")
case("with the name and the LAN url", body == {"name": "Eddie's hub", "lanUrl": f"http://hub.example:{port}"}, str(body))
creds = sup.credentials_path()
cdoc = tomllib.loads(creds.read_text())
case("the credentials file holds the hub token and grant secret",
     cdoc["hub_token"] == "hubtok_SECRET" and cdoc["grant_secret"] == "grant_SECRET"
     and cdoc["hub_id"] == "hub_FAKE1")
# The hub refuses a credentials file with any other key ("unknown field"), and then runs
# without Starfront: an extra `server` key once made exactly that happen silently.
case("the credentials file holds exactly the keys the hub accepts",
     set(cdoc) == {"hub_id", "hub_token", "grant_secret"}, str(sorted(cdoc)))
if os.name != "nt":
    case("the credentials file is 0600", stat.S_IMODE(creds.stat().st_mode) == 0o600,
         oct(stat.S_IMODE(creds.stat().st_mode)))
saved = settings_path.read_text()
case("settings.json has the id and name but neither secret",
     "hub_FAKE1" in saved and "Eddie's hub" in saved
     and "hubtok_SECRET" not in saved and "grant_SECRET" not in saved)
case("the TOML now names the Starfront server and hub id",
     tomllib.loads(sup.config_path().read_text()).get("starfront") == {"url": STARFRONT, "hub_id": "hub_FAKE1"})
case("the secrets are not in the TOML the hub is handed",
     "SECRET" not in sup.config_path().read_text())
try:
    sup.register("again")
    case("registering twice without force is refused", False)
except CollabError as exc:
    case("registering twice without force is refused", "already registered" in str(exc))
case("registering while running restarts the hub to pick up the id",
     until(lambda: len(runs()) >= 7 and sup.summary()["state"] == "running", 10), str(sup.summary()))
config.update("collab", {"userToken": ""})
try:
    sup.register("no token", force=True)
    case("registering without signing in is refused", False)
except CollabError as exc:
    case("registering without signing in is refused", exc.status == 401)
config.update("collab", {"userToken": "usr_PERSON"})

# ------------------------------------------------------------------------ stop
print("-- stop")
pid = sup.status()["pid"]
caff3 = sup._caffeinate
t0 = time.monotonic()
sup.request_stop()
case("request_stop returns at once", time.monotonic() - t0 < 0.5)
case("the hub is stopped and the state says so",
     until(lambda: sup.summary()["state"] == "stopped", 10), str(sup.summary()))


def alive(p):
    try:
        os.kill(p, 0)
        return True
    except OSError:
        return False


case("the child process is gone", until(lambda: not alive(pid), 5))
case("and so is its caffeinate", caff3 is None or caff3.poll() is not None)
case("a stopped hub is not restarted", (time.sleep(0.8), len(runs()))[1] == len(runs())
     and sup.summary()["state"] == "stopped")

sup.start()
case("it can be started again", until(lambda: sup.summary()["state"] == "running", 10))
pid = sup.status()["pid"]
sup.shutdown()
case("shutdown (program exit) waits for the hub to be gone",
     sup.summary()["state"] == "stopped" and not alive(pid))
hub._platform, hub.CAFFEINATE = real_platform, real_caff

# ------------------------------------------------------- a hub that ignores TERM
print("-- a stubborn hub")
STUBBORN = FAKE_HUB.replace('ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler).serve_forever()',
                            'import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                            'ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler).serve_forever()')
if os.name != "nt":
    script("collab-hub", STUBBORN)
    stubborn = hub.HubSupervisor(config, poll_seconds=0.3, terminate_wait=1)
    stubborn.start()
    until(lambda: stubborn.summary()["state"] == "running", 10)
    pid = stubborn.status()["pid"]
    stubborn.shutdown()
    case("a hub that ignores terminate is killed", not alive(pid) and stubborn.summary()["state"] == "stopped")
    script("collab-hub", FAKE_HUB)

# ---------------------------------------------------------------------- routes
print("-- routes")
try:
    from astrocontrol import desktop, main
except Exception as exc:                                        # noqa: BLE001
    case("main.py imports for the route checks", False, repr(exc))
    main = None

if main is not None:
    main.config.update("hub", {
        "enabled": False, "binary": str(fake_hub), "configDir": str(config_dir),
        "dataRoot": str(hub_data), "catalogueDir": str(catalogue),
        "bind": f"127.0.0.1:{free_port()}", "adminToken": "rscu_secretvalue",
        "registeredId": "", "registeredName": ""})
    main.config.update("collab", {"serverUrl": STARFRONT, "userToken": "usr_PERSON"})
    main.hub_supervisor.poll_seconds = 0.3
    import urllib.error
    import urllib.request
    server = desktop.ServerThread("127.0.0.1", free_port())
    server.start()

    class Reply:
        def __init__(self, status, text):
            self.status_code, self.text = status, text

        def json(self):
            return json.loads(self.text)

    class Client:
        """The program's real HTTP surface, over loopback."""

        def _go(self, method, path, body=None):
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(server.url.rstrip("/") + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return Reply(resp.status, resp.read().decode())
            except urllib.error.HTTPError as exc:
                return Reply(exc.code, exc.read().decode())

        def get(self, path):
            return self._go("GET", path)

        def post(self, path, json=None):
            return self._go("POST", path, json)

    client = Client()
    r = client.get("/api/hub")
    case("GET /api/hub reads state without a hub", r.status_code == 200 and r.json()["state"] == "stopped", r.text[:200])
    case("it never shows the admin token", "rscu_secretvalue" not in r.text and r.json()["hasAdminToken"])
    snap = main._snapshot()
    case("the live snapshot carries a hub summary",
         set(snap["hub"]) == {"enabled", "state", "url", "workers", "lastError", "registered"}, str(snap.get("hub")))
    case("GET /api/settings masks the hub's admin token",
         client.get("/api/settings").json()["hub"]["adminToken"] == "***")
    r = client.post("/api/settings/hub", json={"publicUrl": "http://x.example:1", "adminToken": "***"})
    case("POST /api/settings/hub saves, and a mask never overwrites the token",
         r.status_code == 200 and main.config.get("hub", "publicUrl") == "http://x.example:1"
         and main.config.get("hub", "adminToken") == "rscu_secretvalue", r.text[:200])
    case("an empty or bad body is refused",
         client.post("/api/settings/hub", json={}).status_code == 400
         and client.post("/api/settings/hub", json={"bind": "nonsense"}).status_code == 422)
    t0 = time.monotonic()
    r = client.post("/api/hub/check")
    case("POST /api/hub/check answers at once and the report arrives later",
         r.status_code == 200 and time.monotonic() - t0 < 1
         and until(lambda: (client.get("/api/hub").json()["check"] or {}).get("ok") is True, 10))
    r = client.post("/api/hub/start")
    case("POST /api/hub/start queues the start", r.status_code == 200)
    case("the hub comes up", until(lambda: client.get("/api/hub").json()["state"] == "running", 10))
    case("the snapshot now says running with a worker count",
         main._snapshot()["hub"]["state"] == "running" and main._snapshot()["hub"]["workers"] == 2)
    r = client.post("/api/hub/enrol-code", json={"note": "route"})
    case("POST /api/hub/enrol-code returns a code", r.status_code == 200 and r.json()["code"] == "ABCD-EFGH", r.text[:200])
    r = client.post("/api/hub/register", json={"name": "Route hub"})
    case("POST /api/hub/register registers", r.status_code == 200 and r.json()["registeredId"] == "hub_FAKE1", r.text[:200])
    r = client.post("/api/hub/register", json={"name": "Route hub"})
    case("and a second attempt is refused with a message", r.status_code == 502 and "already" in r.text, r.text[:200])
    lines = client.get("/api/hub/log?n=50").json()["lines"]
    case("GET /api/hub/log returns the ring buffer", lines and "message" in lines[-1])
    r = client.post("/api/hub/stop")
    case("POST /api/hub/stop returns at once", r.status_code == 200)
    case("and the hub stops", until(lambda: client.get("/api/hub").json()["state"] == "stopped", 10))
    r = client.post("/api/hub/enrol-code", json={})
    case("an enrol code with the hub stopped is a clear error", r.status_code == 502 and "not running" in r.text, r.text[:200])
    main.hub_supervisor.shutdown()
    server.stop()

starfront.shutdown()
passed = sum(results)
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
