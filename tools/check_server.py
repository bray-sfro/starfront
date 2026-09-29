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
os.environ["ASTROCOLLAB_DATA"] = str(DATA)
os.environ["ASTROCOLLAB_ADMIN_TOKEN"] = ADMIN
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

os.environ["ASTROCOLLAB_DISCORD_CLIENT_ID"] = "app-1"
os.environ["ASTROCOLLAB_DISCORD_CLIENT_SECRET"] = "shh"
os.environ["ASTROCOLLAB_DISCORD_GUILD"] = GUILD
os.environ["ASTROCOLLAB_DISCORD_ROLE"] = ROLE
os.environ["ASTROCOLLAB_DISCORD_OWNERS"] = "1001"     # Alice runs the server
os.environ["ASTROCOLLAB_PUBLIC_URL"] = BASE
os.environ["ASTROCOLLAB_DISCORD_API"] = DISCORD_BASE
os.environ["ASTROCOLLAB_DISCORD_AUTHORIZE"] = f"{DISCORD_BASE}/oauth2/authorize"


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
     across_after > across_before and after["version"] > before["version"],
     f"{across_before} across -> {across_after} across, version "
     f"{before['version']} -> {after['version']}")
case("...and tonight's list dealt afresh on the new cells",
     after["share"] and all(0 <= i < len(after["cells"]) for i in after["share"]),
     f'{len(after["share"])} of {len(after["cells"])}')
again = held(WIDE_TOKEN, "2026-10-01")
case("...and holds from then on", again["version"] == after["version"])
# The hours a rig gives come off its plan and can shrink - a window pinned
# shorter, a target that sets early. A list dealt for six hours is wrong for
# a rig that now has two, so the list is dealt again, smaller, and held from
# then on. A panel is either reachable tonight or it is not.
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
shorter = held(WIDE_TOKEN, "2026-10-01")
case("a rig whose night shrank is dealt a shorter list for it",
     0 < len(shorter["share"]) < len(before["share"])
     and shorter["version"] > before["version"],
     f'{len(before["share"])} -> {len(shorter["share"])} panels for 2 h')
case("...that fits the two hours",
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
tonight_one = held_on = call("GET", "/api/v1/agent/task?night=2026-10-08",
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

case("a camera that cannot turn is tiled the way it really sits",
     across > down, f"{across} across, {down} down at 268 degrees")
# The same camera straight was 4 across and 3 down. Turned, the long axis
# runs north-south, so it takes more frames to cross the region and fewer to
# climb it - the grid transposes. The total can go either way and is not the
# point.
straight_across = max(c["column"] for c in task["cells"]) + 1
straight_down = max(c["row"] for c in task["cells"]) + 1
case("...the grid transposed against the same camera straight",
     across > straight_across and down < straight_down,
     f"turned {across}x{down}, straight {straight_across}x{straight_down}")

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

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
