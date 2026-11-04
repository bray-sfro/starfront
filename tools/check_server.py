"""Exercise the collaboration server and the agent that talks to it.

    python tools/check_server.py

Over real HTTP, against a real server on a real port, with a real SQLite file.
Not a test client: the thing being checked is a protocol between two machines,
and the parts most likely to be wrong — who is allowed to do what, what happens
when the same night is reported twice, what an agent does when the server is
not there — are exactly the parts a shortcut around the network would skip.

No test framework, for the same reason as the other checks here.
"""

import json
import math
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = Path(tempfile.mkdtemp())
ADMIN = "test-admin-token"
os.environ["STARFRONT_DATA"] = str(DATA)
os.environ["STARFRONT_ADMIN_TOKEN"] = ADMIN
os.environ.setdefault("ASTRO_DATA_DIR", tempfile.mkdtemp())

from astrocontrol import collab                                  # noqa: E402
from astrocontrol.collabclient import CollabClient               # noqa: E402
from astrocontrol.config import Config                           # noqa: E402

results = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    results.append(bool(ok))
    return ok


# --------------------------------------------------------------- the server
def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"

# --------------------------------------------------------------- a fake Discord
# Enough of Discord to sign somebody in: the authorize page (which just sends
# the person straight back with a code), the token exchange, who-am-I, and
# the guild membership check. Which person "signs in" is chosen per test by
# the code they carry, so one fake serves a member with the role, a member
# without it, and a stranger.
DISCORD_PORT = free_port()
DISCORD_BASE = f"http://127.0.0.1:{DISCORD_PORT}"
GUILD = "guild-777"
ROLE = "role-imager"
PEOPLE = {
    "code-alice": {"id": "1001", "username": "alice", "global_name": "Alice",
                   "member": True, "roles": [ROLE]},
    "code-bob": {"id": "1002", "username": "bob", "global_name": "Bob",
                 "member": True, "roles": []},
    "code-mallory": {"id": "1003", "username": "mallory", "global_name": "Mallory",
                     "member": False, "roles": []},
    "code-carol": {"id": "1004", "username": "carol", "global_name": "Carol",
                   "member": True, "roles": []},
    "code-dave": {"id": "1005", "username": "dave", "global_name": "Dave",
                  "member": True, "roles": []},
}


def fake_discord():
    import urllib.parse
    import uvicorn
    from fastapi import FastAPI, Header, Request
    from fastapi.responses import RedirectResponse
    fake = FastAPI()

    @fake.get("/oauth2/authorize")
    def authorize(redirect_uri: str, state: str, who: str = "code-alice"):
        return RedirectResponse(f"{redirect_uri}?code={who}&state={state}")

    @fake.post("/oauth2/token")
    async def token(request: Request):
        form = dict(urllib.parse.parse_qsl((await request.body()).decode()))
        code = form.get("code", "")
        if form.get("client_secret") != "shh" or code not in PEOPLE:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="bad code")
        return {"access_token": f"bearer-{code}", "token_type": "Bearer"}

    def person(authorization: str):
        return PEOPLE.get(authorization.replace("Bearer bearer-", ""))

    @fake.get("/users/@me")
    def me(authorization: str = Header(default="")):
        p = person(authorization)
        return {"id": p["id"], "username": p["username"],
                "global_name": p["global_name"], "avatar": ""}

    @fake.get("/users/@me/guilds/{guild}/member")
    def member(guild: str, authorization: str = Header(default="")):
        p = person(authorization)
        if guild != GUILD or not p["member"]:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Unknown Guild")
        return {"roles": p["roles"], "nick": None}

    uvicorn.run(fake, host="127.0.0.1", port=DISCORD_PORT, log_level="error")


threading.Thread(target=fake_discord, daemon=True).start()

os.environ["STARFRONT_DISCORD_CLIENT_ID"] = "app-1"
os.environ["STARFRONT_DISCORD_CLIENT_SECRET"] = "shh"
os.environ["STARFRONT_DISCORD_GUILD"] = GUILD
# Under the name the setting had before the server was called Starfront: a
# discord.env written then must still be read.
os.environ["ASTROCOLLAB_DISCORD_ROLE"] = ROLE
os.environ["STARFRONT_DISCORD_OWNERS"] = "1001"     # Alice runs the server
os.environ["STARFRONT_PUBLIC_URL"] = BASE
os.environ["STARFRONT_DISCORD_API"] = DISCORD_BASE
os.environ["STARFRONT_DISCORD_AUTHORIZE"] = f"{DISCORD_BASE}/oauth2/authorize"

# A folder of N.I.N.A. plugin releases for the server to offer: one manifest
# and the zip it points at, plus a file that is not a release at all.
NINA_DIR = Path(tempfile.mkdtemp())
os.environ["STARFRONT_NINA_DIR"] = str(NINA_DIR)
(NINA_DIR / "Starfront.Collab.1.0.0.0.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
(NINA_DIR / "Starfront.Collab.1.0.0.0.manifest.json").write_text(json.dumps({
    "Name": "Starfront Collaboration", "Identifier": "896B4E28-7CEB-4731-BB97-8BADE7460268",
    "Version": {"Major": "1", "Minor": "0", "Patch": "0", "Build": "0"},
    "Author": "test", "Repository": "https://example.org", "License": "MPL-2.0",
    "LicenseURL": "https://www.mozilla.org/en-US/MPL/2.0/",
    "MinimumApplicationVersion": {"Major": "3", "Minor": "2", "Patch": "0", "Build": "9001"},
    "Descriptions": {"ShortDescription": "test"},
    "Installer": {"URL": f"{BASE}/nina/download/Starfront.Collab.1.0.0.0.zip", "Type": "ARCHIVE",
                  "Checksum": "00", "ChecksumType": "SHA256"}}), encoding="utf-8")
(NINA_DIR / "notes.txt").write_text("not a release", encoding="utf-8")
(NINA_DIR / "CHANGELOG.md").write_text("# Change log\n\n## 1.0.0.0\n", encoding="utf-8")
(NINA_DIR / "Starfront Collaboration for NINA.pdf").write_bytes(b"%PDF-1.4\n%test\n")
# The installers the server offers at /download: two versions, where the
# newer one sorts first only by number, and a file that is not an installer.
DOWNLOAD_DIR = Path(tempfile.mkdtemp())
os.environ["STARFRONT_DOWNLOAD_DIR"] = str(DOWNLOAD_DIR)
(DOWNLOAD_DIR / "Starfront-Setup-0.2.9.exe").write_bytes(b"MZ-old" + b"\x00" * 10)
(DOWNLOAD_DIR / "Starfront-Setup-0.2.35.exe").write_bytes(b"MZ-new" + b"\x00" * 10)
(DOWNLOAD_DIR / "secrets.txt").write_text("not for anybody", encoding="utf-8")
# An older release left behind in the folder: the feed offers only the newest.
(NINA_DIR / "Starfront.Collab.0.9.0.0.manifest.json").write_text(json.dumps({
    "Name": "Starfront Collaboration", "Identifier": "896B4E28-7CEB-4731-BB97-8BADE7460268",
    "Version": {"Major": "0", "Minor": "9", "Patch": "0", "Build": "0"},
    "Author": "test", "Repository": "https://example.org", "License": "MPL-2.0",
    "LicenseURL": "https://www.mozilla.org/en-US/MPL/2.0/",
    "MinimumApplicationVersion": {"Major": "3", "Minor": "2", "Patch": "0", "Build": "9001"},
    "Descriptions": {"ShortDescription": "test"},
    "Installer": {"URL": f"{BASE}/nina/download/Starfront.Collab.0.9.0.0.zip", "Type": "ARCHIVE",
                  "Checksum": "00", "ChecksumType": "SHA256"}}), encoding="utf-8")


# The database the server opens is one from the day files were lights only:
# the files table without its kind column, keyed on the name alone. The
# server must bring it up to date at start rather than fail to start - which
# it did once, with the new index asked for before the column existed.
import sqlite3                                                   # noqa: E402
_old = sqlite3.connect(str(DATA / "collab.sqlite"))
_old.executescript("""
CREATE TABLE files (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, user TEXT NOT NULL,
    name TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
    received REAL NOT NULL, telescope TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX files_once ON files (project, user, name);
""")
_old.commit()
_old.close()


def serve():
    import uvicorn
    from server.app import app
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="error")


threading.Thread(target=serve, daemon=True).start()

for _ in range(100):
    try:
        urllib.request.urlopen(f"{BASE}/api/v1/health", timeout=1).read()
        break
    except Exception:
        time.sleep(0.2)
else:
    print("FAIL  the server did not start")
    sys.exit(1)


def call(method, path, body=None, token=None, expect=200):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                     headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode() or "{}")
            return response.status, payload
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


status, health = call("GET", "/api/v1/health")
case("the server answers", status == 200 and health["ok"])
case("and says which protocol it speaks", health["protocol"] == collab.PROTOCOL)

# ------------------------------------------------------------ who may do what
status, _ = call("POST", "/api/v1/agents", {"name": "nope"})
case("enrolling a telescope needs the coordinator's token", status == 401)

status, made = call("POST", "/api/v1/agents",
                    {"name": "Telescope 1", "owner": "bray"}, token=ADMIN)
case("the coordinator can enrol one", status == 200 and made["token"])
AGENT_TOKEN = made["token"]
AGENT_ID = made["agent"]["id"]
case("and the token is handed back exactly once",
     "token" not in made["agent"])

status, _ = call("GET", "/api/v1/agents", token=AGENT_TOKEN)
case("a telescope's token cannot administer the server", status == 401)

status, _ = call("POST", "/api/v1/agent/hello", {"protocol": 1}, token="rubbish")
case("an unknown token is refused", status == 401)

# ------------------------------------------------------ signing in with Discord
print("\n-- signing in with Discord --")


def sign_in(who):
    """The device-code flow end to end, as Starfront and a browser would do it.

    Starfront asks for a code; the browser opens the start page, is sent to
    Discord, comes back to the callback; Starfront's poll then hands over the
    user token. The fake Discord signs in whoever `who` names.
    """
    _, begun = call("POST", "/api/v1/auth/login")
    code = begun["code"]
    # The browser: follow the redirects by hand, because urllib would follow
    # them for us and hide what each hop said.
    import urllib.parse
    request = urllib.request.Request(begun["url"], method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        opener.open(request, timeout=10)
        raise AssertionError("expected a redirect to Discord")
    except urllib.error.HTTPError as hop:
        to_discord = hop.headers.get("Location")
    # Pick who signs in, the way a real person picks by logging into Discord.
    to_discord += f"&who={who}"
    try:
        opener.open(urllib.request.Request(to_discord, method="GET"), timeout=10)
        raise AssertionError("expected a redirect back from Discord")
    except urllib.error.HTTPError as hop:
        back = hop.headers.get("Location")
    try:
        with urllib.request.urlopen(back, timeout=10) as page:
            landed = page.status, page.read().decode()
    except urllib.error.HTTPError as exc:
        landed = exc.code, exc.read().decode()
    _, polled = call("GET", f"/api/v1/auth/poll?code={code}")
    return begun, to_discord, landed, polled


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


status, info = call("GET", "/api/v1/auth")
case("the server says Discord sign-in is on, and no role is needed to start one",
     status == 200 and info["discord"] and info["roleRequired"] is False)

begun, to_discord, landed, polled = sign_in("code-alice")
case("a sign-in begins with a code and a page to open",
     begun["code"] and begun["url"].startswith(BASE + "/auth/discord/start"))
case("...the page sends the browser to Discord asking only who and which guild",
     "scope=identify+guilds.members.read" in to_discord
     and "client_id=app-1" in to_discord
     and f"state={begun['code']}" in to_discord, to_discord[:120])
case("...Discord sends them back and the page says who they are",
     landed[0] == 200 and "Signed in as Alice" in landed[1])
case("...and Starfront's poll is handed the token, once",
     polled["state"] == "done" and polled["token"]
     and polled["user"]["name"] == "Alice" and polled["user"]["canStart"] is True,
     str({k: v for k, v in polled.items() if k != "token"}))
ALICE = polled["token"]
_, again = call("GET", f"/api/v1/auth/poll?code={begun['code']}")
case("...polling the same code again gets nothing", again["state"] == "claimed")

_, me = call("GET", "/api/v1/auth/me", token=ALICE)
case("the token says who it is, and that she may start collaborations",
     me["name"] == "Alice" and me["canStart"] is True)
case("...and that she owns the server, by her Discord id, with no token pasted",
     me["admin"] is True and polled["user"]["admin"] is True)

_, _, _, polled = sign_in("code-bob")
BOB = polled["token"]
case("a member without the customer role signs in too, and may start one: being in the server is the gate",
     polled["state"] == "done" and polled["user"]["canStart"] is True)

_, _, landed, polled = sign_in("code-mallory")
case("somebody not in the Discord server is turned away, and told so",
     landed[0] == 403 and "not a member" in landed[1] and polled["state"] == "pending",
     f"{landed[0]}")

# -------------------------------------------------------- who may do what, now
print("\n-- what a member may do --")

status, mine = call("POST", "/api/v1/agents", {"name": "Alice's rig"}, token=ALICE)
case("a member can enrol their own telescope", status == 200 and mine["token"])
case("...and it is theirs", mine["agent"]["owner_id"] == "1001"
     and mine["agent"]["owner"] == "Alice")
status, listing = call("GET", "/api/v1/agents", token=BOB)
case("...a member sees their own telescopes and nobody else's",
     status == 200 and listing["agents"] == [])
_, everything = call("GET", "/api/v1/agents", token=ALICE)
case("...while an owner sees them all", len(everything["agents"]) >= 2)

status, started = call("POST", "/api/v1/projects", {
    "name": "Alice's Veil", "region": {"ra": 313.0, "dec": 31.0, "width": 3.0,
                                       "height": 3.0}}, token=ALICE)
case("a member with the role can start a collaboration",
     status == 200 and started["project"]["owner_id"] == "1001"
     and started["project"]["coordinator"] == "Alice")
VEIL = started["project"]["id"]
_, browse = call("GET", "/api/v1/agent/projects", token=AGENT_TOKEN)
case("...and everybody browsing sees whose it is",
     next(p for p in browse["projects"] if p["id"] == VEIL)["ownerId"] == "1001")

status, bobs = call("POST", "/api/v1/projects", {
    "name": "Bob's", "region": {"ra": 0, "dec": 0, "width": 1, "height": 1}},
    token=BOB)
case("a member without the role can start one too", status == 200
     and bobs["project"]["owner_id"] == "1002" and bobs["project"]["coordinator"] == "Bob",
     bobs.get("detail") or bobs["project"]["coordinator"])
status, _ = call("PUT", f"/api/v1/projects/{bobs['project']['id']}", {"status": "closed"}, token=BOB)
case("...and close it again", status == 200)

# The lookup a program with no catalog of its own starts a collaboration from.
status, found = call("GET", "/api/v1/lookup?name=m31", token=BOB)
case("a signed-in member can look an object up by name, in degrees",
     status == 200 and found["results"] and found["results"][0]["id"] == "M31"
     and abs(found["results"][0]["ra"] - 10.68) < 0.1
     and abs(found["results"][0]["dec"] - 41.27) < 0.1
     and found["results"][0]["sizeArcmin"] == 178,
     str(found)[:160])
status, _ = call("GET", "/api/v1/lookup?name=m31", token=AGENT_TOKEN)
case("...a telescope's token cannot", status == 401)

status, _ = call("PUT", f"/api/v1/projects/{VEIL}", {"notes": "hi"}, token=BOB)
case("somebody else cannot change Alice's collaboration", status == 403)
status, _ = call("PUT", f"/api/v1/projects/{VEIL}", {"notes": "mine"}, token=ALICE)
case("Alice can", status == 200)
status, _ = call("PUT", f"/api/v1/projects/{VEIL}", {"notes": "owner"}, token=ADMIN)
case("...and so can the admin token", status == 200)
status, _ = call("PUT", f"/api/v1/projects/{VEIL}", {"notes": "x"}, token=AGENT_TOKEN)
case("a telescope's token still cannot", status == 401)

status, _ = call("POST", "/api/v1/auth/logout", token=BOB)
status2, _ = call("GET", "/api/v1/auth/me", token=BOB)
case("signing out stops the token working", status == 200 and status2 == 401)

# ------------------------------------------------------------------- hello
PROFILE = {
    "name": "Telescope 1", "focalLength": 530.0, "pixelSize": 3.76,
    "sensorWidth": 6248, "sensorHeight": 4176, "binning": 1,
    "filters": {"Ha": 3.0, "OIII": 3.0, "L": None},
    "typicalHfr": 2.4, "typicalGuideRms": 0.6,
}
status, hello = call("POST", "/api/v1/agent/hello",
                     {"protocol": 1, "profile": PROFILE}, token=AGENT_TOKEN)
case("a telescope can say hello", status == 200 and hello["agent"] == AGENT_ID)

status, _ = call("POST", "/api/v1/agent/hello", {"protocol": 99}, token=AGENT_TOKEN)
case("an agent from the future is refused rather than misunderstood",
     status == 409)

# ------------------------------------------------------------- no task yet
status, answer = call("GET", "/api/v1/agent/task", token=AGENT_TOKEN)
case("with nothing assigned there is no task", status == 200
     and answer["task"] is None)

# ----------------------------------------------------------------- a project
status, project = call("POST", "/api/v1/projects", {
    "name": "Cygnus Wall deep",
    "region": {"ra": 313.0, "dec": 44.5, "width": 2.0, "height": 1.5,
               "rotation": 0.0},
    "requirements": {"maxScale": 2.5, "maxHfr": 3.5, "maxGuideRms": 1.2,
                     "filters": {"Ha": 5.0, "OIII": 5.0},
                     "minExposure": 120},
    "goals": {"Ha": 8.0, "OIII": 8.0},
}, token=ADMIN)
case("a coordinator can start a collaboration", status == 200)
PROJECT = project["project"]["id"]

status, listing = call("GET", "/api/v1/projects")
case("and anyone can browse it — that is how people find one to join",
     status == 200 and any(p["id"] == PROJECT for p in listing["projects"]))

# ------------------------------------------------------------------ a task
status, made = call("POST", f"/api/v1/projects/{PROJECT}/tasks", {
    "agent": AGENT_ID,
    "region": {"ra": 313.2, "dec": 44.6, "width": 0.6, "height": 0.5},
    "filters": [{"filter": "Ha", "exposure": 600, "hours": 4.0}],
    "note": "north-east chunk",
}, token=ADMIN)
case("a chunk of sky can be delegated to a telescope", status == 200)
TASK = made["task"]["id"]
case("and it arrives offered rather than started",
     made["task"]["state"] == "offered")

status, answer = call("GET", "/api/v1/agent/task", token=AGENT_TOKEN)
case("the telescope is given it when it asks",
     status == 200 and answer["task"]["id"] == TASK)
case("along with what the project will accept",
     answer["requirements"]["maxHfr"] == 3.5)
case("and how many frames that is",
     collab.Task.read(answer["task"]).filters[0].frames() == 24,
     str(collab.Task.read(answer["task"]).filters[0].frames()))

status, answer = call("POST", f"/api/v1/agent/task/{TASK}",
                      {"state": "accepted"}, token=AGENT_TOKEN)
case("the operator can accept it", status == 200
     and answer["task"]["state"] == "accepted")
case("and accepting moves the version on, so a poll notices",
     answer["task"]["version"] == 2)

# A telescope must not be able to touch another's work.
status, other = call("POST", "/api/v1/agents", {"name": "Telescope 2"}, token=ADMIN)
OTHER_TOKEN = other["token"]
status, _ = call("POST", f"/api/v1/agent/task/{TASK}", {"state": "complete"},
                 token=OTHER_TOKEN)
case("one telescope cannot alter another's task", status == 404)

status, answer = call("GET", "/api/v1/agent/task", token=OTHER_TOKEN)
case("nor see it", answer["task"] is None)

# ----------------------------------------------------------- reporting a night
GOOD = {
    "task": TASK, "night": "2026-09-17", "filterName": "Ha",
    "frames": 24, "seconds": 14400, "exposure": 600,
    "footprint": {"ra": 313.2, "dec": 44.6, "width": 0.6, "height": 0.5},
    "scale": 1.46, "hfr": 2.6, "guideRms": 0.7, "bandpass": 3.0,
}
status, answer = call("POST", "/api/v1/agent/report",
                      {"contributions": [GOOD]}, token=AGENT_TOKEN)
case("a good night is accepted", status == 200
     and answer["recorded"][0]["accepted"], str(answer["recorded"][0]["verdict"]))

# The same night reported twice must not count twice — an agent retrying after
# a dropped connection is the normal case, not an error.
status, again = call("POST", "/api/v1/agent/report",
                     {"contributions": [GOOD]}, token=AGENT_TOKEN)
case("reporting it again does not count it twice",
     again["recorded"][0]["duplicate"] is True)

status, project = call("GET", f"/api/v1/projects/{PROJECT}")
case("and the ledger holds one row for it",
     len(project["contributions"]) == 1, str(len(project["contributions"])))

BAD = {**GOOD, "night": "2026-09-18", "hfr": 5.2, "guideRms": 2.4}
status, answer = call("POST", "/api/v1/agent/report",
                      {"contributions": [BAD]}, token=AGENT_TOKEN)
verdict = answer["recorded"][0]
case("a night outside the rules is rejected, with reasons",
     not verdict["accepted"] and len(verdict["verdict"]["reasons"]) == 2,
     verdict["verdict"]["summary"])

WIDE = {**GOOD, "night": "2026-09-19", "bandpass": 7.0}
status, answer = call("POST", "/api/v1/agent/report",
                      {"contributions": [WIDE]}, token=AGENT_TOKEN)
case("a filter too wide for the project is rejected",
     not answer["recorded"][0]["accepted"],
     answer["recorded"][0]["verdict"]["summary"])

# The coordinator has the last word.
rejected = [row for row in call("GET", f"/api/v1/projects/{PROJECT}")[1]["contributions"]
            if not row["accepted"]][0]
status, answer = call(
    "POST",
    f"/api/v1/contributions/{rejected['id']}/verdict?accepted=true"
    "&reason=only+data+on+that+sky",
    token=ADMIN)
case("a coordinator can overrule a rejection",
     status == 200 and answer["contribution"]["accepted"]
     and answer["contribution"]["overridden"])

# -------------------------------------------------------------- the agent side
class FakeManager:
    def __init__(self, devices):
        self.devices = devices

    def get(self, kind):
        return self.devices.get(kind)


class FakeRig:
    def __init__(self, config):
        self.id = "rig1"
        self.name = "Telescope 1"
        self.config = config
        self.manager = FakeManager({})


class FakeRigs:
    def __init__(self, rig):
        self.master = rig
        self.all = [rig]
        self.lines = []

    def log(self, message, level="info"):
        self.lines.append((level, message))


class FakeTargets:
    def listing(self):
        # Two nights of HFR in *pixels*, which is what the logs actually hold.
        return [{"integration": {"log": [
            {"hfrSum": 2.0 * 30, "hfrCount": 30},
            {"hfrSum": 2.4 * 20, "hfrCount": 20},
        ]}}]


config = Config(Path(tempfile.mkdtemp()) / "settings.json")
config.update("optics", {"focalLength": 530.0, "pixelSize": 3.76,
                         "sensorWidth": 6248, "sensorHeight": 4176})
config.update("camera", {"filterNames": ["L", "Ha", "OIII"],
                         "filterBandpass": {"Ha": 3.0, "OIII": 3.0}})
config.update("collab", {"enabled": True, "serverUrl": BASE,
                         "token": AGENT_TOKEN})
rigs = FakeRigs(FakeRig(config))
client = CollabClient(rigs, config, FakeTargets(), log=rigs.log)

profile = client.profile()
case("the agent works out its own image scale",
     abs(profile["scale"] - 1.4636) < 0.01, f"{profile['scale']}")
case("and its field of view",
     abs(profile["field"][0] - 2.54) < 0.05, f"{profile['field']}")
# Two nights at 2.0 and 2.4 px: a median of 2.2 px, which at this plate scale
# is 3.22 arcseconds. The conversion is the point — 2.2 px means nothing to a
# project, and comparing it directly against an arcsecond limit would call this
# rig four times better than it is.
case("and reports star size in arcseconds, not pixels",
     abs(profile["typicalHfr"] - 2.2 * 1.4636) < 0.05,
     f"{profile['typicalHfr']}\" from a median of 2.2 px")

task = client.poll()
case("the agent picks the task up", task and task["id"] == TASK)
case("and knows whether it can actually do it",
     client.status()["compatibility"]["ok"] is True,
     client.status()["compatibility"]["summary"])

# The near miss is the case worth having: 3.22" passes a 3.5" limit and fails a
# 3.0" one, and the difference is entirely in the arcsecond conversion.
tight = collab.compatibility(
    collab.RigProfile.read(profile),
    collab.Requirements.read({"maxHfr": 3.0, "filters": {"Ha": 5.0}}))
case("and a rig that is close but not close enough is told so",
     not tight["ok"], tight["summary"])

# A rig that cannot meet the rules should be told so, in terms it can act on.
narrow = Config(Path(tempfile.mkdtemp()) / "settings.json")
narrow.update("optics", {"focalLength": 130.0, "pixelSize": 9.0,
                         "sensorWidth": 1000, "sensorHeight": 1000})
narrow.update("camera", {"filterNames": ["Ha"], "filterBandpass": {"Ha": 12.0}})
verdict = collab.compatibility(
    collab.RigProfile.read({
        "focalLength": 130.0, "pixelSize": 9.0, "sensorWidth": 1000,
        "sensorHeight": 1000, "filters": {"Ha": 12.0}, "typicalHfr": 2.0}),
    collab.Requirements.read({"maxScale": 2.5, "filters": {"Ha": 5.0}}))
case("a rig that cannot contribute is told which part fails",
     not verdict["ok"] and len(verdict["checks"]) >= 2, verdict["summary"])

# Offline is a normal state for a remote rig, not a fault.
config.update("collab", {"serverUrl": "http://127.0.0.1:1"})
try:
    client.poll()
    raised = False
except Exception:
    raised = True
case("an unreachable server raises rather than corrupting the task", raised)
case("and the task already in hand is kept",
     client.status()["task"]["id"] == TASK)
config.update("collab", {"serverUrl": BASE})

# Reporting from the agent side, end to end.
answer = client.report([{
    "task": TASK, "night": "2026-09-20", "filterName": "OIII",
    "frames": 20, "seconds": 12000, "exposure": 600,
    "scale": 1.46, "hfr": 2.5, "guideRms": 0.8, "bandpass": 3.0,
}])
case("the agent can report a night and hear the verdict",
     answer["recorded"][0]["accepted"] is True,
     answer["recorded"][0]["verdict"]["summary"])

client.respond("complete")
case("and mark the task finished",
     client.status()["task"]["state"] == "complete")

# --------------------------------------------- reporting what was really shot
print("\n-- panels reported as they are shot --")

# The program tallies every sub against the panel it landed on, and the client
# sends those tallies, per night and per panel, on each poll. What has to be
# true: what was shot is what is reported, with the panel's own footprint; a
# night sent once is not sent again; and a night that grows after it was sent
# is sent again with the larger figure, which the server takes as the record.
from astrocontrol.targets import TargetStore                        # noqa: E402

store_ = TargetStore(Path(tempfile.mkdtemp()) / "targets.json")
mosaic = store_.create("Cygnus wall", 20.9, 44.6, 0.0, 2.3, 1.5, rows=2,
                       columns=2, collab={"project": PROJECT, "task": TASK,
                                          "version": 1, "server": BASE})
store_.add_panel_integration(mosaic["id"], "2026-09-21", 1, "Ha", 600.0, hfr=2.0)
store_.add_panel_integration(mosaic["id"], "2026-09-21", 1, "Ha", 600.0, hfr=2.4)
store_.add_panel_integration(mosaic["id"], "2026-09-21", 3, "OIII", 600.0, hfr=2.2)
pending = store_.unreported_panels(mosaic["id"])
case("subs are tallied against the panel they landed on",
     sorted((row["panel"], row["filter"], row["frames"]) for row in pending)
     == [(1, "Ha", 2), (3, "OIII", 1)],
     str([(row["panel"], row["filter"], row["frames"]) for row in pending]))

reporter = CollabClient(rigs, config, store_, log=rigs.log)
sent = reporter.report_pending()
case("the client reports every panel not yet sent", sent == 2, f"{sent} sent")
case("...and sends nothing the second time", reporter.report_pending() == 0)

_, ledger = call("GET", f"/api/v1/projects/{PROJECT}")
mine_rows = [row for row in ledger["contributions"]
             if row["night"] == "2026-09-21"]
case("the server holds one row per panel and filter for the night",
     sorted((r["payload"]["panel"], r["payload"]["filterName"], r["payload"]["frames"])
            for r in mine_rows) == [("1", "H", 2), ("3", "O", 1)],
     str([(r["payload"]["panel"], r["payload"]["filterName"]) for r in mine_rows]))
panel_row = next(r for r in mine_rows if r["payload"]["panel"] == "1")
panel_one = next(p for p in store_.get(mosaic["id"])["panels"] if p["index"] == 1)
case("...each with the panel's own footprint, not the mosaic's centre",
     abs(panel_row["payload"]["footprint"]["ra"] - panel_one["ra"] * 15.0) < 1e-6
     and abs(panel_row["payload"]["footprint"]["dec"] - panel_one["dec"]) < 1e-6,
     f'{panel_row["payload"]["footprint"]["ra"]:.3f}, '
     f'{panel_row["payload"]["footprint"]["dec"]:.3f}')
case("...star size in arcseconds, from the pixels the log holds",
     abs(panel_row["payload"]["hfr"] - 2.2 * 1.4636) < 0.05,
     f'{panel_row["payload"]["hfr"]:.2f}"')

# More frames on a panel already sent: sent again, and the larger figure wins.
store_.add_panel_integration(mosaic["id"], "2026-09-21", 1, "Ha", 600.0, hfr=2.0)
case("a panel that grows after it was sent is sent again",
     reporter.report_pending() == 1)
_, ledger = call("GET", f"/api/v1/projects/{PROJECT}")
grown = next(r for r in ledger["contributions"]
             if r["night"] == "2026-09-21" and r["payload"]["panel"] == "1")
case("...and the server keeps one row, at the larger figure",
     grown["payload"]["frames"] == 3 and grown["seconds"] == 1800.0,
     f'{grown["payload"]["frames"]} frames, {grown["seconds"]}s')

# --------------------------------------------------------- unverified is not ok
partial = collab.judge(
    collab.Contribution.read({"filterName": "Ha", "exposure": 600,
                              "bandpass": 3.0, "seconds": 3600}),
    collab.Requirements.read({"maxHfr": 3.0, "filters": {"Ha": 5.0},
                              "maxScale": 2.5}))
case("a night with nothing measured is accepted but flagged as unverified",
     partial["accepted"] and len(partial["unverified"]) == 2,
     partial["summary"])

# ==================================================================== joining
print("\n-- joining a mosaic: the whole of it, shared out --")

# The bug this pins down: a rig joining a forty-panel mosaic was handed one
# cell. Two rigs with different cameras join a wide project; the first, alone,
# holds all of it; when the second arrives the first's share shrinks and its
# version moves, which is how its program finds out.
def enrol(name, focal, width, height, hours):
    _, made = call("POST", "/api/v1/agents", {"name": name}, token=ADMIN)
    call("POST", "/api/v1/agent/hello", {
        "protocol": collab.PROTOCOL,
        "profile": {"name": name, "focalLength": focal, "pixelSize": 3.76,
                    "sensorWidth": width, "sensorHeight": height, "binning": 1,
                    "filters": {"Ha": 3.0}, "hoursPerNight": hours,
                    # field is what the server sizes cells by
                    "scale": 206.265 * 3.76 / focal,
                    "field": [206.265 * 3.76 / focal * width / 3600.0,
                              206.265 * 3.76 / focal * height / 3600.0]}},
        token=made["token"])
    return made["token"], made["agent"]["id"]


WIDE_TOKEN, WIDE_ID = enrol("wide", 389.0, 9576, 6388, 6.0)
NARROW_TOKEN, NARROW_ID = enrol("narrow", 1000.0, 6000, 4000, 3.0)

_, orion = call("POST", "/api/v1/projects", {
    "name": "Orion wide", "region": {"ra": 84.0, "dec": 0.0,
                                     "width": 16.0, "height": 8.0},
    "requirements": {"filters": {"Ha": 3.0}}, "goals": {"Ha": 10.0}},
    token=ADMIN)
ORION = orion["project"]["id"]

status, joined = call("POST", f"/api/v1/agent/projects/{ORION}/join", {},
                      token=WIDE_TOKEN)
task = joined["task"]
case("joining hands back the whole project's sky, not one cell",
     status == 200 and abs(task["region"]["width"] - 16.0) < 1e-6,
     f'{task["region"]["width"]} x {task["region"]["height"]} degrees')
case("...tiled with this rig's own camera",
     len(task["cells"]) > 4, f'{len(task["cells"])} cells')


def held(token, night):
    """What a rig is told to shoot on a given night, on the Orion project."""
    _, answer = call("GET", f"/api/v1/agent/task?night={night}", token=token)
    return next(t for t in answer["tasks"] if t["project"] == ORION)


def sky(task_, indices):
    return [collab.Region.read(task_["cells"][i]) for i in indices]


def overlap(a, b):
    """Sky two lists have in common, counting only cells that really coincide.

    Neighbouring cells of one tiling overlap by a tenth of a frame on purpose,
    so a panel next to somebody else's always shares a sliver with it. That
    is not two rigs on the same panel; half a cell or more is.
    """
    return sum(collab.overlap_area(x, y) for x in a for y in b
               if collab.overlap_area(x, y) > 0.5 * min(x.area(), y.area()))


# Alone on twelve panels with six hours, a rig is not given all twelve: a
# visit has to be worth stacking on its own - ten frames at least, in each
# filter - and six hours holds six such visits, not twelve.
wide1 = held(WIDE_TOKEN, "2026-10-01")
frames1 = wide1["visit"]["frames"]
case("...and, alone on it, is given as many panels as its night holds",
     1 < len(wide1["share"]) < len(wide1["cells"]),
     f'{len(wide1["share"])} of {len(wide1["cells"])} for 6 hours')
case("...with enough frames on each of them to stack",
     all(n >= 10 for n in frames1.values()), str(frames1))
case("...and the night about full",
     0.8 < len(wide1["share"]) * (wide1["visit"]["seconds"] + 90) / (6 * 3600) <= 1.0,
     f'{len(wide1["share"])} x {wide1["visit"]["seconds"]:.0f}s of 6 h')
first_version = wide1["version"]

# A second rig, narrower camera, half the hours, arriving the same night.
status, second = call("POST", f"/api/v1/agent/projects/{ORION}/join", {},
                      token=NARROW_TOKEN)
case("a second rig can join the same project", status == 200)
case("...with more cells, because its camera is narrower",
     len(second["task"]["cells"]) > len(task["cells"]),
     f'{len(second["task"]["cells"])} vs {len(task["cells"])}')
narrow1 = held(NARROW_TOKEN, "2026-10-01")
case("...and takes a night's worth of its own, not all of it",
     0 < len(narrow1["share"]) < len(narrow1["cells"]),
     f'{len(narrow1["share"])} of {len(narrow1["cells"])}')

# The first rig's list holds. Somebody joining at nine must not move the
# panels a rig has been shooting since eight.
same = held(WIDE_TOKEN, "2026-10-01")
case("the first rig's list for the night holds when a second rig joins",
     same["share"] == wide1["share"] and same["version"] == first_version,
     f'{len(same["share"])} panels, version {same["version"]}')
narrow_sky = sky(narrow1, narrow1["share"])
case("...and the newcomer is sent to sky the first rig is not on tonight",
     overlap(sky(wide1, wide1["share"]), narrow_sky)
     < 0.1 * sum(r.area() for r in narrow_sky),
     f'{overlap(sky(wide1, wide1["share"]), narrow_sky):.1f} square degrees in common')

# ------------------------------------------------- a camera that has turned
print("\n-- a camera that turns out to sit somewhere else --")

# The wide rig has no rotator and said its camera sits at 0. The first plate
# solve of the night finds it at 268: its program lays its mosaic again and
# says so in its profile, and the server cuts its cells again to match - the
# grid transposes - and deals tonight's list afresh on them.
before = held(WIDE_TOKEN, "2026-10-01")
across_before = max(c["column"] for c in before["cells"]) + 1
call("POST", "/api/v1/agent/hello", {
    "protocol": collab.PROTOCOL,
    "profile": {"name": "wide", "focalLength": 389.0, "pixelSize": 3.76,
                "sensorWidth": 9576, "sensorHeight": 6388, "binning": 1,
                "filters": {"Ha": 3.0}, "hoursPerNight": 6.0, "rotation": 268.0,
                "scale": 206.265 * 3.76 / 389.0,
                "field": [206.265 * 3.76 / 389.0 * 9576 / 3600.0,
                          206.265 * 3.76 / 389.0 * 6388 / 3600.0]}},
    token=WIDE_TOKEN)
after = held(WIDE_TOKEN, "2026-10-01")
across_after = max(c["column"] for c in after["cells"]) + 1
case("a camera that reports a new angle has its cells cut again",
     (len(after["cells"]) != len(before["cells"])
      or all(abs(c["rotation"] - 268.0) < 1e-6 for c in after["cells"]))
     and after["version"] > before["version"],
     f"{len(before['cells'])} cells -> {len(after['cells'])} at 268, version "
     f"{before['version']} -> {after['version']}")
case("...laid along the camera's own axes, cell for panel with the rig's program",
     all(abs(c["rotation"] - 268.0) < 1e-6 for c in after["cells"])
     and len(after["cells"]) == len(collab.camera_grid(
         collab.Region.read(after["region"]), 206.265 * 3.76 / 389.0 * 9576 / 3600.0,
         206.265 * 3.76 / 389.0 * 6388 / 3600.0, 268.0)),
     f"{len(after['cells'])} cells")
case("...and tonight's list dealt afresh on the new cells",
     after["share"] and all(0 <= i < len(after["cells"]) for i in after["share"]),
     f'{len(after["share"])} of {len(after["cells"])}')
again = held(WIDE_TOKEN, "2026-10-01")
case("...and holds from then on", again["version"] == after["version"])
# The hours a rig gives come off its plan and can shrink - a window pinned
# shorter, a target that sets early. The list dealt for tonight holds all
# the same: a sequence drawn up at dusk is the sequence that runs, and the
# panels the shorter night does not reach are simply not shot. The smaller
# hours are dealt for the next night.
before = held(WIDE_TOKEN, "2026-10-01")
call("POST", "/api/v1/agent/hello", {
    "protocol": collab.PROTOCOL,
    "profile": {"name": "wide", "focalLength": 389.0, "pixelSize": 3.76,
                "sensorWidth": 9576, "sensorHeight": 6388, "binning": 1,
                "filters": {"Ha": 3.0}, "hoursPerNight": 2.0, "rotation": 268.0,
                "scale": 206.265 * 3.76 / 389.0,
                "field": [206.265 * 3.76 / 389.0 * 9576 / 3600.0,
                          206.265 * 3.76 / 389.0 * 6388 / 3600.0]}},
    token=WIDE_TOKEN)
same_night = held(WIDE_TOKEN, "2026-10-01")
case("a rig whose night shrank keeps tonight's list exactly as dealt",
     same_night["share"] == before["share"] and same_night["version"] == before["version"],
     f'{len(before["share"])} panels, version {before["version"]} -> {same_night["version"]}')
shorter = held(WIDE_TOKEN, "2026-10-01b")
case("...and the next night is dealt for the two hours it now gives",
     0 < len(shorter["share"]) < len(before["share"])
     and shorter["version"] > before["version"],
     f'{len(before["share"])} -> {len(shorter["share"])} panels for 2 h')
case("...fitting the two hours",
     len(shorter["share"]) * (shorter["visit"]["seconds"] + 90) <= 2 * 3600 + 1,
     f'{len(shorter["share"])} x {shorter["visit"]["seconds"]:.0f}s')

# Back to straight for the nights below, which were written for it.
call("POST", "/api/v1/agent/hello", {
    "protocol": collab.PROTOCOL,
    "profile": {"name": "wide", "focalLength": 389.0, "pixelSize": 3.76,
                "sensorWidth": 9576, "sensorHeight": 6388, "binning": 1,
                "filters": {"Ha": 3.0}, "hoursPerNight": 6.0,
                "scale": 206.265 * 3.76 / 389.0,
                "field": [206.265 * 3.76 / 389.0 * 9576 / 3600.0,
                          206.265 * 3.76 / 389.0 * 6388 / 3600.0]}},
    token=WIDE_TOKEN)
held(WIDE_TOKEN, "2026-10-01")

# ============================================================== night by night
print("\n-- three rigs, four nights --")

# The pressure test: a field too big for anyone to finish in a night, three
# telescopes on it, and the server dealing each night from what came in the
# night before. What has to come out: no two rigs on the same panel the same
# night; every rig, over the nights, on every part of the field, rather than
# one camera owning one corner; panels that are done left alone; and each
# rig's list holding still until its night turns.
_, third_made = call("POST", "/api/v1/agents", {"name": "third"}, token=ADMIN)
THIRD_TOKEN = third_made["token"]
call("POST", "/api/v1/agent/hello", {
    "protocol": collab.PROTOCOL,
    "profile": {"name": "third", "focalLength": 389.0, "pixelSize": 3.76,
                "sensorWidth": 9576, "sensorHeight": 6388, "binning": 1,
                "filters": {"Ha": 3.0}, "hoursPerNight": 4.0,
                "scale": 206.265 * 3.76 / 389.0,
                "field": [206.265 * 3.76 / 389.0 * 9576 / 3600.0,
                          206.265 * 3.76 / 389.0 * 6388 / 3600.0]}},
    token=THIRD_TOKEN)
call("POST", f"/api/v1/agent/projects/{ORION}/join", {}, token=THIRD_TOKEN)

RIGS = [("wide", WIDE_TOKEN), ("narrow", NARROW_TOKEN), ("third", THIRD_TOKEN)]
visited = {name: {} for name, _ in RIGS}          # name -> cell index -> nights
tasks_by = {}


def shoot(name, token, night):
    """A rig does its list for the night and reports every panel of it."""
    task_ = held(token, night)
    tasks_by[name] = task_
    rows = []
    for index in task_["share"]:
        visited[name].setdefault(index, []).append(night)
        cell = task_["cells"][index]
        for filter_name, count in task_["visit"]["frames"].items():
            exposure = next(f["exposure"] for f in task_["filters"]
                            if f["filter"] == filter_name)
            rows.append({
                "task": task_["id"], "night": night, "panel": str(index),
                "filterName": filter_name, "frames": count,
                "seconds": count * exposure, "exposure": exposure,
                "footprint": {"ra": cell["ra"], "dec": cell["dec"],
                              "width": cell["width"], "height": cell["height"]},
                "scale": 2.0, "hfr": 2.5, "guideRms": 0.7, "bandpass": 3.0})
    _, answer = call("POST", "/api/v1/agent/report", {"contributions": rows},
                     token=token)
    return task_, all(r["accepted"] for r in answer["recorded"])


NIGHTS = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
clashes = []
accepted = True
per_night = {}
for night in NIGHTS:
    lists = {}
    for name, token in RIGS:
        task_, ok = shoot(name, token, night)
        accepted = accepted and ok
        lists[name] = sky(task_, task_["share"])
    per_night[night] = lists
    for i, (a, _) in enumerate(RIGS):
        for b, _ in RIGS[i + 1:]:
            common = overlap(lists[a], lists[b])
            if common > 0:
                clashes.append((night, a, b, round(common, 1)))

case("every night's reports are accepted, panel by panel", accepted)
case("no two rigs are sent to the same sky on the same night",
     not clashes, str(clashes) if clashes else "no clashes over four nights")

# Evenness: over the nights, each rig is sent everywhere - as far as its
# nights reach. The narrow camera has a hundred and fifty cells and does
# three a night; four nights cannot cover that, and what matters for it is
# that it is never sent back to one before it has been to the rest.
for name, _ in RIGS:
    cells_ = len(tasks_by[name]["cells"])
    visits = sum(len(nights) for nights in visited[name].values())
    seen = len(visited[name])
    case(f"{name} has, over four nights, been sent across the field",
         seen >= 0.8 * min(cells_, visits),
         f"{seen} of its {cells_} cells in {visits} visits")
    twice = max(len(nights) for nights in visited[name].values())
    least = min((len(visited[name].get(i, [])) for i in range(cells_)),
                default=0)
    # The rig dealt first each night gets exactly this; the ones after it
    # take what the first has left, and with ten of twelve panels spoken for
    # a night can be a visit behind.
    slack = 1 if name == "wide" else 2
    case(f"...and never to one panel twice before the rest once",
         twice - least <= slack, f"most {twice}, least {least}")

# The depth map drives it: what each rig is sent to on night two is what it
# was not sent to on night one.
first, second_ = per_night[NIGHTS[0]]["wide"], per_night[NIGHTS[1]]["wide"]
case("the wide rig's second night is the sky its first night was not",
     overlap(first, second_) == 0,
     f"{overlap(first, second_):.1f} square degrees repeated")

# And a list holds within a night, however much comes in from the others.
before = held(WIDE_TOKEN, NIGHTS[-1])
after = held(WIDE_TOKEN, NIGHTS[-1])
case("a rig's list for the night does not move while it is shooting it",
     before["share"] == after["share"] and before["version"] == after["version"])

# Panels that are finished are left alone. Push one cell of the wide rig's
# tiling past the project's ten hours and it drops out of the next night.
_, wide_now = call("GET", f"/api/v1/agent/task?night={NIGHTS[-1]}", token=WIDE_TOKEN)
wide_task = next(t for t in wide_now["tasks"] if t["project"] == ORION)
done_cell = wide_task["cells"][0]
call("POST", "/api/v1/agent/report", {"contributions": [{
    "task": wide_task["id"], "night": "2026-10-05", "panel": "0",
    "filterName": "Ha", "frames": 200, "seconds": 60000, "exposure": 300,
    "footprint": {"ra": done_cell["ra"], "dec": done_cell["dec"],
                  "width": done_cell["width"], "height": done_cell["height"]},
    "scale": 2.0, "hfr": 2.5, "guideRms": 0.7, "bandpass": 3.0}]},
    token=WIDE_TOKEN)
later = [held(token, "2026-10-06") for _, token in RIGS]
done_region = collab.Region.read(done_cell)
touched = sum(overlap(sky(t, t["share"]), [done_region]) for t in later)
case("a panel at the project's depth is not handed to anybody",
     touched == 0, f"{touched:.2f} of {done_region.area():.1f} square degrees")

# The busier rig still does the most: six hours of panels against three.
case("the rig giving the most hours is sent to the most panels a night",
     len(tasks_by["wide"]["share"]) > len(tasks_by["narrow"]["share"])
     * tasks_by["narrow"]["cells"][0]["width"] * tasks_by["narrow"]["cells"][0]["height"]
     / (tasks_by["wide"]["cells"][0]["width"] * tasks_by["wide"]["cells"][0]["height"]),
     f'wide {len(tasks_by["wide"]["share"])} big panels, '
     f'narrow {len(tasks_by["narrow"]["share"])} small ones')

# A rig with no rotator reports the angle its camera sits at, and the server
# cuts its cells to what that camera really spans: at 268 degrees the wide
# rig's long axis runs north-south, so it gets more cells across than down.
_, fixed_made = call("POST", "/api/v1/agents", {"name": "fixed"}, token=ADMIN)
scale = 206.265 * 3.76 / 389.0
call("POST", "/api/v1/agent/hello", {
    "protocol": collab.PROTOCOL,
    "profile": {"name": "fixed", "focalLength": 389.0, "pixelSize": 3.76,
                "sensorWidth": 9576, "sensorHeight": 6388, "binning": 1,
                "filters": {"Ha": 3.0}, "hoursPerNight": 4.0, "rotation": 268.0,
                "scale": scale, "field": [scale * 9576 / 3600.0,
                                          scale * 6388 / 3600.0]}},
    token=fixed_made["token"])
_, turned = call("POST", f"/api/v1/agent/projects/{ORION}/join", {},
                 token=fixed_made["token"])
cells = turned["task"]["cells"]
across = max(c["column"] for c in cells) + 1
down = max(c["row"] for c in cells) + 1
# ------------------------------------------------- one filter a night each
print("\n-- a three-filter mosaic: one filter a night per telescope --")

# A mosaic night is one filter per telescope, and which filter is the
# collaboration's choice: the one with the most still outstanding once what
# the other rigs are already putting in tonight is counted. Three identical
# rigs on a project wanting equal Ha, OIII and SII are sent one to each; a
# filter at depth is handed to nobody; a rig alone on it rotates night by night.
def enrol_nb(name, hours=6.0):
    _, made = call("POST", "/api/v1/agents", {"name": name}, token=ADMIN)
    scale_ = 206.265 * 3.76 / 389.0
    call("POST", "/api/v1/agent/hello", {
        "protocol": collab.PROTOCOL,
        "profile": {"name": name, "focalLength": 389.0, "pixelSize": 3.76,
                    "sensorWidth": 9576, "sensorHeight": 6388, "binning": 1,
                    "filters": {"Ha": 3.0, "OIII": 3.0, "SII": 3.0},
                    "exposures": {"Ha": 300.0, "OIII": 300.0, "SII": 300.0},
                    "hoursPerNight": hours, "scale": scale_,
                    "field": [scale_ * 9576 / 3600.0, scale_ * 6388 / 3600.0]}},
        token=made["token"])
    return made["token"]


NB = [("nb1", enrol_nb("nb1")), ("nb2", enrol_nb("nb2")), ("nb3", enrol_nb("nb3"))]
_, band = call("POST", "/api/v1/projects", {
    "name": "Three-band", "region": {"ra": 300.0, "dec": 40.0, "width": 16.0, "height": 8.0},
    "requirements": {"filters": {"Ha": 3.0, "OIII": 3.0, "SII": 3.0}},
    "goals": {"Ha": 10.0, "OIII": 10.0, "SII": 10.0}}, token=ADMIN)
BAND = band["project"]["id"]
for _, tok in NB:
    status, _ = call("POST", f"/api/v1/agent/projects/{BAND}/join", {}, token=tok)
    assert status == 200, status


def held_on(token, night, project_id, extra=""):
    _, answer = call("GET", f"/api/v1/agent/task?night={night}{extra}", token=token)
    return next(t for t in answer["tasks"] if t["project"] == project_id)


def report_night(token, task_, night):
    rows = []
    for index in task_["share"]:
        cell = task_["cells"][index]
        for filter_name, count in task_["visit"]["frames"].items():
            exposure = next(f["exposure"] for f in task_["filters"] if f["filter"] == filter_name)
            rows.append({"task": task_["id"], "night": night, "panel": str(index),
                         "filterName": filter_name, "frames": count,
                         "seconds": count * exposure, "exposure": exposure,
                         "footprint": {"ra": cell["ra"], "dec": cell["dec"],
                                       "width": cell["width"], "height": cell["height"]},
                         "scale": 2.0, "hfr": 2.5, "guideRms": 0.7, "bandpass": 3.0})
    call("POST", "/api/v1/agent/report", {"contributions": rows}, token=token)


night1 = {name: held_on(tok, "2026-11-01", BAND) for name, tok in NB}
filters_night1 = {name: list(t["visit"]["frames"]) for name, t in night1.items()}
case("each telescope is given exactly one filter for the night",
     all(len(f) == 1 for f in filters_night1.values()), str(filters_night1))
case("...and the visit names it",
     all(t["visit"].get("filter") == list(t["visit"]["frames"])[0] for t in night1.values()))
case("three telescopes on three equal filters are sent one to each",
     len({f[0] for f in filters_night1.values()}) == 3, str(filters_night1))
case("...with the whole night's frames in that filter",
     all(list(t["visit"]["frames"].values())[0] >= 10 for t in night1.values()),
     str({n: t["visit"]["frames"] for n, t in night1.items()}))

for name, tok in NB:
    report_night(tok, night1[name], "2026-11-01")

# Push OIII to the project's depth everywhere: nobody is sent to it after that.
o_task = night1["nb2"]
done_rows = []
for index, cell in enumerate(o_task["cells"]):
    done_rows.append({"task": o_task["id"], "night": "2026-11-02", "panel": str(index),
                      "filterName": "OIII", "frames": 200, "seconds": 60000, "exposure": 300,
                      "footprint": {"ra": cell["ra"], "dec": cell["dec"],
                                    "width": cell["width"], "height": cell["height"]},
                      "scale": 2.0, "hfr": 2.5, "guideRms": 0.7, "bandpass": 3.0})
call("POST", "/api/v1/agent/report", {"contributions": done_rows}, token=NB[1][1])
night3 = {name: held_on(tok, "2026-11-03", BAND) for name, tok in NB}
filters_night3 = {name: list(t["visit"]["frames"])[0] for name, t in night3.items()}
case("a filter at the project's depth is handed to nobody",
     "O" not in filters_night3.values(), str(filters_night3))
case("...and the telescopes are spread over what is left",
     set(filters_night3.values()) == {"H", "S"}, str(filters_night3))

# A rig alone on a three-filter mosaic: one filter a night, moving on as
# each becomes the deepest.
_, lone = call("POST", "/api/v1/projects", {
    "name": "Lone three-band", "region": {"ra": 20.0, "dec": 60.0, "width": 10.0, "height": 6.0},
    "requirements": {"filters": {"Ha": 3.0, "OIII": 3.0, "SII": 3.0}},
    "goals": {"Ha": 4.0, "OIII": 4.0, "SII": 4.0}}, token=ADMIN)
LONE = lone["project"]["id"]
call("POST", f"/api/v1/agent/projects/{LONE}/join", {}, token=NB[0][1])
lone_filters = []
for night in ("2026-11-10", "2026-11-11", "2026-11-12"):
    t = held_on(NB[0][1], night, LONE)
    lone_filters.append(list(t["visit"]["frames"])[0])
    report_night(NB[0][1], t, night)
case("a telescope alone on a three-filter mosaic shoots one filter a night",
     all(len(held_on(NB[0][1], n, LONE)["visit"]["frames"]) == 1
         for n in ("2026-11-10", "2026-11-11", "2026-11-12")))
case("...and moves to the thinnest filter each night rather than repeating one",
     len(set(lone_filters)) == 3, " -> ".join(lone_filters))

# The Moon decides the kind of filter. A rig tells the server how lit the
# Moon is and how much of its dark hours it is up; a bright Moon makes it a
# night for Ha or SII, a dark one a night for OIII, LRGB - what cannot be
# shot any other time - with the narrowband kept for the moonlit nights.
_, moony = call("POST", "/api/v1/projects", {
    "name": "Moon test", "region": {"ra": 40.0, "dec": 50.0, "width": 10.0, "height": 6.0},
    "requirements": {"filters": {"Ha": 3.0, "OIII": 3.0, "SII": 3.0}},
    "goals": {"Ha": 6.0, "OIII": 6.0, "SII": 6.0}}, token=ADMIN)
MOONY = moony["project"]["id"]
call("POST", f"/api/v1/agent/projects/{MOONY}/join", {}, token=NB[2][1])
dark_night = held_on(NB[2][1], "2026-11-15", MOONY, "&moon=0.05&moonUp=0.1")
case("under no Moon the night goes to what moonlight would spoil: OIII first",
     dark_night["visit"].get("filter") == "O", str(dark_night["visit"]))
bright_night = held_on(NB[2][1], "2026-11-26", MOONY, "&moon=0.95&moonUp=0.9")
case("under a bright Moon the night goes to the narrowband that shoots through it",
     bright_night["visit"].get("filter") in ("H", "S"), str(bright_night["visit"]))
# All of the Moon-proof work done: a bright night is still spent on what is left.
for name in ("Ha", "SII"):
    rows_ = [{"task": bright_night["id"], "night": "2026-11-27", "panel": str(i),
              "filterName": name, "frames": 200, "seconds": 60000, "exposure": 300,
              "footprint": {"ra": c["ra"], "dec": c["dec"], "width": c["width"], "height": c["height"]},
              "scale": 2.0, "hfr": 2.5, "guideRms": 0.7, "bandpass": 3.0}
             for i, c in enumerate(bright_night["cells"])]
    call("POST", "/api/v1/agent/report", {"contributions": rows_}, token=NB[2][1])
leftover = held_on(NB[2][1], "2026-11-28", MOONY, "&moon=0.95&moonUp=0.9")
case("...and when the narrowband is finished, a bright night still shoots what is left",
     leftover["visit"].get("filter") == "O", str(leftover["visit"]))

# Nothing moves a list under its night - not even a list that looks wrong.
# Whatever the server would deal differently now, it deals the next night.
from server import app as server_app                                 # noqa: E402
old_style = held_on(NB[0][1], "2026-12-01", BAND)
stale = dict(old_style)
stale["visit"] = {"seconds": 18000.0,
                  "frames": {f["filter"]: 10 for f in old_style["filters"]}}
server_app.store.set_task(stale)
again = held_on(NB[0][1], "2026-12-01", BAND)
case("a list dealt for tonight holds for tonight, whatever it looks like",
     again["version"] == stale["version"] and len(again["visit"]["frames"]) > 1,
     f'v{again["version"]}, {again["visit"]["frames"]}')
tomorrow = held_on(NB[0][1], "2026-12-02", BAND)
case("...and is dealt properly, one filter, when the night turns",
     len(tomorrow["visit"]["frames"]) == 1 and tomorrow["version"] > again["version"],
     f'{tomorrow["visit"]["frames"]} v{tomorrow["version"]}')
status, health = call("GET", "/api/v1/health")
case("the health line says which build is running",
     bool(health.get("version")), str(health.get("version")))

# A list made before the rig said anything about its Moon holds for its
# night like any other; the Moon is counted the next night. (The join now
# carries the Moon, so a blind deal only happens on an old program.)
_, blind_project = call("POST", "/api/v1/projects", {
    "name": "Blind deal", "region": {"ra": 60.0, "dec": 30.0, "width": 10.0, "height": 6.0},
    # OIII first in the project's order, so a deal that knows nothing of the
    # Moon - equal progress, tie to the first - lands on it.
    "requirements": {"filters": {"OIII": 3.0, "Ha": 3.0}},
    "goals": {"OIII": 6.0, "Ha": 6.0}}, token=ADMIN)
BLIND = blind_project["project"]["id"]
call("POST", f"/api/v1/agent/projects/{BLIND}/join", {}, token=NB[1][1])
blind = held_on(NB[1][1], "2026-12-05", BLIND)
case("dealt with no word of the Moon, the night goes to what moonlight would spoil",
     blind["visit"].get("filter") == "O" and "moon" not in blind["visit"], str(blind["visit"]))
told = held_on(NB[1][1], "2026-12-05", BLIND, "&moon=0.9&moonUp=0.9")
case("...and holds for the night even when the Moon is reported later",
     told["visit"].get("filter") == "O" and told["version"] == blind["version"], str(told["visit"]))
next_night = held_on(NB[1][1], "2026-12-06", BLIND, "&moon=0.9&moonUp=0.9")
case("...the next night is dealt with the Moon in mind",
     next_night["visit"].get("filter") == "H" and next_night["version"] > blind["version"],
     str(next_night["visit"]))

# Joining twice is one share. The second press hands back the task the rig
# already holds, and a rig that somehow holds two is left with the newest.
_, first_join = call("POST", f"/api/v1/agent/projects/{BLIND}/join", {}, token=NB[2][1])
_, second_join = call("POST", f"/api/v1/agent/projects/{BLIND}/join", {}, token=NB[2][1])
case("joining a project twice hands back the share already held",
     second_join.get("alreadyJoined") is True
     and second_join["task"]["id"] == first_join["task"]["id"])
dupe = dict(first_join["task"])
dupe.update({"id": "dupe-task", "issued": float(dupe.get("issued") or 0.0) - 100.0})
server_app.store.add_task(dupe)
_, listing = call("GET", "/api/v1/agent/task?night=2026-12-06", token=NB[2][1])
live = [t for t in listing["tasks"] if t["project"] == BLIND and t["state"] in ("offered", "accepted")]
retired = server_app.store.task("dupe-task")
case("a rig holding two shares of one project is left with the newest",
     len(live) == 1 and live[0]["id"] == first_join["task"]["id"]
     and retired["state"] == "superseded",
     f'{len(live)} live, dupe is {retired["state"]}')

# ------------------------------------------------------- one target, one spot
print("\n-- a single-target collaboration --")

# One object, and everybody points at it. The narrow camera would tile the
# framer's 5.3 by 3.5 degree field into a dozen cells as a mosaic; as a single
# target it gets one cell, its own frame, centred on the object.
_, spot = call("POST", "/api/v1/projects", {
    "name": "Horsehead", "kind": "single",
    "region": {"ra": 85.25, "dec": -2.46, "width": 5.3, "height": 3.5,
               "rotation": 12.0},
    "requirements": {"filters": {"Ha": 3.0}}, "goals": {"Ha": 8.0}},
    token=ADMIN)
SPOT = spot["project"]["id"]
case("a project can be started as one target",
     spot["project"]["payload"].get("kind") == "single")
_, browse = call("GET", "/api/v1/agent/projects", token=NARROW_TOKEN)
case("...and says so when browsed",
     next(p for p in browse["projects"] if p["id"] == SPOT)["kind"] == "single")

status, one = call("POST", f"/api/v1/agent/projects/{SPOT}/join", {},
                   token=NARROW_TOKEN)
narrow_field = 206.265 * 3.76 / 1000.0 * 6000 / 3600.0
case("a narrow camera joining it is not sent to mosaic the framer's field",
     status == 200 and len(one["task"]["cells"]) == 1,
     f'{len(one["task"]["cells"])} cell(s)')
case("...its one cell is its own frame, centred on the object",
     abs(one["task"]["cells"][0]["width"] - narrow_field) < 1e-6
     and abs(one["task"]["cells"][0]["ra"] - 85.25) < 1e-9,
     f'{one["task"]["cells"][0]["width"]:.3f} degrees wide at '
     f'{one["task"]["cells"][0]["ra"]:.2f}')
case("...and the task says it is a single target",
     one["task"].get("kind") == "single")
tonight_one = call("GET", "/api/v1/agent/task?night=2026-10-08",
                   token=NARROW_TOKEN)[1]
spot_task = next(t for t in tonight_one["tasks"] if t["project"] == SPOT)
case("...dealt that one cell for the night, with the whole night on it",
     spot_task["share"] == [0]
     and spot_task["visit"]["frames"].get("H", 0) * 300 >= 3 * 3600 * 0.9,
     f'share {spot_task["share"]}, {spot_task["visit"]["frames"]} frames')

# A single target's depth map shows the union of everybody's fields of view
# as they were really turned. A 2 by 0.5 degree camera at a position angle
# of 90 covers half a degree east-west and two degrees north-south; credited
# north-up it put its seconds beside the field instead of above and below.
# The map also reaches beyond the region to take the frames around it in,
# says which cells are inside the box, and hands back the frames themselves.
call("POST", "/api/v1/agent/report", {"contributions": [{
    "task": one["task"]["id"], "night": "2026-10-08", "panel": "0", "filterName": "Ha",
    "frames": 12, "seconds": 3600, "exposure": 300,
    "footprint": {"ra": 85.25, "dec": -2.46, "width": 2.0, "height": 0.5, "rotation": 90.0},
    "scale": 2.0, "hfr": 2.5, "guideRms": 0.7, "bandpass": 3.0}]}, token=NARROW_TOKEN)
_, spot_map = call("GET", f"/api/v1/agent/projects/{SPOT}/depth", token=NARROW_TOKEN)


def cell_near(grid_, dx, dy):
    """The cell whose centre is nearest `dx` degrees east and `dy` north of the region's centre."""
    import math as _m
    cosine = _m.cos(_m.radians(grid_["region"]["dec"]))
    best, best_d = None, None
    for i, c in enumerate(grid_["cells"]):
        x = (((c["ra"] - grid_["region"]["ra"] + 180.0) % 360.0) - 180.0) * cosine
        y = c["dec"] - grid_["region"]["dec"]
        d = (x - dx) ** 2 + (y - dy) ** 2
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


north = spot_map["seconds"]["H"][cell_near(spot_map, 0.0, 0.8)]
east = spot_map["seconds"]["H"][cell_near(spot_map, 0.8, 0.0)]
case("a frame turned ninety degrees credits the cells above and below the field, not beside it",
     north > 0 and east == 0, f"north {north:.0f}s, east {east:.0f}s")
case("...the single target's map is a fine grid that says which cells are inside the region",
     spot_map["kind"] == "single" and len(spot_map["cells"]) > 1
     and any(c["inside"] for c in spot_map["cells"]) and spot_map["columns"] * spot_map["rows"] == len(spot_map["cells"]),
     f'{len(spot_map["cells"])} cells')
case("...and hands back each camera's field as it was shot, with its angle and its hours",
     len(spot_map["footprints"]) == 1 and spot_map["footprints"][0]["rotation"] == 90.0
     and spot_map["footprints"][0]["seconds"] == 3600 and spot_map["footprints"][0]["filter"] == "H",
     str(spot_map["footprints"])[:160])

# --------------------------------------------- changing one after it started
print("\n-- changing a project after it started --")

status, changed = call("PUT", f"/api/v1/projects/{ORION}", {
    "requirements": {"filters": {"Ha": 7.0}, "maxHfr": 4.0},
    "goals": {"Ha": 25.0}, "notes": "loosened: 7 nm is fine after all"},
    token=ADMIN)
case("the coordinator can change what a project asks for", status == 200)
case("...and only what was sent moves, filters in the one spelling",
     changed["project"]["payload"]["requirements"]["filters"] == {"H": 7.0}
     and changed["project"]["payload"]["goals"] == {"H": 25.0}
     and abs(changed["project"]["payload"]["region"]["width"] - 16.0) < 1e-6,
     "region kept, filters and goals replaced")

# Every rig on it finds out when it asks: the requirements travel with the task.
_, refreshed = call("GET", "/api/v1/agent/task", token=WIDE_TOKEN)
case("a rig on the project sees the new rules on its next poll",
     refreshed["requirementsByProject"][ORION]["maxHfr"] == 4.0,
     str(refreshed["requirementsByProject"][ORION]))

status, _ = call("PUT", f"/api/v1/projects/{ORION}", {"notes": "nope"},
                 token=WIDE_TOKEN)
case("a telescope's token cannot change a project", status == 401)

# A project the admin token started is nobody's but an owner's. Alice, owner
# by Discord id, may change it; a plain member may not. Bob signed out above,
# so a fresh member signs in for this.
status, _ = call("PUT", f"/api/v1/projects/{ORION}", {"notes": "owner by id"},
                 token=ALICE)
case("an owner by Discord id can change a project the admin token started",
     status == 200)
_, _, _, polled = sign_in("code-bob")
status, _ = call("PUT", f"/api/v1/projects/{ORION}", {"notes": "no"},
                 token=polled["token"])
case("...and a plain member cannot", status == 403)

status, closed = call("PUT", f"/api/v1/projects/{ORION}", {"status": "closed"},
                      token=ADMIN)
case("closing takes it off the open list",
     status == 200 and closed["project"]["status"] == "closed")
_, listing = call("GET", "/api/v1/agent/projects", token=WIDE_TOKEN)
case("...so a rig browsing no longer sees it",
     all(p["id"] != ORION for p in listing["projects"]))
case("...while what was collected is still on record",
     call("GET", f"/api/v1/projects/{ORION}")[1]["project"]["status"] == "closed")
call("PUT", f"/api/v1/projects/{ORION}", {"status": "open"}, token=ADMIN)

# A camera that cannot turn is tiled the way its program tiles it: along
# the camera's own axes, the long axis running north-south at 268, so the
# grid climbs the region in more rows than the same camera straight needs.
straight_across = max(c["column"] for c in task["cells"]) + 1
straight_down = max(c["row"] for c in task["cells"]) + 1
case("a camera that cannot turn is tiled the way it really sits",
     all(abs(c["rotation"] - 268.0) < 1e-6 for c in cells) and down > straight_down,
     f"turned {across}x{down}, straight {straight_across}x{straight_down}")
# And in the program's own order, so that cell i on the server is panel
# i + 1 on the rig and a share of neighbouring cells is a walk between
# neighbouring panels.
from astrocontrol import framing as _framing                       # noqa: E402
laid = _framing.mosaic_panels(
    turned["task"]["region"]["ra"], turned["task"]["region"]["dec"],
    scale * 9576 / 3600.0, scale * 6388 / 3600.0, rows=down, columns=across,
    overlap=0.1, position_angle=268.0, align="fixed")
case("...cell for panel, in the order the program walks them",
     len(laid) == len(cells) and all(
         abs(c["ra"] / 15.0 - p["ra"]) < 1e-4 and abs(c["dec"] - p["dec"]) < 1e-4
         for c, p in zip(cells, laid)),
     f"{len(cells)} cells, {len(laid)} panels")

# A task tiled by the old rule - north-up cells of the turned frame's
# bounding box - is cut again on the rig's next poll, without the camera
# having moved, so a project started before the change is not stuck with
# a grid that no longer matches the rig's panels.
from server import app as _server_app                                # noqa: E402
stale_task = dict(_server_app.store.task(turned["task"]["id"]))
old_cells = collab.grid(collab.Region.read(stale_task["region"]),
                        *collab.footprint(scale * 9576 / 3600.0, scale * 6388 / 3600.0, 268.0), 0.1)
stale_task["cells"] = old_cells
stale_task["share"] = [0, 1, 2]
_server_app.store.set_task(stale_task)
_, polled = call("GET", "/api/v1/agent/task?night=2026-12-10", token=fixed_made["token"])
recut = next(t for t in polled["tasks"] if t["id"] == turned["task"]["id"])


def same_centres(a, b):
    return len(a) == len(b) and all(
        abs(x["ra"] - y["ra"]) < 0.01 and abs(x["dec"] - y["dec"]) < 0.01 for x, y in zip(a, b))


# On this region both rules happen to make ten cells, so it is the centres
# that tell them apart: the old grid's are gone and the program's are back.
case("cells cut by the old rule are cut again on the next poll, camera unmoved",
     same_centres(recut["cells"], cells) and not same_centres(recut["cells"], old_cells)
     and all(0 <= i < len(recut["cells"]) for i in recut["share"]),
     f"{len(old_cells)} old cells -> {len(recut['cells'])}, share {recut['share']}")
_, polled_again = call("GET", "/api/v1/agent/task?night=2026-12-10", token=fixed_made["token"])
same_again = next(t for t in polled_again["tasks"] if t["id"] == turned["task"]["id"])
case("...and once they match, nothing is cut again",
     same_again["version"] == recut["version"] and same_again["cells"] == recut["cells"])

# The camera's angle in the profile moves by a few hundredths of a degree
# with every plate solve. That is the same tiling with a wobble, not a new
# rule, and must not re-cut the cells - re-cutting cleared the share and
# restarted the rig's run every couple of hours, all night.
call("POST", "/api/v1/agent/hello", {
    "protocol": collab.PROTOCOL,
    "profile": {"name": "fixed", "focalLength": 389.0, "pixelSize": 3.76,
                "sensorWidth": 9576, "sensorHeight": 6388, "binning": 1,
                "filters": {"Ha": 3.0}, "hoursPerNight": 4.0, "rotation": 268.08,
                "scale": scale, "field": [scale * 9576 / 3600.0, scale * 6388 / 3600.0]}},
    token=fixed_made["token"])
_, wobbled = call("GET", "/api/v1/agent/task?night=2026-12-10", token=fixed_made["token"])
wobble_task = next(t for t in wobbled["tasks"] if t["id"] == turned["task"]["id"])
case("a solve's worth of wobble in the camera angle does not re-cut the cells",
     wobble_task["version"] == recut["version"] and wobble_task["cells"] == recut["cells"]
     and wobble_task["share"] == recut["share"],
     f'v{recut["version"]} -> v{wobble_task["version"]}')

# --------------------------------------- dealt at the rig's own exposures
# A share is dealt at the exposure each rig shoots that filter at - what its
# darks are built for - so every light it takes can be calibrated. A default
# outside what the project takes is refused at the door, naming the filter.
_, strict = call("POST", "/api/v1/projects", {
    "name": "Long subs only",
    "region": {"ra": 120.0, "dec": 20.0, "width": 1.0, "height": 1.0, "rotation": 0.0},
    "requirements": {"filters": {"Ha": 3.0}, "minExposure": 300, "maxExposure": 900},
    "goals": {"Ha": 4.0},
}, token=ADMIN)
STRICT = strict["project"]["id"]
LONG_TOKEN, _ = enrol("long-subs", 389.0, 9576, 6388, 4.0)
status, dealt = call("POST", f"/api/v1/agent/projects/{STRICT}/join",
                     {"exposures": {"ha": 600.0, "L": 120.0}}, token=LONG_TOKEN)
case("a share is dealt at the rig's own exposure for the filter",
     status == 200 and dealt["task"]["filters"][0]["exposure"] == 600.0,
     str(dealt.get("task", {}).get("filters") or dealt))
SHORT_TOKEN, _ = enrol("short-subs", 389.0, 9576, 6388, 4.0)
status, refused = call("POST", f"/api/v1/agent/projects/{STRICT}/join",
                       {"exposures": {"H": 120.0}}, token=SHORT_TOKEN, expect=409)
case("a default the project will not take is refused, naming the filter",
     status == 409 and "H at 120s" in str(refused) and "Equipment" in str(refused),
     str(refused))
NONE_TOKEN, _ = enrol("no-defaults", 389.0, 9576, 6388, 4.0)
status, middle = call("POST", f"/api/v1/agent/projects/{STRICT}/join", {},
                      token=NONE_TOKEN)
case("a rig that names no exposure still gets the middle of the range",
     status == 200 and middle["task"]["filters"][0]["exposure"] == 600.0,
     str(middle.get("task", {}).get("filters")))

# A rig takes part in the filters it carries. Three RASAs with one fixed
# filter each were turned away from a project wanting H, O, R, G and B -
# which wanted exactly what they had - and a rig that did get in was dealt
# every filter of the project, carried or not.
_, lrgb = call("POST", "/api/v1/projects", {
    "name": "Five filters", "kind": "single",
    "region": {"ra": 10.68, "dec": 41.27, "width": 3.0, "height": 2.0, "rotation": 0.0},
    "requirements": {"filters": {"H": 5.0, "O": 5.0, "R": None, "G": None, "B": None}},
    "goals": {"H": 10.0, "O": 10.0, "R": 10.0, "G": 10.0, "B": 10.0},
}, token=ADMIN)
FIVE = lrgb["project"]["id"]
_, rasa = call("POST", "/api/v1/agents", {"name": "rasa-r"}, token=ADMIN)
call("POST", "/api/v1/agent/hello", {
    "protocol": collab.PROTOCOL,
    "profile": {"name": "rasa-r", "focalLength": 620.0, "pixelSize": 3.76,
                "sensorWidth": 9576, "sensorHeight": 6388, "binning": 1,
                "filters": {"R": None}, "hoursPerNight": 6.0,
                "scale": 206.265 * 3.76 / 620.0,
                "field": [206.265 * 3.76 / 620.0 * 9576 / 3600.0,
                          206.265 * 3.76 / 620.0 * 6388 / 3600.0]}},
    token=rasa["token"])
status, only_r = call("POST", f"/api/v1/agent/projects/{FIVE}/join",
                      {"exposures": {"R": 120.0}}, token=rasa["token"])
dealt_filters = [f["filter"] for f in (only_r.get("task") or {}).get("filters") or []]
case("a rig with one fixed filter joins a five-filter project",
     status == 200, str(only_r)[:120])
case("...and is dealt that filter alone, at its own exposure",
     dealt_filters == ["R"] and only_r["task"]["filters"][0]["exposure"] == 120.0,
     str((only_r.get("task") or {}).get("filters")))
_, r_night = call("GET", f"/api/v1/agent/task?night=2026-10-07&project={FIVE}",
                  token=rasa["token"])
r_task = next((t for t in (r_night.get("tasks") or [r_night.get("task") or {}])
               if (t or {}).get("project") == FIVE), r_night.get("task") or {})
r_frames = ((r_task or {}).get("visit") or {}).get("frames") or {}
case("...and tonight's deal for it names no other filter",
     set(r_frames) <= {"R"} and r_frames.get("R", 0) > 0, str(r_frames))

# A single field's night goes to the thinnest filter first. The M31 test
# collaboration sat at a third of the depth in blue for a week: every rig's
# night was split evenly by the goals, blue came last in the order, and dawn
# took it. Here H, O, R and G are eight hours deep of a ten-hour goal and B
# is empty, so a two-hour night is all blue, the next rig the same night is
# dealt around that, and a long night still puts blue first.
_, lopsided = call("POST", "/api/v1/projects", {
    "name": "Lopsided", "kind": "single",
    "region": {"ra": 10.68, "dec": 41.27, "width": 3.0, "height": 2.0, "rotation": 0.0},
    "requirements": {"filters": {"H": None, "O": None, "R": None, "G": None, "B": None}},
    "goals": {"H": 10.0, "O": 10.0, "R": 10.0, "G": 10.0, "B": 10.0},
}, token=ADMIN)
LOPSIDED = lopsided["project"]["id"]


def enrol_five(name, hours):
    _, made = call("POST", "/api/v1/agents", {"name": name}, token=ADMIN)
    scale_ = 206.265 * 3.76 / 620.0
    call("POST", "/api/v1/agent/hello", {
        "protocol": collab.PROTOCOL,
        "profile": {"name": name, "focalLength": 620.0, "pixelSize": 3.76,
                    "sensorWidth": 9576, "sensorHeight": 6388, "binning": 1,
                    "filters": {"H": None, "O": None, "R": None, "G": None, "B": None},
                    "exposures": {"H": 300.0, "O": 300.0, "R": 300.0, "G": 300.0, "B": 300.0},
                    "hoursPerNight": hours, "scale": scale_,
                    "field": [scale_ * 9576 / 3600.0, scale_ * 6388 / 3600.0]}},
        token=made["token"])
    return made["token"]


deep_token = enrol_five("deep-already", 2.0)
_, deep_join = call("POST", f"/api/v1/agent/projects/{LOPSIDED}/join", {}, token=deep_token)
deep_cell = deep_join["task"]["cells"][0]
_, _ = call("POST", "/api/v1/agent/report", {"contributions": [
    {"task": deep_join["task"]["id"], "night": "2026-10-01", "filterName": name,
     "frames": 96, "seconds": 8 * 3600, "exposure": 300,
     "footprint": {"ra": deep_cell["ra"], "dec": deep_cell["dec"],
                   "width": deep_cell["width"] * 3, "height": deep_cell["height"] * 3},
     "scale": 1.25, "hfr": 2.5, "guideRms": 0.7}
    for name in ("H", "O", "R", "G")]}, token=deep_token)
_, deep_night = call("GET", f"/api/v1/agent/task?night=2026-10-09&project={LOPSIDED}",
                     token=deep_token)
deep_task = next(t for t in deep_night["tasks"] if t["project"] == LOPSIDED)
deep_frames = deep_task["visit"]["frames"]
case("a short night on a lopsided field is spent entirely on the thin filter, and fits the night",
     list(deep_frames) == ["B"] and 20 <= deep_frames["B"] <= 24, str(deep_frames))
case("...and the task's filters are put in the night's order, the thin one first",
     [f["filter"] for f in deep_task["filters"]][0] == "B",
     str([f["filter"] for f in deep_task["filters"]]))

second_token = enrol_five("second-tonight", 2.0)
_, second_join = call("POST", f"/api/v1/agent/projects/{LOPSIDED}/join", {"night": "2026-10-09"},
                      token=second_token)
_, second_night = call("GET", f"/api/v1/agent/task?night=2026-10-09&project={LOPSIDED}",
                       token=second_token)
second_task = next(t for t in second_night["tasks"] if t["project"] == LOPSIDED)
case("the next rig dealt the same night is sent to blue as well, since it is still thinnest",
     list(second_task["visit"]["frames"]) == ["B"], str(second_task["visit"]["frames"]))

long_token = enrol_five("long-night", 9.0)
_, long_join = call("POST", f"/api/v1/agent/projects/{LOPSIDED}/join", {"night": "2026-10-09"},
                    token=long_token)
_, long_night = call("GET", f"/api/v1/agent/task?night=2026-10-09&project={LOPSIDED}",
                     token=long_token)
long_task = next(t for t in long_night["tasks"] if t["project"] == LOPSIDED)
long_frames = long_task["visit"]["frames"]
case("a long night still starts with blue and gives it the most, then levels the others up",
     list(long_frames)[0] == "B" and long_frames["B"] == max(long_frames.values())
     and len(long_frames) > 1 and all(n >= 10 for n in long_frames.values()),
     str(long_frames))

# Thinnest is measured against the goal. Asked for five hundred hours of H
# and a hundred of B, a field with twenty of H and four of B is further
# behind in H (4%) than in B (4%... just), and the night goes there.
_, steep = call("PUT", f"/api/v1/projects/{LOPSIDED}", {
    "goals": {"H": 500.0, "O": 10.0, "R": 10.0, "G": 10.0, "B": 10.0}}, token=ADMIN)
steep_token = enrol_five("steep-goals", 2.0)
_, steep_join = call("POST", f"/api/v1/agent/projects/{LOPSIDED}/join", {"night": "2026-10-10"},
                     token=steep_token)
_, steep_night = call("GET", f"/api/v1/agent/task?night=2026-10-10&project={LOPSIDED}",
                      token=steep_token)
steep_task = next(t for t in steep_night["tasks"] if t["project"] == LOPSIDED)
case("behind is measured against the goal: a filter asked for fifty times more comes first",
     list(steep_task["visit"]["frames"])[0] == "H", str(steep_task["visit"]["frames"]))
call("PUT", f"/api/v1/projects/{LOPSIDED}", {
    "goals": {"H": 10.0, "O": 10.0, "R": 10.0, "G": 10.0, "B": 10.0}}, token=ADMIN)

# A project that adds a filter deals it to the rigs already on it. Their
# filters were fixed the night they joined, and a project that added
# luminance a week later never dealt it to anyone until they left and
# joined again.
_, _ = call("PUT", f"/api/v1/projects/{LOPSIDED}", {
    "requirements": {"filters": {"H": None, "O": None, "R": None, "G": None, "B": None, "L": None}},
    "goals": {"H": 10.0, "O": 10.0, "R": 10.0, "G": 10.0, "B": 10.0, "L": 10.0}}, token=ADMIN)
lum_token = enrol_five("gets-luminance", 2.0)
call("POST", "/api/v1/agent/hello", {
    "protocol": collab.PROTOCOL,
    "profile": {"name": "gets-luminance", "focalLength": 620.0, "pixelSize": 3.76,
                "sensorWidth": 9576, "sensorHeight": 6388, "binning": 1,
                "filters": {"H": None, "O": None, "R": None, "G": None, "B": None, "L": None},
                "exposures": {"H": 300.0, "O": 300.0, "R": 300.0, "G": 300.0, "B": 300.0, "L": 120.0},
                "hoursPerNight": 2.0, "scale": 206.265 * 3.76 / 620.0,
                "field": [206.265 * 3.76 / 620.0 * 9576 / 3600.0, 206.265 * 3.76 / 620.0 * 6388 / 3600.0]}},
    token=lum_token)
# The long-night rig carries L too, it just never said so; the one that
# does not carry it (the two-hour rig) must not be dealt it.
call("POST", "/api/v1/agent/hello", {
    "protocol": collab.PROTOCOL,
    "profile": {"name": "long-night", "focalLength": 620.0, "pixelSize": 3.76,
                "sensorWidth": 9576, "sensorHeight": 6388, "binning": 1,
                "filters": {"H": None, "O": None, "R": None, "G": None, "B": None, "L": None},
                "hoursPerNight": 9.0, "scale": 206.265 * 3.76 / 620.0,
                "field": [206.265 * 3.76 / 620.0 * 9576 / 3600.0, 206.265 * 3.76 / 620.0 * 6388 / 3600.0]}},
    token=long_token)
_, lum_night = call("GET", f"/api/v1/agent/task?night=2026-10-11&project={LOPSIDED}", token=long_token)
long_now = next(t for t in lum_night["tasks"] if t["project"] == LOPSIDED)
# Blue and luminance are both at nothing of their goals here, so they share
# the night; the point is that luminance is in it at all.
case("a rig already on a project is dealt a filter the project added later, at the project's exposure",
     any(f["filter"] == "L" and f["exposure"] == 300.0 for f in long_now["filters"])
     and long_now["visit"]["frames"].get("L", 0) >= 10,
     f'{[f["filter"] for f in long_now["filters"]]} {long_now["visit"]["frames"]}')
_, no_lum = call("GET", f"/api/v1/agent/task?night=2026-10-11&project={LOPSIDED}", token=deep_token)
deep_now = next(t for t in no_lum["tasks"] if t["project"] == LOPSIDED)
case("...and a rig that does not carry the new filter is not dealt it",
     not any(f["filter"] == "L" for f in deep_now["filters"]),
     str([f["filter"] for f in deep_now["filters"]]))
_, lum_join = call("POST", f"/api/v1/agent/projects/{LOPSIDED}/join", {"exposures": {"L": 120.0}},
                   token=lum_token)
case("...and a rig joining now gets it at its own exposure",
     next((f["exposure"] for f in lum_join["task"]["filters"] if f["filter"] == "L"), None) == 120.0,
     str(lum_join["task"]["filters"]))

# The coordinator can make every rig's list for tonight be dealt afresh:
# the day the dealing rules changed, the lists already made for that night
# held, made the old way, and would have held all night.
status, _ = call("POST", f"/api/v1/projects/{LOPSIDED}/redeal", {}, token=deep_token)
case("a telescope cannot make a project deal everyone afresh", status == 401)
status, forgotten = call("POST", f"/api/v1/projects/{LOPSIDED}/redeal", {}, token=ADMIN)
case("the coordinator can, and every live list's night is forgotten",
     status == 200 and forgotten.get("forgotten") >= 3, str(forgotten))
_, again_night = call("GET", f"/api/v1/agent/task?night=2026-10-09&project={LOPSIDED}",
                      token=long_token)
again_task = next(t for t in again_night["tasks"] if t["project"] == LOPSIDED)
case("...and a rig's next ask is dealt again for its night",
     again_task.get("assignedNight") == "2026-10-09" and list(again_task["visit"]["frames"])[0] == "B",
     f'{again_task.get("assignedNight")} {again_task["visit"]["frames"]}')

# ------------------------------------------------- who is on the sky
# Every check-in may say where the telescope points and what it is doing;
# the group sees each other on the chart, and each project says how many
# telescopes are on it and how many are about tonight.
status, _ = call("POST", "/api/v1/agent/hello", {
    "protocol": collab.PROTOCOL, "profile": PROFILE,
    "presence": {"ra": 5.5883, "dec": -5.39, "state": "imaging", "target": "M42"}},
    token=AGENT_TOKEN)
case("a check-in can carry where the telescope points", status == 200)
status, who = call("GET", "/api/v1/presence", token=LONG_TOKEN)
me = next((t for t in who.get("telescopes", []) if t["id"] == AGENT_ID), None)
case("everybody can see who is on the sky",
     status == 200 and me is not None and me["online"] is True
     and abs(me["ra"] - 5.5883) < 1e-6 and me["target"] == "M42" and me["state"] == "imaging",
     str(me))
case("...and how many telescopes are online", who.get("online", 0) >= 2, str(who.get("online")))

# ------------------------------------------------- who has contributed
# The opened card on the Collab tab: each person's accepted hours by filter,
# with their telescopes, grouped by the person rather than the telescope.
status, who_did = call("GET", f"/api/v1/agent/projects/{PROJECT}/people", token=AGENT_TOKEN)
case("a project lists who has contributed", status == 200 and len(who_did["people"]) >= 1,
     str(who_did)[:160])
top = who_did["people"][0] if who_did.get("people") else {}
case("...with accepted hours by filter and the telescope that shot them",
     top.get("hours", 0) > 0 and any(k.upper().startswith("O") for k in (top.get("byFilter") or {}))
     and len(top.get("telescopes") or []) >= 1 and "avatar" in top and "name" in top,
     f"{top.get('name')}: {top.get('hours')} h, {top.get('byFilter')}, {top.get('telescopes')}")
case("...and the total is the sum of the people",
     abs(who_did["hours"] - sum(p["hours"] for p in who_did["people"])) < 0.01)
status, _ = call("GET", "/api/v1/agent/projects/nonsense/people", token=AGENT_TOKEN, expect=404)
case("...and an unknown project is a 404", status == 404)

# The life behind the numbers: frames accepted and telescopes joining,
# newest first, across every open project, with who did it.
status, feed = call("GET", "/api/v1/agent/activity?hours=240", token=AGENT_TOKEN)
frames_events = [e for e in feed.get("events", []) if e["kind"] == "frames"]
joins = [e for e in feed.get("events", []) if e["kind"] == "joined"]
case("the activity feed lists the frames accepted lately, with who shot them and on what",
     status == 200 and frames_events and all("person" in e and "avatar" in e and e["filter"]
                                             and e["seconds"] > 0 and e["projectName"] for e in frames_events),
     str(frames_events[:1])[:200])
case("...and the telescopes that joined, newest first",
     joins and all(feed["events"][i]["at"] >= feed["events"][i + 1]["at"]
                   for i in range(len(feed["events"]) - 1)),
     f'{len(joins)} joins, {len(feed["events"])} events')
case("...with what the last day brought each project",
     PROJECT in feed.get("projects", {}) and "hoursToday" in feed["projects"][PROJECT]
     and "peopleToday" in feed["projects"][PROJECT], str(feed.get("projects", {}).get(PROJECT)))

# ------------------------------------------------- the sky, for a web page
# Open to anyone, with nothing in it but what a telescope said about
# itself: a page on the observatory's website shows where everybody points.
status, _ = call("POST", "/api/v1/agent/hello", {
    "protocol": collab.PROTOCOL, "profile": PROFILE,
    "presence": {"ra": 5.6, "dec": -5.4, "state": "imaging", "target": "M42"}},
    token=AGENT_TOKEN)
request = urllib.request.Request(f"{BASE}/api/v1/sky?hours=14")
with urllib.request.urlopen(request, timeout=10) as response:
    sky = json.loads(response.read())
    cors = response.headers.get("Access-Control-Allow-Origin")
mine = next((t for t in sky["telescopes"] if t["name"] in (PROFILE.get("name"), "agent", "rig")
             or abs(t["ra"] - 5.6) < 1e-6), None)
case("the sky is public, and answers with permissive CORS",
     cors == "*" and mine is not None and mine["online"] is True, str(mine))
case("...and carries no ids, owners or pictures",
     mine is not None and not any(k in mine for k in ("id", "owner", "ownerName", "avatar", "project")),
     str(sorted(mine.keys())) if mine else "")
case("...and the track holds every pointing the telescope has sent tonight",
     mine is not None and len(mine["trail"]) >= 2
     and abs(mine["trail"][-1][1] - 5.6) < 1e-6 and abs(mine["trail"][-2][1] - 5.5883) < 1e-3,
     str((mine or {}).get("trail")))
for name, kind in (("widget.js", "javascript"), ("stars.json", "json"), ("index.html", "html")):
    with urllib.request.urlopen(f"{BASE}/sky/{name}", timeout=10) as response:
        body, served = response.read(), response.headers.get("Content-Type", "")
    if kind not in served or len(body) < 100:
        break
else:
    body = b"ok"
case("the widget, the stars and the page are served", body != b"" and kind == "html", served)
status, _ = call("GET", "/sky/..%2Fcollab.sqlite")
case("...and nothing else from that folder", status == 404)
# The owner's Discord picture and name travel with the telescope, for the
# chart. An agent enrolled by the owner's token has no signed-in person
# behind it, so its picture is blank rather than a broken link.
case("a telescope carries its owner's name and picture for the chart",
     "avatar" in me and me["avatar"] == "" and "ownerName" in me, str(me))
from server import app as _srv                                       # noqa: E402
case("...a signed-in person's picture is a Discord CDN link",
     _srv._avatar_url({"id": "1001", "avatar": "abc123"})
     == "https://cdn.discordapp.com/avatars/1001/abc123.png?size=64"
     and _srv._avatar_url({"id": "1001", "avatar": "a_moving"}).endswith("a_moving.gif?size=64")
     and _srv._avatar_url({"id": "1001", "avatar": ""}) == "")
status, listing = call("GET", "/api/v1/agent/projects", token=LONG_TOKEN)
strict_card = next((p for p in listing["projects"] if p["id"] == STRICT), {})
case("a project says how many telescopes are on it",
     strict_card.get("participants") == 2
     and strict_card.get("participantsOnline") == 2
     and set(strict_card.get("participantNames") or []) == {"long-subs", "no-defaults"},
     str({k: strict_card.get(k) for k in ("participants", "participantsOnline", "participantNames")}))
status, _ = call("POST", "/api/v1/agent/hello", {"protocol": collab.PROTOCOL,
                                                   "profile": PROFILE}, token=AGENT_TOKEN)
status, who = call("GET", "/api/v1/presence", token=AGENT_TOKEN)
me = next((t for t in who.get("telescopes", []) if t["id"] == AGENT_ID), None)
case("a check-in that says nothing keeps what was said before",
     me is not None and me["target"] == "M42", str(me))
# The chart shows the name the rig gives itself - its equipment profile -
# over the "Telescope 1" it was enrolled under.
call("POST", "/api/v1/agent/hello", {"protocol": collab.PROTOCOL, "profile": PROFILE,
                                     "presence": {"telescope": "fsq106"}}, token=AGENT_TOKEN)
status, who = call("GET", "/api/v1/presence", token=AGENT_TOKEN)
me = next((t for t in who.get("telescopes", []) if t["id"] == AGENT_ID), None)
case("a telescope on the chart carries the name its rig gives itself",
     me is not None and me["name"] == "fsq106" and me["enrolledAs"] == "Telescope 1", str(me))

# ------------------------------------------- the coordinator's night rules
status, ruled = call("POST", "/api/v1/projects", {
    "name": "High and dark",
    "region": {"ra": 100.0, "dec": 10.0, "width": 1.0, "height": 1.0, "rotation": 0.0},
    "requirements": {"filters": {"L": None}, "minAltitude": 40,
                     "minMoonSeparation": 50},
    "goals": {"L": 2.0},
}, token=ADMIN)
case("a project can carry an altitude floor and a Moon distance", status == 200)
status, listing = call("GET", "/api/v1/projects")
shown = next((p for p in listing["projects"] if p["id"] == ruled["project"]["id"]), {})
wants = (shown.get("payload") or {}).get("requirements") or {}
case("...and every rig browsing it sees them",
     wants.get("minAltitude") == 40.0 and wants.get("minMoonSeparation") == 50.0,
     str(wants))

# A join that says which night it is and what the Moon is doing is dealt
# for that night with the Moon in mind, so the first deal is the one the
# night keeps - rather than a blind deal that the first poll replaces under
# a sequence already shooting it.
_, moonjoin = call("POST", "/api/v1/projects", {
    "name": "Join under the Moon", "region": {"ra": 80.0, "dec": 30.0, "width": 10.0, "height": 6.0},
    "requirements": {"filters": {"OIII": 3.0, "Ha": 3.0}},
    "goals": {"OIII": 6.0, "Ha": 6.0}}, token=ADMIN)
MOONJOIN = moonjoin["project"]["id"]
_, joined_bright = call("POST", f"/api/v1/agent/projects/{MOONJOIN}/join",
                        {"night": "2026-12-30", "moon": 0.9, "moonUp": 0.9}, token=NB[2][1])
case("a join that carries the night and a bright Moon is dealt narrowband at once",
     joined_bright["task"]["visit"].get("filter") == "H"
     and joined_bright["task"].get("assignedNight") == "2026-12-30",
     str(joined_bright["task"]["visit"]))
held_after = held_on(NB[2][1], "2026-12-30", MOONJOIN, "&moon=0.9&moonUp=0.9")
case("...and the first poll of that night keeps it",
     held_after["version"] == joined_bright["task"]["version"]
     and held_after["visit"].get("filter") == "H")

# ------------------------------------------- progress is depth everywhere
print("\n-- progress: the goal at every point of the field --")

# Ten hours on one corner of a mosaic is not two thirds of a ten-hour goal.
# Progress is the share of the field at the goal depth, so a frame-sized
# patch at full depth is a small fraction, and the whole region at depth
# is one, whatever the hours add up to.
_, deep = call("POST", "/api/v1/projects", {
    "name": "Depth everywhere", "region": {"ra": 120.0, "dec": 20.0, "width": 12.0, "height": 6.0},
    "requirements": {"filters": {"Ha": 3.0}}, "goals": {"Ha": 2.0}}, token=ADMIN)
DEEP = deep["project"]["id"]
_, deep_join = call("POST", f"/api/v1/agent/projects/{DEEP}/join", {}, token=NB[0][1])
deep_task = deep_join["task"]


def progress_of(project_id):
    _, listing_ = call("GET", "/api/v1/agent/projects", token=NB[0][1])
    return next(p for p in listing_["projects"] if p["id"] == project_id)


empty_p = progress_of(DEEP)["progress"]
case("with nothing shot, nothing of the field is at goal",
     empty_p.get("H", {}).get("atGoal") == 0.0 and empty_p["H"]["average"] == 0.0, str(empty_p))

# Two hours (the whole goal) on a 2 x 2 degree patch: a sixth of the area.
call("POST", "/api/v1/agent/report", {"contributions": [{
    "task": deep_task["id"], "night": "2026-12-20", "panel": "1", "filterName": "Ha",
    "frames": 24, "seconds": 7200, "exposure": 300,
    "footprint": {"ra": 120.0, "dec": 20.0, "width": 2.0, "height": 2.0},
    "scale": 2.0, "hfr": 2.5, "guideRms": 0.7, "bandpass": 3.0}]}, token=NB[0][1])
patch = progress_of(DEEP)
case("the goal on one patch is that patch's share of the field, not the goal's share of the hours",
     0.03 <= patch["progress"]["H"]["atGoal"] <= 0.08 and patch["collected"]["H"] == 2.0,
     f'{patch["progress"]["H"]} vs {patch["collected"]["H"]}h collected')
case("...and the thinnest part of the field is still empty",
     patch["progress"]["H"]["thinnest"] == 0.0)

# The whole region at the goal, from two half-depth passes: done.
for night in ("2026-12-21", "2026-12-22"):
    call("POST", "/api/v1/agent/report", {"contributions": [{
        "task": deep_task["id"], "night": night, "panel": "0", "filterName": "Ha",
        "frames": 12, "seconds": 3600, "exposure": 300,
        "footprint": {"ra": 120.0, "dec": 20.0, "width": 12.0, "height": 6.0},
        "scale": 2.0, "hfr": 2.5, "guideRms": 0.7, "bandpass": 3.0}]}, token=NB[0][1])
whole = progress_of(DEEP)["progress"]["H"]
case("the whole field at the goal is done, however the hours add up",
     whole["atGoal"] == 1.0 and whole["average"] >= 0.99 and whole["thinnest"] >= 0.9, str(whole))

# The map itself: the cells of the region with everybody's seconds on each,
# per filter, for the picture on the Plan tab.
status, depth_map = call("GET", f"/api/v1/agent/projects/{DEEP}/depth", token=NB[0][1])
case("a participant can fetch the depth map of a project",
     status == 200 and depth_map["columns"] * depth_map["rows"] == len(depth_map["cells"])
     and len(depth_map["seconds"]["H"]) == len(depth_map["cells"]),
     f'{depth_map.get("columns")}x{depth_map.get("rows")}, {len(depth_map.get("cells", []))} cells')
case("...and every cell carries the hours the whole field was given",
     all(s >= 2 * 3600 * 0.9 for s in depth_map["seconds"]["H"])
     and depth_map["progress"]["H"]["atGoal"] == 1.0,
     f'min {min(depth_map["seconds"]["H"]):.0f}s')

# ------------------------------------------------- the N.I.N.A. plugin feed
print("\n-- the server as a N.I.N.A. plugin repository --")

# N.I.N.A. is given `<url>/nina` as a plugin repository and fetches
# `<url>/nina/plugins/manifests`: a JSON list of manifests, each naming the
# zip to download. The zip comes from the same server.
status, feed = call("GET", "/nina/plugins/manifests")
case("the server offers the plugin manifests as a list, the newest of each plugin only",
     status == 200 and isinstance(feed, list) and len(feed) == 1
     and feed[0]["Name"] == "Starfront Collaboration" and feed[0]["Version"]["Major"] == "1",
     str(feed)[:120])
case("...and health says which releases are on offer",
     "Starfront Collaboration 1.0.0.0" in call("GET", "/api/v1/health")[1].get("ninaPlugins", []))
request = urllib.request.Request(feed[0]["Installer"]["URL"], method="GET")
with urllib.request.urlopen(request, timeout=10) as response:
    zipped = response.read(), response.headers.get("Content-Type", "")
case("the zip the manifest points at downloads from the server",
     zipped[0].startswith(b"PK") and "zip" in zipped[1], zipped[1])
for wrong in ("notes.txt", "..%2Fcollab.sqlite", "nothing.zip", ".zip"):
    status, _ = call("GET", f"/nina/download/{wrong}")
    if status != 404:
        break
case("...and anything that is not a release is not served", status == 404)
request = urllib.request.Request(f"{BASE}/nina/changelog", method="GET")
with urllib.request.urlopen(request, timeout=10) as response:
    log = response.read().decode("utf-8")
case("the plugin's change log is served for the manifests' link", "## 1.0.0.0" in log)
request = urllib.request.Request(f"{BASE}/nina/guide", method="GET")
with urllib.request.urlopen(request, timeout=10) as response:
    guide = response.read(), response.headers.get("Content-Type", ""), response.headers.get("Content-Disposition", "")
case("the plugin's guide is served as a PDF to read in the browser",
     guide[0].startswith(b"%PDF") and "pdf" in guide[1] and "inline" in guide[2], f"{guide[1]} {guide[2]}")

# ---------------------------------------------------- the Starfront download
print("\n-- the download page --")

# The source is private; the installer is handed out from the server. The
# newest version is the download, by number and not by spelling, so 0.2.35
# beats 0.2.9.
status, latest = call("GET", "/download/latest.json")
case("latest.json names the newest installer by version number",
     status == 200 and latest["version"] == "0.2.35" and latest["file"] == "Starfront-Setup-0.2.35.exe"
     and latest["url"] == "/download/Starfront-Setup-0.2.35.exe" and latest["size"] == 16, str(latest))
case("...and health says which version is on offer",
     call("GET", "/api/v1/health")[1].get("download") == "0.2.35")
request = urllib.request.Request(f"{BASE}/download/latest", method="GET")
with urllib.request.urlopen(request, timeout=10) as response:
    fetched = response.read(), response.headers.get("Content-Disposition", ""), response.url
case("/download/latest fetches that installer, named as a file to save",
     fetched[0].startswith(b"MZ-new") and "Starfront-Setup-0.2.35.exe" in fetched[1]
     and fetched[2].endswith("/download/Starfront-Setup-0.2.35.exe"), str(fetched[1:]))
request = urllib.request.Request(f"{BASE}/download/Starfront-Setup-0.2.9.exe", method="GET")
with urllib.request.urlopen(request, timeout=10) as response:
    older = response.read()
case("an older installer's own link keeps working", older.startswith(b"MZ-old"))
request = urllib.request.Request(f"{BASE}/download", method="GET")
with urllib.request.urlopen(request, timeout=10) as response:
    page = response.read().decode("utf-8"), response.headers.get("Content-Type", "")
case("the download page offers the newest one, with its size, and says where N.I.N.A. users go",
     "text/html" in page[1] and 'href="/download/Starfront-Setup-0.2.35.exe"' in page[0]
     and "Download Starfront 0.2.35 for Windows" in page[0] and f"{BASE}/nina" in page[0]
     and "0.2.9" not in page[0], page[1])
case("...and says nothing of volunteer workers while no worker release is published",
     "Lending a computer" not in page[0] and "collab-worker" not in page[0])
for wrong in ("secrets.txt", "..%2Fcollab.sqlite", "Starfront-Setup-9.9.9.exe", "Starfront-Setup-.exe",
              "Starfront-Setup-0.2.35.exe.bak"):
    status, _ = call("GET", f"/download/{wrong}")
    if status != 404:
        break
case("...and nothing but installers is served from the folder", status == 404, f"{wrong} -> {status}")

# ============================ the worker and hub binaries (collab-worker, collab-hub)
print("\n-- the worker and hub binaries --")


def fetch(path):
    """(status, headers, body) for any path, with no redirects and no exception."""
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


MANIFEST_TEXT = b'{"version": "1.2.3", "contract": "1.27.0", "assets": {}}'
for product in ("collab-worker", "collab-hub"):
    folder = DOWNLOAD_DIR / product
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "latest.json").write_bytes(MANIFEST_TEXT)
    (folder / "latest.json.sig").write_bytes(b"ed25519-signature-" + product.encode())
    (folder / f"{product}-1.2.3-linux-x64.tar.gz").write_bytes(b"\x1f\x8b" + b"tar-" + product.encode())
    (folder / f"{product}-1.2.3-windows-x64.zip").write_bytes(b"PK\x03\x04" + b"zip-" + product.encode())
    (folder / f"{product}-1.2.3-macos-arm64.zip").write_bytes(b"PK\x03\x04" + b"mac-" + product.encode())
    other = "collab-hub" if product == "collab-worker" else "collab-worker"
    prefix = f"/download/{product}"

    status, headers, body = fetch(f"{prefix}/latest.json")
    case(f"{product}: latest.json is served as JSON and never cached",
         status == 200 and body == MANIFEST_TEXT and headers.get("Content-Type", "").startswith("application/json")
         and headers.get("Cache-Control") == "no-cache" and headers.get("Content-Length") == str(len(MANIFEST_TEXT)),
         f"{status} {headers.get('Content-Type')} {headers.get('Cache-Control')}")
    status, headers, body = fetch(f"{prefix}/latest.json.sig")
    case(f"{product}: latest.json.sig is served as bytes and never cached",
         status == 200 and body == b"ed25519-signature-" + product.encode()
         and headers.get("Content-Type") == "application/octet-stream"
         and headers.get("Cache-Control") == "no-cache" and headers.get("Content-Length") == str(len(body)),
         f"{status} {headers.get('Content-Type')} {headers.get('Cache-Control')}")
    for archive in (f"{product}-1.2.3-linux-x64.tar.gz", f"{product}-1.2.3-windows-x64.zip",
                    f"{product}-1.2.3-macos-arm64.zip"):
        status, headers, body = fetch(f"{prefix}/{archive}")
        case(f"{product}: {archive} is served as bytes, immutable for a year, named as a file",
             status == 200 and body.endswith(product.encode())
             and headers.get("Content-Type") == "application/octet-stream"
             and headers.get("Cache-Control") == "public, max-age=31536000, immutable"
             and headers.get("Content-Length") == str(len(body))
             and archive in (headers.get("Content-Disposition") or ""),
             f"{status} {headers.get('Content-Type')} {headers.get('Cache-Control')}")
    for wrong in ("evil.sh", f"{product}-1.0-macos.zip", "../latest.json", "..%2Flatest.json", "%2e%2e%2flatest.json",
                  "collab-hub-1.2.3-plan9.zip", f"{product}-1.2.3-plan9.zip", f"{product}-1.2.3-linux-x64.rar",
                  f"{other}-1.2.3-linux-x64.tar.gz", "latest.json.bak", "latest.json%0A"):
        status, _, _ = fetch(f"{prefix}/{wrong}")
        if status != 404:
            break
    case(f"{product}: only the pattern is served; {wrong} is a 404", status == 404, f"{wrong} -> {status}")
    # The manifest is the one thing a release may not have produced yet.
    (folder / "latest.json").rename(folder / "latest.json.moved")
    status, _, _ = fetch(f"{prefix}/latest.json")
    (folder / "latest.json.moved").rename(folder / "latest.json")
    case(f"{product}: a missing latest.json is a 404", status == 404, str(status))

# The download page lists what a published manifest names, for people who
# install by hand or want to check a hash, but only files that are really
# there: a manifest naming an archive that was never copied up, or a name the
# route would refuse, must not become a link to a 404. The hub's manifest
# above names no assets, so the hub gets no heading.
WIN_SHA = "ab" * 32
(DOWNLOAD_DIR / "collab-worker" / "latest.json").write_text(json.dumps({
    "version": "1.2.3", "contract": "1.27.0", "assets": {
        "windows-x64": {"primary": {"url": "x", "size": 26, "sha256": WIN_SHA,
                                    "filename": "collab-worker-1.2.3-windows-x64.zip"}},
        "linux-x64": {"primary": {"url": "x", "size": 9, "sha256": "cd" * 32,
                                  "filename": "collab-worker-1.2.4-linux-x64.tar.gz"}},
        "macos-arm64": {"primary": {"url": "x", "size": 9, "sha256": "ef" * 32, "filename": "../evil.sh"}}}}),
    encoding="utf-8")
status, _, body = fetch("/download")
page = body.decode("utf-8")
case("the download page offers a published worker by platform, with its SHA-256",
     status == 200 and "Lending a computer to live stacking" in page and "collab-worker 1.2.3" in page
     and 'href="/download/collab-worker/collab-worker-1.2.3-windows-x64.zip"' in page
     and f"SHA-256 {WIN_SHA}" in page and "Collaborations &rarr; Volunteer this machine" in page
     and 'href="/download/collab-worker/latest.json"' in page, page[-1500:])
case("...but not an archive missing from disk, a name the route refuses, or a hub with none",
     "collab-worker-1.2.4" not in page and "evil.sh" not in page and "collab-hub 1.2.3" not in page
     and 'href="/download/collab-hub/latest.json"' not in page, page[-1500:])
(DOWNLOAD_DIR / "collab-worker" / "latest.json").write_text("{not json", encoding="utf-8")
status, _, body = fetch("/download")
case("...and a broken manifest leaves the page as it was, not a 500",
     status == 200 and "Lending a computer" not in body.decode("utf-8"), str(status))
(DOWNLOAD_DIR / "collab-worker" / "latest.json").write_bytes(MANIFEST_TEXT)

# ======================================== a colour camera with a dual-band filter
print("\n-- a colour camera with a dual-band filter --")

# A project wants channels; a filter delivers one or more of them. A one-shot
# colour camera behind a UV/IR cut delivers red, green and blue in one sub,
# and behind an L-eXtreme delivers Ha and OIII at once. It used to be told it
# carried none of the wanted filters, because nothing knew what an
# "L-eXtreme" was, and a night through it would have counted for one
# unknown filter.
_, osc_made = call("POST", "/api/v1/agents", {"name": "osc"}, token=ADMIN)
OSC_TOKEN = osc_made["token"]
status, _ = call("POST", "/api/v1/agent/hello", {
    "protocol": collab.PROTOCOL,
    "profile": {"name": "osc", "focalLength": 400.0, "pixelSize": 3.76, "sensorWidth": 6248,
                "sensorHeight": 4176, "binning": 1, "colour": True, "hoursPerNight": 4.0,
                "filters": {"UV/IR cut": None, "L-eXtreme": 7.0},
                "channels": {"UV/IR cut": ["OSC-RGB"], "L-eXtreme": ["OSC-Ha+OIII"]},
                "exposures": {"UV/IR cut": 120.0, "L-eXtreme": 300.0}, "bortle": 2}},
    token=OSC_TOKEN)
case("a colour camera says what each of its filters delivers, and its sky", status == 200)
_, started = call("POST", "/api/v1/projects", {
    "name": "Veil in everything", "kind": "single",
    "region": {"ra": 313.0, "dec": 31.0, "width": 2.0, "height": 1.5},
    # Mono channels and, kept apart from them, the two colour-camera bands.
    "requirements": {"filters": {"H": 7.0, "O": 7.0, "S": 7.0, "R": None, "G": None, "B": None,
                                 "OSC-RGB": None, "OSC-Ha+OIII": 7.0},
                     "acceptOscRgb": True, "acceptOscNarrowband": True, "maxBortle": 4},
    "goals": {"H": 10.0, "O": 10.0, "S": 10.0, "R": 4.0, "G": 4.0, "B": 4.0,
              "OSC-RGB": 4.0, "OSC-Ha+OIII": 10.0}}, token=ADMIN)
EVERYTHING = started["project"]["id"]
_, browse = call("GET", "/api/v1/agent/projects", token=OSC_TOKEN)
listing = next(p for p in browse["projects"] if p["id"] == EVERYTHING)
filters_check = next(c for c in listing["compatibility"]["checks"] if c["check"] == "filters")
case("...and is told it can give the two OSC bands, through which filters, under a sky that qualifies",
     listing["compatibility"]["ok"] and "L-eXtreme gives OSC-Ha+OIII" in filters_check["detail"]
     and "UV/IR cut gives OSC-RGB" in filters_check["detail"]
     and any(c["check"] == "sky" and c["ok"] for c in listing["compatibility"]["checks"]),
     filters_check["detail"])
status, joined = call("POST", f"/api/v1/agent/projects/{EVERYTHING}/join", {"night": "2026-10-12"},
                      token=OSC_TOKEN)
dealt = joined["task"]
by_name = {f["filter"]: f for f in dealt["filters"]}
case("joining deals it its own filters, each with the band it delivers, at its own exposures",
     status == 200 and sorted(by_name) == ["L-eXtreme", "UV/IR cut"]
     and by_name["L-eXtreme"]["channels"] == ["OSC-Ha+OIII"]
     and by_name["UV/IR cut"]["channels"] == ["OSC-RGB"]
     and by_name["L-eXtreme"]["exposure"] == 300.0 and by_name["UV/IR cut"]["exposure"] == 120.0,
     str(dealt["filters"]))
frames = (dealt.get("visit") or {}).get("frames") or {}
case("...and tonight's frames are named by those filters",
     bool(frames) and set(frames) <= {"L-eXtreme", "UV/IR cut"}, str(frames))

NIGHT = {"task": dealt["id"], "night": "2026-10-12", "panel": "1", "filterName": "L-eXtreme",
         "channels": ["OSC-Ha+OIII"], "frames": 12, "seconds": 3600, "exposure": 300,
         "footprint": {"ra": 313.0, "dec": 31.0, "width": 2.0, "height": 1.5},
         "scale": 1.94, "focalLength": 400.0, "colour": True, "bandpass": 7.0, "hfr": 3.0, "bortle": 2}
status, answer = call("POST", "/api/v1/agent/report", {"contributions": [NIGHT]}, token=OSC_TOKEN)
case("a night through the dual-band is accepted",
     status == 200 and answer["recorded"][0]["accepted"], str(answer["recorded"][0]["verdict"]))
_, browse = call("GET", "/api/v1/agent/projects", token=OSC_TOKEN)
listing = next(p for p in browse["projects"] if p["id"] == EVERYTHING)
got = listing["collected"]
case("...and is logged as OSC-Ha+OIII, apart from the mono Ha and OIII",
     abs(got.get("OSC-Ha+OIII", 0) - 1.0) < 0.01 and not got.get("H") and not got.get("O"), str(got))
case("...with the progress measured per band",
     listing["progress"]["OSC-Ha+OIII"]["average"] > 0 and listing["progress"]["H"]["average"] == 0
     and listing["progress"]["OSC-RGB"]["average"] == 0,
     str({k: v["average"] for k, v in listing["progress"].items()}))
status, answer = call("POST", "/api/v1/agent/report",
                      {"contributions": [{**NIGHT, "frames": 24, "seconds": 7200}]}, token=OSC_TOKEN)
_, browse = call("GET", "/api/v1/agent/projects", token=OSC_TOKEN)
got = next(p for p in browse["projects"] if p["id"] == EVERYTHING)["collected"]
case("...reported again as the night grew, it is the larger figure and not the sum",
     abs(got.get("OSC-Ha+OIII", 0) - 2.0) < 0.01, str(got))
status, answer = call("POST", "/api/v1/agent/report",
                      {"contributions": [{**NIGHT, "panel": "2", "bortle": 6}]}, token=OSC_TOKEN)
case("a night under a brighter sky than the project wants is refused, and told",
     status == 200 and not answer["recorded"][0]["accepted"]
     and "Bortle 6" in answer["recorded"][0]["verdict"]["summary"],
     answer["recorded"][0]["verdict"]["summary"])
strict = call("POST", "/api/v1/projects", {
    "name": "Narrow only", "kind": "single",
    "region": {"ra": 313.0, "dec": 31.0, "width": 2.0, "height": 1.5},
    "requirements": {"filters": {"OSC-Ha+OIII": 5.0}}, "goals": {"OSC-Ha+OIII": 10.0}}, token=ADMIN)[1]
_, browse = call("GET", "/api/v1/agent/projects", token=OSC_TOKEN)
listing = next(p for p in browse["projects"] if p["id"] == strict["project"]["id"])
case("a project wanting 5 nm turns the 7 nm dual-band away, naming it",
     not listing["compatibility"]["ok"] and "L-eXtreme is 7 nm" in listing["compatibility"]["summary"],
     listing["compatibility"]["summary"])
mono_only = call("POST", "/api/v1/projects", {
    "name": "Mono only", "kind": "single",
    "region": {"ra": 313.0, "dec": 31.0, "width": 2.0, "height": 1.5},
    "requirements": {"filters": {"H": 7.0, "O": 7.0}}, "goals": {"H": 10.0, "O": 10.0}}, token=ADMIN)[1]
_, browse = call("GET", "/api/v1/agent/projects", token=OSC_TOKEN)
listing = next(p for p in browse["projects"] if p["id"] == mono_only["project"]["id"])
case("a project listing mono channels only has nothing for a colour camera, and says so",
     not listing["compatibility"]["ok"] and "takes no one-shot colour data" in listing["compatibility"]["summary"],
     listing["compatibility"]["summary"])

# ------------------------------------------------- the files: lights collected
print("\n-- collecting the lights --")


def raw(method, path, data=None, headers=None, expect_json=True):
    """A request with a body that is not JSON, and whatever headers."""
    request = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            return response.status, (json.loads(body.decode() or "{}") if expect_json else body), response.headers
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body.decode() or "{}"), exc.headers
        except ValueError:
            return exc.code, body, exc.headers


FITS = b"SIMPLE  =                    T / conforms to FITS" + b" " * 30
FITS = FITS.ljust(2880, b" ") + bytes(2880)        # one header block, one data block

_, _, _, polled = sign_in("code-bob")
BOB2 = polled["token"]
status, made = call("POST", "/api/v1/projects", {
    "name": "Rosette, files collected", "kind": "single",
    "region": {"ra": 97.98, "dec": 4.95, "width": 2.0, "height": 1.5},
    "requirements": {"filters": {"H": None}}, "goals": {"H": 10.0},
    "collectFiles": True}, token=BOB2)
FILES_PROJECT = made["project"]["id"]
case("a project can be started with its lights collected",
     status == 200 and made["project"]["payload"]["collectFiles"] is True)
_, browse = call("GET", "/api/v1/agent/projects", token=AGENT_TOKEN)
listing = next(p for p in browse["projects"] if p["id"] == FILES_PROJECT)
case("...and the listing says so, with where to send them",
     listing["collectFiles"] is True and listing["filesUrl"].endswith(f"/files#{FILES_PROJECT}"),
     listing["filesUrl"])

# ------------------------------------------- live stacking: the join code
# A collaboration used to name the Rapid Stacker Collab project that stacks
# its lights by a join code pasted from the Collab website. Since T706 the
# collaboration is the only place a project is made (the author's rule): a new
# pasted code is refused, and an old collaboration keeps the code it has,
# read-only. The code is a credential: it is handed out only to telescopes on
# the collaboration and to the owner's own, and never in a public read.
print("\n-- live stacking: the join code --")
status, answer = call("POST", "/api/v1/projects", {
    "name": "Orion, live-stacked", "region": {"ra": 84.0, "dec": 0.0,
                                               "width": 16.0, "height": 8.0},
    "requirements": {"filters": {"Ha": 3.0}}, "goals": {"Ha": 10.0},
    "stackingJoinCode": " h4rt 92bk-wnpc "}, token=BOB2)
case("a new collaboration with a pasted join code is refused, and says what to do instead",
     status == 400 and "no longer taken" in answer.get("detail", "")
     and "Link your Rapid Stacker Collab account" in answer.get("detail", ""), str(answer)[:200])
status, answer = call("POST", "/api/v1/projects", {
    "name": "typo", "region": {"ra": 84.0, "dec": 0.0, "width": 16.0, "height": 8.0},
    "stackingJoinCode": "H4RT-92BK-WNP1"}, token=BOB2)
case("a code that cannot be real is refused", status == 400 and "twelve characters" in answer["detail"],
     str(answer)[:200])
status, answer = call("POST", "/api/v1/projects", {
    "name": "short", "region": {"ra": 84.0, "dec": 0.0, "width": 16.0, "height": 8.0},
    "stackingJoinCode": "H4RT-92BK"}, token=BOB2)
case("...and so is one that is too short", status == 400)
# An old collaboration, as one made before T706 is stored: the pasted code in
# the payload, both ways.
status, made = call("POST", "/api/v1/projects", {
    "name": "Orion, live-stacked", "region": {"ra": 84.0, "dec": 0.0,
                                               "width": 16.0, "height": 8.0},
    "requirements": {"filters": {"Ha": 3.0}}, "goals": {"Ha": 10.0},
    "stacking": {"backend": ""}}, token=BOB2)
STACKED = made["project"]["id"]
from server import app as server_app_                              # noqa: E402
_legacy = dict(server_app_.store.project(STACKED)["payload"])
_legacy.update(stackingJoinCode="H4RT-92BK-WNPC", stacking={"backend": "cloud", "joinCode": "H4RT-92BK-WNPC",
                                                             "apiBase": "", "hubId": "", "visibility": ""})
server_app_.store.set_project(STACKED, _legacy)
case("(setup) an old collaboration carries a pasted code",
     server_app_._stacking_of(server_app_.store.project(STACKED)["payload"])["joinCode"] == "H4RT-92BK-WNPC")
_, public_read = call("GET", f"/api/v1/projects/{STACKED}")
_, public_list = call("GET", "/api/v1/projects")
case("the code is in no public read: neither the project nor the list carries it (T706)",
     "H4RT-92BK-WNPC" not in json.dumps(public_read) and "H4RT-92BK-WNPC" not in json.dumps(public_list)
     and "stacking" in public_read["project"]["payload"], str(public_read["project"]["payload"].get("stacking")))

_, bobs = call("POST", "/api/v1/agents", {"name": "Bob's rig"}, token=BOB2)
_, browse = call("GET", "/api/v1/agent/projects", token=bobs["token"])
listing = next(p for p in browse["projects"] if p["id"] == STACKED)
case("the owner's own telescope is handed the code without joining",
     listing["stackingLinked"] is True and listing["stackingJoinCode"] == "H4RT-92BK-WNPC",
     str(listing.get("stackingJoinCode")))
_, browse = call("GET", "/api/v1/agent/projects", token=AGENT_TOKEN)
listing = next(p for p in browse["projects"] if p["id"] == STACKED)
case("a telescope that is only browsing learns that the lights are stacked, not the code",
     listing["stackingLinked"] is True and listing["stackingJoinCode"] == "",
     str(listing.get("stackingJoinCode")))
STACKER_TOKEN, _ = enrol("stacker", 389.0, 9576, 6388, 6.0)
status, _ = call("POST", f"/api/v1/agent/projects/{STACKED}/join", {}, token=STACKER_TOKEN)
_, browse = call("GET", "/api/v1/agent/projects", token=STACKER_TOKEN)
listing = next(p for p in browse["projects"] if p["id"] == STACKED)
case("a telescope on the collaboration is handed the code",
     status == 200 and listing["stackingJoinCode"] == "H4RT-92BK-WNPC",
     f"join {status}, code {listing.get('stackingJoinCode')!r}")
_, browse = call("GET", "/api/v1/agent/projects", token=AGENT_TOKEN)
listing = next(p for p in browse["projects"] if p["id"] == FILES_PROJECT)
case("a collaboration without one says so", listing["stackingLinked"] is False
     and listing["stackingJoinCode"] == "" and listing["stacking"]["state"] == "")

status, changed = call("PUT", f"/api/v1/projects/{STACKED}", {"notes": "still stacked"}, token=BOB2)
case("an edit that leaves the code out leaves it alone",
     status == 200 and changed["project"]["payload"]["stackingJoinCode"] == "H4RT-92BK-WNPC")
status, changed = call("PUT", f"/api/v1/projects/{STACKED}", {"stackingJoinCode": "h4rt 92bk wnpc"}, token=BOB2)
case("...one that sends the same code back (a plugin's Edit form) changes nothing",
     status == 200 and changed["project"]["payload"]["stackingJoinCode"] == "H4RT-92BK-WNPC")
status, answer = call("PUT", f"/api/v1/projects/{STACKED}", {"stackingJoinCode": "wnpc-h4rt-92bk"}, token=BOB2)
case("...one that sends a different code is refused: the old link is read-only (T706)",
     status == 400 and "no longer taken" in answer.get("detail", ""), str(answer)[:160])
status, _ = call("PUT", f"/api/v1/projects/{STACKED}", {"stackingJoinCode": "nope"}, token=BOB2)
_, browse = call("GET", "/api/v1/agent/projects", token=STACKER_TOKEN)
listing = next(p for p in browse["projects"] if p["id"] == STACKED)
case("...a bad one is refused and the old one stands",
     status == 400 and listing["stackingJoinCode"] == "H4RT-92BK-WNPC")
status, changed = call("PUT", f"/api/v1/projects/{STACKED}", {"stackingJoinCode": ""}, token=BOB2)
_, browse = call("GET", "/api/v1/agent/projects", token=STACKER_TOKEN)
listing = next(p for p in browse["projects"] if p["id"] == STACKED)
case("...and a blank one clears it", status == 200 and changed["project"]["payload"]["stackingJoinCode"] == ""
     and listing["stackingLinked"] is False and listing["stackingJoinCode"] == "")

status, answer, _ = raw("PUT", f"/api/v1/projects/{FILES_PROJECT}/files/M31_Ha_001.fits", FITS,
                        {"Authorization": f"Bearer {ALICE}", "Content-Length": str(len(FITS))})
case("a signed-in person can upload a light", status == 200 and answer["file"]["name"] == "M31_Ha_001.fits"
     and answer["file"]["bytes"] == len(FITS) and answer["already"] is False, str(answer)[:200])
status, answer, _ = raw("PUT", f"/api/v1/projects/{FILES_PROJECT}/files/M31_Ha_001.fits", FITS,
                        {"Authorization": f"Bearer {ALICE}", "Content-Length": str(len(FITS))})
case("...the same file again is not taken twice", status == 200 and answer["already"] is True)
status, answer, _ = raw("PUT", f"/api/v1/projects/{FILES_PROJECT}/files/notes.txt", b"hello",
                        {"Authorization": f"Bearer {ALICE}", "Content-Length": "5"})
case("a file that is not a frame is refused", status == 400 and "not a frame" in answer["detail"])
status, answer, _ = raw("PUT", f"/api/v1/projects/{FILES_PROJECT}/files/fake.fits", b"JFIF" * 100,
                        {"Authorization": f"Bearer {ALICE}", "Content-Length": "400"})
case("...and so is a .fits that is not a FITS inside", status == 400 and "does not start like" in answer["detail"])
status, answer, _ = raw("PUT", f"/api/v1/projects/{FILES_PROJECT}/files/../../escape.fits", FITS,
                        {"Authorization": f"Bearer {ALICE}", "Content-Length": str(len(FITS))})
case("a path in the name goes nowhere", status in (200, 400, 404, 405)
     and not (DATA / "escape.fits").exists() and not (DATA.parent / "escape.fits").exists()
     and not (DATA / "files" / "escape.fits").exists(),
     f"{status} {str(answer)[:80]}")
status, answer, _ = raw("PUT", f"/api/v1/projects/{FILES_PROJECT}/files/M31_Ha_002.fits", FITS,
                        {"Content-Length": str(len(FITS))})
case("nobody uploads without signing in", status == 401)
status, answer, _ = raw("PUT", f"/api/v1/projects/{ORION}/files/M42.fits", FITS,
                        {"Authorization": f"Bearer {ALICE}", "Content-Length": str(len(FITS))})
case("a project not collecting files takes none (unless it is yours)",
     status in (409, 200), str(status))

# A telescope's token uploads under its owner: that is how the plugin will
# send each light as it is saved.
_, mine_rig = call("POST", "/api/v1/agents", {"name": "Bob's rig"}, token=BOB2)
status, answer, _ = raw("PUT", f"/api/v1/projects/{FILES_PROJECT}/files/M31_Ha_002.fits", FITS,
                        {"Authorization": f"Bearer {mine_rig['token']}", "Content-Length": str(len(FITS))})
case("a telescope's token uploads under its owner, named after the telescope",
     status == 200 and answer["file"]["telescope"] == "Bob's rig" and answer["file"]["uploader"] == "Bob",
     str(answer)[:200])

_, listed = call("GET", f"/api/v1/projects/{FILES_PROJECT}/files", token=ALICE)
case("the coordinator (Alice owns the server) sees everybody's files",
     listed["owned"] is True and sorted(f["name"] for f in listed["files"]) == ["M31_Ha_001.fits", "M31_Ha_002.fits"]
     and listed["bytes"] == 2 * len(FITS), str([f["name"] for f in listed["files"]]))
_, _, _, polled = sign_in("code-bob")
BOB3 = polled["token"]
_, listed_bob = call("GET", f"/api/v1/projects/{FILES_PROJECT}/files", token=BOB3)
case("the project's owner sees them too", listed_bob["owned"] is True and len(listed_bob["files"]) == 2)
_, _, landed, polled = sign_in("code-alice")
ALICE = polled["token"]            # a program sign-in replaces the program token
status, got, _ = raw("GET", f"/api/v1/projects/{FILES_PROJECT}/files/1001/light/M31_Ha_001.fits", None,
                     {"Authorization": f"Bearer {BOB3}"}, expect_json=False)
case("...and can download each one", status == 200 and got == FITS)
status, got, _ = raw("GET", f"/api/v1/projects/{FILES_PROJECT}/files/1002/light/M31_Ha_002.fits", None,
                     {"Authorization": f"Bearer {polled['token']}"}, expect_json=False)
case("a person may download their own", status == 200 and got == FITS)
case("...and files live under the files folder by project, person and kind",
     (DATA / "files" / FILES_PROJECT / "1002" / "lights" / "M31_Ha_002.fits").is_file())

# Calibration frames: told apart by the name N.I.N.A. gave them, or by the
# kind the uploader chose, each in its own folder.
status, answer, _ = raw("PUT", f"/api/v1/projects/{FILES_PROJECT}/files/2026-10-12_FLAT_Ha_0001.fits", FITS,
                        {"Authorization": f"Bearer {BOB3}", "Content-Length": str(len(FITS))})
case("a flat is told from its name and filed as one",
     status == 200 and answer["file"]["kind"] == "flat"
     and (DATA / "files" / FILES_PROJECT / "1002" / "flats" / "2026-10-12_FLAT_Ha_0001.fits").is_file(),
     str(answer)[:160])
status, answer, _ = raw("PUT", f"/api/v1/projects/{FILES_PROJECT}/files/2026-10-12_DARKFLAT_Ha_0001.fits", FITS,
                        {"Authorization": f"Bearer {BOB3}", "Content-Length": str(len(FITS))})
case("...a dark flat is not read as a dark", status == 200 and answer["file"]["kind"] == "darkflat")
status, answer, _ = raw("PUT", f"/api/v1/projects/{FILES_PROJECT}/files/frame_0007.fits?kind=bias", FITS,
                        {"Authorization": f"Bearer {BOB3}", "Content-Length": str(len(FITS))})
case("...and an unmarked name takes the kind the uploader chose",
     status == 200 and answer["file"]["kind"] == "bias")
status, answer, _ = raw("PUT", f"/api/v1/projects/{FILES_PROJECT}/files/frame_0007.fits?kind=plate", FITS,
                        {"Authorization": f"Bearer {BOB3}", "Content-Length": str(len(FITS))})
case("...a kind that is not one of the five is refused", status == 400)
_, listed = call("GET", f"/api/v1/projects/{FILES_PROJECT}/files", token=ALICE)
case("the listing says what each file is",
     sorted((f["name"], f["kind"]) for f in listed["files"]) == sorted([
         ("M31_Ha_001.fits", "light"), ("M31_Ha_002.fits", "light"),
         ("2026-10-12_FLAT_Ha_0001.fits", "flat"), ("2026-10-12_DARKFLAT_Ha_0001.fits", "darkflat"),
         ("frame_0007.fits", "bias")]),
     str([(f["name"], f["kind"]) for f in listed["files"]]))
status, info, _ = raw("GET", "/api/v1/files/projects", None, {"Authorization": f"Bearer {ALICE}"})
project_row = next(p for p in info["projects"] if p["id"] == FILES_PROJECT)
case("...and the page's list counts them by kind",
     project_row["kinds"] == {"light": 2, "flat": 1, "darkflat": 1, "bias": 1}, str(project_row["kinds"]))
status, _ = call("DELETE", f"/api/v1/projects/{FILES_PROJECT}/files/frame_0007.fits?kind=bias", token=BOB3)
case("a calibration frame comes off by its kind", status == 200
     and not (DATA / "files" / FILES_PROJECT / "1002" / "bias" / "frame_0007.fits").exists())
status, _ = call("DELETE", f"/api/v1/projects/{FILES_PROJECT}/files/M31_Ha_002.fits", token=BOB3)
_, listed = call("GET", f"/api/v1/projects/{FILES_PROJECT}/files", token=ALICE)
case("a person can take their own file off again",
     status == 200 and "M31_Ha_002.fits" not in [f["name"] for f in listed["files"]]
     and not (DATA / "files" / FILES_PROJECT / "1002" / "lights" / "M31_Ha_002.fits").exists())
_, health = call("GET", "/api/v1/health")
case("health reports what the files folder holds and the room left",
     # Alice's M31_Ha_001 here, the M42 she put on Orion as the owner, and
     # Bob's flat and dark flat.
     health["files"]["count"] == 4 and health["files"]["bytes"] == 4 * len(FITS)
     and health["files"]["free"] > 0,
     str(health["files"]))

# The browser: a sign-in begun from the files page comes back to it with a
# cookie, and that cookie is a credential of its own - Alice's program token
# still works afterwards.
opener = urllib.request.build_opener(_NoRedirect())
try:
    opener.open(urllib.request.Request(f"{BASE}/files/signin", method="GET"), timeout=10)
    raise AssertionError("expected a redirect")
except urllib.error.HTTPError as hop:
    to_start = hop.headers.get("Location")
case("the files page's sign-in begins a login and goes to Discord's start page",
     to_start and to_start.startswith("/auth/discord/start?code="), str(to_start))
try:
    opener.open(urllib.request.Request(BASE + to_start, method="GET"), timeout=10)
    raise AssertionError("expected a redirect")
except urllib.error.HTTPError as hop:
    to_discord = hop.headers.get("Location") + "&who=code-alice"
try:
    opener.open(urllib.request.Request(to_discord, method="GET"), timeout=10)
    raise AssertionError("expected a redirect")
except urllib.error.HTTPError as hop:
    back = hop.headers.get("Location")
try:
    opener.open(urllib.request.Request(back, method="GET"), timeout=10)
    raise AssertionError("expected a redirect back to the files page")
except urllib.error.HTTPError as hop:
    hop_code = hop.code
    landed_at = hop.headers.get("Location")
    cookie = hop.headers.get("Set-Cookie") or ""
case("...and Discord's callback sends the browser back to the files page with a cookie",
     hop_code == 303 and landed_at == "/files" and cookie.startswith("starfront_web=") and "HttpOnly" in cookie,
     f"{hop_code} {landed_at} {cookie[:40]}")
jar = cookie.split(";", 1)[0]
status, info, _ = raw("GET", "/api/v1/files/projects", None, {"Cookie": jar})
case("the cookie signs the page in", status == 200 and info["signedIn"] and info["name"] == "Alice"
     and any(p["id"] == FILES_PROJECT for p in info["projects"]), str(info)[:200])
status, answer, _ = raw("PUT", f"/api/v1/projects/{FILES_PROJECT}/files/M31_Ha_003.fits", FITS,
                        {"Cookie": jar, "Content-Length": str(len(FITS))})
case("...and uploads on it", status == 200 and answer["file"]["name"] == "M31_Ha_003.fits")
_, me = call("GET", "/api/v1/auth/me", token=ALICE)
case("signing in on the files page did not sign Alice's program out", me.get("name") == "Alice")
status, page, _ = raw("GET", "/files", None, {}, expect_json=False)
case("the files page is served", status == 200 and b"Starfront files" in page)
status, _, _ = raw("POST", "/files/signout", b"", {"Cookie": jar})
status, info, _ = raw("GET", "/api/v1/files/projects", None, {"Cookie": jar})
case("signing out on the page forgets the cookie's token", not info["signedIn"])

# ------------------------------------------- live stacking: hubs
print("\n-- live stacking: hubs --")
from server import hubs as hubs_module                           # noqa: E402

status, _ = call("POST", "/api/v1/hubs", {"name": "x"})
case("registering a hub needs somebody signed in", status == 401)
status, _ = call("POST", "/api/v1/hubs", {"name": "x"}, token=AGENT_TOKEN)
case("...and a telescope's token is not a person", status == 401)
status, reg = call("POST", "/api/v1/hubs",
                   {"name": "Eddie's hub", "lanUrl": "http://10.0.0.5:8790"}, token=BOB3)
HUB, HTOK, HSEC = reg.get("hubId"), reg.get("hubToken", ""), reg.get("grantSecret", "")
case("a person registers a hub and gets an id, a token and a secret once",
     status == 200 and HUB and HTOK.startswith("sfh_") and len(HTOK) == 68
     and len(HSEC) == 43, str(reg)[:120])
status, bad = call("POST", "/api/v1/hubs", {"name": "", "lanUrl": "x"}, token=BOB3)
case("a nameless hub is refused", status == 400)
status, bad = call("POST", "/api/v1/hubs", {"name": "h", "lanUrl": "ftp://x"}, token=BOB3)
case("...and so is a lanUrl that is not http(s)", status == 400)
status, listed = call("GET", "/api/v1/hubs", token=BOB3)
case("the owner lists their hub",
     status == 200 and [h["id"] for h in listed["hubs"]] == [HUB]
     and listed["hubs"][0]["lanUrl"] == "http://10.0.0.5:8790")
case("...and the list never carries the token or the secret",
     HTOK not in json.dumps(listed) and HSEC not in json.dumps(listed)
     and not any(k in listed["hubs"][0] for k in ("token", "grant_secret", "grantSecret", "hubToken")))
case("only a hash of the token is stored",
     server_app.store.hub(HUB)["token"] == hubs_module.hash_token(HTOK) != HTOK
     and server_app.store.hub(HUB)["grant_secret"] == HSEC)
status, other = call("GET", "/api/v1/hubs", token=ALICE)
case("another person does not see it", status == 200 and other["hubs"] == [])
status, _ = call("DELETE", f"/api/v1/hubs/{HUB}", token=ALICE)
case("...and cannot revoke it", status == 404)
status, _ = call("PUT", f"/api/v1/hubs/{HUB}/coordinators", {"coordinators": ["1001"]}, token=ALICE)
case("...or change its coordinators", status == 404)

status, _ = call("POST", f"/api/v1/hubs/{HUB}/heartbeat", {"workers": 2}, token=HTOK)
case("a heartbeat on the hub's token is taken", status == 200)
status, _ = call("POST", f"/api/v1/hubs/{HUB}/heartbeat", {"workers": 2}, token="sfh_" + "0" * 64)
case("...a wrong token is refused", status == 401)
status, _ = call("POST", f"/api/v1/hubs/{HUB}/heartbeat", {"workers": 2}, token=BOB3)
case("...and so is a person's token", status == 401)
status, listed = call("GET", "/api/v1/hubs", token=BOB3)
case("the heartbeat's status and time are kept",
     listed["hubs"][0]["status"] == {"workers": 2} and listed["hubs"][0]["seen"] > 0)

status, got = call("PUT", f"/api/v1/hubs/{HUB}/coordinators",
                   {"coordinators": ["1001", "1001", "1002"]}, token=BOB3)
case("the owner sets the coordinators list (deduplicated, owner implied)",
     status == 200 and got["allowedCoordinators"] == ["1001"], str(got))
status, _ = call("PUT", f"/api/v1/hubs/{HUB}/coordinators", {"coordinators": "1001"}, token=BOB3)
case("...a list that is not a list is refused", status == 400)
_, listed = call("GET", "/api/v1/hubs", token=BOB3)
case("...and the hub reports it", listed["hubs"][0]["allowedCoordinators"] == ["1001"])

# The change feed: pull answers what came after `since`, and waits when
# there is nothing.
s1 = server_app.store.append_change(HUB, "project", "prj_a")
s2 = server_app.store.append_change(HUB, "member", "prj_a/u1")
server_app.store.append_change("hub_elsewhere", "project", "prj_z")
status, got = call("GET", f"/api/v1/hubs/{HUB}/pull?since=0", token=HTOK)
case("pull returns this hub's changes, in order, and the new seq",
     status == 200 and [c["seq"] for c in got["changes"]] == [s1, s2] and got["seq"] == s2
     and got["changes"][0]["kind"] == "project" and got["changes"][0]["key"] == "prj_a", str(got)[:200])
status, got = call("GET", f"/api/v1/hubs/{HUB}/pull?since={s1}", token=HTOK)
case("...only those after `since`", [c["seq"] for c in got["changes"]] == [s2])
began = time.time()
status, got = call("GET", f"/api/v1/hubs/{HUB}/pull?since={s2}&wait=1", token=HTOK)
case("with nothing new it waits, then answers empty at the same seq",
     status == 200 and got == {"changes": [], "seq": s2} and 0.8 < time.time() - began < 3,
     f"{time.time() - began:.1f}s")
late = []
threading.Timer(0.6, lambda: late.append(server_app.store.append_change(HUB, "rig", "u1/r1"))).start()
began = time.time()
status, got = call("GET", f"/api/v1/hubs/{HUB}/pull?since={s2}", token=HTOK)
case("...and a change arriving meanwhile ends the wait early",
     status == 200 and late and got["seq"] == late[0] and time.time() - began < 5,
     f"{time.time() - began:.1f}s")
began = time.time()
status, _ = call("GET", "/api/v1/hubs/other/pull?since=0", token=HTOK)
case("a hub cannot pull another hub's id", status == 401)
status, _ = call("GET", f"/api/v1/hubs/{HUB}/pull?since=0", token=BOB3)
case("...nor a person pull on the hub's behalf", status == 401)
case("pull recorded the hub's cursor", server_app.store.hub(HUB)["cursor"] >= s2)

# Grants.
NOW = 1_800_000_000
hub_row = server_app.store.hub(HUB)
grant = hubs_module.mint_grant(hub_row, "1001", "Alice", "agent_1", "prj_a", NOW)
claims = hubs_module.verify_grant(grant, HSEC, NOW + 10)
case("a grant starts sfg_ and round-trips",
     grant.startswith("sfg_") and claims and claims["hub"] == HUB and claims["sub"] == "1001"
     and claims["name"] == "Alice" and claims["agent"] == "agent_1"
     and claims["project"] == "prj_a" and claims["v"] == 1
     and claims["exp"] == claims["iat"] + 86400 == NOW + 86400, str(claims))
payload, signature = grant[4:].split(".")
import base64                                                    # noqa: E402


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64e(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


forged = dict(json.loads(b64d(payload)), sub="1002")
tampered = "sfg_" + b64e(json.dumps(forged, separators=(",", ":")).encode()) + "." + signature
case("a tampered payload is refused", hubs_module.verify_grant(tampered, HSEC, NOW + 10) is None)
flipped = signature[:-2] + ("AA" if signature[-2:] != "AA" else "BB")
case("a tampered signature is refused",
     hubs_module.verify_grant(f"sfg_{payload}.{flipped}", HSEC, NOW + 10) is None)
case("an expired grant is refused (past the leeway)",
     hubs_module.verify_grant(grant, HSEC, NOW + 86400 + 301) is None)
case("...but a few minutes of clock difference is forgiven",
     hubs_module.verify_grant(grant, HSEC, NOW + 86400 + 200) is not None
     and hubs_module.verify_grant(grant, HSEC, NOW - 200) is not None
     and hubs_module.verify_grant(grant, HSEC, NOW - 301) is None)
status, reg_b = call("POST", "/api/v1/hubs", {"name": "Hub B"}, token=BOB3)
case("hub A's grant fails under hub B's secret",
     hubs_module.verify_grant(grant, reg_b["grantSecret"], NOW + 10) is None)
case("garbage is refused without raising",
     all(hubs_module.verify_grant(junk, HSEC, NOW) is None
         for junk in ("", "sfg_", "sfg_.", "sfg_abc.def", "rscu_x", "sfg_" + payload, "sfg_!!.!!")))

status, _ = call("DELETE", f"/api/v1/hubs/{HUB}", token=BOB3)
case("the owner revokes the hub", status == 200)
status, _ = call("POST", f"/api/v1/hubs/{HUB}/heartbeat", {}, token=HTOK)
case("...and its token stops working", status == 401)
_, listed = call("GET", "/api/v1/hubs", token=BOB3)
case("...and it is gone from the list", [h["name"] for h in listed["hubs"]] == ["Hub B"])

# ------------------------------------------- live stacking: the project binding
# A collaboration is stacked on the cloud (a join code) or on a hub. Binding
# one to a hub mints the stacking project the hub will serve, tells the hub,
# and puts a `stacking` block (with a grant, held to the join code's secrecy
# rule) in the listing. The old fields have to keep working for 1.0.0.33.
print("\n-- live stacking: the project binding --")
import re as _re                                                  # noqa: E402
from server.stack import defaults as stack_defaults               # noqa: E402

REGION = {"ra": 84.0, "dec": 0.0, "width": 16.0, "height": 8.0}
_, _, _, polled = sign_in("code-bob")
BOBH = polled["token"]
_, _, _, polled = sign_in("code-carol")
CAROL = polled["token"]
_, _, _, polled = sign_in("code-alice")
ALICE = polled["token"]
status, reg_h = call("POST", "/api/v1/hubs", {"name": "Bind hub", "lanUrl": "http://10.0.0.7:8790"},
                     token=BOBH)
BH, BHSEC, BHTOK = reg_h["hubId"], reg_h["grantSecret"], reg_h["hubToken"]
call("PUT", f"/api/v1/hubs/{BH}/coordinators", {"coordinators": ["1004"]}, token=BOBH)
_, pulled = call("GET", f"/api/v1/hubs/{BH}/pull?since=0&wait=0", token=BHTOK)
SINCE = pulled["seq"]
case("a new hub's feed is empty", pulled["changes"] == [])


def project_in_listing(token, project_id):
    _, browse = call("GET", "/api/v1/agent/projects", token=token)
    return next(p for p in browse["projects"] if p["id"] == project_id)


# Cloud, in the new form, with a pasted code: refused since T706 (the server
# makes the Collab project). An old collaboration's code is kept both ways.
status, answer = call("POST", "/api/v1/projects", {
    "name": "Cloud stacked", "region": REGION, "requirements": {"filters": {"Ha": 3.0}},
    "stacking": {"backend": "cloud", "joinCode": " h4rt 92bk-wnpc "}}, token=BOBH)
case("stacking.backend=cloud with a pasted join code is refused (T706)",
     status == 400 and "no longer taken" in answer.get("detail", ""), str(answer)[:160])
status, made = call("POST", "/api/v1/projects", {
    "name": "Cloud stacked", "region": REGION, "requirements": {"filters": {"Ha": 3.0}},
    "stacking": {"backend": ""}}, token=BOBH)
CLOUD = made["project"]["id"]
payload = dict(made["project"]["payload"])
payload.update(stackingJoinCode="H4RT-92BK-WNPC", stacking={"backend": "cloud", "joinCode": "H4RT-92BK-WNPC",
                                                             "apiBase": "", "hubId": "", "visibility": ""})
server_app.store.set_project(CLOUD, payload)
status, answer = call("POST", "/api/v1/projects", {
    "name": "no code", "region": REGION, "stacking": {"backend": "cloud"}}, token=BOBH)
case("...the cloud without a linked Collab account is refused, and says to link it",
     status == 400 and "link" in answer.get("detail", ""), str(answer)[:160])
status, answer = call("POST", "/api/v1/projects", {
    "name": "bad code", "region": REGION,
    "stacking": {"backend": "cloud", "joinCode": "H4RT-92BK-WNP1"}}, token=BOBH)
case("...and so is one that cannot be real", status == 400)
status, answer = call("POST", "/api/v1/projects", {
    "name": "bad backend", "region": REGION, "stacking": {"backend": "ftp"}}, token=BOBH)
case("...and an unknown backend", status == 422)
case("a cloud collaboration has no stacking project row", server_app.store.stack_project_of(CLOUD) is None)

# An old payload, with only stackingJoinCode, reads as cloud.
case("_stacking_of reads a payload with only stackingJoinCode as cloud",
     server_app._stacking_of({"stackingJoinCode": "H4RT-92BK-WNPC"})
     == {"backend": "cloud", "joinCode": "H4RT-92BK-WNPC", "apiBase": "", "hubId": "", "visibility": ""})
case("...and one with nothing as unbound", server_app._stacking_of({})["backend"] == ""
     and server_app._stacking_of({"stackingJoinCode": ""})["backend"] == "")
listing = project_in_listing(bobs_token := call("POST", "/api/v1/agents", {"name": "Bob bind rig"}, token=BOBH)[1]["token"], CLOUD)
case("the listing keeps stackingLinked and stackingJoinCode and says cloud in the block",
     listing["stackingLinked"] is True and listing["stackingJoinCode"] == "H4RT-92BK-WNPC"
     and listing["stacking"]["backend"] == "cloud" and listing["stacking"]["grant"] == ""
     and listing["stacking"]["collabProjectId"] == "", str(listing["stacking"]))
case("...an old pasted code is ready to join, and not one this server made",
     listing["stacking"]["state"] == "ready" and listing["stacking"]["managed"] is False
     and listing["stacking"]["problem"] == "", str(listing["stacking"]))

# Bind to a hub: as its owner, then as a listed coordinator, then as someone else.
status, made = call("POST", "/api/v1/projects", {
    "name": "Orion Hub Wide!", "region": REGION, "notes": "hub stacked",
    "requirements": {"filters": {"Ha": 3.0, "OIII": 3.0}},
    "stacking": {"backend": "hub", "hubId": BH}}, token=BOBH)
HUBP = made["project"]["id"]
row = server_app.store.stack_project_of(HUBP) or {}
doc = row.get("doc") or {}
case("the hub's owner binds a collaboration to it", status == 200 and row, str(made)[:200])
case("...the stacking project id is prj_ + 20 lower-case Crockford characters",
     _re.fullmatch(r"prj_[0-9a-hjkmnp-tv-z]{20}", row.get("project_id", "")) is not None,
     row.get("project_id", ""))
case("...the slug is made from the name and fits the contract",
     row.get("slug") == "orion-hub-wide" and _re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", row["slug"]),
     row.get("slug", ""))
case("...the join code is twelve characters as XXXX-XXXX-XXXX and a real one",
     _re.fullmatch(r"[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}", row.get("join_code", "")) is not None
     and server_app._stacking_join_code(row["join_code"]) == row["join_code"], row.get("join_code", ""))
case("...unlisted by default, open, approval open, bound to the hub",
     doc.get("visibility") == "unlisted" and doc.get("status") == "open"
     and doc.get("approval") == "open" and row.get("hub") == BH, str(doc)[:200])
case("...the document is a Project record: ids, filters, palette, grid, policy, licence",
     doc.get("project_id") == row["project_id"] and doc.get("slug") == row["slug"]
     and doc.get("name") == "Orion Hub Wide!" and doc.get("filters") == ["HA", "OIII"]
     and doc.get("palette") == "HOO" and doc.get("licence") == "members_only"
     and doc.get("rotation_from_first_light") is True
     and doc.get("policy") == stack_defaults.POLICY, str(doc)[:300])
grid = doc.get("grid") or {}
case("...the grid is the region's centre and size in the contract's limits",
     grid.get("center_ra_deg") == 84.0 and grid.get("center_dec_deg") == 0.0
     and 64 <= grid.get("width_px", 0) <= 12000 and 64 <= grid.get("height_px", 0) <= 12000
     and 0.1 <= grid.get("scale_arcsec_px", 0) <= 20
     and abs(grid["width_px"] * grid["scale_arcsec_px"] / 3600.0 - 16.0) < 0.05
     and abs(grid["height_px"] * grid["scale_arcsec_px"] / 3600.0 - 8.0) < 0.05, str(grid))
_, pulled = call("GET", f"/api/v1/hubs/{BH}/pull?since={SINCE}&wait=0", token=BHTOK)
case("...and a project change for it is in the hub's feed",
     [(c["kind"], c["key"]) for c in pulled["changes"]] == [("project", row["project_id"])],
     str(pulled["changes"]))
SINCE = pulled["seq"]
status, second = call("POST", "/api/v1/projects", {
    "name": "Orion Hub Wide!", "region": REGION,
    "stacking": {"backend": "hub", "hubId": BH, "visibility": "public"}}, token=BOBH)
other_row = server_app.store.stack_project_of(second["project"]["id"]) or {}
case("a second collaboration of the same name gets its own unique slug and join code",
     status == 200 and other_row.get("slug") == "orion-hub-wide-2"
     and other_row["join_code"] != row["join_code"]
     and other_row["doc"]["visibility"] == "public")

# A mosaic is stacked on a grid its panels frame, not one the first light
# moves. On 2026-11-04 M81_M82 (a 2 x 2 mosaic, FRA600 with its camera at
# 268 degrees) had the region's placeholder with rotation_from_first_light,
# the first light (panel 1) moved the grid's centre onto itself, and panel 2's
# clear frames fell 90 % off it: refused pointing_off_target.
M81_REGION = {"ra": 148.94167, "dec": 69.32278, "width": 1.06, "height": 1.08, "rotation": 5.06}
status, made = call("POST", "/api/v1/projects", {
    "name": "M81 mosaic", "region": M81_REGION, "kind": "mosaic",
    "requirements": {"filters": {"L": None}}, "stacking": {"backend": "hub", "hubId": BH}}, token=BOBH)
M81P = made["project"]["id"]
m81_doc = (server_app.store.stack_project_of(M81P) or {}).get("doc") or {}
case("a mosaic nobody has joined is stacked on the region's placeholder, framed by the first light",
     status == 200 and m81_doc.get("rotation_from_first_light") is True
     and m81_doc.get("grid", {}).get("center_ra_deg") == 148.94167, str(m81_doc.get("grid")))
_, fra = call("POST", "/api/v1/agents", {"name": "FRA600"}, token=BOBH)
call("POST", "/api/v1/agent/hello", {
    "protocol": collab.PROTOCOL,
    "profile": {"name": "FRA600", "focalLength": 600.0, "pixelSize": 2.9, "sensorWidth": 3840,
                "sensorHeight": 2160, "binning": 1, "filters": {"Lum": None},
                "channels": {"Lum": ["L"]}, "rotation": 268.18, "client": "nina"}}, token=fra["token"])
status, m81_join = call("POST", f"/api/v1/agent/projects/{M81P}/join", {}, token=fra["token"])
m81_cells = m81_join.get("task", {}).get("cells") or []
m81_doc = server_app.store.stack_project_of(M81P)["doc"]
m81_grid = m81_doc.get("grid") or {}
m81_scale = 206.265 * 2.9 / 600.0


def _on_m81_grid(ra, dec):
    """A sky position's offset from the M81 grid's centre, in its pixels."""
    dx, dy = stack_defaults._offsets(*stack_defaults._to_plane(
        m81_grid["center_ra_deg"], m81_grid["center_dec_deg"], ra, dec), m81_grid["rotation_deg"])
    return dx * 3600.0 / m81_grid["scale_arcsec_px"], dy * 3600.0 / m81_grid["scale_arcsec_px"]


m81_inside = []
for cell in m81_cells:
    cx, cy = _on_m81_grid(cell["ra"], cell["dec"])
    m81_inside.append(abs(cx) + 3840 / 2 <= m81_grid.get("width_px", 0) / 2
                      and abs(cy) + 2160 / 2 <= m81_grid.get("height_px", 0) / 2)
case("once a rig joins, the grid is framed by its panels and no longer by the first light",
     status == 200 and len(m81_cells) == 4 and "rotation_from_first_light" not in m81_doc,
     f"{len(m81_cells)} cells, {m81_doc.get('rotation_from_first_light')}")
case("...every panel lies wholly on it",
     m81_inside == [True] * 4, f"{m81_inside} {m81_grid}")
case("...centred on the mosaic, not on one panel",
     abs(m81_grid.get("center_ra_deg", 0) - 148.94) < 0.02
     and abs(m81_grid.get("center_dec_deg", 0) - 69.32) < 0.02, str(m81_grid))
case("...turned to the camera, at its scale, about 2.1 x 1.2 degrees",
     m81_grid.get("rotation_deg") == 88.18 and abs(m81_grid.get("scale_arcsec_px", 0) - m81_scale) < 1e-3
     and 7400 < m81_grid.get("width_px", 0) < 7800 and 4200 < m81_grid.get("height_px", 0) < 4500,
     str(m81_grid))
_, pulled = call("GET", f"/api/v1/hubs/{BH}/pull?since={SINCE}&wait=0", token=BHTOK)
case("...and the hub is told", any(c["key"] == m81_doc["project_id"] for c in pulled["changes"]),
     str(pulled["changes"]))
SINCE = pulled["seq"]
_, again = call("GET", "/api/v1/agent/task?night=2026-11-04", token=fra["token"])
_, pulled = call("GET", f"/api/v1/hubs/{BH}/pull?since={SINCE}&wait=0", token=BHTOK)
case("...a poll with nothing moved sends the hub nothing",
     not any(c["key"] == m81_doc["project_id"] for c in pulled["changes"]), str(pulled["changes"]))
SINCE = pulled["seq"]
def _cell_gap(cells):
    """The sky distance between the first two cells of a column, in degrees."""
    a, b = cells[0], cells[1]
    dx = (b["ra"] - a["ra"]) * math.cos(math.radians((a["dec"] + b["dec"]) / 2.0))
    return math.hypot(dx, b["dec"] - a["dec"])


# The overlap between panels is the collaboration's to choose. It was a fixed
# 10 %, which on M81_M82 (2026-11-04) left the panels about 0.1 degrees of
# shared sky to match their brightness by.
m81_gap10 = _cell_gap(m81_cells)
status, _ = call("PUT", f"/api/v1/projects/{M81P}", {"overlap": 20}, token=BOBH)
_, tonight = call("GET", "/api/v1/agent/task?night=2026-11-04", token=fra["token"])
still = next(t for t in tonight["tasks"] if t["project"] == M81P)
case("a panel overlap of 10 % is what a collaboration that says nothing gets",
     abs(m81_gap10 - 1.0633 * 0.9) < 0.01, f"{m81_gap10:.3f} degrees")
case("editing the overlap leaves a night already dealt alone",
     status == 200 and _cell_gap(still["cells"]) == m81_gap10 and still.get("tiledOverlap") in (None, 0.1),
     f"{_cell_gap(still['cells']):.3f} {still.get('tiledOverlap')}")
_, next_night = call("GET", "/api/v1/agent/task?night=2026-11-05", token=fra["token"])
recut = next(t for t in next_night["tasks"] if t["project"] == M81P)
case("...and the next night's cells are cut 20 % apart",
     abs(_cell_gap(recut["cells"]) - 1.0633 * 0.8) < 0.01 and recut.get("tiledOverlap") == 0.2,
     f"{_cell_gap(recut['cells']):.3f} degrees, {recut.get('tiledOverlap')}")
m81_regrid = server_app.store.stack_project_of(M81P)["doc"]["grid"]
case("...and the stacking grid follows the new panels, still centred on the mosaic",
     m81_regrid != m81_grid and abs(m81_regrid["center_dec_deg"] - 69.32) < 0.02
     and abs(m81_regrid["center_ra_deg"] - 148.94) < 0.02,
     f"{len(recut['cells'])} cells, {m81_regrid}")
status, made = call("POST", "/api/v1/projects", {
    "name": "Wide overlap", "region": M81_REGION, "kind": "mosaic", "overlap": 20,
    "requirements": {"filters": {"L": None}}}, token=BOBH)
case("a collaboration can be started with its overlap",
     status == 200 and server_app.store.project(made["project"]["id"])["payload"].get("overlap") == 20.0,
     str(made)[:160])
status, _ = call("POST", "/api/v1/projects", {
    "name": "Too much", "region": M81_REGION, "overlap": 60}, token=BOBH)
case("...of at most 50 %", status == 422, str(status))

case("a mosaic grid wider than the contract's 12 000 px is coarsened to fit, not cut",
     (lambda g: g["width_px"] <= 12000 and g["height_px"] <= 12000
      and g["width_px"] * g["scale_arcsec_px"] / 3600.0 > 6.0)(stack_defaults.mosaic_grid(
          [{"ra": 84.0 + i * 1.0, "dec": 0.0, "width": 1.06, "height": 0.6, "rotation": 0.0}
           for i in range(6)], 1.0)))
case("...and a mosaic with no camera known keeps the placeholder",
     stack_defaults.mosaic_grid([], 1.0) is None
     and stack_defaults.mosaic_grid([{"ra": 1.0, "dec": 1.0, "width": 1.0, "height": 1.0}], 0.0) is None)

status, made = call("POST", "/api/v1/projects", {
    "name": "Coordinator's", "region": REGION, "requirements": {"filters": {"Ha": 3.0}},
    "stacking": {"backend": "hub", "hubId": BH}}, token=CAROL)
COORD = made["project"]["id"]
case("a coordinator the owner listed may bind to the hub too",
     status == 200 and (server_app.store.stack_project_of(COORD) or {}).get("hub") == BH, str(made)[:160])
status, answer = call("POST", "/api/v1/projects", {
    "name": "Not allowed", "region": REGION, "stacking": {"backend": "hub", "hubId": BH}},
    token=ALICE)
case("anybody else is refused, as 403, and nothing is made",
     status == 403 and server_app.store._one("SELECT 1 AS x FROM projects WHERE name = ?", "Not allowed") is None,
     f"{status} {answer}")
status, answer = call("POST", "/api/v1/projects", {
    "name": "No such hub", "region": REGION, "stacking": {"backend": "hub", "hubId": "hub_nope"}},
    token=BOBH)
case("...and so is a hub that does not exist", status == 403)
status, _ = call("POST", "/api/v1/projects", {
    "name": "No hub id", "region": REGION, "stacking": {"backend": "hub"}}, token=BOBH)
case("...or a hub binding that names none", status == 403)
call("PUT", f"/api/v1/hubs/{BH}/coordinators", {"coordinators": []}, token=BOBH)
status, _ = call("PUT", f"/api/v1/projects/{CLOUD}", {"stacking": {"backend": "hub", "hubId": BH}},
                 token=BOBH)
case("an edit may move a collaboration onto a hub its owner owns", status == 200
     and (server_app.store.stack_project_of(CLOUD) or {}).get("hub") == BH)
call("PUT", f"/api/v1/hubs/{BH}/coordinators", {"coordinators": ["1004"]}, token=BOBH)
payload = server_app.store.project(CLOUD)["payload"]
case("...which clears the cloud join code, both ways",
     payload["stackingJoinCode"] == "" and payload["stacking"]["backend"] == "hub")

# The listing: who is handed the grant.
OWNER_RIG = call("POST", "/api/v1/agents", {"name": "Bob's hub rig"}, token=BOBH)[1]["token"]
JOINED_RIG, JOINED_ID = enrol("hub joiner", 389.0, 9576, 6388, 6.0)
status, _ = call("POST", f"/api/v1/agent/projects/{HUBP}/join", {}, token=JOINED_RIG)
case("a telescope joins the hub-backed collaboration", status == 200)
wanted_id = row["project_id"]
mine_l = project_in_listing(OWNER_RIG, HUBP)["stacking"]
joined_l = project_in_listing(JOINED_RIG, HUBP)["stacking"]
third_l = project_in_listing(AGENT_TOKEN, HUBP)["stacking"]
case("the listing's block names the hub and where its front door is",
     mine_l["backend"] == "hub" and mine_l["hubId"] == BH and mine_l["hubName"] == "Bind hub"
     and mine_l["hubLanUrl"] == "http://10.0.0.7:8790" and mine_l["collabProjectId"] == wanted_id
     and mine_l["apiBase"] == BASE + "/stack/api/", str(mine_l))
case("...the owner's own telescope is handed a grant without joining", mine_l["grant"].startswith("sfg_"))
case("...a joined telescope is handed one", joined_l["grant"].startswith("sfg_"))
case("...a third telescope, only browsing, gets the block and an empty grant",
     third_l["backend"] == "hub" and third_l["collabProjectId"] == wanted_id and third_l["grant"] == "",
     str(third_l))
case("the owner's telescope is told the stacking project's visibility, for the Edit form",
     mine_l["visibility"] == "unlisted", str(mine_l))
case("...as is a joined telescope, as it is handed the grant", joined_l["visibility"] == "unlisted", str(joined_l))
case("...a browsing telescope is not (no grant, no visibility)", third_l["visibility"] == "", str(third_l))
def collab_id_of(sf_id):
    row = server_app.store._one("SELECT collab_user_id FROM stack_identities WHERE user_id = ?", sf_id)
    return row["collab_user_id"] if row else None


claims = hubs_module.verify_grant(mine_l["grant"], BHSEC)
case("the owner's grant verifies with the hub's secret and names the project and the person",
     claims and claims["hub"] == BH and claims["project"] == wanted_id
     and claims["sub"] == collab_id_of("1002") and claims["name"] == "Bob", str(claims))
# The hub keys members by collab user id and has no way to map a Starfront id,
# so a grant that named Bob's Discord id was "not a member" straight to the hub.
case("...by collab user id, never the Starfront id the hub cannot map",
     claims and claims["sub"] not in ("1002", "") and len(claims["sub"]) == 36, str(claims))
claims = hubs_module.verify_grant(joined_l["grant"], BHSEC)
case("...a joined one names its telescope and the project",
     claims and claims["agent"] == JOINED_ID and claims["project"] == wanted_id
     and claims["sub"] == collab_id_of(JOINED_ID), str(claims))
old_style = hubs_module.mint_grant(server_app.store.hub(BH), "1002", "Bob", OWNER_RIG, wanted_id)
_, as_new = call("GET", "/stack/api/auth/me", token=mine_l["grant"])
_, as_old = call("GET", "/stack/api/auth/me", token=old_style)
case("a grant minted before (Starfront id in sub) is still the same person at the front door",
     (as_new.get("user") or {}).get("user_id") == (as_old.get("user") or {}).get("user_id") == collab_id_of("1002"),
     f"{as_new} {as_old}")
case("...and neither verifies under another hub's secret",
     hubs_module.verify_grant(mine_l["grant"], reg_b["grantSecret"]) is None
     and hubs_module.verify_grant(joined_l["grant"], reg_b["grantSecret"]) is None)
old_view = project_in_listing(OWNER_RIG, HUBP)
case("the old fields are as today: a hub project carries no join code for old plugins",
     old_view["stackingLinked"] is False and old_view["stackingJoinCode"] == "")

# Editing keeps the document in step; an old-form edit leaves a hub binding alone.
status, _ = call("PUT", f"/api/v1/projects/{HUBP}", {"stackingJoinCode": ""}, token=BOBH)
case("a blank stackingJoinCode from an old plugin does not unbind a hub project",
     status == 200 and server_app._stacking_of(server_app.store.project(HUBP)["payload"])["backend"] == "hub")
_, pulled = call("GET", f"/api/v1/hubs/{BH}/pull?since={SINCE}&wait=0", token=BHTOK)
SINCE = pulled["seq"]
status, _ = call("PUT", f"/api/v1/projects/{HUBP}", {"name": "Orion Renamed"}, token=BOBH)
doc = server_app.store.stack_project_of(HUBP)["doc"]
_, pulled = call("GET", f"/api/v1/hubs/{BH}/pull?since={SINCE}&wait=0", token=BHTOK)
case("renaming a collaboration renames the stacking project and tells the hub",
     doc["name"] == "Orion Renamed" and doc["slug"] == "orion-hub-wide"
     and any(c["key"] == wanted_id for c in pulled["changes"]), str(pulled["changes"]))
SINCE = pulled["seq"]

# The backend cannot change once a frame exists.
server_app.store._run("INSERT INTO stack_frames (frame_id, project_id) VALUES ('frm_t403', ?)", wanted_id)
status, answer = call("PUT", f"/api/v1/projects/{HUBP}", {"stacking": {"backend": ""}}, token=BOBH)
case("with a frame stacked, unbinding is refused as a conflict", status == 409, f"{status} {answer}")
status, answer = call("PUT", f"/api/v1/projects/{HUBP}", {"stacking": {
    "backend": "hub", "hubId": reg_b["hubId"]}}, token=BOBH)
case("...and so is moving it to another hub", status == 409, f"{status} {answer}")
status, _ = call("PUT", f"/api/v1/projects/{HUBP}", {"stacking": {"backend": "hub", "hubId": BH},
                                                     "notes": "still here"}, token=BOBH)
case("...while an edit that keeps the binding goes through", status == 200)
server_app.store._run("DELETE FROM stack_frames WHERE frame_id = 'frm_t403'")

# Unbinding closes, never deletes.
status, _ = call("PUT", f"/api/v1/projects/{HUBP}", {"stacking": {"backend": ""}}, token=BOBH)
row = server_app.store.stack_project_of(HUBP) or {}
_, pulled = call("GET", f"/api/v1/hubs/{BH}/pull?since={SINCE}&wait=0", token=BHTOK)
case("unbinding marks the stacking project closed, keeps the row, and tells the hub",
     status == 200 and row.get("doc", {}).get("status") == "closed"
     and any(c["key"] == wanted_id for c in pulled["changes"]), str(row.get("doc", {}).get("status")))
listing = project_in_listing(OWNER_RIG, HUBP)
case("...and the listing no longer stacks it or hands out a grant",
     listing["stacking"]["backend"] == "" and listing["stacking"]["grant"] == ""
     and listing["stackingLinked"] is False)
status, _ = call("PUT", f"/api/v1/projects/{HUBP}", {"stacking": {"backend": "hub", "hubId": BH}}, token=BOBH)
again = server_app.store.stack_project_of(HUBP)
case("binding it again reopens the same stacking project",
     status == 200 and again["project_id"] == wanted_id and again["join_code"] == row["join_code"]
     and again["doc"]["status"] == "open")

# The policy defaults are the contract's own, when the contract is at hand.
spec = None
for candidate in (ROOT.parent / "Rust" / "rapid-stacker-collab", ROOT.parent.parent / "Rust" / "rapid-stacker-collab",
                  ROOT.parent / "rapid-stacker-collab", ROOT / "nina" / "docs" / "api" / ".."):
    found = candidate / "docs" / "api" / "openapi.yaml" if candidate.name != ".." else candidate / "openapi.yaml"
    if found.is_file():
        spec = found
        break
try:
    import yaml as _yaml
except ImportError:
    _yaml = None
if spec is None or _yaml is None:
    print("SKIP  policy defaults against openapi.yaml: "
          + ("PyYAML is not installed" if spec else "no openapi.yaml beside this repository"))
else:
    schemas = _yaml.safe_load(spec.read_text(encoding="utf-8"))["components"]["schemas"]
    contract = {key: value["default"] for key, value in schemas["Policy"]["properties"].items()
                if "default" in value}
    case("server/stack/defaults.py POLICY equals the contract's Policy defaults",
         contract == stack_defaults.POLICY,
         str({k: (contract.get(k), stack_defaults.POLICY.get(k))
              for k in set(contract) | set(stack_defaults.POLICY)
              if contract.get(k) != stack_defaults.POLICY.get(k)}))
    filters_enum = tuple(schemas["CanonicalFilter"]["enum"])
    case("...and CANONICAL_FILTERS equals the contract's CanonicalFilter",
         filters_enum == stack_defaults.CANONICAL_FILTERS, str(filters_enum))

# ------------------------------------------- live stacking: front door
# The hub-backed project's door, in the Collab contract's envelope: grants for
# auth, projects, membership, rigs, and no 403/404 anywhere under /stack/api.
print("\n-- live stacking: front door --")
import subprocess as _sp                                          # noqa: E402
import uuid as _uuid                                              # noqa: E402
from server.stack import envelope as stack_envelope               # noqa: E402
from server.stack import header as stack_header                   # noqa: E402
from server.stack import rigs as stack_rigs                       # noqa: E402

API = "/stack/api"
OWNER_GRANT = mine_l["grant"]
JOINED_GRANT = joined_l["grant"]
SECOND_ID = second["project"]["id"]
SECOND_GRANT = project_in_listing(OWNER_RIG, SECOND_ID)["stacking"]["grant"]
FD = server_app.store.stack_project_of(HUBP)
FD_ID, FD_SLUG, FD_CODE = FD["project_id"], FD["slug"], FD["join_code"]
_, pulled = call("GET", f"/api/v1/hubs/{BH}/pull?since=0&wait=0", token=BHTOK)
FEED0 = pulled["seq"]

# What the contract says, when it is at hand.
rsc = None
for candidate in (ROOT.parent / "Rust" / "rapid-stacker-collab", ROOT.parent.parent / "Rust" / "rapid-stacker-collab",
                  ROOT.parent / "rapid-stacker-collab"):
    if (candidate / "docs" / "api" / "openapi.yaml").exists():
        rsc = candidate
        break
schemas = {}
if rsc is not None and _yaml is not None:
    contract_doc = _yaml.safe_load((rsc / "docs" / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    schemas = contract_doc["components"]["schemas"]
else:
    print("SKIP  front door against openapi.yaml: "
          + ("PyYAML is not installed" if rsc else "no Rapid Stacker Collab beside this repository"))


def resolve_ref(node):
    while isinstance(node, dict) and "$ref" in node:
        node = schemas[node["$ref"].rsplit("/", 1)[1]]
    return node


def problems(value, node, path="$"):
    """A small required/type/enum/const/properties/additionalProperties check."""
    node = resolve_ref(node)
    found = []
    if "oneOf" in node:
        if not any(not problems(value, option, path) for option in node["oneOf"]):
            found.append(f"{path}: matches none of oneOf")
        return found
    kinds = node.get("type")
    kinds = [kinds] if isinstance(kinds, str) else (kinds or [])
    pytypes = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
               "array": list, "object": dict, "null": type(None)}
    if kinds and not any(isinstance(value, pytypes[k]) and not (k in ("integer", "number") and isinstance(value, bool))
                         for k in kinds):
        return [f"{path}: {type(value).__name__} is not {kinds}"]
    if "const" in node and value != node["const"]:
        found.append(f"{path}: not {node['const']!r}")
    if "enum" in node and value not in node["enum"]:
        found.append(f"{path}: {value!r} not in enum")
    if isinstance(value, str) and "pattern" in node and not _re.search(node["pattern"], value):
        found.append(f"{path}: {value!r} fails pattern")
    if isinstance(value, dict):
        props = node.get("properties", {})
        for key in node.get("required", []):
            if key not in value:
                found.append(f"{path}.{key}: missing")
        for key, item in value.items():
            if key in props:
                found += problems(item, props[key], f"{path}.{key}")
            elif node.get("additionalProperties") is False:
                found.append(f"{path}.{key}: not in the schema")
    if isinstance(value, list) and "items" in node:
        for i, item in enumerate(value):
            found += problems(item, node["items"], f"{path}[{i}]")
    return found


def conforms(name, body):
    return (not schemas) or not problems(body, {"$ref": f"#/components/schemas/{name}"})


def why(name, body):
    return "; ".join(problems(body, {"$ref": f"#/components/schemas/{name}"}))[:300] if schemas else ""


def front(method, path, body=None, token=None):
    return call(method, API + path, body, token=token)


# -- the envelope table and the allow-list against the contract
if schemas:
    import re as _re2
    text = schemas["ErrorCode"]["description"]
    table = {m.group(1): int(m.group(2)) for m in _re2.finditer(r"- `(\w+)` \((\d+)\)", text)}
    case("envelope.ERROR_STATUS equals the contract's ErrorCode table",
         table == stack_envelope.ERROR_STATUS,
         str({k for k in set(table) | set(stack_envelope.ERROR_STATUS)
              if table.get(k) != stack_envelope.ERROR_STATUS.get(k)}))
    case("...and no code of the table is a 403 or 404",
         not {403, 404} & set(stack_envelope.ERROR_STATUS.values()))
    initheader = schemas["InitHeader"]["properties"]
    kind_of = {"string": "str", "number": "num", "integer": "int"}
    same = set(initheader) == set(stack_header.ALLOWED) and all(
        stack_header.ALLOWED[k][0] == ("numstr" if isinstance(v["type"], list) else kind_of[v["type"]])
        for k, v in initheader.items())
    case("header.ALLOWED equals the contract's InitHeader allow-list", same,
         str(set(initheader) ^ set(stack_header.ALLOWED)))

# -- header allow-list
case("an allowed header passes", stack_header.check({"FILTER": "Ha", "EXPTIME": 300.0, "XBINNING": 1,
                                                    "RA": "05 35 17", "DEC": -5.4}) is not None)
for card in ("SITELAT", "SITELONG", "SITEELEV", "OBSERVER", "HISTORY"):
    try:
        stack_header.check({"FILTER": "Ha", card: "x"})
        refused = ""
    except stack_envelope.ApiError as exc:
        refused = f"{exc.code} {exc.status} {exc.extra['errors'][0]['field']}"
    case(f"header: {card} is refused with header_rejected", refused == f"header_rejected 400 header.{card}", refused)
case("header: a location card is never kept from a file's header",
     stack_header.without_location({"SITELAT": 1, "OBSERVER": "me", "FILTER": "Ha"}) == {"FILTER": "Ha"})
try:
    stack_header.check({"EXPTIME": "long"})
    wrong = ""
except stack_envelope.ApiError as exc:
    wrong = exc.code
case("header: a value of the wrong type is validation_failed", wrong == "validation_failed", wrong)

# -- authentication
status, got = front("GET", "/rigs")
case("no credential: 401 not_authenticated in the envelope",
     status == 401 and got.get("ok") is False and got.get("code") == "not_authenticated", f"{status} {got}")
tampered = OWNER_GRANT[:-3] + ("AAA" if OWNER_GRANT[-3:] != "AAA" else "BBB")
status, got = front("GET", f"/projects/{FD_ID}/membership", token=tampered)
case("a tampered grant: 401 token_invalid", status == 401 and got.get("code") == "token_invalid", f"{status} {got}")
status, got = front("GET", "/rigs", token="rscu_" + "0" * 64)
case("a token of another kind: 401 token_invalid", status == 401 and got.get("code") == "token_invalid", f"{status} {got}")
other_secret = hubs_module.mint_grant({"id": BH, "grant_secret": reg_b["grantSecret"]}, "1002", "Bob", "x", FD_ID)
status, got = front("GET", "/rigs", token=other_secret)
case("a grant signed with another hub's secret: 401 token_invalid",
     status == 401 and got.get("code") == "token_invalid", f"{status} {got}")
old_grant = hubs_module.mint_grant({"id": BH, "grant_secret": BHSEC}, "1002", "Bob", "x", FD_ID, time.time() - 3 * 86400)
status, got = front("GET", "/rigs", token=old_grant)
case("an expired grant: 401 token_invalid", status == 401 and got.get("code") == "token_invalid", f"{status} {got}")
status, got = front("GET", f"/projects/{FD_ID}/membership", token=SECOND_GRANT)
case("a grant for another project: 401 token_scope",
     status == 401 and got.get("code") == "token_scope", f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/join", {"display_name": "Zed", "accepted_terms_version": "x"}, token=SECOND_GRANT)
case("...on a write too", status == 401 and got.get("code") == "token_scope", f"{status} {got}")

# -- the envelope for what is not there
status, got = front("GET", "/nope")
case("an unknown path answers the envelope, not 403 or 404",
     status not in (403, 404) and got.get("ok") is False and got.get("code") in stack_envelope.ERROR_STATUS
     and status == stack_envelope.ERROR_STATUS[got["code"]], f"{status} {got}")
for path in ("/auth/me", f"/projects/{FD_ID}/masters"):
    status, got = front("GET", path)
    case(f"{path.rsplit('/', 1)[-1]} without a credential is the envelope",
         status not in (403, 404) and got.get("ok") is False and "code" in got, f"{status} {got}")
status, got = front("DELETE", "/rigs")
case("a method not allowed is the envelope too", status not in (403, 404, 405) and got.get("ok") is False, f"{status} {got}")

# -- resolve-join-code
status, got = front("POST", "/projects/resolve-join-code", {"code": FD_CODE.lower().replace("-", " ")}, token=OWNER_GRANT)
case("a grant resolves the join code, in any case and spacing",
     status == 200 and got.get("ok") is True and got["project"]["project_id"] == FD_ID
     and got["terms_version"] and got["already_member"] is False, f"{status} {str(got)[:200]}")
case("...the response validates against ResolveJoinCodeResponse", conforms("ResolveJoinCodeResponse", got), why("ResolveJoinCodeResponse", got))
status, got = front("POST", "/projects/resolve-join-code", {"code": "H4RT-92BK-WNPC"}, token=OWNER_GRANT)
case("an unknown code: 400 invalid_code", status == 400 and got.get("code") == "invalid_code", f"{status} {got}")
status, got = front("POST", "/projects/resolve-join-code", {}, token=OWNER_GRANT)
case("a missing code: 400 invalid_request", status == 400 and got.get("code") == "invalid_request", f"{status} {got}")
status, got, _ = raw("POST", API + "/projects/resolve-join-code", data=b"{not json",
                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {OWNER_GRANT}"})
case("a body that is not JSON: 400 invalid_request", status == 400 and got.get("code") == "invalid_request", f"{status} {got}")

# -- projects, by id and by slug
status, by_id = front("GET", f"/projects/{FD_ID}", token=OWNER_GRANT)
status2, by_slug = front("GET", f"/projects/{FD_SLUG}", token=OWNER_GRANT)
case("the project reads by id and by slug", status == status2 == 200
     and by_id["project"]["project_id"] == FD_ID == by_slug["project"]["project_id"]
     and by_id["project"]["slug"] == FD_SLUG, f"{status} {status2}")
case("...and validates against ProjectResponse", conforms("ProjectResponse", by_id), why("ProjectResponse", by_id))
case("...with the placeholder grid, the filters and no membership yet",
     by_id["project"]["grid"]["width_px"] >= 64 and by_id["project"]["filters"] == ["HA", "OIII"]
     and by_id["project"]["my_membership"] is None and by_id["project"]["owner_name"] == "Bob")
status, got = front("GET", "/projects/prj_zzzzzzzzzzzzzzzzzzzz", token=OWNER_GRANT)
case("an unknown project: 400 not_found", status == 400 and got.get("code") == "not_found", f"{status} {got}")
status, got = front("GET", f"/projects/{FD_SLUG}")
case("an unlisted project reads without a credential", status == 200 and got["project"]["project_id"] == FD_ID, f"{status} {got}")

# -- joining
body = {"code": "", "display_name": "Bobby", "accepted_terms_version": "2020-01-01",
        "filter_mapping": [{"raw": "Ha 7nm", "canonical": "HA"}]}
status, got = front("POST", f"/projects/{FD_ID}/join", body, token=OWNER_GRANT)
case("joining on stale terms: 409 terms_outdated with the current version",
     status == 409 and got.get("code") == "terms_outdated" and got.get("terms_version"), f"{status} {got}")
body["accepted_terms_version"] = by_id["project"]["terms_version"]
status, got = front("POST", f"/projects/{FD_ID}/join", dict(body, code="WNPC-WNPC-WNPC"), token=OWNER_GRANT)
case("a non-empty code that is not the project's: 400 invalid_code",
     status == 400 and got.get("code") == "invalid_code", f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/join", dict(body, filter_mapping=[{"raw": "x", "canonical": "NOPE"}]), token=OWNER_GRANT)
case("a filter mapping outside the canonical filters: 400 validation_failed",
     status == 400 and got.get("code") == "validation_failed" and got.get("errors"), f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/join", {"display_name": "Bobby"}, token=OWNER_GRANT)
case("a body missing the terms version: 400 invalid_request", status == 400 and got.get("code") == "invalid_request", f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/join", body, token=OWNER_GRANT)
member = got.get("membership") or {}
case("an empty code joins on a grant for the project, active under approval open",
     status == 200 and member.get("status") == "active" and member.get("role") == "contributor"
     and member.get("display_name") == "Bobby" and member.get("filter_mapping") == body["filter_mapping"], f"{status} {str(got)[:200]}")
case("...the response validates against JoinResponse", conforms("JoinResponse", got), why("JoinResponse", got))
OWNER_UID = member.get("user_id", "")
case("...the user id is a UUID v4 that is not the Starfront id",
     _re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", OWNER_UID) is not None
     and OWNER_UID != "1002")
case("...recorded in stack_identities, once",
     (server_app.store._one("SELECT collab_user_id FROM stack_identities WHERE user_id = '1002'") or {}).get("collab_user_id") == OWNER_UID)
status, got = front("POST", f"/projects/{FD_ID}/join", body, token=OWNER_GRANT)
case("joining twice: 409 already_member", status == 409 and got.get("code") == "already_member", f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/join", dict(body, display_name="  BOBBY ", code=FD_CODE.lower()), token=JOINED_GRANT)
case("a second person with the same display name (any case): 409 display_name_taken",
     status == 409 and got.get("code") == "display_name_taken", f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/join", dict(body, display_name="Joiner", code=FD_CODE.lower()), token=JOINED_GRANT)
case("...who joins under another name with the real code (lower case)",
     status == 200 and got["membership"]["display_name"] == "Joiner"
     and got["membership"]["user_id"] != OWNER_UID, f"{status} {got}")
JOINER_UID = got["membership"]["user_id"]

# -- membership
status, got = front("GET", f"/projects/{FD_ID}/membership", token=OWNER_GRANT)
case("the membership reads back", status == 200 and got["membership"]["user_id"] == OWNER_UID
     and got["membership"]["usage"]["frames_used"] == 0 and conforms("MembershipResponse", got), f"{status} {why('MembershipResponse', got)}")
status, got = front("PUT", f"/projects/{FD_ID}/membership", {"display_name": "Robert", "country": "GB",
                    "allow_owner_raw_download": True,
                    "filter_mapping": [{"raw": "Oiii", "canonical": "OIII"}, {"raw": "Dark", "canonical": "SKIP"}]}, token=OWNER_GRANT)
case("...updates (name, country, opt-in, mapping)", status == 200 and got["membership"]["display_name"] == "Robert"
     and got["membership"]["country"] == "GB" and got["membership"]["allow_owner_raw_download"] is True
     and len(got["membership"]["filter_mapping"]) == 2 and conforms("MembershipResponse", got), f"{status} {str(got)[:200]}")
status, got = front("PUT", f"/projects/{FD_ID}/membership", {"display_name": "joiner"}, token=OWNER_GRANT)
case("...and a name another member has is 409 display_name_taken", status == 409 and got.get("code") == "display_name_taken", f"{status} {got}")
status, got = front("PUT", f"/projects/{FD_ID}/membership", {"display_name": 5}, token=OWNER_GRANT)
case("...and a bad body is 400 invalid_request", status == 400 and got.get("code") == "invalid_request", f"{status} {got}")
status, got = front("GET", f"/projects/{FD_ID}", token=OWNER_GRANT)
case("the project now shows the caller's membership and the member count",
     got["project"]["my_membership"]["display_name"] == "Robert" and got["project"]["counts"]["member_count"] == 2
     and conforms("ProjectResponse", got), why("ProjectResponse", got))
NOBODY = hubs_module.mint_grant({"id": BH, "grant_secret": BHSEC}, "9999", "Nobody", "x", FD_ID)
status, got = front("GET", f"/projects/{FD_ID}/membership", token=NOBODY)
case("a grant for the project whose holder has not joined: 401 not_member",
     status == 401 and got.get("code") == "not_member", f"{status} {got}")

# -- a browser session reads, and cannot write
server_app.store.add_web_token("web-front-door", "1002")
request = urllib.request.Request(f"{BASE}{API}/projects/{FD_ID}/membership", headers={"Cookie": "starfront_web=web-front-door"})
with urllib.request.urlopen(request, timeout=10) as response:
    cookie_read = json.loads(response.read())
case("the starfront_web cookie reads the same membership", cookie_read["membership"]["user_id"] == OWNER_UID, str(cookie_read)[:200])
status, got, _ = raw("PUT", API + f"/projects/{FD_ID}/membership", data=b'{"country": "FR"}',
                     headers={"Content-Type": "application/json", "Cookie": "starfront_web=web-front-door"})
case("...but a write on the cookie alone is refused in the envelope", status == 401 and got.get("code") == "not_authenticated", f"{status} {got}")
status, got, _ = raw("GET", API + "/rigs", headers={"Cookie": "starfront_web=unknown"})
case("...and an unknown session is 401 session_expired", status == 401 and got.get("code") == "session_expired", f"{status} {got}")

# -- the dashboard's two reads: who am I, and my projects with the hub's heartbeat
def with_cookie(path, cookie):
    return raw("GET", API + path, headers={"Cookie": f"starfront_web={cookie}"})


status, got, _ = raw("GET", API + "/auth/me")
case("auth/me without a cookie is 401 not_authenticated in the envelope",
     status == 401 and got.get("ok") is False and got.get("code") == "not_authenticated", f"{status} {got}")
status, me_body, _ = with_cookie("/auth/me", "web-front-door")
case("auth/me with a signed-in cookie names the collab user id and the display name",
     status == 200 and me_body.get("ok") is True and me_body["user"] == {"user_id": OWNER_UID, "display_name": "Bob"},
     f"{status} {me_body}")
case("...and no Discord id is anywhere in the body", "1002" not in json.dumps(me_body), json.dumps(me_body))
status, got, _ = with_cookie("/auth/me", "unknown")
case("auth/me with an unknown cookie is 401 session_expired", status == 401 and got.get("code") == "session_expired", f"{status} {got}")
status, got = front("GET", "/auth/me", token=OWNER_GRANT)
case("auth/me also answers a grant, with the same collab id", status == 200 and got["user"]["user_id"] == OWNER_UID, f"{status} {got}")

call("POST", f"/api/v1/hubs/{BH}/heartbeat", {"workers": 3}, token=BHTOK)
status, mine_p, _ = with_cookie("/projects?scope=mine", "web-front-door")
mine_ids = [p["project_id"] for p in mine_p.get("projects", [])]
case("projects?scope=mine lists the signed-in user's project with their membership",
     status == 200 and mine_ids == [FD_ID] and mine_p["projects"][0]["my_membership"]["display_name"] == "Robert"
     and mine_p["projects"][0]["slug"] == FD_SLUG, f"{status} {mine_ids}")
hub_state = mine_p["projects"][0].get("stacked_by") or {}
case("...with the hub that stacks it, its workers and when it was last heard",
     hub_state.get("hub_name") == "Bind hub" and hub_state.get("workers") == 3 and hub_state.get("online") is True
     and str(hub_state.get("last_seen", "")).endswith("Z"), str(hub_state))
case("...and neither the join code nor a Discord id", "join_code" not in mine_p["projects"][0]
     and '"1002"' not in json.dumps(mine_p), "")
server_app.store.add_web_token("web-front-door-nobody", "1001")
status, none_p, _ = with_cookie("/projects?scope=mine", "web-front-door-nobody")
case("another signed-in user, in no project, gets an empty list", status == 200 and none_p["projects"] == [], f"{status} {none_p}")
status, got, _ = raw("GET", API + "/projects?scope=mine")
case("projects?scope=mine without a cookie is 401", status == 401 and got.get("code") == "not_authenticated", f"{status} {got}")
status, got, _ = raw("GET", API + "/projects?scope=bogus")
case("...and an unknown scope is 400 validation_failed", status == 400 and got.get("code") == "validation_failed", f"{status} {got}")

# -- rigs
RIG = {"camera": "ASI2600MM Pro ", "telescope": "RedCat 51", "focal_length_mm": 250.4,
       "pixel_size_um": 3.76, "binning": 1, "name": "Wide rig"}
status, got = front("POST", "/rigs", RIG, token=OWNER_GRANT)
rig = got.get("rig") or {}
case("a rig is upserted, confirmed, with its pixel scale", status == 200 and rig.get("confirmed") is True
     and rig.get("osc") is False and rig.get("name") == "Wide rig" and rig.get("pixel_scale_arcsec") == 3.0973
     and conforms("RigResponse", got), f"{status} {why('RigResponse', got)} {rig}")
want = stack_rigs.rig_id_of(stack_rigs.fingerprint("asi2600mm pro", "redcat 51", 250, 3.8, 1, False))
case("...its rig_id is the fingerprint's first 12 hex characters", rig.get("rig_id") == want, f"{rig.get('rig_id')} {want}")
# The fingerprint against Rapid Stacker Collab's own code, when node and the file are at hand.
js = rsc / "backend" / "lambdas" / "shared" / "rigs.js" if rsc else None
node_ok = False
if js is not None and js.exists():
    probe = ("const ids=require('" + str(js.parent / "ids.js") + "');"
             "const src=require('fs').readFileSync('" + str(js) + "','utf8');"
             "const m={exports:{}};const crypto=require('crypto');"
             "const fn=new Function('require','module','exports',src.replace(/require\\(\"\\.\\/ddb\"\\)/,'{}'));"
             "fn(p=>p==='./ids'?ids:require(p),m,m.exports);"
             "const cases=JSON.parse(process.argv[1]);"
             "console.log(JSON.stringify(cases.map(c=>m.exports.fingerprint(c))));")
    vectors = [dict(camera="ASI2600MM Pro ", telescope="RedCat 51", focal_length_mm=250.4, pixel_size_um=3.76, binning=1, osc=False),
               dict(camera="IMX571", telescope=" Esprit 100", focal_length_mm=550.5, pixel_size_um=3.75, binning=2, osc=True),
               dict(camera="QHY268C", telescope="X", focal_length_mm=1000, pixel_size_um=2.25, binning=1, osc=True),
               dict(camera="A", telescope="B", focal_length_mm=0.5, pixel_size_um=4.65, binning=3, osc=False)]
    try:
        out = _sp.run(["node", "-e", probe, json.dumps(vectors)], capture_output=True, text=True, timeout=30,
                      env={**os.environ, "NODE_PATH": str(rsc / "backend" / "node_modules")})
        theirs = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, ValueError, _sp.TimeoutExpired):
        theirs = None
    if theirs is None:
        print("SKIP  rig fingerprint against rigs.js: it could not be run here")
    else:
        mine_fp = [stack_rigs.fingerprint(v["camera"], v["telescope"], v["focal_length_mm"], v["pixel_size_um"],
                                          v["binning"], v["osc"]) for v in vectors]
        case("the rig fingerprint equals Rapid Stacker Collab's rigs.js on four vectors", mine_fp == theirs, f"{mine_fp} {theirs}")
else:
    print("SKIP  rig fingerprint against rigs.js: no Rapid Stacker Collab beside this repository")
status, again = front("POST", "/rigs", dict(RIG, camera="asi2600mm pro", name="Renamed"), token=OWNER_GRANT)
case("the same tuple upserts the same rig (idempotent, case-insensitive), renaming it",
     again["rig"]["rig_id"] == rig["rig_id"] and again["rig"]["name"] == "Renamed"
     and again["rig"]["created_at"] == rig["created_at"], str(again)[:200])
status, got = front("POST", "/rigs", dict(RIG, bayer_pattern="RGGB", width_px=6248, height_px=4176), token=OWNER_GRANT)
case("a bayer pattern makes it a different, OSC rig", got["rig"]["osc"] is True and got["rig"]["rig_id"] != rig["rig_id"]
     and got["rig"]["width_px"] == 6248 and conforms("RigResponse", got), f"{status} {why('RigResponse', got)}")
status, got = front("POST", "/rigs", dict(RIG, binning=0), token=OWNER_GRANT)
case("a bad rig body is validation_failed", status == 400 and got.get("code") == "validation_failed", f"{status} {got}")
status, got = front("POST", "/rigs", {"camera": "x"}, token=OWNER_GRANT)
case("...or invalid_request when a field is missing", status == 400 and got.get("code") == "invalid_request", f"{status} {got}")
status, got = front("GET", "/rigs", token=OWNER_GRANT)
case("rigs list, oldest first, validating against RigListResponse",
     status == 200 and len(got["rigs"]) == 2 and got["rigs"][0]["rig_id"] == rig["rig_id"]
     and conforms("RigListResponse", got), f"{status} {why('RigListResponse', got)}")
status, got = front("GET", "/rigs", token=JOINED_GRANT)
case("...and are the caller's own", status == 200 and got["rigs"] == [])
status, got = front("PATCH", f"/rigs/{rig['rig_id']}", {"name": "Final", "confirmed": False}, token=OWNER_GRANT)
case("a rig is patched (name, confirmed)", status == 200 and got["rig"]["name"] == "Final"
     and got["rig"]["confirmed"] is False and conforms("RigResponse", got), f"{status} {got}")
status, got = front("PATCH", f"/rigs/{rig['rig_id']}", {"name": "  "}, token=OWNER_GRANT)
case("...a blank name is validation_failed", status == 400 and got.get("code") == "validation_failed", f"{status} {got}")
status, got = front("PATCH", "/rigs/rig_000000000000", {"name": "x"}, token=OWNER_GRANT)
case("...a rig that is not yours is 400 not_found", status == 400 and got.get("code") == "not_found", f"{status} {got}")
status, got = front("PATCH", f"/rigs/{rig['rig_id']}", {"name": "Hijack"}, token=JOINED_GRANT)
case("...another person cannot patch it", status == 400 and got.get("code") == "not_found", f"{status} {got}")

# -- the hub hears of it, and never learns a Starfront id
_, pulled = call("GET", f"/api/v1/hubs/{BH}/pull?since={FEED0}&wait=0", token=BHTOK)
members = [c["key"] for c in pulled["changes"] if c["kind"] == "member"]
rigs_seen = [c["key"] for c in pulled["changes"] if c["kind"] == "rig"]
case("each membership write is a member row in the feed, key project_id/user_id",
     members.count(f"{FD_ID}/{OWNER_UID}") == 2 and members.count(f"{FD_ID}/{JOINER_UID}") == 1, str(members))
case("each rig write is a rig row, key user_id/rig_id",
     rigs_seen.count(f"{OWNER_UID}/{rig['rig_id']}") >= 3 and all(k.split("/")[0] in (OWNER_UID,) for k in rigs_seen), str(rigs_seen))
everything = json.dumps([by_id, member, cookie_read, pulled]) + json.dumps(
    [server_app.store._all("SELECT * FROM stack_members"), server_app.store._all("SELECT * FROM stack_rigs")])
case("no Starfront or Discord id appears in a response, a member row, a rig row or the feed",
     not any(f'"{sid}"' in everything or f"/{sid}\"" in everything or f'{sid}/' in everything
             for sid in ("1002", JOINED_ID)), "")

# ------------------------------------------- live stacking: frames
# Frame upload on the front door: multipart onto this server's disk, the part
# PUT at /stack/s3/{upload_id}/{n}, complete, the lists, withdraw, and the rig
# library (decision D001: this server's library serves hub projects only).
print("\n-- live stacking: frames --")
import hashlib as _hl                                             # noqa: E402
import random as _random                                          # noqa: E402
import urllib.parse as _up                                        # noqa: E402
from concurrent.futures import ThreadPoolExecutor as _Pool        # noqa: E402
from server import files as _files_mod                            # noqa: E402
from server.stack import frames as stack_frames                   # noqa: E402

RELAY = DATA / "stack" / "relay"
PART = stack_frames.PART_SIZE
FRONT = server_app.STACK_FRONT.state


def fits_bytes(size, seed):
    rnd = _random.Random(seed)
    head = b"SIMPLE  =                    T / conforms to FITS standard".ljust(80)
    return (head + rnd.randbytes(max(0, size - 80)))[:size]


def put_part(url, data):
    """A part PUT as the plugin sends it: the body only, a Content-Length, no Authorization."""
    request = urllib.request.Request(url, data=data, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()
    except (urllib.error.URLError, ConnectionError):      # the server answered early and closed
        return 0, {}, b""


def header_for(**more):
    return {"IMAGETYP": "Light", "FILTER": "Ha", "EXPTIME": 300.0, "XBINNING": 1, "YBINNING": 1,
            "INSTRUME": "ASI2600MM Pro", "TELESCOP": "RedCat 51", "FOCALLEN": 250.4, "XPIXSZ": 3.76,
            "DATE-OBS": "2026-10-20T22:00:00.000", "OBJECT": "M33", "NAXIS1": 6248, "NAXIS2": 4176, **more}


def init_for(data, name="frame.fits", token=None, header=None, project=None, **more):
    return front("POST", f"/projects/{project or FD_ID}/frames/init", {
        "sha256": _hl.sha256(data).hexdigest(), "size": len(data), "filename": name,
        "header": header if header is not None else header_for(), "stripped": True, **more},
        token=token or OWNER_GRANT)


def etag_list(sent):
    return [{"part_number": n, "etag": e} for n, e in sorted(sent.items())]


def set_role(user_id, role):
    row = server_app.store._one("SELECT doc FROM stack_members WHERE project_id = ? AND user_id = ?", FD_ID, user_id)
    doc = json.loads(row["doc"])
    doc["role"] = role
    server_app.store._run("UPDATE stack_members SET doc = ? WHERE project_id = ? AND user_id = ?",
                          json.dumps(doc), FD_ID, user_id)


def feed_since(seq):
    return call("GET", f"/api/v1/hubs/{BH}/pull?since={seq}&wait=0", token=BHTOK)[1]


FEED1 = feed_since(0)["seq"]

# -- A: three parts, out of order, with a resume in the middle
DATA_A = fits_bytes(2 * PART + 1000, 1)
status, init_a = init_for(DATA_A, "M33_Ha_0001.fits")
up_a = init_a.get("upload") or {}
case("init answers a multipart upload of 16 MiB parts, three of them, with absolute part URLs on the public origin",
     status == 200 and init_a.get("status") == "uploading" and init_a.get("resumed") is False
     and up_a.get("type") == "multipart" and up_a.get("part_size") == PART and up_a.get("part_count") == 3
     and [p["url"] for p in up_a["part_urls"]] == [f"{BASE}/stack/s3/{up_a['upload_id']}/{n}" for n in (1, 2, 3)]
     and [p["size"] for p in up_a["part_urls"]] == [PART, PART, 1000] and up_a["completed_parts"] == []
     and conforms("FrameInitResponse", init_a), f"{status} {why('FrameInitResponse', init_a)} {str(init_a)[:200]}")
case("...the upload id is 32 random bytes in hex and the frame is a lower-case ULID",
     _re.fullmatch(r"[0-9a-f]{64}", up_a.get("upload_id", "")) is not None
     and _re.fullmatch(r"frm_[0-9a-hjkmnp-tv-z]{26}", init_a.get("frame_id", "")) is not None)
case("...a light is filed under its filter, resolved through the member's mapping or the alias table",
     init_a.get("filter") == "HA" and init_a["resolution"]["canonical"] == "HA"
     and init_a["resolution"]["source"] in ("contributor_mapping", "alias_table", "pattern") and init_a.get("frame_type") == "light")
FA = init_a["frame_id"]
urls_a = {p["part_number"]: p["url"] for p in up_a["part_urls"]}
sent_a = {}
status, headers, _ = put_part(urls_a[3], DATA_A[2 * PART:])
sent_a[3] = headers.get("ETag", "")
case("a part PUT answers 200 with the quoted MD5 of the part as its ETag",
     status == 200 and sent_a[3] == '"' + _hl.md5(DATA_A[2 * PART:]).hexdigest() + '"', str(status))
status, headers, _ = put_part(urls_a[1], DATA_A[:PART])
sent_a[1] = headers.get("ETag", "")
case("...parts may arrive in any order (3 before 1)", status == 200 and sent_a[1] == '"' + _hl.md5(DATA_A[:PART]).hexdigest() + '"')
status, got = front("POST", f"/projects/{FD_ID}/frames/{FA}/complete", {"etags": etag_list(sent_a)}, token=OWNER_GRANT)
case("complete with a part missing: 409 upload_incomplete listing it",
     status == 409 and got.get("code") == "upload_incomplete" and got.get("outstanding_parts") == [2], f"{status} {got}")
status, again_a = init_for(DATA_A, "M33_Ha_0001.fits")
case("init again with the same hash resumes: the same frame, completed parts, URLs for the rest only",
     status == 200 and again_a.get("resumed") is True and again_a["frame_id"] == FA
     and [p["part_number"] for p in again_a["upload"]["completed_parts"]] == [1, 3]
     and again_a["upload"]["completed_parts"][0]["etag"] == sent_a[1]
     and [p["part_number"] for p in again_a["upload"]["part_urls"]] == [2]
     and again_a["upload"]["upload_id"] == up_a["upload_id"] and conforms("FrameInitResponse", again_a),
     f"{status} {why('FrameInitResponse', again_a)} {str(again_a)[:300]}")
status2, got2 = front("POST", f"/projects/{FD_ID}/frames/init", {
    "sha256": _hl.sha256(DATA_A).hexdigest(), "size": len(DATA_A) + 1, "filename": "M33_Ha_0001.fits",
    "header": header_for(), "stripped": True}, token=OWNER_GRANT)
case("the same hash with another size is not a resume: 400 validation_failed",
     status2 == 400 and got2.get("code") == "validation_failed", f"{status2} {got2}")
bad_wrong_size = put_part(urls_a[2], DATA_A[PART:PART + 1000])
case("a part of the wrong size is refused (400)", bad_wrong_size[0] == 400, str(bad_wrong_size[0]))
case("a part number outside the upload is refused (400)",
     put_part(f"{BASE}/stack/s3/{up_a['upload_id']}/9", b"x")[0] == 400 and put_part(f"{BASE}/stack/s3/{up_a['upload_id']}/0", b"x")[0] == 400)
before = sorted(str(p) for p in RELAY.rglob("*"))
stray = put_part(f"{BASE}/stack/s3/{'ab' * 32}/1", b"x" * 10)
case("a stray PUT to an unknown upload id is refused (403) and writes nothing",
     stray[0] == 403 and sorted(str(p) for p in RELAY.rglob("*")) == before, str(stray[0]))
case("...and so is one whose id is not even an upload id", put_part(f"{BASE}/stack/s3/nonsense/1", b"x")[0] == 403)
status, headers, _ = put_part(urls_a[2], DATA_A[PART:2 * PART])
sent_a[2] = headers.get("ETag", "")
case("the missing part arrives", status == 200)
wrong = etag_list(sent_a)
wrong[0] = {"part_number": 1, "etag": '"' + "0" * 32 + '"'}
status, got = front("POST", f"/projects/{FD_ID}/frames/{FA}/complete", {"etags": wrong}, token=OWNER_GRANT)
case("complete with a wrong ETag: 400 bad_etags", status == 400 and got.get("code") == "bad_etags", f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/frames/{FA}/complete",
                    {"etags": etag_list(sent_a) + [{"part_number": 2, "etag": sent_a[2]}]}, token=OWNER_GRANT)
case("...a part listed twice: 400 bad_etags", status == 400 and got.get("code") == "bad_etags", f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/frames/{FA}/complete", {"etags": etag_list(sent_a) + [{"part_number": 4, "etag": "x"}]}, token=OWNER_GRANT)
case("...a part beyond the count: 400 bad_etags", status == 400 and got.get("code") == "bad_etags", f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/frames/{FA}/complete", {"etags": []}, token=OWNER_GRANT)
case("...an empty list: 400 invalid_request", status == 400 and got.get("code") == "invalid_request", f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/frames/{FA}/complete", {"etags": etag_list(sent_a)}, token=JOINED_GRANT)
case("another member cannot complete it: 400 not_found", status == 400 and got.get("code") == "not_found", f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/frames/{FA}/complete", {"etags": [{"part_number": n, "etag": e.strip('"')} for n, e in sorted(sent_a.items())]}, token=OWNER_GRANT)
case("complete accepts ETags with or without their quotes, assembles the file: status uploaded",
     status == 200 and got.get("status") == "uploaded" and got.get("already_complete") is False
     and conforms("FrameCompleteResponse", got), f"{status} {why('FrameCompleteResponse', got)} {got}")
stored_a = list(RELAY.glob(f"{FD_ID}/*/{FA}.fits"))
case("...the bytes sit under relay/{project}/{collab user}/{frame_id}.fits, identical to what was sent",
     len(stored_a) == 1 and stored_a[0].read_bytes() == DATA_A and stored_a[0].parent.name == OWNER_UID
     and not list(RELAY.glob(f"{FD_ID}/*/{FA}.fits.part")), str(stored_a))
status, got = front("POST", f"/projects/{FD_ID}/frames/{FA}/complete", {"etags": etag_list(sent_a)}, token=OWNER_GRANT)
case("complete again is idempotent: already_complete", status == 200 and got.get("already_complete") is True and got.get("status") == "uploaded")
rows = [c for c in feed_since(FEED1)["changes"] if c["kind"] == "frame"]
case("the hub's feed carries a frame row, key project_id/frame_id, once",
     [c["key"] for c in rows] == [f"{FD_ID}/{FA}"], str(rows))
status, _, _ = put_part(urls_a[1], DATA_A[:100])
case("a part PUT to a completed upload is refused (403)", status == 403, str(status))
status, got = init_for(DATA_A, "M33_Ha_0001.fits")
case("init with the hash of an uploaded frame: 409 duplicate, with the caller's existing_frame",
     status == 409 and got.get("code") == "duplicate" and got.get("status") == "uploaded"
     and got.get("existing_frame", {}).get("frame_id") == FA and conforms("ErrorResponse", got), f"{status} {got}")
status, got = init_for(DATA_A, "M33_Ha_0001.fits", token=JOINED_GRANT)
case("...another member sees 409 duplicate without existing_frame",
     status == 409 and got.get("code") == "duplicate" and "existing_frame" not in got, f"{status} {got}")

# -- B: the plugin's byte layout (UploadEngine): four parts in parallel, no Authorization, quoted ETags back
DATA_B = fits_bytes(3 * PART + 12345, 2)
status, init_b = init_for(DATA_B, "M33_Ha_0002.fits")
up_b = init_b["upload"]
todo = sorted(up_b["part_urls"], key=lambda p: p["part_number"])


def plugin_part(p):
    offset = (p["part_number"] - 1) * up_b["part_size"]       # UploadEngine: (PartNumber - 1) * PartSize
    status_, headers_, _ = put_part(p["url"], DATA_B[offset:offset + p["size"]])
    return p["part_number"], status_, headers_.get("ETag")


with _Pool(4) as pool:
    part_results = list(pool.map(plugin_part, todo))
case("the plugin's layout: parts at (n-1)*part_size, 4 in parallel, each answered with a quoted ETag",
     len(part_results) == 4 and all(s == 200 and e and e.startswith('"') and e.endswith('"') for _, s, e in part_results), str(part_results))
status, got = front("POST", f"/projects/{FD_ID}/frames/{init_b['frame_id']}/complete",
                    {"etags": etag_list({n: e for n, _, e in part_results})}, token=OWNER_GRANT)
stored_b = list(RELAY.glob(f"{FD_ID}/*/{init_b['frame_id']}.fits"))
case("...completes with the ETags exactly as the PUTs returned them; the file is byte-identical",
     status == 200 and got.get("status") == "uploaded" and len(stored_b) == 1 and stored_b[0].read_bytes() == DATA_B,
     f"{status} {got}")

# -- C: refusals
DATA_C = b"this is not a FITS file at all " * 40
status, init_c = init_for(DATA_C, "notfits.fits")
put_part(init_c["upload"]["part_urls"][0]["url"], DATA_C)
sent_c = {1: '"' + _hl.md5(DATA_C).hexdigest() + '"'}
status, got = front("POST", f"/projects/{FD_ID}/frames/{init_c['frame_id']}/complete", {"etags": etag_list(sent_c)}, token=OWNER_GRANT)
case("bytes that do not start like a FITS file are refused at complete (400 validation_failed)",
     status == 400 and got.get("code") == "validation_failed" and conforms("ErrorResponse", got), f"{status} {got}")
status, got = front("GET", f"/projects/{FD_ID}/frames/{init_c['frame_id']}", token=OWNER_GRANT)
case("...and the frame is gone, its file with it",
     got.get("code") == "not_found" and not list(RELAY.glob(f"{FD_ID}/*/{init_c['frame_id']}*")), f"{status} {got}")
DATA_X = b"XISF0100" + b"\0" * 200
status, init_x = init_for(DATA_X, "scan.xisf")
put_part(init_x["upload"]["part_urls"][0]["url"], DATA_X)
status, got = front("POST", f"/projects/{FD_ID}/frames/{init_x['frame_id']}/complete",
                    {"etags": etag_list({1: '"' + _hl.md5(DATA_X).hexdigest() + '"'})}, token=OWNER_GRANT)
case("an XISF file passes its magic check", status == 200 and got.get("status") == "uploaded", f"{status} {got}")
DATA_T = fits_bytes(5000, 3)
status, init_t = init_for(DATA_T, "short.fits")
put_part(init_t["upload"]["part_urls"][0]["url"], DATA_T)
for part_file in RELAY.glob(f"{FD_ID}/*/{init_t['frame_id']}.fits.part"):
    part_file.write_bytes(part_file.read_bytes()[:4000])
status, got = front("POST", f"/projects/{FD_ID}/frames/{init_t['frame_id']}/complete",
                    {"etags": etag_list({1: '"' + _hl.md5(DATA_T).hexdigest() + '"'})}, token=OWNER_GRANT)
case("a stored file that is not the declared size: 409 size_mismatch, and the frame is removed",
     status == 409 and got.get("code") == "size_mismatch"
     and front("GET", f"/projects/{FD_ID}/frames/{init_t['frame_id']}", token=OWNER_GRANT)[1].get("code") == "not_found", f"{status} {got}")
status, got = init_for(fits_bytes(100, 4), header=dict(header_for(), SITELAT=51.5))
case("a header with SITELAT is refused: 400 header_rejected, the card not echoed back",
     status == 400 and got.get("code") == "header_rejected" and "51.5" not in json.dumps(got), f"{status} {got}")
status, got = init_for(fits_bytes(100, 5), header=header_for(IMAGETYP="Snapshot"))
case("an IMAGETYP that is no frame type: 400 unsupported_frame_type", status == 400 and got.get("code") == "unsupported_frame_type", f"{status} {got}")
status, got = init_for(fits_bytes(100, 6), header=header_for(EXPTIME=1.0))
case("a light under the project's minimum exposure: 409 policy_violation (below_min_exposure)",
     status == 409 and got.get("code") == "policy_violation" and got.get("reason") == "below_min_exposure", f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/frames/init", {"sha256": "zz", "size": 0, "filename": "x.txt", "header": {}}, token=OWNER_GRANT)
case("a bad init body: 400 validation_failed with errors", status == 400 and got.get("code") == "validation_failed" and got.get("errors"), f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/frames/init", {"sha256": "0" * 64, "size": 600 * 1024 * 1024, "filename": "x.fits", "header": {}}, token=OWNER_GRANT)
case("a frame over 512 MiB: 400 frame_too_large", status == 400 and got.get("code") == "frame_too_large", f"{status} {got}")
status, got = init_for(fits_bytes(100, 7), token=SECOND_GRANT)
case("a grant for another project is token_scope", status == 401 and got.get("code") == "token_scope", f"{status} {got}")
status, got = call("POST", f"{API}/projects/{FD_ID}/frames/init", {}, token=None)
case("no credential: 401 not_authenticated", status == 401 and got.get("code") == "not_authenticated")

# -- D: the quotas
DATA_D = fits_bytes(70000, 8)
status, init_d = init_for(DATA_D, "d.fits")
saved_floor = _files_mod.KEEP_FREE
_files_mod.KEEP_FREE = 1 << 60
try:
    status, got = init_for(fits_bytes(5000, 9), "e.fits")
    case("below the volume's KEEP_FREE floor: 429 quota_exceeded", status == 429 and got.get("code") == "quota_exceeded"
         and got.get("quota") and conforms("ErrorResponse", got), f"{status} {got}")
    status, got = init_for(DATA_D, "d.fits")
    case("...but a resumed upload already holds its room: it still resumes", status == 200 and got.get("resumed") is True, f"{status} {got}")
finally:
    _files_mod.KEEP_FREE = saved_floor


def set_quotas(**changes):
    row = server_app.store.stack_project(FD_ID)
    doc = row["doc"]
    doc["quotas"] = {**doc["quotas"], **changes}
    server_app.store.set_stack_project(FD_ID, row["hub"], doc)


used = server_app.store._one("SELECT COUNT(*) AS n, SUM(size) AS b FROM stack_frames WHERE project_id = ? AND user_id = ?"
                             " AND status NOT IN ('withdrawn', 'rejected')", FD_ID, OWNER_UID)
set_quotas(contributor_max_frames=used["n"])
status, got = init_for(fits_bytes(5000, 10), "f.fits")
case("the per-contributor frame limit: 429 quota_exceeded, quota contributor_frames",
     status == 429 and got.get("quota") == "contributor_frames", f"{status} {got}")
set_quotas(contributor_max_frames=2000, contributor_max_bytes=used["b"] + 100)
status, got = init_for(fits_bytes(5000, 11), "g.fits")
case("the per-contributor byte limit: 429, quota contributor_bytes", status == 429 and got.get("quota") == "contributor_bytes", f"{status} {got}")
set_quotas(contributor_max_bytes=60 * 1000 ** 3, project_max_bytes=used["b"] + 100)
status, got = init_for(fits_bytes(5000, 12), "h.fits")
case("the per-project byte limit: 429, quota project_bytes", status == 429 and got.get("quota") == "project_bytes", f"{status} {got}")
set_quotas(project_max_bytes=2 * 1000 ** 4)
status, got = init_for(fits_bytes(5000, 13), "i.fits")
case("...and the limits lifted, the same init is taken", status == 200, f"{status} {got}")
FI = got["frame_id"]
status, got = front("GET", f"/projects/{FD_ID}/membership", token=OWNER_GRANT)
case("membership usage counts frames still uploading", got["membership"]["usage"]["frames_used"] >= 4, str(got["membership"]["usage"]))

# -- E: get, list
status, got = front("GET", f"/projects/{FD_ID}/frames/{FA}", token=OWNER_GRANT)
case("frames/{id} returns the frame (uploaded, no verdict yet), validating against FrameResponse",
     status == 200 and got["frame"]["status"] == "uploaded" and got["frame"]["filter"] == "HA"
     and got["frame"]["reject_reason"] is None and got["frame"]["sha256"] == _hl.sha256(DATA_A).hexdigest()
     and conforms("FrameResponse", got), f"{status} {why('FrameResponse', got)}")
status, got = front("GET", f"/projects/{FD_ID}/frames/{FA}", token=JOINED_GRANT)
case("a contributor cannot read another's frame: 400 not_found", status == 400 and got.get("code") == "not_found")
status, got = front("GET", f"/projects/{FD_ID}/frames/frm_nonsense", token=OWNER_GRANT)
case("a malformed frame id: 400 not_found", status == 400 and got.get("code") == "not_found")
status, got = front("GET", f"/projects/{FD_ID}/frames?mine=1", token=OWNER_GRANT)
ids_mine = [f["frame_id"] for f in got.get("frames", [])]
case("the list is the caller's own, newest first, validating against FrameListResponse",
     status == 200 and ids_mine == sorted(ids_mine, reverse=True) and FA in ids_mine and conforms("FrameListResponse", got)
     and all("contributor" not in f for f in got["frames"]), f"{status} {why('FrameListResponse', got)}")
status, got = front("GET", f"/projects/{FD_ID}/frames", token=JOINED_GRANT)
case("a contributor's list is forced to their own (none yet)", status == 200 and got["frames"] == [])
status, got = front("GET", f"/projects/{FD_ID}/frames?status=uploaded&limit=1", token=OWNER_GRANT)
case("status filter and paging: one frame and a cursor", len(got["frames"]) == 1 and got["next_cursor"]
     and got["frames"][0]["status"] == "uploaded", str(got)[:200])
status, page2 = front("GET", f"/projects/{FD_ID}/frames?status=uploaded&limit=1&cursor={got['next_cursor']}", token=OWNER_GRANT)
case("...the cursor leads to the next, older one", page2["frames"] and page2["frames"][0]["frame_id"] < got["frames"][0]["frame_id"])
status, got = front("GET", f"/projects/{FD_ID}/frames?contributor=Bobby", token=JOINED_GRANT)
case("another member's frames by name need a maintainer: 401 not_maintainer", status == 401 and got.get("code") == "not_maintainer", f"{status} {got}")
status, got = front("GET", f"/projects/{FD_ID}/frames?limit=0", token=OWNER_GRANT)
case("limit 0: 400 validation_failed", status == 400 and got.get("code") == "validation_failed")
status, got = front("GET", f"/projects/{FD_ID}/frames?filter=HA", token=OWNER_GRANT)
case("the filter filter", got["frames"] and all(f["filter"] == "HA" for f in got["frames"]))
OWNER_NAME = server_app.store._one("SELECT doc FROM stack_members WHERE project_id = ? AND user_id = ?", FD_ID, OWNER_UID)
OWNER_NAME = json.loads(OWNER_NAME["doc"])["display_name"]
set_role(JOINER_UID, "maintainer")
try:
    status, got = front("GET", f"/projects/{FD_ID}/frames", token=JOINED_GRANT)
    case("a maintainer sees everyone's frames, naming the contributor by display name",
         status == 200 and got["frames"] and all(f.get("contributor") == OWNER_NAME for f in got["frames"])
         and conforms("FrameListResponse", got), f"{status} {str(got)[:300]}")
    status, got = front("GET", f"/projects/{FD_ID}/frames?contributor={OWNER_NAME.upper()}", token=JOINED_GRANT)
    case("...?contributor=<display name> pages through one member's frames (any case)",
         status == 200 and len(got["frames"]) >= 2, f"{status} {got}")
    status, got = front("GET", f"/projects/{FD_ID}/frames?contributor=nobody", token=JOINED_GRANT)
    case("...an unknown name gives an empty list", status == 200 and got["frames"] == [])
    status, got = front("GET", f"/projects/{FD_ID}/frames?mine=1", token=JOINED_GRANT)
    case("...mine=1 still means their own", got["frames"] == [])
    status, got = front("GET", f"/projects/{FD_ID}/frames/{FA}", token=JOINED_GRANT)
    case("...and reads any frame", status == 200 and got["frame"]["frame_id"] == FA)
finally:
    set_role(JOINER_UID, "contributor")

# -- F: withdraw
status, got = front("POST", f"/projects/{FD_ID}/frames/{FI}/withdraw", token=JOINED_GRANT)
case("another contributor cannot withdraw it: 400 not_found", status == 400 and got.get("code") == "not_found")
status, got = front("POST", f"/projects/{FD_ID}/frames/{FI}/withdraw", token=OWNER_GRANT)
case("withdrawing an uploading frame aborts it: withdrawn, file and parts gone, validating against WithdrawResponse",
     status == 200 and got.get("status") == "withdrawn" and conforms("WithdrawResponse", got)
     and not list(RELAY.glob(f"{FD_ID}/*/{FI}*")), f"{status} {why('WithdrawResponse', got)}")
status, got = front("POST", f"/projects/{FD_ID}/frames/{FI}/withdraw", token=OWNER_GRANT)
case("...and it is idempotent", status == 200 and got.get("status") == "withdrawn")
status, got = init_for(fits_bytes(5000, 13), "i.fits")
case("...a withdrawn frame no longer blocks its hash", status == 200 and got["frame_id"] != FI and got["resumed"] is False)
FEED2 = feed_since(0)["seq"]
status, got = front("POST", f"/projects/{FD_ID}/frames/{init_b['frame_id']}/withdraw", token=OWNER_GRANT)
case("withdrawing an uploaded frame: withdrawn, raw deleted after seven days",
     status == 200 and got["status"] == "withdrawn" and got["raw_delete_after"] > time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 6 * 86400))
     and got["raw_delete_after"] < time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 8 * 86400)), f"{status} {got}")
case("...and the hub is told (a frame row)",
     [c["key"] for c in feed_since(FEED2)["changes"] if c["kind"] == "frame"] == [f"{FD_ID}/{init_b['frame_id']}"])
status, got = front("GET", f"/projects/{FD_ID}/frames/{init_b['frame_id']}", token=OWNER_GRANT)
case("...the frame reads back withdrawn", got["frame"]["status"] == "withdrawn")
status, got = front("POST", f"/projects/{FD_ID}/frames/{init_b['frame_id']}/complete", {"etags": etag_list(sent_a)}, token=OWNER_GRANT)
case("completing a withdrawn frame: 409 frame_state", status == 409 and got.get("code") == "frame_state" and got.get("status") == "withdrawn", f"{status} {got}")
set_role(JOINER_UID, "maintainer")
status, got = front("POST", f"/projects/{FD_ID}/frames/{FA}/withdraw", token=JOINED_GRANT)
set_role(JOINER_UID, "contributor")
case("a maintainer may withdraw anyone's frame", status == 200 and got["status"] == "withdrawn", f"{status} {got}")

# -- G: the sweep of expired uploads
DATA_S = fits_bytes(9000, 14)
status, init_s = init_for(DATA_S, "s.fits")
put_part(init_s["upload"]["part_urls"][0]["url"], DATA_S)
case("an upload in progress holds a partial file", len(list(RELAY.glob(f"{FD_ID}/*/{init_s['frame_id']}.fits.part"))) == 1)
case("a sweep now leaves it alone", FRONT.frames.sweep() == 0)
case("a sweep four days later aborts it: row, parts and file gone",
     FRONT.frames.sweep(time.time() + 4 * 86400) >= 1
     and front("GET", f"/projects/{FD_ID}/frames/{init_s['frame_id']}", token=OWNER_GRANT)[1].get("code") == "not_found"
     and not list(RELAY.glob(f"{FD_ID}/*/{init_s['frame_id']}*"))
     and not server_app.store._all("SELECT 1 FROM stack_parts WHERE upload_id = ?", init_s["upload"]["upload_id"]))
case("the sweep thread is installed", any(t.name == "stack-sweep" for t in threading.enumerate()))

# -- H: the rig library (D001: this server's library serves hub projects only)
RID = rig["rig_id"]
LIB = f"/rigs/{RID}/library"


def lib_header(**more):
    return {"IMAGETYP": "Dark", "EXPTIME": 300.0, "GAIN": 100, "OFFSET": 50, "XBINNING": 1, "SET-TEMP": -10.0,
            "INSTRUME": "ASI2600MM Pro", "DATE-OBS": "2026-10-19T10:00:00.000", "NAXIS1": 6248, "NAXIS2": 4176, **more}


def lib_init(data, name="dark.fits", token=OWNER_GRANT, rig_id=RID, **changes):
    body = {"sha256": _hl.sha256(data).hexdigest(), "size": len(data), "filename": name,
            "header": lib_header(), "stripped": True}
    body.update(changes)
    return front("POST", f"/rigs/{rig_id}/library/frames/init", body, token=token)


status, got = front("GET", LIB, token=OWNER_GRANT)
case("an empty library: usage, no frames, no masters, validating against LibraryResponse",
     status == 200 and got["frames"] == [] and got["masters"] == [] and got["built_at"] is None
     and got["usage"] == {"bytes_used": 0, "frames_used": 0, "max_bytes": 30 * 1024 ** 3, "max_frames": 1500}
     and conforms("LibraryResponse", got), f"{status} {why('LibraryResponse', got)}")
status, got = front("GET", LIB, token=JOINED_GRANT)
case("a rig that is not yours: 400 not_found", status == 400 and got.get("code") == "not_found")
DATA_L = fits_bytes(20000, 20)
status, got = lib_init(DATA_L, stripped=False)
case("a library frame must be stripped: 400 strip_required", status == 400 and got.get("code") == "strip_required", f"{status} {got}")
status, got = lib_init(DATA_L, header=lib_header(SITELAT=1))
case("...a location card: 400 header_rejected", status == 400 and got.get("code") == "header_rejected")
status, got = lib_init(DATA_L, header=lib_header(IMAGETYP="Light"))
case("...a light: 400 unsupported_frame_type", status == 400 and got.get("code") == "unsupported_frame_type", f"{status} {got}")
status, got = lib_init(DATA_L, header=lib_header(IMAGETYP="Flat"))
case("...a flat never comes from the library: 400 unsupported_frame_type", status == 400 and got.get("code") == "unsupported_frame_type")
status, got = lib_init(DATA_L, header=lib_header(INSTRUME="Other camera"))
case("...a header naming another camera: 400 validation_failed", status == 400 and got.get("code") == "validation_failed")
status, got = lib_init(DATA_L, header=lib_header(XBINNING=2))
case("...another binning: 400 validation_failed", status == 400 and got.get("code") == "validation_failed")
status, got = lib_init(DATA_L)
lib_up = got.get("upload") or {}
case("library init: a multipart upload, status uploading, the rig and a dark, validating against LibraryFrameInitResponse",
     status == 200 and got.get("status") == "uploading" and got.get("frame_type") == "dark" and got.get("rig_id") == RID
     and lib_up.get("part_count") == 1 and conforms("LibraryFrameInitResponse", got), f"{status} {why('LibraryFrameInitResponse', got)} {str(got)[:200]}")
LF = got["frame_id"]
status, headers, _ = put_part(lib_up["part_urls"][0]["url"], DATA_L)
case("library part URLs are absolute on the public origin too",
     all(p["url"].startswith(BASE + "/stack/s3/") for p in lib_up["part_urls"]))
case("a library part PUT is the same route", status == 200 and headers.get("ETag") == '"' + _hl.md5(DATA_L).hexdigest() + '"')
status, got = lib_init(DATA_L)
case("library init again resumes", status == 200 and got.get("resumed") is True and got["frame_id"] == LF
     and [p["part_number"] for p in got["upload"]["completed_parts"]] == [1] and got["upload"]["part_urls"] == [])
status, got = front("POST", f"{LIB}/frames/{LF}/complete", {"etags": [{"part_number": 1, "etag": "x"}]}, token=OWNER_GRANT)
case("library complete with a wrong ETag: 400 bad_etags", status == 400 and got.get("code") == "bad_etags")
status, got = front("POST", f"{LIB}/frames/{LF}/complete", {"etags": [{"part_number": 1, "etag": headers["ETag"]}]}, token=JOINED_GRANT)
case("...another user cannot complete it (400 not_found)", status == 400 and got.get("code") == "not_found")
status, got = front("POST", f"{LIB}/frames/{LF}/complete", {"etags": [{"part_number": 1, "etag": headers["ETag"]}]}, token=OWNER_GRANT)
case("library complete: ready, validating against LibraryFrameCompleteResponse",
     status == 200 and got.get("status") == "ready" and got.get("already_complete") is False
     and conforms("LibraryFrameCompleteResponse", got), f"{status} {why('LibraryFrameCompleteResponse', got)}")
lib_files = list(RELAY.glob(f"lib/{OWNER_UID}/{RID}/{LF}.fits"))
case("...the file sits under relay/lib/{collab user}/{rig}/", len(lib_files) == 1 and lib_files[0].read_bytes() == DATA_L)
status, got = front("POST", f"{LIB}/frames/{LF}/complete", {"etags": [{"part_number": 1, "etag": headers["ETag"]}]}, token=OWNER_GRANT)
case("...and complete again is already_complete", got.get("already_complete") is True and got.get("status") == "ready")
status, got = lib_init(DATA_L)
case("the same hash again: 409 duplicate (no existing_frame)", status == 409 and got.get("code") == "duplicate" and "existing_frame" not in got)
status, got = front("GET", LIB, token=OWNER_GRANT)
lf = (got.get("frames") or [{}])[0]
case("the library lists it with its settings, and the usage counts it",
     status == 200 and lf.get("frame_id") == LF and lf.get("status") == "ready" and lf.get("exposure_s") == 300.0
     and lf.get("gain") == 100 and lf.get("binning") == 1 and lf.get("set_temp_c") == -10.0
     and got["usage"]["frames_used"] == 1 and got["usage"]["bytes_used"] == len(DATA_L) and conforms("LibraryResponse", got),
     f"{status} {why('LibraryResponse', got)} {got}")
case("the hub is told of the library frame (kind library, key user/rig/frame)",
     f"{OWNER_UID}/{RID}/{LF}" in [c["key"] for c in feed_since(FEED2)["changes"] if c["kind"] == "library"])
DATA_L2 = fits_bytes(300, 21)
status, got = lib_init(DATA_L2, "bias.fits", header=lib_header(IMAGETYP="Bias", EXPTIME=0.0))
LF2 = got["frame_id"]
status, got = front("POST", f"{LIB}/frames/{LF2}/withdraw", token=OWNER_GRANT)
case("withdrawing an uploading library frame aborts it", status == 200 and got["status"] == "withdrawn"
     and conforms("WithdrawResponse", got) and not list(RELAY.glob(f"lib/*/*/{LF2}*")))
status, got = front("POST", f"{LIB}/frames/{LF}/withdraw", token=OWNER_GRANT)
case("withdrawing a ready one: withdrawn, and the usage is released",
     status == 200 and got["status"] == "withdrawn" and front("GET", LIB, token=OWNER_GRANT)[1]["usage"]["frames_used"] == 0)
status, got = front("POST", f"{LIB}/frames/{LF}/withdraw", token=OWNER_GRANT)
case("...idempotent", status == 200 and got["status"] == "withdrawn")
status, got = front("POST", f"{LIB}/frames/{LF}/withdraw", token=JOINED_GRANT)
case("...another user cannot (400 not_found)", status == 400 and got.get("code") == "not_found")
status, got = front("GET", LIB, token=OWNER_GRANT)
case("the library no longer lists a withdrawn frame", all(f["frame_id"] != LF for f in got["frames"]))
DATA_L3 = fits_bytes(400, 22)
status, got = lib_init(DATA_L3, "old.fits")
LF3 = got["frame_id"]
case("a library frame upload also expires after three days (swept)",
     FRONT.library.sweep() == 0 and FRONT.library.sweep(time.time() + 4 * 86400) == 1
     and server_app.store._one("SELECT 1 AS x FROM stack_library WHERE frame_id = ?", LF3) is None)

# -- the hub's view never carries a Starfront id
disk = " ".join(str(p) for p in RELAY.rglob("*"))
everything = json.dumps([server_app.store._all("SELECT * FROM stack_frames"), server_app.store._all("SELECT * FROM stack_library"),
                         feed_since(FEED1)["changes"]]) + disk
case("no Starfront or Discord id appears in a frame row, a library row, a relay path or the feed",
     not any(f'"{sid}"' in everything or f"/{sid}/" in everything for sid in ("1002", JOINED_ID)), "")

# ------------------------------------------- live stacking: relay
# What the hub fetches: its projects' uploaded frames, ranged bytes, ack, and
# the deletion of relay copies (server/stack/relay.py).
print("\n-- live stacking: relay --")

BHB, BHBTOK = reg_b["hubId"], reg_b["hubToken"]
KEEP_ENV = "STARFRONT_STACK_RELAY_KEEP_DAYS"


def raw_call(method, path, token=None, headers=None):
    request = urllib.request.Request(f"{BASE}{path}", method=method,
                                     headers={**({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def upload_frame(data, name):
    status, got = init_for(data, name)
    assert status == 200, got
    etags = {}
    for part in got["upload"]["part_urls"]:
        n = part["part_number"]
        lo = (n - 1) * PART
        etags[n] = put_part(part["url"], data[lo:lo + part["size"]])[1].get("ETag", "")
    status, done = front("POST", f"/projects/{FD_ID}/frames/{got['frame_id']}/complete",
                         {"etags": etag_list(etags)}, token=OWNER_GRANT)
    assert status == 200, done
    return got["frame_id"]


def listed(hub=BH, tok=BHTOK, query="state=uploaded&limit=500"):
    status, got = call("GET", f"/api/v1/hubs/{hub}/frames?{query}", token=tok)
    return status, got.get("frames", [])


def relay_path(frame_id):
    return RELAY / server_app.store._one("SELECT path FROM stack_frames WHERE frame_id = ?", frame_id)["path"]


def ack(frame_id, data, hub=BH, tok=BHTOK, kind=""):
    return call("POST", f"/api/v1/hubs/{hub}/frames/{frame_id}/ack{kind}", {"sha256": _hl.sha256(data).hexdigest()}, token=tok)


def collected_names():
    return [f["name"] for f in call("GET", f"/api/v1/projects/{HUBP}/files", token=BOBH)[1]["files"]]


os.environ.pop(KEEP_ENV, None)
DATA_B = fits_bytes(2 * PART + 1000, 40)       # three parts, so a range can cross a boundary
FB = upload_frame(DATA_B, "relay_big.fits")
status, mine_list = listed()
ours = [f for f in mine_list if f["project_id"] == FD_ID]
case("a hub lists uploaded frames of its projects, oldest first",
     status == 200 and ours and len(ours) == len(mine_list)
     and [f["frame_id"] for f in mine_list] == [r["frame_id"] for r in server_app.store._all(
         "SELECT frame_id FROM stack_frames WHERE status = 'uploaded' AND project_id = ? ORDER BY received, frame_id", FD_ID)],
     f"{status} {len(mine_list)}")
rec = next((f for f in mine_list if f["frame_id"] == FB), {})
case("...each record has the contract's fields plus the user, header, stripped and rig",
     all(k in rec for k in ("frame_id", "project_id", "user_id", "filter", "size", "sha256", "filename", "header",
                            "uploaded_at", "rig_id", "stripped", "frame_type"))
     and rec["user_id"] == OWNER_UID and rec["sha256"] == _hl.sha256(DATA_B).hexdigest()
     and rec["header"].get("FILTER") == "Ha" and rec["stripped"] is True, str(rec)[:300])
status, other = listed(BHB, BHBTOK)
case("another hub's token lists none of them", status == 200 and other == [])
case("a hub token only speaks for its own hub id: 401", listed(BH, BHBTOK)[0] == 401)
case("another hub's bytes request: 404", raw_call("GET", f"/api/v1/hubs/{BHB}/frames/{FB}/bytes", BHBTOK)[0] == 404)
case("bytes without a token: 401", raw_call("GET", f"/api/v1/hubs/{BH}/frames/{FB}/bytes")[0] == 401)
status, headers, body = raw_call("GET", f"/api/v1/hubs/{BH}/frames/{FB}/bytes", BHTOK, {"Range": "bytes=0-99"})
case("bytes with Range: bytes=0-99 answer 206 with Content-Range and the right slice",
     status == 206 and body == DATA_B[:100] and headers.get("Content-Range") == f"bytes 0-99/{len(DATA_B)}",
     f"{status} {headers.get('Content-Range')} {len(body)}")
status, headers, body = raw_call("GET", f"/api/v1/hubs/{BH}/frames/{FB}/bytes", BHTOK, {"Range": f"bytes={PART - 5}-{PART + 4}"})
case("...a slice across a part boundary is right too", status == 206 and body == DATA_B[PART - 5:PART + 5])
status, headers, body = raw_call("GET", f"/api/v1/hubs/{BH}/frames/{FB}/bytes", BHTOK, {"Range": f"bytes={len(DATA_B) - 10}-"})
case("...and an open-ended range reaches the end", status == 206 and body == DATA_B[-10:])
status, headers, body = raw_call("GET", f"/api/v1/hubs/{BH}/frames/{FB}/bytes", BHTOK)
case("the full download matches the sha256", status == 200 and _hl.sha256(body).hexdigest() == rec["sha256"])
status, got = call("POST", f"/api/v1/hubs/{BH}/frames/{FB}/ack", {"sha256": "0" * 64}, token=BHTOK)
case("ack with a wrong sha256: 409, still listed", status == 409 and any(f["frame_id"] == FB for f in listed()[1]))
case("another hub cannot ack it: 404", ack(FB, DATA_B, BHB, BHBTOK)[0] == 404)
before = feed_since(0)["seq"]
status, got = ack(FB, DATA_B)
row_a = server_app.store._one("SELECT * FROM stack_frames WHERE frame_id = ?", FB)
case("ack with the right sha256: relayed (a timestamp), no longer listed, no feed row",
     status == 200 and row_a["relayed"] > 0 and all(f["frame_id"] != FB for f in listed()[1])
     and feed_since(0)["seq"] == before, f"{status} {got}")
case("...the bytes are still there (default keep is seven days) and a repeated ack is fine",
     (RELAY / row_a["path"]).is_file() and ack(FB, DATA_B)[0] == 200)

# -- retention
case("retention: a sweep now (seven days kept) deletes nothing of the relayed frame",
     FRONT.relay.sweep() == 0 and (RELAY / row_a["path"]).is_file())
case("...at eight days it is deleted, the row stays with its status",
     FRONT.relay.sweep(time.time() + 8 * 86400) >= 1 and not (RELAY / row_a["path"]).exists()
     and server_app.store._one("SELECT status FROM stack_frames WHERE frame_id = ?", FB)["status"] == "uploaded")
case("...and its bytes are 404 afterwards", raw_call("GET", f"/api/v1/hubs/{BH}/frames/{FB}/bytes", BHTOK)[0] == 404)
DATA_R = fits_bytes(5000, 31)
FR = upload_frame(DATA_R, "keep0.fits")
p_r = relay_path(FR)
os.environ[KEEP_ENV] = "0"
case("KEEP_DAYS=0: a sweep leaves an unrelayed frame alone and it is listed",
     any(f["frame_id"] == FR for f in listed()[1]) and FRONT.relay.sweep() == 0 and p_r.is_file())
os.environ.pop(KEEP_ENV)
DATA_R2 = fits_bytes(5000, 32)
FR2 = upload_frame(DATA_R2, "keep0b.fits")
ack(FR2, DATA_R2)
p_r2 = relay_path(FR2)
os.environ[KEEP_ENV] = "0"
case("KEEP_DAYS=0: the next sweep deletes a relayed frame's bytes and keeps its row",
     p_r2.is_file() and FRONT.relay.sweep() == 1 and not p_r2.exists()
     and server_app.store._one("SELECT status FROM stack_frames WHERE frame_id = ?", FR2) is not None)
DATA_R3 = fits_bytes(5000, 33)
FR3 = upload_frame(DATA_R3, "keep0c.fits")
p_r3 = relay_path(FR3)
ack(FR3, DATA_R3)
case("...and with KEEP_DAYS=0 the ack itself deletes them", not p_r3.exists())
os.environ.pop(KEEP_ENV)
status, got = front("POST", f"/projects/{FD_ID}/frames/{FR}/withdraw", token=OWNER_GRANT)
case("a withdrawn frame is no longer listed to the hub, and its bytes are refused",
     status == 200 and all(f["frame_id"] != FR for f in listed()[1])
     and raw_call("GET", f"/api/v1/hubs/{BH}/frames/{FR}/bytes", BHTOK)[0] == 404)
case("...its bytes stay until raw_delete_after, then the sweep deletes them (row kept)",
     p_r.is_file() and FRONT.relay.sweep() == 0 and FRONT.relay.sweep(time.time() + 8 * 86400) >= 1
     and not p_r.exists() and server_app.store._one("SELECT status FROM stack_frames WHERE frame_id = ?", FR)["status"] == "withdrawn")

# -- collectFiles
call("PUT", f"/api/v1/projects/{HUBP}", {"collectFiles": True}, token=BOBH)
DATA_C = fits_bytes(6000, 34)
FC = upload_frame(DATA_C, "collect_Ha.fits")
p_c = relay_path(FC)
case("a collectFiles project's frame appears in /files at upload completion", "collect_Ha.fits" in collected_names(),
     str(collected_names()))
collected = list(Path(server_app.FILES_DIR).glob(f"{HUBP}/*/lights/collect_Ha.fits"))
case("...as a file in the files layout holding the same bytes", len(collected) == 1 and collected[0].read_bytes() == DATA_C)
ack(FC, DATA_C)
os.environ[KEEP_ENV] = "0"
FRONT.relay.sweep()
os.environ.pop(KEEP_ENV)
case("the relay copy is deleted but the collected file survives",
     not p_c.exists() and collected[0].read_bytes() == DATA_C and collected_names().count("collect_Ha.fits") == 1)
call("PUT", f"/api/v1/projects/{HUBP}", {"collectFiles": False}, token=BOBH)
DATA_N = fits_bytes(3000, 35)
FN = upload_frame(DATA_N, "not_collected.fits")
case("a project that does not collect puts nothing in /files", "not_collected.fits" not in collected_names())
call("PUT", f"/api/v1/projects/{HUBP}", {"collectFiles": True}, token=BOBH)
ack(FN, DATA_N)
case("...but one collecting from later on gets it at the ack", "not_collected.fits" in collected_names())

# -- library frames
DATA_LL = fits_bytes(7000, 36)
status, got = lib_init(DATA_LL, "dark_relay.fits")
LL = got["frame_id"]
etag = put_part(got["upload"]["part_urls"][0]["url"], DATA_LL)[1]["ETag"]
front("POST", f"{LIB}/frames/{LL}/complete", {"etags": [{"part_number": 1, "etag": etag}]}, token=OWNER_GRANT)
status, libs = listed(query="kind=library")
lrec = next((f for f in libs if f["frame_id"] == LL), {})
case("library frames are listed with kind=library: owner, rig, sha256, header, and not among the project frames",
     status == 200 and lrec.get("user_id") == OWNER_UID and lrec.get("rig_id") == RID
     and lrec.get("sha256") == _hl.sha256(DATA_LL).hexdigest() and lrec.get("header", {}).get("IMAGETYP") == "Dark"
     and all(f["frame_id"] != LL for f in listed()[1]), str(libs)[:300])
case("...another hub (no project of that owner) sees none and gets 404 on the bytes",
     listed(BHB, BHBTOK, "kind=library")[1] == []
     and raw_call("GET", f"/api/v1/hubs/{BHB}/frames/{LL}/bytes?kind=library", BHBTOK)[0] == 404)
status, headers, body = raw_call("GET", f"/api/v1/hubs/{BH}/frames/{LL}/bytes?kind=library", BHTOK, {"Range": "bytes=10-19"})
case("...ranged bytes work for them", status == 206 and body == DATA_LL[10:20])
case("a wrong sha256 on a library ack: 409",
     call("POST", f"/api/v1/hubs/{BH}/frames/{LL}/ack?kind=library", {"sha256": "1" * 64}, token=BHTOK)[0] == 409)
status, _ = ack(LL, DATA_LL, kind="?kind=library")
case("a right one: relayed, no longer listed", status == 200 and all(f["frame_id"] != LL for f in listed(query="kind=library")[1]))
p_l = RELAY / server_app.store._one("SELECT path FROM stack_library WHERE frame_id = ?", LL)["path"]
case("...its bytes go after the keep period and the row stays",
     p_l.is_file() and FRONT.relay.sweep() == 0 and FRONT.relay.sweep(time.time() + 8 * 86400) >= 1
     and not p_l.exists() and server_app.store._one("SELECT 1 AS x FROM stack_library WHERE frame_id = ?", LL) is not None)

# ------------------------------------------- live stacking: the schema
print("\n-- live stacking: hub and stacking tables --")
import sqlite3                                                   # noqa: E402
from server import store as store_module                         # noqa: E402

STACK_TABLES = {"hubs", "stack_projects", "stack_members", "stack_rigs",
                "stack_frames", "stack_parts", "stack_library", "stack_changes",
                "stack_identities", "stack_notified"}


def tables_of(path):
    link = sqlite3.connect(str(path))
    try:
        return {row[0] for row in link.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        link.close()


fresh_path = Path(tempfile.mkdtemp()) / "fresh.db"
fresh = store_module.Store(fresh_path)
case("a fresh database has every hub and stacking table",
     STACK_TABLES <= tables_of(fresh_path), str(STACK_TABLES - tables_of(fresh_path)))

# A database from before this change: today's schema without the new tables,
# holding a row that must survive being opened by the new code.
old_path = Path(tempfile.mkdtemp()) / "old.db"
link = sqlite3.connect(str(old_path))
link.executescript(store_module.SCHEMA.split("-- Live stacking")[0])
link.execute("INSERT INTO agents (id, token, name, created) VALUES"
             " ('a1', 't1', 'Old rig', 1)")
link.commit()
case("the test's pre-change database really lacks the new tables",
     not (STACK_TABLES & tables_of(old_path)))
link.close()
old = store_module.Store(old_path)
case("opening a pre-change database adds the tables",
     STACK_TABLES <= tables_of(old_path))
case("...and keeps what was in it", (old.agent("a1") or {}).get("name") == "Old rig")
old.close()
again = store_module.Store(old_path)
case("opening it a second time is harmless", (again.agent("a1") or {}).get("name") == "Old rig")
again.close()

made_hub = fresh.add_hub("hub_1", "u1", "Eddie's hub", "sfh_tok", "secret", "http://10.0.0.5:8790")
case("a hub can be stored and read back",
     made_hub["name"] == "Eddie's hub" and made_hub["status"] == {}
     and fresh.hub("hub_1")["grant_secret"] == "secret" and fresh.hub("nope") is None)
seqs = [fresh.append_change("hub_1", "project", "prj_a"),
        fresh.append_change("hub_1", "member", "prj_a/u1"),
        fresh.append_change("hub_2", "project", "prj_b")]
case("stack_changes.seq only ever increases", seqs == sorted(set(seqs)) and seqs[0] >= 1, str(seqs))
fresh._run("DELETE FROM stack_changes")
case("...and is not reused after the rows are pruned", fresh.append_change("hub_1", "project", "x") > seqs[-1])
fresh.close()

# ------------------------------------------- the progress pages under /stack/
print("\n-- the progress pages: routes, CSP, public artefacts --")


def raw_get(path, method="GET"):
    """(status, headers, body) without following redirects or raising."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(f"{BASE}{path}", method=method)
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


CSP_WANTED = ("default-src 'self'; script-src 'self'; style-src 'self'; "
              "style-src-attr 'unsafe-inline'; img-src 'self' data: blob:; "
              "connect-src 'self'; frame-ancestors 'none'")
for label, page, marker in (
        ("the project page", "/stack/p/horsehead-sho", b"project-body"),
        ("a filter page", "/stack/p/horsehead-sho/filter/HA@osc", b"filter-body"),
        ("a filter page with an escaped stream key", "/stack/p/horsehead-sho/filter/HA%40osc", b"filter-body"),
        ("the combined page", "/stack/p/horsehead-sho/combined", b"combined-body"),
        ("the dashboard", "/stack/dashboard", b"dashboard-body")):
    status, headers, body = raw_get(page)
    case(f"{label} is HTML with the CSP",
         status == 200 and headers.get("Content-Type", "").startswith("text/html")
         and marker in body and headers.get("Content-Security-Policy") == CSP_WANTED
         and headers.get("X-Content-Type-Options") == "nosniff",
         f"{status} {headers.get('Content-Type')}")
status, headers, _ = raw_get("/files/signin?next=/stack/dashboard")
login_code = (headers.get("Location") or "").rpartition("code=")[2]
case("/files/signin?next= returns to the dashboard after sign-in",
     status == 303 and (server_app.store.login(login_code) or {}).get("back") == "/stack/dashboard", str(headers.get("Location")))
for bad_next in ("//evil.example/x", "https://evil.example/", "/download", "/stack/../etc", "/stack\\x"):
    status, headers, _ = raw_get("/files/signin?next=" + urllib.parse.quote(bad_next, safe=""))
    code_ = (headers.get("Location") or "").rpartition("code=")[2]
    case(f"...a next of {bad_next!r} falls back to /files", (server_app.store.login(code_) or {}).get("back") == "/files")
status, headers, _ = raw_get("/stack/p/x")
case("a slug that cannot exist is a 404 (still under the CSP)",
     status == 404 and headers.get("Content-Security-Policy") == CSP_WANTED)
status, headers, body = raw_get("/stack/js/pages/project.js")
case("a page script is served as a script under the CSP",
     status == 200 and b"parseProjectPath" in body and "javascript" in headers.get("Content-Type", "")
     and headers.get("Content-Security-Policy") == CSP_WANTED, headers.get("Content-Type", ""))
status, headers, body = raw_get("/stack/js/site-config.js")
case("site-config.js points the pages at /stack/api and /stack/public",
     status == 200 and b'"/stack/api"' in body and b'"/stack/public"' in body)
status, headers, _ = raw_get("/stack/css/tokens.css")
case("a stylesheet is served as CSS", status == 200 and "text/css" in headers.get("Content-Type", ""))
status, headers, _ = raw_get("/stack/")
case("/stack/ redirects somewhere useful", status == 302 and headers.get("Location", "").startswith("/"),
     str(headers.get("Location")))

PUB = DATA / "stack" / "public" / "prj_x"
(PUB / "deepzoom" / "HA" / "g000001" / "image_files" / "0").mkdir(parents=True)
(PUB / "HA" / "gold").mkdir(parents=True)
(PUB / "status.json").write_text('{"schema_version": 1}')
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
(PUB / "HA" / "gold" / "latest.png").write_bytes(PNG)
(PUB / "deepzoom" / "HA" / "g000001" / "image.dzi").write_text("<Image/>")
(PUB / "deepzoom" / "HA" / "g000001" / "image_files" / "0" / "0_0.jpg").write_bytes(b"\xff\xd8\xff")
(DATA / "stack" / "secret.txt").write_text("not for the web")

status, headers, body = raw_get("/stack/public/prj_x/status.json")
case("a pushed status.json is served as JSON for a minute",
     status == 200 and json.loads(body) == {"schema_version": 1}
     and headers.get("Content-Type", "").startswith("application/json")
     and headers.get("Cache-Control") == "public, max-age=60", str(headers.get("Cache-Control")))
case("...under the CSP and nosniff", headers.get("Content-Security-Policy") == CSP_WANTED
     and headers.get("X-Content-Type-Options") == "nosniff")
status, headers, body = raw_get("/stack/public/prj_x/HA/gold/latest.png")
case("a preview PNG is served as image/png for a minute",
     status == 200 and body == PNG and headers.get("Content-Type") == "image/png"
     and headers.get("Cache-Control") == "public, max-age=60")
status, headers, body = raw_get("/stack/public/prj_x/deepzoom/HA/g000001/image_files/0/0_0.jpg")
case("a deep-zoom tile is image/jpeg and immutable",
     status == 200 and headers.get("Content-Type") == "image/jpeg"
     and headers.get("Cache-Control") == "public, max-age=31536000, immutable")
status, headers, _ = raw_get("/stack/public/prj_x/deepzoom/HA/g000001/image.dzi")
case("...and its descriptor is XML and immutable",
     status == 200 and headers.get("Content-Type") == "application/xml"
     and "immutable" in headers.get("Cache-Control", ""))
status, _, _ = raw_get("/stack/public/prj_x/HA/gold/latest.png", method="HEAD")
case("HEAD works on a public file", status == 200)
status, _, _ = raw_get("/stack/public/prj_x/missing.json")
case("a missing public file is a 404", status == 404)
status, _, _ = raw_get("/stack/public/prj_nobody/status.json")
case("...and so is a project that has pushed nothing", status == 404)
for label, bad in (("a dot-dot path", "/stack/public/prj_x/../secret.txt"),
                   ("an encoded dot-dot", "/stack/public/prj_x/%2e%2e/secret.txt"),
                   ("a dot-dot inside the folder", "/stack/public/prj_x/HA/../../secret.txt"),
                   ("an encoded slash", "/stack/public/prj_x/..%2fsecret.txt"),
                   ("a doubled slash (an absolute path)", "/stack/public/prj_x//etc/hosts"),
                   ("a backslash", "/stack/public/prj_x/..%5csecret.txt"),
                   ("a dot-dot as the project", "/stack/public/../secret.txt"),
                   ("an encoded dot-dot as the project", "/stack/public/%2e%2e/secret.txt")):
    status, _, body = raw_get(bad)
    case(f"{label} is refused", status in (400, 404) and b"not for the web" not in body, str(status))

status, headers, _ = raw_get("/download")
case("/download is untouched: no CSP header", "Content-Security-Policy" not in headers
     and status in (200, 404))
status, headers, _ = raw_get("/sky")
case("/sky has none either", "Content-Security-Policy" not in headers)
status, headers, _ = raw_get("/api/v1/health")
case("...nor does the API", "Content-Security-Policy" not in headers)

# ------------------------------------------- live stacking: Discord notifications
print("\n-- live stacking: Discord notifications --")
import http.server                                                 # noqa: E402
from server import notify as notify_module                         # noqa: E402
from server.stack.projects import terms_version                   # noqa: E402

REAL_POST = notify_module._post
POSTS = []                 # (url, content) the fake poster was given
MODE = {"fail": ""}        # "" | "raise" | "hang"


def fake_post(url, content):
    if MODE["fail"] == "raise":
        raise OSError("connection refused")
    if MODE["fail"] == "hang":
        time.sleep(3)
    POSTS.append((url, content))


notify_module._post = fake_post     # nothing below may reach discord.com
HOOK = "https://discord.com/api/webhooks/1234567890/tok_en-ABC"
SFP = server_app.store.stack_project(FD_ID)["sf_project"]
COLLAB = lambda: server_app.store.project(SFP)                     # noqa: E731

for url, want in [(HOOK, True), ("", True), ("https://discordapp.com/api/webhooks/1/x", True),
                  ("http://discord.com/api/webhooks/1/x", False),
                  ("https://discord.com.evil.example/api/webhooks/1/x", False),
                  ("https://evil.example/api/webhooks/1/x", False),
                  ("https://discord.com/api/webhooks/abc/x", False),
                  ("https://discord.com/api/webhooks/1/x?wait=true", False)]:
    case(f"the webhook check: {url or 'blank'!r} is {'accepted' if want else 'refused'}",
         notify_module.webhook_ok(url) is want)

status, got = call("PUT", f"/api/v1/projects/{HUBP}", {"discordWebhookUrl": HOOK}, token=CAROL)
case("a coordinator who does not own the collaboration cannot set the webhook",
     status == 403 and COLLAB()["payload"].get("discordWebhookUrl") != HOOK, f"{status} {got}")
status, got = call("PUT", f"/api/v1/projects/{HUBP}", {"discordWebhookUrl": "http://discord.com/api/webhooks/1/x"}, token=BOBH)
case("the owner's webhook that is not Discord's: 400", status == 400, f"{status} {got}")
status, got = call("PUT", f"/api/v1/projects/{HUBP}", {"discordWebhookUrl": HOOK}, token=BOBH)
case("the owner saves a Discord webhook", status == 200 and COLLAB()["payload"].get("discordWebhookUrl") == HOOK,
     f"{status} {str(got)[:120]}")
status, got = call("PUT", f"/api/v1/projects/{HUBP}", {"notes": "Hub notes"}, token=BOBH)
case("...and a later edit that leaves it out keeps it",
     status == 200 and COLLAB()["payload"].get("discordWebhookUrl") == HOOK)
status, got = call("POST", "/api/v1/projects", {"name": "Notify Create", "region": REGION,
                   "discordWebhookUrl": "https://evil.example/x"}, token=BOBH)
case("a bad webhook on a new collaboration: 400, nothing made", status == 400, f"{status} {got}")
status, got = call("POST", "/api/v1/projects", {"name": "Notify Create", "region": REGION,
                   "discordWebhookUrl": HOOK}, token=BOBH)
CREATED = (got.get("project") or {}).get("id", "")
case("a webhook given on a new collaboration is saved",
     status == 200 and server_app.store.project(CREATED)["payload"].get("discordWebhookUrl") == HOOK)

# Hidden from everybody but the owner: listings, reads, agents, the front door.
status, listed = call("GET", "/api/v1/projects")
case("the public project list has no webhook", status == 200 and HOOK not in json.dumps(listed)
     and "discordWebhookUrl" not in json.dumps(listed))
status, read = call("GET", f"/api/v1/projects/{HUBP}")
case("a public read of the project has no webhook", status == 200 and "discordWebhookUrl" not in json.dumps(read))
status, agent_list = call("GET", "/api/v1/agent/projects", token=OWNER_RIG)
case("an agent listing (the owner's telescope too) has no webhook",
     status == 200 and HOOK not in json.dumps(agent_list) and "discordWebhookUrl" not in json.dumps(agent_list))
status, got = front("GET", f"/projects/{FD_ID}", token=OWNER_GRANT)
case("the front door has no webhook, for the owner's grant either",
     status == 200 and "discordWebhookUrl" not in json.dumps(got) and HOOK not in json.dumps(got))
case("nor does the stacking project row",
     HOOK not in json.dumps(server_app.store.stack_project(FD_ID)))

# Joins: a new active membership posts once; a re-join and a retry post nothing.
def joiner(sub, name):
    return hubs_module.mint_grant({"id": BH, "grant_secret": BHSEC}, sub, name, "agent-x", FD_ID)


JOIN_BODY = {"accepted_terms_version": terms_version()}
POSTS.clear()
status, got = front("POST", f"/projects/{FD_ID}/join", dict(JOIN_BODY, display_name="Notify One"),
                    token=joiner("n-1", "Notify One"))
notify_module.drain()
MEMBER_ONE = (got.get("membership") or {}).get("user_id", "")
case("a new member joins (200, active)", status == 200 and got["membership"]["status"] == "active", f"{status} {got}")
case("the join posts once, to the webhook, with the display name",
     POSTS == [(HOOK, f"Notify One joined {COLLAB()['name']} live stacking.")], str(POSTS))
status, got = front("POST", f"/projects/{FD_ID}/join", dict(JOIN_BODY, display_name="Notify One"),
                    token=joiner("n-1", "Notify One"))
notify_module.drain()
case("a second join of the same member is refused and posts nothing",
     status == 409 and len(POSTS) == 1, f"{status} {got} {len(POSTS)}")
notify_module.notify_member_joined(server_app.store, COLLAB(), "Notify One", member_id=MEMBER_ONE)
notify_module.drain()
case("a retried notification for that member posts nothing", len(POSTS) == 1)
claims = server_app.store._one("SELECT COUNT(*) AS n FROM stack_notified WHERE project_id = ? AND event = 'member_joined'", SFP)
case("the join is recorded once in stack_notified", claims and claims["n"] == 1, str(claims))

# A webhook that hangs, or errors, never holds up or breaks the join.
MODE["fail"] = "hang"
started = time.time()
status, got = front("POST", f"/projects/{FD_ID}/join", dict(JOIN_BODY, display_name="Notify Two"),
                     token=joiner("n-2", "Notify Two"))
took = time.time() - started
case("a webhook that hangs does not hold up the join", status == 200 and took < 2.0, f"{status} {took:.2f}s")
notify_module.drain()
MODE["fail"] = ""
case("...and the hung post still lands once it returns", POSTS[-1][1].startswith("Notify Two joined"), str(POSTS[-1:]))
MODE["fail"] = "raise"
status, got = front("POST", f"/projects/{FD_ID}/join", dict(JOIN_BODY, display_name="Notify Three"),
                     token=joiner("n-3", "Notify Three"))
notify_module.drain()
MODE["fail"] = ""
case("a webhook that errors does not break the join", status == 200 and got.get("ok") is True, f"{status} {got}")
case("...and a failed post is not sent again", not any("Notify Three" in c for _, c in POSTS))

# The real poster, against a local HTTP server: the body, the timeout and the errors.
LOCAL_HITS = []


class Local(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(size)
        if self.path == "/hang":
            time.sleep(8)
            return
        LOCAL_HITS.append((self.path, self.headers.get("Content-Type"), body))
        self.send_response(500 if self.path == "/err" else 204)
        self.end_headers()


local_port = free_port()
local = http.server.ThreadingHTTPServer(("127.0.0.1", local_port), Local)
threading.Thread(target=local.serve_forever, daemon=True).start()
LOCAL = f"http://127.0.0.1:{local_port}"
notify_module._post = REAL_POST
try:
    REAL_POST(f"{LOCAL}/ok", "hello")
    case("the poster sends {\"content\": ...} as JSON",
         LOCAL_HITS and LOCAL_HITS[-1][1] == "application/json"
         and json.loads(LOCAL_HITS[-1][2])["content"] == "hello", str(LOCAL_HITS[-1:]))
    try:
        REAL_POST(f"{LOCAL}/err", "x")
        raised = False
    except Exception:
        raised = True
    case("the poster raises on a 500 (and the caller swallows it)", raised)
    started = time.time()
    notify_module._deliver(f"{LOCAL}/hang", "x")
    case("a hung webhook is given up after the 5 s timeout, without raising",
         4.5 <= time.time() - started < 7.5, f"{time.time() - started:.2f}s")
finally:
    notify_module._post = fake_post
    local.shutdown()

# Gold integrations: once per (project, stream, gold_seq).
POSTS.clear()
notify_module.notify_gold_published(server_app.store, COLLAB(), "Ha", 3, 12.4, 4)
notify_module.notify_gold_published(server_app.store, COLLAB(), "Ha", 3, 12.4, 4)
notify_module.drain()
case("a gold integration posts its message once",
     POSTS == [(HOOK, "Gold integration 3 published for Ha: 12.4 h from 4 contributors.")], str(POSTS))
notify_module.notify_gold_published(server_app.store, COLLAB(), "Ha", 4, 12.4, 4)
notify_module.notify_gold_published(server_app.store, COLLAB(), "OIII", 3, 2.0, 1)
notify_module.drain()
case("a new gold_seq or stream is a new post", len(POSTS) == 3 and POSTS[-1][1].endswith("from 1 contributor."), str(POSTS))
notify_module.notify_gold_published(server_app.store, {"id": "prj_nohook", "name": "Quiet", "payload": {}},
                                    "Ha", 1, 1.0, 1)
notify_module.drain()
quiet = server_app.store._one("SELECT COUNT(*) AS n FROM stack_notified WHERE project_id = 'prj_nohook'")
case("a collaboration with no webhook posts nothing and claims nothing", len(POSTS) == 3 and quiet["n"] == 0)

# ------------------------------------------- live stacking: hub records
# The full member and rig rows a hub reads for the keys in its feed
# (server/stack/records.py), and the removal that reaches it.
print("\n-- live stacking: hub records --")
LEAVER_RIG, LEAVER_ID = enrol("records leaver", 389.0, 9576, 6388, 6.0)
status, leaver_join = call("POST", f"/api/v1/agent/projects/{HUBP}/join", {}, token=LEAVER_RIG)
LEAVER_GRANT = project_in_listing(LEAVER_RIG, HUBP)["stacking"]["grant"]
status, got = front("GET", f"/projects/{FD_ID}", token=LEAVER_GRANT)
terms_now = got["project"]["terms_version"]
status, got = front("POST", f"/projects/{FD_ID}/join", {"display_name": "Leaver", "accepted_terms_version": terms_now,
                    "filter_mapping": [{"raw": "Ha 7nm", "canonical": "HA"}], "code": FD_CODE.lower()}, token=LEAVER_GRANT)
LEAVER_UID = (got.get("membership") or {}).get("user_id", "")
status, got = front("POST", "/rigs", {"camera": "ASI294MC", "telescope": "RedCat 51", "focal_length_mm": 250,
                    "pixel_size_um": 4.63, "binning": 1, "bayer_pattern": "RGGB"}, token=LEAVER_GRANT)
LEAVER_RIGID = (got.get("rig") or {}).get("rig_id", "")
case("setup: a second member with a rig, joined on the hub's project", bool(LEAVER_UID and LEAVER_RIGID), str(got)[:160])

REC = f"/api/v1/hubs/{BH}/records"
status, got = call("GET", f"{REC}/member/{FD_ID}/{LEAVER_UID}", token=BHTOK)
case("a hub reads a member record: collab id, display name, role, status, mapping, terms, joined_at",
     status == 200 and got.get("user_id") == LEAVER_UID and got.get("display_name") == "Leaver"
     and got.get("role") == "contributor" and got.get("status") == "active"
     and got.get("filter_mapping") == [{"raw": "Ha 7nm", "canonical": "HA"}]
     and got.get("accepted_terms_version") == terms_now and got.get("joined_at"), f"{status} {got}")
status, got = call("GET", f"{REC}/member/{FD_ID}/no-such-user", token=BHTOK)
case("an unknown member is 404 with a plain detail", status == 404 and set(got) == {"detail"}, f"{status} {got}")
status, got = call("GET", f"{REC}/member/prj_unbound00000000000/{LEAVER_UID}", token=BHTOK)
case("a project that is not this hub's is 404", status == 404, f"{status} {got}")
status, got = call("GET", f"/api/v1/hubs/{BHB}/records/member/{FD_ID}/{LEAVER_UID}", token=BHBTOK)
case("another hub's token reads nothing of this hub's members", status == 404, f"{status} {got}")
status, got = call("GET", f"{REC}/member/{FD_ID}/{LEAVER_UID}", token=BHBTOK)
case("...and is refused on this hub's path", status == 401, f"{status} {got}")
status, got = call("GET", f"{REC}/member/{FD_ID}/{LEAVER_UID}")
case("no token is 401", status == 401, f"{status}")

status, got = call("GET", f"{REC}/rig/{LEAVER_UID}/{LEAVER_RIGID}", token=BHTOK)
case("a hub reads a member's rig",
     status == 200 and got.get("rig_id") == LEAVER_RIGID and got.get("user_id") == LEAVER_UID
     and got.get("camera") == "ASI294MC" and got.get("osc") is True and got.get("bayer_pattern") == "RGGB", f"{status} {got}")
status, got = call("GET", f"/api/v1/hubs/{BHB}/records/rig/{LEAVER_UID}/{LEAVER_RIGID}", token=BHBTOK)
case("another hub cannot read it", status == 404, f"{status} {got}")
STRANGER = "00000000-0000-4000-8000-000000000001"
server_app.store._run("INSERT INTO stack_rigs (user_id, rig_id, doc) VALUES (?, 'rig_000000000000', ?)",
                      STRANGER, json.dumps({"rig_id": "rig_000000000000", "name": "x", "camera": "c", "telescope": "t",
                                            "focal_length_mm": 1, "pixel_size_um": 1, "binning": 1, "created_at": "2026-01-01T00:00:00.000Z"}))
status, got = call("GET", f"{REC}/rig/{STRANGER}/rig_000000000000", token=BHTOK)
case("a rig whose owner is no member of the hub's projects is 404", status == 404, f"{status} {got}")

status, got = call("POST", f"{REC}/batch", {"member": [[FD_ID, LEAVER_UID], [FD_ID, "nope"], ["prj_unbound00000000000", LEAVER_UID]],
                                            "rig": [[LEAVER_UID, LEAVER_RIGID], [STRANGER, "rig_000000000000"]]}, token=BHTOK)
case("the batch answers what it can and lists the rest as missing",
     status == 200 and [m["user_id"] for m in got.get("member", [])] == [LEAVER_UID]
     and [r["rig_id"] for r in got.get("rig", [])] == [LEAVER_RIGID] and len(got.get("missing", [])) == 3
     and {"kind": "member", "key": f"{FD_ID}/nope"} in got["missing"], f"{status} {got}")
status, got = call("POST", f"{REC}/batch", {"member": [[FD_ID, f"u{i}"] for i in range(201)]}, token=BHTOK)
case("a batch over 200 keys is 400", status == 400 and set(got) == {"detail"}, f"{status} {str(got)[:100]}")
status, got = call("POST", f"{REC}/batch", {"member": [["only-one"]]}, token=BHTOK)
case("a malformed batch is 400", status == 400, f"{status}")
status, got = call("POST", f"{REC}/batch", {"member": []}, token=BHBTOK)
case("a batch with another hub's token is 401", status == 401, f"{status}")

bodies = json.dumps([call("GET", f"{REC}/member/{FD_ID}/{LEAVER_UID}", token=BHTOK)[1],
                     call("GET", f"{REC}/rig/{LEAVER_UID}/{LEAVER_RIGID}", token=BHTOK)[1],
                     call("POST", f"{REC}/batch", {"member": [[FD_ID, LEAVER_UID]], "rig": [[LEAVER_UID, LEAVER_RIGID]]}, token=BHTOK)[1]])
case("no Starfront or Discord id appears in any record body",
     LEAVER_ID not in bodies and not any(sid in bodies for sid in ("1002", JOINED_ID)), "")

before = feed_since(0)["seq"]
task = (leaver_join.get("task") or {})
status, _ = call("POST", f"/api/v1/agent/task/{task.get('id')}", {"state": "declined"}, token=LEAVER_RIG)
rows = [r for r in feed_since(before)["changes"] if r["kind"] == "member"]
case("a telescope leaving the collaboration appends a member feed row", status == 200
     and [r["key"] for r in rows] == [f"{FD_ID}/{LEAVER_UID}"], f"{status} {rows}")
status, got = call("GET", f"{REC}/member/{FD_ID}/{LEAVER_UID}", token=BHTOK)
case("...and the record answers status removed, still with the real name",
     status == 200 and got.get("status") == "removed" and got.get("display_name") == "Leaver" and got.get("removed_at"), f"{status} {got}")
status, got = call("GET", f"{REC}/rig/{LEAVER_UID}/{LEAVER_RIGID}", token=BHTOK)
case("...and its rig is no longer the hub's to read", status == 404, f"{status} {got}")
before = feed_since(0)["seq"]
status, _ = call("POST", f"/api/v1/agent/task/{task.get('id')}", {"state": "declined"}, token=LEAVER_RIG)
case("declining again changes nothing", status == 200 and not [r for r in feed_since(before)["changes"] if r["kind"] == "member"])

# Joining again undoes the Leave. On 2026-10-28 one press of the dock's Leave
# took the backyard rig off the Bubble Nebula for good: it could not rejoin
# (membership_removed) and a hand upload was refused (not_member).
before = feed_since(0)["seq"]
status, rejoin = call("POST", f"/api/v1/agent/projects/{HUBP}/join", {}, token=LEAVER_RIG)
rows = [r for r in feed_since(before)["changes"] if r["kind"] == "member"]
case("a telescope that left joins the collaboration again and the hub is told",
     status == 200 and [r["key"] for r in rows] == [f"{FD_ID}/{LEAVER_UID}"], f"{status} {rows}")
status, got = call("GET", f"{REC}/member/{FD_ID}/{LEAVER_UID}", token=BHTOK)
case("...its stacking membership is active again, with no removal left on it",
     status == 200 and got.get("status") == "active" and got.get("display_name") == "Leaver"
     and not any(got.get(k) for k in ("removed_at", "removed_by", "frames_disposition")), f"{status} {got}")
status, got = call("GET", f"{REC}/rig/{LEAVER_UID}/{LEAVER_RIGID}", token=BHTOK)
case("...its rig is the hub's to read again", status == 200 and got.get("rig_id") == LEAVER_RIGID, f"{status} {got}")
status, got = front("GET", f"/projects/{FD_ID}", token=project_in_listing(LEAVER_RIG, HUBP)["stacking"]["grant"])
case("...and the front door treats it as a member again (its frames would be taken)",
     status == 200 and (got["project"].get("my_membership") or {}).get("status") == "active", f"{status} {got}")


def stacking_member_doc():
    return json.loads(server_app.store._one("SELECT doc FROM stack_members WHERE project_id = ? AND user_id = ?",
                                            FD_ID, LEAVER_UID)["doc"])


def set_stacking_member_doc(doc):
    server_app.store._run("UPDATE stack_members SET doc = ? WHERE project_id = ? AND user_id = ?",
                          json.dumps(doc), FD_ID, LEAVER_UID)


# A member still waiting for approval who leaves and comes back is waiting again, not let in.
doc = stacking_member_doc()
doc["status"] = "pending"
set_stacking_member_doc(doc)
status, _ = call("POST", f"/api/v1/agent/task/{(rejoin.get('task') or {}).get('id')}", {"state": "declined"}, token=LEAVER_RIG)
status, rejoin = call("POST", f"/api/v1/agent/projects/{HUBP}/join", {}, token=LEAVER_RIG)
status, got = call("GET", f"{REC}/member/{FD_ID}/{LEAVER_UID}", token=BHTOK)
case("a pending member who leaves and joins again is pending again", got.get("status") == "pending", f"{got}")

# Any removal but the telescope's own is final: joining the collaboration again does not undo it.
status, _ = call("POST", f"/api/v1/agent/task/{(rejoin.get('task') or {}).get('id')}", {"state": "declined"}, token=LEAVER_RIG)
doc = stacking_member_doc()
doc["removed_by"] = "00000000-0000-4000-8000-0000000000aa"   # an owner's id, as the contract records it
set_stacking_member_doc(doc)
before = feed_since(0)["seq"]
status, rejoin = call("POST", f"/api/v1/agent/projects/{HUBP}/join", {}, token=LEAVER_RIG)
status, got = call("GET", f"{REC}/member/{FD_ID}/{LEAVER_UID}", token=BHTOK)
case("a removal that was not the telescope's own stays: joining again leaves it removed",
     got.get("status") == "removed" and not [r for r in feed_since(before)["changes"] if r["kind"] == "member"],
     f"{got}")

# The member is the person, not one telescope. On 2026-10-28 the owner's
# StellaLyra re-dealt its share (Leave, then Join four seconds later) while
# their backyard rig still held one, and the owner's member was removed.
PAIR_A, PAIR_A_ID = enrol("pair stellalyra", 800.0, 6248, 4176, 6.0)
PAIR_B, PAIR_B_ID = enrol("pair backyard", 600.0, 3840, 2160, 6.0)
server_app.store._run("UPDATE agents SET owner_id = ? WHERE id IN (?, ?)", "1777", PAIR_A_ID, PAIR_B_ID)
status, a_join = call("POST", f"/api/v1/agent/projects/{HUBP}/join", {}, token=PAIR_A)
status, b_join = call("POST", f"/api/v1/agent/projects/{HUBP}/join", {}, token=PAIR_B)
PAIR_GRANT = project_in_listing(PAIR_B, HUBP)["stacking"]["grant"]
status, got = front("POST", f"/projects/{FD_ID}/join", {"display_name": "Pair Owner", "accepted_terms_version": terms_now},
                    token=PAIR_GRANT)
PAIR_UID = (got.get("membership") or {}).get("user_id", "")
case("a person with two telescopes on a hub collaboration is one member", status == 200 and PAIR_UID, f"{status} {got}")


def pair_status():
    return call("GET", f"{REC}/member/{FD_ID}/{PAIR_UID}", token=BHTOK)[1].get("status")


before = feed_since(0)["seq"]
status, _ = call("POST", f"/api/v1/agent/task/{(a_join.get('task') or {}).get('id')}", {"state": "declined"}, token=PAIR_A)
case("one of a person's telescopes leaving, while the other is still on, leaves them a member",
     status == 200 and pair_status() == "active"
     and not [r for r in feed_since(before)["changes"] if r["kind"] == "member"], pair_status())

# The state the old rule left on 2026-10-28: removed by a telescope while the
# person was still on the collaboration. Pressing Join on a telescope that still
# holds its share (the dock's "Already on ...") gives the membership back.
doc = json.loads(server_app.store._one("SELECT doc FROM stack_members WHERE project_id = ? AND user_id = ?",
                                       FD_ID, PAIR_UID)["doc"])
doc.update(status="removed", removed_by="telescope", removed_at="2026-10-28T04:07:02.126Z", frames_disposition="keep")
server_app.store._run("UPDATE stack_members SET doc = ? WHERE project_id = ? AND user_id = ?",
                      json.dumps(doc), FD_ID, PAIR_UID)
status, again = call("POST", f"/api/v1/agent/projects/{HUBP}/join", {}, token=PAIR_B)
case("joining again with a telescope still on the project gives back a membership a leave took",
     status == 200 and again.get("alreadyJoined") and pair_status() == "active", f"{status} {pair_status()}")

status, _ = call("POST", f"/api/v1/agent/task/{(b_join.get('task') or {}).get('id')}", {"state": "declined"}, token=PAIR_B)
case("the last of a person's telescopes leaving does take the membership", pair_status() == "removed", pair_status())

# ------------------------------------------- live stacking: push
print("\n-- live stacking: push --")
import io as _io                                                   # noqa: E402
import secrets                                                     # noqa: E402
import tarfile as _tar                                             # noqa: E402

POSTS.clear()


def multipart(parts):
    """[(name, bytes)] as multipart/form-data; the `records` part is JSON."""
    boundary = "----t407" + secrets.token_hex(8)
    body = b""
    for name, data in parts:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name.rsplit("/", 1)[-1]}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n').encode() + data + b"\r\n"
    return boundary, body + f"--{boundary}--\r\n".encode()


def push(records, files=(), token=None, hub=None):
    parts = [("records", json.dumps(records).encode()), *files]
    boundary, body = multipart(parts)
    request = urllib.request.Request(
        f"{BASE}/api/v1/hubs/{hub or BH}/push", data=body, method="POST",
        headers={"Authorization": f"Bearer {token or BHTOK}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def tar_of(members):
    buf = _io.BytesIO()
    with _tar.open(fileobj=buf, mode="w") as t:
        for name, data in members:
            info = _tar.TarInfo(name)
            info.size = len(data)
            t.addfile(info, _io.BytesIO(data))
    return buf.getvalue()


def tree(root):
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file())


PUBDIR = DATA / "stack" / "public"
GRID = {"width_px": 6400, "height_px": 4400, "scale_arcsec_px": 1.5, "ra_deg": 84.0, "dec_deg": 0.0}
PUSH_FRAME = "frm_" + "0" * 20 + "pvshts"        # an uploaded frame of the owner's, to give a verdict
server_app.store._run("INSERT INTO stack_frames (frame_id, project_id, user_id, status, size, sha256, doc) VALUES (?, ?, ?, 'uploaded', 10, ?, ?)",
                      PUSH_FRAME, FD_ID, OWNER_UID, "ab" * 32, json.dumps({**json.loads(server_app.store._one("SELECT doc FROM stack_frames WHERE frame_id = ?", FA)["doc"]),
                                  "filename": "p.fits"}))
SECOND_PID = server_app.store.stack_project_of(SECOND_ID)["project_id"]
STATUS_DOC = {"schema_version": 1, "generated_at": "2026-10-22T01:00:00Z", "access": "public",
              "totals": {"frames_accepted": 7, "integration_s": 2100.0, "last_update": "2026-10-22T01:00:00Z"},
              "streams": [], "timeline": []}
DZ = tar_of([("HA/g000003/image.dzi", b"<Image/>"), ("HA/g000003/image_files/0/0_0.jpg", b"\xff\xd8\xff\xd9")])
MASTER_REC = {"stream_key": "HA", "gold_seq": 3, "format": "fits", "integration_s": 2100.0, "frames": 7,
              "sessions": 2, "contributors": 2, "licence": "members_only", "created_at": "2026-10-22T01:00:00Z"}
RECORDS = {
    "frames": [{"frame_id": PUSH_FRAME, "project_id": FD_ID, "status": "rejected", "reject_reason": "fwhm_too_high",
                "reject_detail": "FWHM 6.1 px", "metrics": {"fwhm_arcsec": 6.1, "n_stars": 120},
                "integration_s": 0, "verdict_at": "2026-10-22T00:10:00.000Z"}],
    "projects": [{"project_id": FD_ID, "grid": GRID, "grid_rev": 2}],
    "streams": [{"project_id": FD_ID, "stream_key": "HA", "gold_seq": 3, "gold_published_at": "2026-10-22T01:00:00Z",
                 "stats": {"frames_accepted": 7, "integration_s": 2100.0, "contributors": 2}}],
    "events": [{"project_id": FD_ID, "event_id": "evt_0123456789abcdefghjkmnpqrs", "ts": "2026-10-22T01:00:00Z",
                "type": "gold_published", "text": "Gold 3 published for HA", "stream_key": "HA"},
               {"project_id": FD_ID, "ts": "2026-10-22T00:30:00Z", "type": "frames_accepted",
                "text": "7 frames accepted", "count": 7}],
}
FILES = [("status.json", json.dumps(STATUS_DOC).encode()), ("HA/gold/latest.png", PNG),
         ("masters/HA_g000003_fits.json", json.dumps(MASTER_REC).encode()),
         ("masters/HA_g000003_fits.fits", b"SIMPLE  = T" + b"\0" * 50), ("deepzoom.tar", DZ)]

status, got = push(RECORDS, FILES, token="sfh_" + "0" * 64)
case("a push with an unknown hub token is 401", status == 401, str(status))
status, got = push(RECORDS, FILES, hub="hub_other", token=BHTOK)
case("a push to another hub's id is 401", status == 401, str(status))
status, got = push(RECORDS, FILES)
case("a push of records, status.json, a PNG, masters and a deep-zoom tar is accepted",
     status == 200 and got["ok"] and got["files"]["written"] == 6 and got["frames"]["merged"] == 1
     and got["projects"] == 1 and got["streams"] == 1 and got["events"] == 2, f"{status} {got}")
status, headers, body = raw_get(f"/stack/public/{FD_ID}/status.json")
case("status.json is served with the 60 s cache",
     status == 200 and json.loads(body) == STATUS_DOC and headers.get("Cache-Control") == "public, max-age=60")
status, headers, body = raw_get(f"/stack/public/{FD_ID}/HA/gold/latest.png")
case("the PNG is served as image/png", status == 200 and body == PNG and headers.get("Content-Type") == "image/png")
status, headers, body = raw_get(f"/stack/public/{FD_ID}/deepzoom/HA/g000003/image_files/0/0_0.jpg")
case("a deep-zoom tile from the tar is served immutable",
     status == 200 and headers.get("Cache-Control") == "public, max-age=31536000, immutable")
status, _, _ = raw_get(f"/stack/public/{FD_ID}/masters/HA_g000003_fits.fits")
case("masters are not served by the open route", status == 404, str(status))
case("no temp file is left behind", not [p for p in (PUBDIR / FD_ID).rglob("*") if p.name.endswith(".tmp")])

status, got = front("GET", f"/projects/{FD_ID}/frames/{PUSH_FRAME}", token=OWNER_GRANT)
case("the verdict shows in the frame (status, reason, detail, metrics)",
     status == 200 and got["frame"]["status"] == "rejected" and got["frame"]["reject_reason"] == "fwhm_too_high"
     and got["frame"]["reject_detail"] == "FWHM 6.1 px" and got["frame"]["metrics"]["n_stars"] == 120
     and conforms("FrameResponse", got), f"{status} {why('FrameResponse', got)}")
status, got = front("GET", f"/projects/{FD_ID}/frames?mine=1&status=rejected", token=OWNER_GRANT)
case("...and in frames?mine=1", status == 200 and any(f["frame_id"] == PUSH_FRAME for f in got["frames"]))
status, got = front("GET", f"/projects/{FD_ID}", token=OWNER_GRANT)
case("the project carries the pushed grid and the counts from status.json",
     got["project"]["grid"] == GRID and got["project"]["counts"]["frames_accepted"] == 7
     and got["project"]["counts"]["integration_s"] == 2100.0, str(got["project"].get("counts")))
case("the grid_rev is kept", server_app.store.stack_project(FD_ID)["doc"]["grid_rev"] == 2)

case("the gold was announced once", POSTS == [(HOOK, "Gold integration 3 published for HA: 0.6 h from 2 contributors.")], str(POSTS))
before, rev_before = tree(PUBDIR / FD_ID), server_app.store.stack_project(FD_ID)["rev"]
frame_rev = server_app.store._one("SELECT rev FROM stack_frames WHERE frame_id = ?", PUSH_FRAME)["rev"]
status, got = push(RECORDS, FILES)
notify_module.drain()
case("the same push again writes nothing and changes nothing",
     status == 200 and got["files"]["written"] == 0 and got["frames"] == {"merged": 0, "unchanged": 1, "skipped": 0}
     and got["projects"] == got["streams"] == got["events"] == 0
     and tree(PUBDIR / FD_ID) == before
     and server_app.store.stack_project(FD_ID)["rev"] == rev_before
     and server_app.store._one("SELECT rev FROM stack_frames WHERE frame_id = ?", PUSH_FRAME)["rev"] == frame_rev, str(got))
case("...and pushing the same gold twice notified once", len(POSTS) == 1, str(POSTS))
RECORDS2 = json.loads(json.dumps(RECORDS))
RECORDS2["streams"][0]["gold_seq"] = 4
push(RECORDS2, [])
notify_module.drain()
case("a higher gold_seq notifies again", len(POSTS) == 2 and "Gold integration 4 published for HA" in POSTS[-1][1], str(POSTS))
RECORDS2["streams"][0]["gold_seq"] = 3
push(RECORDS2, [])
notify_module.drain()
case("a lower gold_seq arriving late changes and announces nothing",
     len(POSTS) == 2 and server_app.store.stack_project(FD_ID)["doc"]["streams"]["HA"]["gold_seq"] == 4)
stale = json.loads(json.dumps(RECORDS))
stale["frames"][0].update(status="accepted", reject_reason=None, verdict_at="2026-10-22T00:00:00.000Z")
status, got = push(stale, [])
case("an older verdict does not overwrite a newer one", got["frames"]["skipped"] == 1
     and server_app.store._one("SELECT status FROM stack_frames WHERE frame_id = ?", PUSH_FRAME)["status"] == "rejected")

status, got = front("GET", f"/projects/{FD_ID}/status", token=OWNER_GRANT)
case("projects/{p}/status answers {ok, status_doc}",
     status == 200 and got["ok"] is True and got["status_doc"] == STATUS_DOC and set(got) == {"ok", "status_doc"}, f"{status} {got}")
status, got = front("GET", f"/projects/{FD_ID}/status")
case("...readable without a credential for an unlisted project", status == 200 and got["ok"])
status, got = front("GET", f"/projects/{SECOND_PID}/status", token=SECOND_GRANT)
case("...and a project with no status document is 400 not_found", status == 400 and got["code"] == "not_found", f"{status} {got}")
status, got = front("GET", f"/projects/{FD_ID}/masters", token=OWNER_GRANT)
case("the masters list answers the contract shape, one entry for the pushed fits",
     status == 200 and conforms("MasterListResponse", got) and len(got["masters"]) == 1
     and got["masters"][0]["stream_key"] == "HA" and got["masters"][0]["gold_seq"] == 3
     and got["masters"][0]["size_bytes"] == 61
     and got["masters"][0]["url"].endswith(f"/stack/api/projects/{FD_ID}/masters/HA_g000003_fits.fits"),
     f"{status} {why('MasterListResponse', got)}")
link = got["masters"][0]["url"].replace(BASE, "")
request = urllib.request.Request(BASE + link, headers={"Authorization": f"Bearer {OWNER_GRANT}"})
with urllib.request.urlopen(request, timeout=10) as response:
    data = response.read()
case("a member downloads the master through the gated link", data.startswith(b"SIMPLE"), str(len(data)))
status, got = front("GET", f"/projects/{FD_ID}/masters")
case("the masters list needs a member (401 not_authenticated)", status == 401 and got["code"] == "not_authenticated")
try:
    urllib.request.urlopen(BASE + link, timeout=10)
    case("...and so does the file", False)
except urllib.error.HTTPError as exc:
    case("...and so does the file", exc.code == 401, str(exc.code))
status, got = front("GET", f"/projects/{FD_ID}/events?limit=1", token=OWNER_GRANT)
case("events: newest first, paged, contract shape",
     status == 200 and conforms("EventListResponse", got) and [e["type"] for e in got["events"]] == ["gold_published"]
     and got["next_cursor"], f"{status} {why('EventListResponse', got)}")
status, got2 = front("GET", f"/projects/{FD_ID}/events?limit=1&cursor={got['next_cursor']}", token=OWNER_GRANT)
case("...the second page ends it", status == 200 and [e["type"] for e in got2["events"]] == ["frames_accepted"]
     and got2["next_cursor"] is None and conforms("EventListResponse", got2))
status, got = front("GET", f"/projects/{SECOND_PID}/events", token=SECOND_GRANT)
case("a project with no events answers an empty list", status == 200 and got["events"] == [] and got["next_cursor"] is None, f"{status} {got}")
status, got = front("GET", f"/projects/{FD_ID}/mosaic", token=OWNER_GRANT)
case("mosaic answers null for a single field, in the contract shape",
     status == 200 and got["mosaic"] is None and conforms("MosaicResponse", got))

# Refusals: nothing may be written.
OTHER = "hub_" + secrets.token_hex(8)
server_app.store.add_hub(OTHER, "1002", "Other hub", "0" * 64, "AAAA", "")
server_app.store.add_stack_project("prj_otherhub", "col_otherhub", OTHER, "other-hub-x", "ABCD-2345-EFGH", {"name": "Other"})
snapshot = tree(PUBDIR)
status, got = push({"frames": [], "projects": [{"project_id": "prj_otherhub", "grid": GRID, "grid_rev": 1}], "streams": []},
                   [("status.json", b"{}")])
case("a push for another hub's project is 409", status == 409, f"{status} {got}")
status, got = push({"projects": [{"project_id": FD_ID, "grid": GRID, "grid_rev": 9}]},
                   [("prj_otherhub/status.json", b"{}")])
case("...also through a file part's project prefix, with the rest of the push unwritten",
     status == 409 and server_app.store.stack_project(FD_ID)["doc"]["grid_rev"] == 2 and tree(PUBDIR) == snapshot)
status, got = push({"projects": [{"project_id": "prj_nosuchproject", "grid": GRID, "grid_rev": 1}]})
case("...and for a project that does not exist", status == 409)
for label, name in (("a dot-dot part name", "../escape.json"), ("a nested dot-dot", "HA/../../escape.json"),
                    ("an absolute part name", "/abs.json"), ("a backslash", "a\\b.json"),
                    ("a part of a kind not accepted", "run.sh"), ("a deep masters path", "masters/x/y.fits"),
                    ("a dot file", ".hidden.json")):
    status, got = push({"projects": [{"project_id": FD_ID, "grid": GRID, "grid_rev": 9}]}, [(name, b"{}"), ("ok.json", b"{}")])
    case(f"{label} is refused with nothing written", status == 400 and tree(PUBDIR) == snapshot
         and server_app.store.stack_project(FD_ID)["doc"]["grid_rev"] == 2 and not (DATA / "stack" / "escape.json").exists(), f"{status} {got}")
for label, members in (("a ../ tar member", [("../../escape.json", b"{}")]),
                       ("a tar member that is absolute", [("/etc/x.json", b"{}")]),
                       ("a tar member with a backslash", [("a\\b.json", b"{}")])):
    status, got = push({"projects": [{"project_id": FD_ID, "grid": GRID, "grid_rev": 9}]}, [(f"{FD_ID}/deepzoom.tar", tar_of(members))])
    case(f"{label} is refused with nothing written", status == 400 and tree(PUBDIR) == snapshot, f"{status} {got}")
buf = _io.BytesIO()
with _tar.open(fileobj=buf, mode="w") as t:
    link_info = _tar.TarInfo("HA/link.json")
    link_info.type, link_info.linkname = _tar.SYMTYPE, "../../../etc/passwd"
    t.addfile(link_info)
status, got = push({}, [(f"{FD_ID}/deepzoom.tar", buf.getvalue())])
case("a tar symlink member is refused", status == 400 and tree(PUBDIR) == snapshot, f"{status} {got}")
status, got = push({}, [(f"{FD_ID}/deepzoom.tar", b"not a tar at all")])
case("a deepzoom.tar that is not a tar is refused", status == 400)
outside = DATA / "stack" / "outside"
outside.mkdir(exist_ok=True)
(PUBDIR / FD_ID / "linked").symlink_to(outside, target_is_directory=True)
status, got = push({}, [(f"{FD_ID}/linked/x.json", b"{}")])
case("a path through a symlink inside the project is refused",
     status == 400 and not (outside / "x.json").exists(), f"{status} {got}")
(PUBDIR / FD_ID / "linked").unlink()
status, got = push({"frames": [{"frame_id": "nope", "project_id": FD_ID, "status": "accepted", "verdict_at": "2026-10-22T00:00:00Z"}]})
case("a malformed frame record is 400", status == 400)
status, got = push({"bogus": []})
case("an unknown records key is 400", status == 400)

os.environ["STARFRONT_STACK_HUB_BUDGET_GB"] = "0.000002"       # about 2 kB
status, got = push({}, [(f"{FD_ID}/big/a.png", b"\0" * 5000)])
case("over the hub's byte budget is 429, with nothing written", status == 429 and tree(PUBDIR) == snapshot, f"{status} {got}")
status, got = push({}, [(f"{FD_ID}/{n}", d) for n, d in FILES])
case("...while a re-push that adds nothing is still fine", status == 200, f"{status} {got}")
del os.environ["STARFRONT_STACK_HUB_BUDGET_GB"]
status, got = push({}, [(f"{FD_ID}/big/a.png", b"\0" * 5000)])
case("...and the default budget lets it in", status == 200 and (PUBDIR / FD_ID / "big" / "a.png").is_file(), f"{status} {got}")

# ------------------------------------------- live stacking: a streamed master (T421)
print("\n-- live stacking: a streamed master --")
import hashlib as _hashlib                                         # noqa: E402
import http.client as _hc                                          # noqa: E402
from urllib.parse import urlsplit as _urlsplit                     # noqa: E402


def put_master(project, name, chunks, sha=None, length=True, token=None, hub=None):
    """PUT a master in pieces; `chunks` is a list of bytes (a generator would do)."""
    total = sum(len(c) for c in chunks)
    parts = _urlsplit(BASE)
    conn = _hc.HTTPConnection(parts.hostname, parts.port, timeout=120)
    headers = {"Authorization": f"Bearer {token or BHTOK}", "Content-Type": "application/octet-stream"}
    if sha is not None:
        headers["X-Content-SHA256"] = sha
    if length:
        headers["Content-Length"] = str(total)
    try:
        conn.request("PUT", f"/api/v1/hubs/{hub or BH}/push/{project}/masters/{name}", body=iter(chunks),
                     headers=headers, encode_chunked=not length)
        response = conn.getresponse()
        raw = response.read()
        return response.status, (json.loads(raw) if raw[:1] == b"{" else raw)
    finally:
        conn.close()


BIG_PIECE = os.urandom(1 << 20)
BIG = [BIG_PIECE] * 40                                              # 40 MiB, made here, never committed
BIG_SHA = _hashlib.sha256(BIG_PIECE * 40).hexdigest()
BIG_REC = {**MASTER_REC, "gold_seq": 4, "format": "xisf", "file": "HA_g000004_xisf.xisf"}
MASTERS = PUBDIR / FD_ID / "masters"

status, got = push({}, [(f"{FD_ID}/masters/HA_g000004_xisf.json", json.dumps(BIG_REC).encode())])
case("the record of a big master goes by the multipart push", status == 200, f"{status} {got}")
status, got = front("GET", f"/projects/{FD_ID}/masters", token=OWNER_GRANT)
case("...and with no data file beside it the list holds only the earlier master",
     status == 200 and [m["gold_seq"] for m in got["masters"]] == [3], str(got))

status, got = put_master(FD_ID, "HA_g000004_xisf.xisf", BIG, sha=BIG_SHA, length=False)
case("a master without Content-Length is 411", status == 411, f"{status} {got}")
status, got = put_master(FD_ID, "HA_g000004_xisf.xisf", [b"x" * 100])
case("a master without X-Content-SHA256 is 400", status == 400, f"{status} {got}")
status, got = put_master(FD_ID, "HA_g000004_xisf.xisf", [b"x" * 100], sha="0" * 64, token="sfh_" + "0" * 64)
case("a master with an unknown hub token is 401", status == 401, f"{status} {got}")
status, got = put_master("prj_otherhub", "HA_g000004_xisf.xisf", [b"x" * 100], sha="0" * 64)
case("a master for another hub's project is 409", status == 409, f"{status} {got}")
for bad in ("..%2Fx.fits", "a%2Fb.fits", ".hidden.fits", "run.sh", "x.json", "x.png"):
    status, got = put_master(FD_ID, bad, [b"x" * 100], sha="0" * 64)
    case(f"the master name {bad!r} is refused", status in (400, 404, 422) and status != 200, f"{status}")
status, got = put_master(FD_ID, "HA_g000004_xisf.xisf", [b"x" * 100], sha="0" * 64)
case("a wrong digest is 422 and nothing is kept (no file, no temp file)",
     status == 422 and not (MASTERS / "HA_g000004_xisf.xisf").exists()
     and not [q for q in MASTERS.iterdir() if q.name.endswith(".tmp")], f"{status} {got}")
status, got = put_master(FD_ID, "HA_g000004_xisf.xisf", BIG, sha="f" * 64)
case("...also for a 40 MiB body", status == 422 and not (MASTERS / "HA_g000004_xisf.xisf").exists()
     and not [q for q in MASTERS.iterdir() if q.name.endswith(".tmp")], f"{status} {got}")

os.environ["STARFRONT_STACK_HUB_BUDGET_GB"] = "0.01"             # about 10 MB: less than the master
status, got = put_master(FD_ID, "HA_g000004_xisf.xisf", BIG, sha=BIG_SHA)
case("a master that would pass the hub's byte budget is 429, nothing kept",
     status == 429 and not (MASTERS / "HA_g000004_xisf.xisf").exists(), f"{status} {got}")
del os.environ["STARFRONT_STACK_HUB_BUDGET_GB"]

status, got = put_master(FD_ID, "HA_g000004_xisf.xisf", BIG, sha=BIG_SHA)
case("a 40 MiB master streams in", status == 200 and got["ok"] and got["written"] is True
     and (MASTERS / "HA_g000004_xisf.xisf").stat().st_size == 40 << 20, f"{status} {got}")
case("...with no temp file left", not [q for q in MASTERS.iterdir() if q.name.endswith(".tmp")])
status, got = front("GET", f"/projects/{FD_ID}/masters", token=OWNER_GRANT)
big = [m for m in (got.get("masters") or []) if m.get("gold_seq") == 4]
case("...and a member's masters list now holds it",
     status == 200 and conforms("MasterListResponse", got) and len(big) == 1
     and big[0]["size_bytes"] == 40 << 20 and big[0]["format"] == "xisf"
     and big[0]["url"].endswith(f"/masters/HA_g000004_xisf.xisf"), f"{status} {why('MasterListResponse', got)}")
status, got = front("GET", f"/projects/{FD_ID}/masters", token=SECOND_GRANT)
case("...and a non-member does not get the list", status in (401, 400) and not got.get("masters"), f"{status} {got}")
status, _, _ = raw_get(f"/stack/public/{FD_ID}/masters/HA_g000004_xisf.xisf")
case("...nor the file from the open route", status == 404, str(status))
before = (MASTERS / "HA_g000004_xisf.xisf").stat().st_mtime_ns
status, got = put_master(FD_ID, "HA_g000004_xisf.xisf", BIG, sha=BIG_SHA)
case("the same master again is a 200 that rewrites nothing",
     status == 200 and got["written"] is False and (MASTERS / "HA_g000004_xisf.xisf").stat().st_mtime_ns == before,
     f"{status} {got}")

# ------------------------------------------- live stacking: deleting a hub-backed collaboration (T704, D004)
# Delete files a 7-day restorable deletion: the doc is `deleted`, the hub is told,
# joins and uploads are 409 project_deleted; restore works inside the window; the
# sweep after it removes this project's relay bytes (keeping rows, status.json and
# masters); the owner removes the kept results explicitly.
print("\n-- live stacking: deleting a hub-backed collaboration --")
from server.stack import deletion as stack_deletion               # noqa: E402

DEL_PUB = PUBDIR / FD_ID
DEL_PUB.mkdir(parents=True, exist_ok=True)
(DEL_PUB / "status.json").write_text(json.dumps({"schema": 1, "kept": True}))
(DEL_PUB / "masters").mkdir(exist_ok=True)
(DEL_PUB / "masters" / "kept_master.json").write_text("{}")
DEL_DATA = fits_bytes(PART + 500, 77)
DEL_FRAME = upload_frame(DEL_DATA, "to_be_purged.fits")
DEL_ROWS = server_app.store._one("SELECT COUNT(*) AS n FROM stack_frames WHERE project_id = ?", FD_ID)["n"]
case("(setup) the relay holds a frame of the project", relay_path(DEL_FRAME).is_file())
DEL_FEED = feed_since(0)["seq"]


def stack_doc():
    return server_app.store.stack_project(FD_ID)["doc"]


def collab_status():
    return server_app.store.project(HUBP)["status"]


was_status, was_collab = stack_doc()["status"], collab_status()
status, got = call("DELETE", f"/api/v1/projects/{HUBP}", token=CAROL)
case("a person who did not start the collaboration cannot delete it: 403",
     status == 403 and stack_doc()["status"] == was_status, f"{status} {got}")
status, got = call("DELETE", f"/api/v1/projects/{HUBP}")
case("deleting without signing in is refused", status in (401, 403), str(status))
_, plain = call("POST", "/api/v1/projects", {"name": "Plain one", "region": REGION,
                                          "requirements": {"filters": {"Ha": 3.0}},
                                          "stacking": {"backend": ""}}, token=BOBH)
status, got = call("DELETE", f"/api/v1/projects/{plain['project']['id']}", token=BOBH)
case("a collaboration not stacked on a hub is not deleted this way: 400", status == 400, f"{status} {got}")

now = time.time()
status, got = call("DELETE", f"/api/v1/projects/{HUBP}", token=BOBH)
d = stack_doc()
case("the owner deletes it: the stacking doc is deleted with deleted_at, delete_after (+7 days) and deleted_by",
     status == 200 and d["status"] == "deleted" and d["deleted_by"] and d["deleted_by_role"] == "owner"
     and abs(stack_deletion.epoch(d["delete_after"]) - (now + 7 * 86400)) < 60
     and abs(stack_deletion.epoch(d["deleted_at"]) - now) < 60, f"{status} {got} {d}")
case("...the collaboration itself is closed, so it leaves everybody's list", collab_status() == "closed")
changes = [c for c in feed_since(DEL_FEED)["changes"] if c["kind"] == "project" and c["key"] == FD_ID]
case("...and a project row is in the hub's feed", len(changes) == 1, str(changes))
status, got = call("DELETE", f"/api/v1/projects/{HUBP}", token=BOBH)
case("deleting it again: 409", status == 409, f"{status} {got}")
status, got = call("PUT", f"/api/v1/projects/{HUBP}", {"notes": "x"}, token=BOBH)
case("a deleted collaboration cannot be edited: 409", status == 409, f"{status} {got}")

status, got = init_for(fits_bytes(5000, 78))
case("an upload to the deleted project is 409 project_deleted",
     status == 409 and got.get("code") == "project_deleted", f"{status} {got}")
status, got = front("POST", f"/projects/{FD_ID}/join", {"display_name": "Late Comer", "accepted_terms_version": "2026-10-13-draft"},
                    token=OWNER_GRANT)
case("a join is 409 project_deleted", status == 409 and got.get("code") == "project_deleted", f"{status} {got}")
status, got = front("GET", f"/projects/{FD_ID}", token=OWNER_GRANT)
case("a member still reads it: status deleted with delete_after and deleted_at, results kept",
     status == 200 and got["project"]["status"] == "deleted" and got["project"].get("delete_after") == d["delete_after"]
     and got["project"].get("deleted_at") and got["project"].get("results_kept") is True, f"{status} {got}")

status, got = call("POST", f"/api/v1/projects/{HUBP}/restore", token=CAROL)
case("restoring is the owner's: 403 for someone else", status == 403, f"{status} {got}")
before_restore = feed_since(0)["seq"]
status, got = call("POST", f"/api/v1/projects/{HUBP}/restore", token=BOBH)
d = stack_doc()
case("the owner restores it within the window: back to what it was, nothing of the deletion left",
     status == 200 and d["status"] == was_status and collab_status() == was_collab
     and not any(k in d for k in ("deleted_at", "delete_after", "deleted_by", "status_before_delete")), f"{status} {got} {d}")
case("...with another project row in the hub's feed",
     [c for c in feed_since(before_restore)["changes"] if c["kind"] == "project" and c["key"] == FD_ID] != [])
status, got = init_for(fits_bytes(5000, 79))
case("...and uploads work again", status == 200, f"{status} {got}")
status, got = call("POST", f"/api/v1/projects/{HUBP}/restore", token=BOBH)
case("restoring what is not deleted: 409", status == 409, f"{status} {got}")

status, got = call("DELETE", f"/api/v1/projects/{HUBP}", token=BOBH)
status_w, got_w = call("DELETE", f"/api/v1/projects/{HUBP}/stack/results", token=BOBH)
case("the kept results cannot be removed inside the window: 409", status_w == 409
     and (DEL_PUB / "status.json").is_file(), f"{status_w} {got_w}")
status_s = server_app.STACK_RELAY.sweep()
case("the sweep inside the window removes nothing of the project",
     relay_path(DEL_FRAME).is_file() and not stack_doc().get("relay_purged_at"), str(status_s))

# Past the window: the sweep purges the relay bytes.
d = stack_doc()
d["delete_after"] = stack_deletion.stamp(time.time() - 60)
server_app.store.set_stack_project(FD_ID, server_app.store.stack_project(FD_ID)["hub"], d)
status, got = call("POST", f"/api/v1/projects/{HUBP}/restore", token=BOBH)
case("after the window a restore is refused with a clear message",
     status == 409 and "window ended" in got.get("detail", "") and d["delete_after"][:10] in got["detail"], f"{status} {got}")
DEL_ROWS = server_app.store._one("SELECT COUNT(*) AS n FROM stack_frames WHERE project_id = ?", FD_ID)["n"]
DATA_DL = fits_bytes(6000, 81)
status, got = lib_init(DATA_DL, "dark_purge.fits")
DEL_LIB = got["frame_id"]
etag = put_part(got["upload"]["part_urls"][0]["url"], DATA_DL)[1]["ETag"]
front("POST", f"{LIB}/frames/{DEL_LIB}/complete", {"etags": [{"part_number": 1, "etag": etag}]}, token=OWNER_GRANT)
LIB_FILES = [RELAY / server_app.store._one("SELECT path FROM stack_library WHERE frame_id = ?", DEL_LIB)["path"]]
case("(setup) the owner has a library frame on the relay", LIB_FILES[0].is_file())
OTHER_LIVE = server_app.store._all(
    "SELECT m.project_id FROM stack_members m JOIN stack_projects p ON p.project_id = m.project_id"
    " WHERE m.user_id = ? AND m.project_id <> ? AND p.hub <> ''", OWNER_UID, FD_ID)
case("a library frame of someone still in another live hub project is not this project's alone (when they are)",
     not OTHER_LIVE or DEL_LIB not in stack_deletion.library_only_for(
         server_app.store, server_app.store.stack_project(FD_ID)), str(OTHER_LIVE))
for o in OTHER_LIVE:
    server_app.store._run("UPDATE stack_members SET doc = json_set(doc, '$.status', 'removed')"
                          " WHERE project_id = ? AND user_id = ?", o["project_id"], OWNER_UID)
case("...and it is once they are in no other", DEL_LIB in stack_deletion.library_only_for(
    server_app.store, server_app.store.stack_project(FD_ID)))
LIB_ROWS = server_app.store._one("SELECT COUNT(*) AS n FROM stack_library")["n"]
FRAME_FILES = [relay_path(r["frame_id"]) for r in server_app.store._all(
    "SELECT frame_id FROM stack_frames WHERE project_id = ? AND path <> ''", FD_ID)]
case("(setup) there are relay files to purge", DEL_FRAME in [r["frame_id"] for r in server_app.store._all(
    "SELECT frame_id FROM stack_frames WHERE project_id = ?", FD_ID)] and any(p.is_file() for p in FRAME_FILES))
server_app.STACK_RELAY.sweep()
case("the sweep after the window deletes the project's relay bytes, and the library frames only it owned",
     not any(p.is_file() for p in FRAME_FILES + LIB_FILES), str([str(p) for p in FRAME_FILES if p.is_file()]))
case("...keeps every frame row (marked relay_deleted)",
     server_app.store._one("SELECT COUNT(*) AS n FROM stack_frames WHERE project_id = ?", FD_ID)["n"] == DEL_ROWS
     and json.loads(server_app.store._one("SELECT doc FROM stack_frames WHERE frame_id = ?", DEL_FRAME)["doc"]).get("relay_deleted")
     and all(server_app.store._one("SELECT 1 AS x FROM stack_library WHERE path = ?", p.relative_to(RELAY).as_posix()) for p in LIB_FILES))
case("...and the library rows", server_app.store._one("SELECT COUNT(*) AS n FROM stack_library")["n"] == LIB_ROWS)
case("...keeps status.json and masters/ (D004)",
     (DEL_PUB / "status.json").is_file() and (DEL_PUB / "masters" / "kept_master.json").is_file())
case("...and records that the relay was purged", bool(stack_doc().get("relay_purged_at")))
status, _, body = raw_get(f"/stack/public/{FD_ID}/status.json")
case("...so the project page's status.json is still served", status == 200 and b"kept" in body, str(status))
status, got = front("GET", f"/projects/{FD_ID}", token=OWNER_GRANT)
case("...and the project still reads, as deleted with its results kept",
     status == 200 and got["project"]["status"] == "deleted" and got["project"]["results_kept"] is True)
server_app.STACK_RELAY.sweep()
case("a second sweep is quiet", server_app.STACK_RELAY.purge_deleted(time.time()) == 0)

status, got = call("DELETE", f"/api/v1/projects/{HUBP}/stack/results", token=CAROL)
case("a person who is not the owner cannot remove the kept results: 403",
     status == 403 and (DEL_PUB / "status.json").is_file(), f"{status} {got}")
status, got = call("DELETE", f"/api/v1/projects/{HUBP}/stack/results", token=BOBH)
case("the owner removes the kept results: the public folder is gone",
     status == 200 and not DEL_PUB.exists() and stack_doc().get("results_removed_at"), f"{status} {got}")
status, got = front("GET", f"/projects/{FD_ID}", token=OWNER_GRANT)
case("...and the project says the results are no longer kept",
     status == 200 and got["project"]["results_kept"] is False, f"{status} {got}")
status, _, _ = raw_get(f"/stack/public/{FD_ID}/status.json")
case("...status.json is 404", status == 404, str(status))


# ------------------------------------- live stacking: it flows down from the collaboration
# T706, the author's rule (2026-10-22): the Starfront collaboration is the only
# place a project is made, and everything the stacking engine needs flows down
# from it. Stacking is on by default; every edit reaches the stacking project
# and the hub's feed; nothing about a project can be changed on /stack/api; and
# a cloud project is made and kept in step by this server through the Collab
# API (checked against Rapid Stacker Collab's local stack when it is at hand).
print("\n-- live stacking: it flows down from the collaboration (T706) --")
_, _, _, polled = sign_in("code-dave")
DAVE = polled["token"]
_, _, _, polled = sign_in("code-bob")
BOBF = polled["token"]
_, _, _, polled = sign_in("code-carol")
CAROLF = polled["token"]


def binding_of(project_id):
    return server_app._stacking_of(server_app.store.project(project_id)["payload"])


# -- 1. on by default
status, opts = call("GET", "/api/v1/stacking", token=DAVE)
case("a person with no hub and no Collab link is told stacking is off by default",
     status == 200 and opts["default"]["backend"] == "" and opts["hubs"] == []
     and opts["cloud"]["linked"] is False, str(opts)[:200])
status, made = call("POST", "/api/v1/projects", {"name": "Dave plain", "region": REGION}, token=DAVE)
case("...and a collaboration they start is not stacked", status == 200
     and binding_of(made["project"]["id"])["backend"] == "", str(made)[:160])
status, opts = call("GET", "/api/v1/stacking", token=BOBF)
BOB_HUBS = [h["id"] for h in server_app.store.hubs_of("1002")]
case("a hub owner's default is one of their own hubs, named",
     status == 200 and opts["default"]["backend"] == "hub" and opts["default"]["hubId"] in BOB_HUBS
     and opts["default"]["hubName"] and all(h["role"] == "owner" for h in opts["hubs"]), str(opts)[:240])
BOB_DEFAULT = opts["default"]["hubId"]
status, made = call("POST", "/api/v1/projects", {
    "name": "Default Bound", "region": REGION, "requirements": {"filters": {"Ha": 3.0}}}, token=BOBF)
DEF_P = made["project"]["id"]
row = server_app.store.stack_project_of(DEF_P) or {}
case("a collaboration started with nothing said is bound to that hub, with its stacking project made",
     status == 200 and binding_of(DEF_P)["backend"] == "hub" and binding_of(DEF_P)["hubId"] == BOB_DEFAULT
     and row.get("hub") == BOB_DEFAULT and row["doc"]["visibility"] == "unlisted", str(binding_of(DEF_P)))
status, made = call("POST", "/api/v1/projects", {
    "name": "Plugin form", "region": REGION, "stackingJoinCode": ""}, token=BOBF)
case("...as is one from a plugin's form (a blank stackingJoinCode)",
     status == 200 and binding_of(made["project"]["id"])["backend"] == "hub")
status, made = call("POST", "/api/v1/projects", {
    "name": "Off on purpose", "region": REGION, "stacking": {"backend": ""}}, token=BOBF)
case("...while {backend: ''} turns it off", status == 200 and binding_of(made["project"]["id"])["backend"] == ""
     and server_app.store.stack_project_of(made["project"]["id"]) is None)
status, opts = call("GET", "/api/v1/stacking", token=CAROLF)
case("a coordinator the owner listed on one hub sees it (role coordinator) as the default",
     status == 200 and opts["default"] == {"backend": "hub", "hubId": BH, "hubName": "Bind hub"}
     and [h["role"] for h in opts["hubs"]] == ["coordinator"], str(opts)[:240])
status, made = call("POST", "/api/v1/projects", {"name": "Carol default", "region": REGION}, token=CAROLF)
case("...and a collaboration they start is bound to it",
     status == 200 and binding_of(made["project"]["id"])["hubId"] == BH)

# -- 2. every field flows down (hub)
FLOW_REQ = {"filters": {"Ha": 3.0, "OIII": 3.0, "OSC-RGB": None, "OSC-Ha+OIII": None},
            "acceptColour": True, "acceptOscRgb": True, "acceptOscNarrowband": True,
            "minExposure": 30, "requireCalibrated": True, "maxHfr": 4.0, "maxGuideRms": 1.0}
status, made = call("POST", "/api/v1/projects", {
    "name": "Flow Down", "region": REGION, "notes": "first notes", "requirements": FLOW_REQ,
    "stacking": {"backend": "hub", "hubId": BH}}, token=BOBF)
FLOW = made["project"]["id"]
FLOW_ID = server_app.store.stack_project_of(FLOW)["project_id"]


def flow_doc():
    return server_app.store.stack_project_of(FLOW)["doc"]


d = flow_doc()
case("the requirements become the stacking project's filters: Ha, OIII, a colour camera's OSC and DUO",
     d["filters"] == ["HA", "OIII", "OSC", "DUO"] and d["palette"] == "HOO", str(d["filters"]))
case("...and its policy: colour cameras taken, calibrated only, the shortest sub, a star-size cap",
     d["policy"]["osc_allowed"] is True and d["policy"]["calibration"] == "required"
     and d["policy"]["min_exposure_s"] == 30 and d["policy"]["max_fwhm_arcsec"] == 8.0
     and set(d["policy"]) == set(stack_defaults.POLICY), str(d["policy"]))
case("...the rest of the policy stays the contract's defaults (guiding has no policy field)",
     all(d["policy"][k] == v for k, v in stack_defaults.POLICY.items()
         if k not in ("osc_allowed", "calibration", "min_exposure_s", "max_fwhm_arcsec")))
case("a policy for a collaboration that takes no colour data keeps osc_allowed off and lists no OSC",
     server_app._stack_fields("x", "", {"requirements": {"filters": {"Ha": 3.0, "OSC-RGB": None},
                                                         "acceptColour": False}}, {})["policy"]["osc_allowed"] is False
     and server_app._stack_fields("x", "", {"requirements": {"filters": {"Ha": 3.0, "OSC-RGB": None},
                                                             "acceptColour": False}}, {})["filters"] == ["HA"])
case("...nor for one that lists only mono filters (Starfront's own rule: an OSC band must be listed)",
     server_app._stack_fields("x", "", {"requirements": {"filters": {"Ha": 3.0}}}, {})["policy"]["osc_allowed"] is False)
case("...and a star-size limit never tightens the contract's FWHM cap",
     server_app._policy_for({"maxHfr": 1.5}, [])["max_fwhm_arcsec"] == 6
     and server_app._policy_for({"maxHfr": 30}, [])["max_fwhm_arcsec"] == 20.0)
FLOW_SEQ = call("GET", f"/api/v1/hubs/{BH}/pull?since=0&wait=0", token=BHTOK)[1]["seq"]


def hub_told():
    global FLOW_SEQ
    got = call("GET", f"/api/v1/hubs/{BH}/pull?since={FLOW_SEQ}&wait=0", token=BHTOK)[1]
    FLOW_SEQ = got["seq"]
    return any(c["kind"] == "project" and c["key"] == FLOW_ID for c in got["changes"])


HUB_GRANT = hubs_module.mint_grant(server_app.store.hub(BH), "hub-sync:" + BH, "Hub sync", "", FLOW_ID)


def hub_view():
    """What the hub reads on a `project` row: GET /stack/api/projects/{p} with its own grant."""
    return front("GET", f"/projects/{FLOW_ID}", token=HUB_GRANT)[1]["project"]


EDITS = [
    ("the name", {"name": "Flow Renamed"}, lambda p: p["name"] == "Flow Renamed" and p["target_name"] == "Flow Renamed"),
    ("the notes", {"notes": "second notes"}, lambda p: p["description"] == "second notes"),
    ("the region", {"region": {"ra": 120.0, "dec": 20.0, "width": 4.0, "height": 2.0}},
     lambda p: p["grid"]["center_ra_deg"] == 120.0 and p["grid"]["center_dec_deg"] == 20.0),
    ("the filters", {"requirements": {**FLOW_REQ, "filters": {"R": None, "G": None, "B": None}}},
     lambda p: p["filters"] == ["R", "G", "B"] and p["palette"] == "RGB" and p["policy"]["osc_allowed"] is False),
    ("the requirements", {"requirements": {"filters": {"Ha": 3.0}, "minExposure": 120}},
     lambda p: p["policy"]["min_exposure_s"] == 120 and p["policy"]["calibration"] == "preferred"),
    ("the visibility", {"stacking": {"backend": "hub", "hubId": BH, "visibility": "private"}},
     lambda p: p["visibility"] == "private"),
    ("closing it", {"status": "closed"}, lambda p: p["status"] == "closed"),
    ("reopening it", {"status": "open"}, lambda p: p["status"] == "open"),
]
for what, change, seen in EDITS:
    status, _ = call("PUT", f"/api/v1/projects/{FLOW}", change, token=BOBF)
    view = hub_view()
    case(f"editing {what} reaches the stacking project, the hub's feed and what the hub reads",
         status == 200 and seen(view) and hub_told(), f"{status} {str(view)[:200]}")
status, _ = call("PUT", f"/api/v1/projects/{FLOW}", {"goals": {"Ha": 20.0}}, token=BOBF)
case("an edit the stacking project has no field for does not trouble the hub", status == 200 and not hub_told())

# -- 3. /stack/api cannot change a project
OWNER_FLOW_GRANT = project_in_listing(OWNER_RIG, FLOW)["stacking"]["grant"]
case("a hub-bound collaboration is listed ready to join, with no problem to report",
     project_in_listing(OWNER_RIG, FLOW)["stacking"]["state"] == "ready"
     and project_in_listing(OWNER_RIG, FLOW)["stacking"]["problem"] == "")
status, joined = front("POST", f"/projects/{FLOW_ID}/join", {
    "display_name": "Bob Flow", "accepted_terms_version": "2026-10-13-draft"}, token=OWNER_FLOW_GRANT)
case("(setup) the owner's telescope joins the stacking project with its grant", status == 200, f"{status} {joined}")
before = flow_doc()
ATTEMPTS = [("PUT", f"/projects/{FLOW_ID}", {"name": "Hijacked", "visibility": "public"}),
            ("PATCH", f"/projects/{FLOW_ID}", {"name": "Hijacked"}),
            ("DELETE", f"/projects/{FLOW_ID}", None),
            ("POST", "/projects", {"slug": "second-project", "name": "Second", "filters": ["HA"],
                                   "owner_display_name": "Bob"}),
            ("POST", f"/projects/{FLOW_ID}/delete", {"confirm_slug": before["slug"]}),
            ("POST", f"/projects/{FLOW_ID}/restore", {}),
            ("POST", f"/projects/{FLOW_ID}/rotate-join-code", {}),
            ("PUT", f"/projects/{FLOW_ID}/mosaic", {}),
            ("PUT", f"/projects/{FLOW_ID}/members/{joined['membership']['user_id']}", {"role": "owner"}),
            ("DELETE", f"/projects/{FLOW_ID}/members/{joined['membership']['user_id']}", None),
            ("PUT", f"/projects/{FLOW_ID}/filters/mapping", {"filter_mapping": []}),
            ("POST", f"/projects/{FLOW_ID}/gold", {})]
refused = []
for method, path, body in ATTEMPTS:
    status, got = front(method, path, body, token=OWNER_FLOW_GRANT)
    if 200 <= status < 300:
        refused.append(f"{method} {path} -> {status}")
after = flow_doc()
case("no /stack/api route creates, edits, deletes or re-keys a project, even for its owner's grant",
     not refused and {k: v for k, v in after.items() if k != "updated_at"}
     == {k: v for k, v in before.items() if k != "updated_at"}, "; ".join(refused))
status, got = front("PUT", f"/projects/{FLOW_ID}/membership", {"role": "owner", "status": "active"},
                    token=OWNER_FLOW_GRANT)
case("a member's own membership cannot raise its role or status",
     status == 400 and got.get("code") == "validation_failed", f"{status} {got}")
status, got = front("PUT", f"/projects/{FLOW_ID}/membership", {"display_name": "Bob Flowing"},
                    token=OWNER_FLOW_GRANT)
others = server_app.store._all("SELECT doc FROM stack_members WHERE project_id = ? AND user_id <> ?",
                               FLOW_ID, joined["membership"]["user_id"])
case("...and what it may change touches only its own row",
     status == 200 and got["membership"]["display_name"] == "Bob Flowing"
     and all("Bob Flowing" not in r["doc"] for r in others), f"{status} {got}")

# -- 4. the cloud, through the Collab API
import subprocess as _sp2                                         # noqa: E402
RSC_DIR = None
# RSC_DIR, when set, is the only place looked (set it to an empty folder to run
# the cloud path against the stand-in below even with a checkout beside this one).
for candidate in ([Path(os.environ["RSC_DIR"])] if os.environ.get("RSC_DIR") else [
        ROOT.parent / "Rust" / "rapid-stacker-collab", ROOT.parent.parent / "Rust" / "rapid-stacker-collab"]):
    if (candidate / "backend" / "local" / "server.mjs").is_file():
        RSC_DIR = candidate
        break
STACK_PROC, RSC = None, ""
if RSC_DIR is not None:
    import shutil as _shutil
    if _shutil.which("node"):
        STACK_PROC = _sp2.Popen(["node", str(RSC_DIR / "backend" / "local" / "server.mjs"), "--port", "0",
                                 "--quiet", "--exit-on-stdin-close"], stdin=_sp2.PIPE, stdout=_sp2.PIPE,
                                stderr=_sp2.PIPE, text=True)
        first = STACK_PROC.stdout.readline()
        found = _re.search(r"(http://127\.0\.0\.1:\d+)", first or "")
        if found:
            RSC = found.group(1)
        else:
            reason = (STACK_PROC.stderr.read() or "").strip().splitlines()
            print("NOTE  the cloud path: Rapid Stacker Collab's local stack did not start ("
                  + (next((l for l in reason if l.startswith("Error")), reason[-1] if reason else "no output"))[:160]
                  + "); set RSC_DIR to a checkout with backend/node_modules and no lambdas/*/node_modules")
            STACK_PROC = None
    else:
        print("NOTE  the cloud path: node is not installed")
else:
    print("NOTE  the cloud path: no rapid-stacker-collab checkout (in RSC_DIR, or beside this repository)")


# A stand-in for Rapid Stacker Collab's API, for where its local stack cannot
# run. Why: that stack is a private repository run with node, so CI (which
# checks out only this one) printed SKIP for the whole cloud path and never ran
# it; a change that broke making the Collab project from a collaboration would
# have gone green. The stand-in answers exactly the calls server/stack/cloud.py
# makes, with the contract's shapes and codes (docs/api/openapi.yaml 1.30.1:
# MeResponse, CreateProjectRequest and UpdateProjectRequest refuse unknown
# fields, draft->open<->paused, a token may not delete or restore). The real
# stack is still preferred when it is at hand, and then this is not started.
from http.server import BaseHTTPRequestHandler as _BHRH, ThreadingHTTPServer as _THS  # noqa: E402

_FAKE_CREATE = {"slug", "name", "target_name", "description", "visibility", "licence", "palette", "filters",
                "grid", "rotation_from_first_light", "policy", "notify", "owner_display_name", "owner_country"}
_FAKE_UPDATE = {"name", "target_name", "description", "visibility", "licence", "palette", "filters", "grid",
                "rotation_from_first_light", "policy", "notify", "status", "reprepare_rejected"}
_FAKE_MOVES = {("draft", "open"), ("open", "paused"), ("paused", "open"), ("open", "closed"),
               ("paused", "closed")}


class _FakeCollab:
    def __init__(self):
        self.tokens, self.projects, self.lock = {}, {}, threading.Lock()

    def mint(self, name, scopes):
        token = "rscu_" + os.urandom(32).hex()
        self.tokens[token] = {"user_id": _uuid.uuid4().hex, "name": name, "scopes": list(scopes)}
        return token

    def find(self, key):
        return self.projects.get(key) or next((p for p in self.projects.values() if p["slug"] == key), None)

    def code(self):
        letters = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
        raw = "".join(letters[b % len(letters)] for b in os.urandom(12))
        return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"

    def answer(self, method, path, who, body):
        """(status, body) for one call; `who` is the token's user or None."""
        def err(status, code, message=""):
            return status, {"ok": False, "code": code, "message": message or code}
        if who is None:
            return err(401, "unauthorized")
        if path == "/auth/me" and method == "GET":
            return 200, {"ok": True, "auth": "token", "scopes": who["scopes"], "expires_at": "2027-11-01T00:00:00Z",
                         "user": {"user_id": who["user_id"], "name": who["name"]}}
        if "projects:manage" not in who["scopes"] and method != "GET":
            return err(403, "token_scope")
        if path == "/projects" and method == "POST":
            if set(body) - _FAKE_CREATE or not {"slug", "name", "filters", "owner_display_name"} <= set(body):
                return err(400, "validation_failed")
            if any(p["slug"] == body["slug"] for p in self.projects.values()):
                return err(409, "slug_taken")
            pid = "prj_" + _uuid.uuid4().hex[:20]
            project = {k: v for k, v in body.items() if k not in ("owner_display_name", "owner_country")}
            project.update(project_id=pid, status="draft", join_code=self.code(), owner=who["user_id"],
                           visibility=body.get("visibility", "unlisted"))
            self.projects[pid] = project
            return 201, {"ok": True, "project": self.view(project, who)}
        parts = path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "projects":
            return err(404, "not_found")
        project = self.find(parts[1])
        if project is None:
            return err(404, "not_found")
        if len(parts) == 3 and parts[2] in ("delete", "restore") and method == "POST":
            return err(401, "session_required", "Only a signed-in browser may do this.")
        if len(parts) != 2:
            return err(404, "not_found")
        if method == "GET":
            return 200, {"ok": True, "project": self.view(project, who)}
        if method != "PUT":
            return err(405, "method_not_allowed")
        if project["owner"] != who["user_id"]:
            return err(403, "forbidden")
        if set(body) - _FAKE_UPDATE:
            return err(400, "validation_failed")
        if "status" in body and body["status"] != project["status"] and \
                (project["status"], body["status"]) not in _FAKE_MOVES:
            return err(409, "invalid_transition")
        project.update(body)
        return 200, {"ok": True, "project": self.view(project, who)}

    def view(self, project, who):
        out = {k: v for k, v in project.items() if k != "owner"}
        out["my_membership"] = {"role": "owner"} if project["owner"] == who["user_id"] else None
        if project["owner"] != who["user_id"]:
            out.pop("join_code", None)
        return out


FAKE_COLLAB, FAKE_SERVER = None, None
if not RSC:
    FAKE_COLLAB = _FakeCollab()

    class _FakeHandler(_BHRH):
        def log_message(self, *args):
            pass

        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                body = None
            auth = self.headers.get("Authorization") or ""
            who = FAKE_COLLAB.tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
            path = self.path.split("?")[0]
            if not path.startswith("/api/") or not isinstance(body, dict):
                status, out = 400, {"ok": False, "code": "invalid_request", "message": "bad request"}
            else:
                with FAKE_COLLAB.lock:
                    status, out = FAKE_COLLAB.answer(self.command, path[4:], who, body)
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = _serve

    FAKE_SERVER = _THS(("127.0.0.1", 0), _FakeHandler)
    threading.Thread(target=FAKE_SERVER.serve_forever, daemon=True).start()
    RSC = f"http://127.0.0.1:{FAKE_SERVER.server_address[1]}"
    print("NOTE  the cloud path runs against the stand-in for Rapid Stacker Collab's API "
          "(this check's _FakeCollab), not its local stack")


def rsc(method, path, body=None, token=None, cookie=None):
    headers = {"Content-Type": "application/json", "Origin": RSC}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(RSC + "/api" + path, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}"), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), exc.headers


def collab_account(name, email, scopes):
    if FAKE_COLLAB is not None:
        return FAKE_COLLAB.mint(name, scopes)
    rsc("POST", "/auth/register", {"name": name, "email": email, "password": "correct horse battery staple"})
    mail = json.loads(urllib.request.urlopen(f"{RSC}/_local/mail?to={email}", timeout=10).read())
    link = next(l for m in mail["messages"] for l in m["links"] if "verify-email" in l)
    rsc("POST", "/auth/verify-email", {"token": _re.search(r"token=([0-9a-f]{64})", link).group(1)})
    _, _, headers = rsc("POST", "/auth/login", {"email": email, "password": "correct horse battery staple"})
    cookie = headers["Set-Cookie"].split(";")[0]
    return rsc("POST", "/tokens", {"name": "Starfront", "scopes": scopes}, cookie=cookie)[1]["token"]


if RSC:
    CTOKEN = collab_account("Dave", "dave@example.test", ["projects:manage", "projects:read"])
    READ_ONLY = collab_account("Dave R", "dave.r@example.test", ["projects:read"])

    def remote(project_id):
        return rsc("GET", f"/projects/{project_id}", token=CTOKEN)[1].get("project") or {}

    def cloud_row(sf):
        return server_app.CLOUD.row(sf)

    status, got = call("PUT", "/api/v1/stacking/cloud", {"token": "rscu_nope", "apiBase": RSC + "/api"}, token=DAVE)
    case("cloud link: something that is not a Collab token is refused", status == 400, f"{status} {got}")
    status, got = call("PUT", "/api/v1/stacking/cloud", {"token": READ_ONLY, "apiBase": RSC + "/api"}, token=DAVE)
    case("...and so is a token without projects:manage, saying how to make one",
         status == 400 and "projects:manage" in got.get("detail", ""), f"{status} {got}")
    status, got = call("PUT", "/api/v1/stacking/cloud", {"token": CTOKEN, "apiBase": RSC + "/api"}, token=DAVE)
    case("a token with projects:manage links the account (checked with the service)",
         status == 200 and got["cloud"]["linked"] is True and got["cloud"]["user"] == "Dave"
         and CTOKEN not in json.dumps(got), f"{status} {got}")
    stored = server_app.store._one("SELECT token FROM stack_cloud_links WHERE person = '1005'")["token"]
    case("...kept sealed on the server (the server has a key), never as typed",
         stored.startswith("v1:") and CTOKEN not in stored and got["cloud"]["sealed"] is True, stored[:12])
    _, opts = call("GET", "/api/v1/stacking", token=DAVE)
    case("with no hub, a linked person's default is the cloud", opts["default"]["backend"] == "cloud"
         and CTOKEN not in json.dumps(opts))

    status, made = call("POST", "/api/v1/projects", {
        "name": "Cloud Flow", "region": REGION, "notes": "cloud notes",
        "requirements": {"filters": {"Ha": 3.0, "OIII": 3.0, "OSC-RGB": None}, "minExposure": 60}}, token=DAVE)
    CF = made["project"]["id"]
    row = cloud_row(CF)
    rp = remote(row["project_id"]) if row and row["project_id"] else {}
    fields = server_app._stack_fields("Cloud Flow", "Dave", server_app.store.project(CF)["payload"],
                                      binding_of(CF))
    case("a collaboration started with nothing said is stacked on the cloud: the server made the Collab project",
         status == 200 and server_app._is_managed(binding_of(CF)) and row["doc"]["state"] == "ok"
         and row["project_id"].startswith("prj_") and rp.get("status") == "open", str(row)[:300])
    case("...with exactly the fields a hub project gets: name, notes, filters, palette, visibility, policy",
         rp.get("name") == "Cloud Flow" and rp.get("description") == "cloud notes"
         and sorted(rp.get("filters", [])) == ["HA", "OIII", "OSC"] and rp.get("palette") == "HOO"
         and rp.get("visibility") == "unlisted" and rp.get("policy") == fields["policy"]  # (a set there: sorted)
         and rp["policy"]["osc_allowed"] is True and rp["policy"]["min_exposure_s"] == 60, str(rp)[:400])
    case("...framed by the region until the first light (rotation_from_first_light)",
         rp.get("grid", {}).get("center_ra_deg") == 84.0 and rp.get("rotation_from_first_light") is True)
    case("...and its join code is kept with it, not in the collaboration's payload",
         _re.fullmatch(r"[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}", row["join_code"])
         and row["join_code"] not in json.dumps(server_app.store.project(CF)["payload"]))
    DAVE_RIG = call("POST", "/api/v1/agents", {"name": "Dave's rig"}, token=DAVE)[1]["token"]
    mine_c = project_in_listing(DAVE_RIG, CF)
    other_c = project_in_listing(AGENT_TOKEN, CF)
    case("the owner's telescope is handed the Collab join code, as a pasted one was; a browsing one is not",
         mine_c["stackingJoinCode"] == row["join_code"] and mine_c["stackingLinked"] is True
         and mine_c["stacking"]["managed"] is True and other_c["stackingJoinCode"] == "", str(mine_c["stacking"]))

    status, _ = call("PUT", f"/api/v1/projects/{CF}", {
        "name": "Cloud Renamed", "notes": "edited notes",
        "region": {"ra": 150.0, "dec": 10.0, "width": 3.0, "height": 2.0},
        "requirements": {"filters": {"R": None, "G": None, "B": None}, "requireCalibrated": True},
        "stacking": {"backend": "cloud", "visibility": "private"}}, token=DAVE)
    rp = remote(cloud_row(CF)["project_id"])
    case("an edit of the collaboration is a projectsUpdate: name, notes, filters, policy, visibility, grid",
         status == 200 and rp.get("name") == "Cloud Renamed" and rp.get("description") == "edited notes"
         and sorted(rp.get("filters", [])) == ["B", "G", "R"] and rp.get("palette") == "RGB" and rp.get("visibility") == "private"
         and rp["policy"]["calibration"] == "required" and rp["policy"]["osc_allowed"] is False
         and rp["policy"]["min_exposure_s"] == 5 and rp["grid"]["center_ra_deg"] == 150.0, str(rp)[:400])
    case("...the policy is sent whole (PUT replaces it): every field is the derived one",
         rp["policy"] == server_app._stack_fields("x", "", server_app.store.project(CF)["payload"], {})["policy"])
    status, _ = call("PUT", f"/api/v1/projects/{CF}", {"status": "closed"}, token=DAVE)
    case("closing the collaboration pauses the Collab project (its 'closed' cannot reopen)",
         status == 200 and remote(cloud_row(CF)["project_id"]).get("status") == "paused")
    status, _ = call("PUT", f"/api/v1/projects/{CF}", {"status": "open"}, token=DAVE)
    case("...and reopening it opens it again", remote(cloud_row(CF)["project_id"]).get("status") == "open")
    status, _ = call("PUT", f"/api/v1/projects/{CF}", {"stackingJoinCode": cloud_row(CF)["join_code"]}, token=DAVE)
    case("a plugin's Edit form sending the code back changes nothing", status == 200
         and server_app._is_managed(binding_of(CF)))
    status, _ = call("PUT", f"/api/v1/projects/{CF}", {"stackingJoinCode": "H4RT-92BK-WNPC"}, token=DAVE)
    case("...a different pasted code is refused", status == 400)

    # A failed call is recorded and retried by the sweep.
    good_base = RSC + "/api"
    server_app.store._run("UPDATE stack_cloud_links SET api_base = ? WHERE person = '1005'",
                          f"http://127.0.0.1:{free_port()}/api")
    status, _ = call("PUT", f"/api/v1/projects/{CF}", {"notes": "while it was down"}, token=DAVE)
    row = cloud_row(CF)
    status_s, seen_s = call("GET", f"/api/v1/projects/{CF}/stacking", token=DAVE)
    case("when Collab cannot be reached the edit is saved and the failure recorded on the collaboration",
         status == 200 and row["doc"]["state"] == "failed" and row["doc"]["attempts"] == 1
         and row["doc"]["next_at"] > time.time() and "could not be reached" in row["doc"]["error"]
         and seen_s["cloud"]["state"] == "failed" and seen_s["cloud"]["error"], str(row["doc"])[:200])
    case("...the sweep does not retry before its back-off", server_app.CLOUD.sweep(time.time()) == 0
         and cloud_row(CF)["doc"]["attempts"] == 1)
    server_app.store._run("UPDATE stack_cloud_links SET api_base = ? WHERE person = '1005'", good_base)
    retried = server_app.CLOUD.sweep(time.time() + 3600)
    case("...and once it answers again the sweep's retry brings the Collab project in step",
         retried >= 1 and cloud_row(CF)["doc"]["state"] == "ok"
         and remote(cloud_row(CF)["project_id"]).get("description") == "while it was down", str(cloud_row(CF)["doc"])[:200])

    # Deleting and restoring.
    status, got = call("DELETE", f"/api/v1/projects/{CF}", token=DAVE)
    row = cloud_row(CF)
    rp = remote(row["project_id"])
    case("deleting the collaboration files the 7-day deletion here and asks Collab to delete (confirm_slug)",
         status == 200 and got["deletion"]["status"] == "deleted" and got["deletion"]["delete_after"]
         and server_app.store.project(CF)["status"] == "closed", f"{status} {got}")
    case("...the contract keeps projectsDelete for a browser session, so the project is paused and the owner told where",
         rp.get("status") in ("paused", "deleted") and (rp.get("status") == "deleted" or (
             row["doc"]["state"] == "needs_owner" and f"/p/{row['slug']}" in row["doc"]["error"])), str(row["doc"])[:300])
    _, opts = call("GET", "/api/v1/stacking", token=DAVE)
    case("...the deleted collaboration is listed for its owner, restorable, with the window",
         any(x["id"] == CF and x["restorable"] and x["backend"] == "cloud" for x in opts["deleted"])
         and opts["windowDays"] == 7)
    case("...and its join code is no longer handed out", server_app.CLOUD.join_code(CF) == "")
    status, got = call("PUT", f"/api/v1/projects/{CF}", {"notes": "x"}, token=DAVE)
    case("...nor may it be edited while deleted: 409", status == 409)
    status, got = call("POST", f"/api/v1/projects/{CF}/restore", token=DAVE)
    case("restoring it reopens the collaboration and the Collab project",
         status == 200 and server_app.store.project(CF)["status"] == "open"
         and remote(cloud_row(CF)["project_id"]).get("status") == "open" and cloud_row(CF)["doc"]["state"] == "ok",
         f"{status} {got}")

    # Unbinding pauses it; binding again reopens the same project.
    pid_before = cloud_row(CF)["project_id"]
    status, _ = call("PUT", f"/api/v1/projects/{CF}", {"stacking": {"backend": ""}}, token=DAVE)
    case("unbinding pauses the Collab project and stops handing out its code",
         status == 200 and remote(pid_before).get("status") == "paused"
         and project_in_listing(DAVE_RIG, CF)["stackingJoinCode"] == "")
    status, _ = call("PUT", f"/api/v1/projects/{CF}", {"stacking": {"backend": "cloud"}}, token=DAVE)
    case("...and binding again reopens the same one",
         status == 200 and cloud_row(CF)["project_id"] == pid_before and remote(pid_before).get("status") == "open")

    # Unlinking: nothing is lost, the next edit waits for a link.
    call("DELETE", "/api/v1/stacking/cloud", token=DAVE)
    status, _ = call("PUT", f"/api/v1/projects/{CF}", {"notes": "unlinked"}, token=DAVE)
    case("with the account unlinked an edit is saved and the sync waits, recorded",
         status == 200 and cloud_row(CF)["doc"]["state"] == "failed"
         and cloud_row(CF)["doc"]["code"] == "not_linked")
    call("PUT", "/api/v1/stacking/cloud", {"token": CTOKEN, "apiBase": RSC + "/api"}, token=DAVE)
    case("...and linking again catches up at once", cloud_row(CF)["doc"]["state"] == "ok"
         and remote(pid_before).get("description") == "unlinked")
    # Whether a telescope can join yet. Why: while the project was being made,
    # or after Collab refused it, the listing carried an empty code and the
    # plugin skipped the collaboration in silence; only the owner's desktop
    # Edit form said why. Now the listing says so, with the reason for the
    # owner's own telescopes only.
    server_app.store._run("UPDATE stack_cloud_links SET api_base = ? WHERE person = '1005'",
                          f"http://127.0.0.1:{free_port()}/api")
    status, made = call("POST", "/api/v1/projects", {
        "name": "Cloud Waiting", "region": REGION, "requirements": {"filters": {"Ha": 3.0}}}, token=DAVE)
    CW = made["project"]["id"]
    mine_w, other_w = project_in_listing(DAVE_RIG, CW), project_in_listing(AGENT_TOKEN, CW)
    case("a cloud project that could not be made is listed as failed, with no code to join",
         status == 200 and mine_w["stacking"]["state"] == "failed" and mine_w["stackingJoinCode"] == ""
         and mine_w["stackingLinked"] is False and other_w["stacking"]["state"] == "failed",
         str(mine_w["stacking"]))
    case("...the owner's telescope is told why, and nobody else's is",
         "could not be reached" in mine_w["stacking"]["problem"] and other_w["stacking"]["problem"] == "",
         f"{mine_w['stacking']['problem']!r} / {other_w['stacking']['problem']!r}")
    server_app.store._run("UPDATE stack_cloud SET doc = json_set(doc, '$.state', 'pending') WHERE sf_project = ?", CW)
    mine_w = project_in_listing(DAVE_RIG, CW)
    case("...one still being made is listed as pending, with no reason to give",
         mine_w["stacking"]["state"] == "pending" and mine_w["stacking"]["problem"] == "", str(mine_w["stacking"]))
    server_app.store._run("UPDATE stack_cloud_links SET api_base = ? WHERE person = '1005'", good_base)
    server_app.CLOUD.sweep(time.time() + 3600)
    mine_w = project_in_listing(DAVE_RIG, CW)
    case("...and once it is made it is ready, with the code, and stackingLinked as before",
         mine_w["stacking"]["state"] == "ready" and mine_w["stacking"]["problem"] == ""
         and mine_w["stackingJoinCode"] == cloud_row(CW)["join_code"] != "" and mine_w["stackingLinked"] is True,
         str(mine_w["stacking"]))

    if STACK_PROC is not None:
        STACK_PROC.stdin.close()
        STACK_PROC.wait(10)
    if FAKE_SERVER is not None:
        FAKE_SERVER.shutdown()

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
