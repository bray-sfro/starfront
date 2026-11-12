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
case("the server says Discord sign-in is on, and a role is needed to start",
     status == 200 and info["discord"] and info["roleRequired"])

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
case("a member without the role signs in too, but may not start one",
     polled["state"] == "done" and polled["user"]["canStart"] is False)

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

status, refused = call("POST", "/api/v1/projects", {
    "name": "Bob's", "region": {"ra": 0, "dec": 0, "width": 1, "height": 1}},
    token=BOB)
case("a member without the role cannot", status == 403, refused.get("detail"))

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

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
