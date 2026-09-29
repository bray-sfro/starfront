"""The collaboration server: where imaging tasks come from.

An observatory runs Starfront and asks this, periodically, "what should I
shoot?".  It answers with a task — a rectangle of sky, some filters, and how
deep to go — and takes back what was actually captured.

**Pull, not push.**  The agent asks; the server never reaches into an
observatory.  That is not only simpler — no delivery guarantees, no queue, no
retries, nothing to get stuck — it is also the only arrangement where a rig
behind a domestic router at a dark site works at all, and where the server
going down means "no new tasks" rather than "the night stops".

**Two kinds of caller.**  An *agent* is a telescope, authenticated with a token
that belongs to the machine.  A *person* is a coordinator, and will eventually
be authenticated through Discord — for now that is a stub provider, so this can
be built and tested before a Discord application exists.  The split matters:
a telescope's credential should never be able to administer a project, and a
person's login should never be able to drive a mount.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astrocontrol import collab, filters                         # noqa: E402
from astrocontrol.config import data_root                        # noqa: E402
from server import auth                                          # noqa: E402
from server.store import Store                                   # noqa: E402

app = FastAPI(title="Starfront collaboration server", version="0.1.0")

# Beside everything else the rig keeps, and through the same function, so a
# machine that still has its folder under the old name is not left with its
# telescope's data in one place and its collaboration's in another.
DATA = Path(os.environ.get("ASTROCOLLAB_DATA") or data_root() / "collab")
store = Store(DATA / "collab.sqlite")

#: The server owner's credential. Can do anything, and is meant for the one
#: person running the server; everybody else signs in with Discord.
ADMIN_TOKEN = os.environ.get("ASTROCOLLAB_ADMIN_TOKEN", "")

#: Discord, if it has been set up. Read once at start, from the environment
#: or `discord.env` beside the database; a server without it still works for
#: the owner alone, on the admin token, which is how it began.
auth.load_env_file(DATA)
DISCORD = auth.settings_from_env()
discord = auth.Discord(DISCORD)


# ---------------------------------------------------------------------------
# Who is asking
# ---------------------------------------------------------------------------

def _bearer(authorization: str) -> str:
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def agent_from(authorization: str = Header(default="")) -> dict[str, Any]:
    """The telescope making this request, or 401."""
    found = store.agent_by_token(_bearer(authorization))
    if found is None:
        raise HTTPException(status_code=401, detail="unknown agent token")
    return found


def _person(token: str) -> dict[str, Any] | None:
    """Who a bearer token belongs to: the owner, a signed-in member, or nobody.

    Two kinds of person, one shape. The owner holds the admin token and may do
    anything; a member signed in with Discord holds a user token and may do
    what is theirs. Telescope tokens are a different thing and are never a
    person: a credential that lives in a settings file next to a mount must
    not be able to rewrite a project.
    """
    if not token:
        return None
    if ADMIN_TOKEN and secrets.compare_digest(token, ADMIN_TOKEN):
        return {"kind": "admin", "id": "", "name": "coordinator", "roles": [],
                "admin": True}
    user = store.user_by_token(token)
    if user is None:
        return None
    # The people who run the server sign in like anybody else and are
    # owners by their Discord id, listed in discord.env. No token to paste.
    return {"kind": "user", "id": user["id"], "name": user["name"],
            "roles": list(user.get("roles") or []),
            "admin": user["id"] in DISCORD.owners}


def person_from(authorization: str = Header(default="")) -> dict[str, Any]:
    """Somebody signed in - the owner or a Discord member - or 401."""
    who = _person(_bearer(authorization))
    if who is None:
        if not ADMIN_TOKEN and not DISCORD.configured():
            raise HTTPException(
                status_code=503,
                detail="nobody can sign in to this server yet: it has no admin "
                       "token and Discord is not set up")
        raise HTTPException(status_code=401, detail="not signed in")
    if who["kind"] == "user":
        store.seen_user(who["id"])
    return who


def require_admin(authorization: str = Header(default="")) -> str:
    """The server owner, on the admin token, or 401.

    Kept for the few things only the owner does - listing every telescope on
    the server, say - and named as it was so that nothing that depends on it
    has to change.
    """
    who = _person(_bearer(authorization))
    if who is None or not who["admin"]:
        if not ADMIN_TOKEN:
            raise HTTPException(
                status_code=503,
                detail="no admin token is set on this server "
                       "(ASTROCOLLAB_ADMIN_TOKEN)")
        raise HTTPException(status_code=401, detail="not the server owner")
    return "coordinator"


def may_start(who: dict[str, Any]) -> bool:
    """Whether this person may start collaborations.

    The owner always may. A member may when the server has no role
    requirement, or holds the role. Joining never needs any of this.
    """
    if who["admin"]:
        return True
    if not DISCORD.role:
        return True
    return DISCORD.role in (who.get("roles") or [])


def owns_project(who: dict[str, Any], project: dict[str, Any]) -> bool:
    """The owner of the server, or the person who started it."""
    if who["admin"]:
        return True
    owner = str(project.get("owner_id") or "")
    return bool(owner) and owner == who["id"]


def _require_project_owner(who: dict[str, Any], project_id: str) -> dict[str, Any]:
    project = store.project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such project")
    if not owns_project(who, project):
        raise HTTPException(status_code=403,
                            detail="this collaboration was started by somebody "
                                   "else; only they, or the server owner, can "
                                   "change it")
    return project


# ---------------------------------------------------------------------------
# Signing in with Discord
# ---------------------------------------------------------------------------

@app.get("/api/v1/auth")
def auth_status() -> dict[str, Any]:
    """Whether people can sign in here, and what it takes to start a project."""
    return {"discord": DISCORD.configured(), "guild": DISCORD.guild,
            "roleRequired": bool(DISCORD.role), "publicUrl": DISCORD.public_url}


@app.post("/api/v1/auth/login")
def auth_login() -> dict[str, Any]:
    """Begin a sign-in: a code for the program to hold and a page to open.

    The device-code flow. Starfront opens `url` in the browser and polls
    `/api/v1/auth/poll` with `code` until somebody has signed in there.
    """
    if not DISCORD.configured():
        raise HTTPException(status_code=503,
                            detail="Discord sign-in is not set up on this server")
    store.sweep_logins(time.time() - 2 * auth.LOGIN_CODE_SECONDS)
    code = auth.new_login_code()
    store.add_login(code)
    return {"code": code,
            "url": f"{DISCORD.public_url.rstrip('/')}/auth/discord/start?code={code}",
            "expiresIn": auth.LOGIN_CODE_SECONDS}


@app.get("/auth/discord/start")
def auth_start(code: str = Query(default="", max_length=64)):
    """The page Starfront opens: straight on to Discord."""
    if not DISCORD.configured():
        return HTMLResponse(auth.page("Not set up", "Discord sign-in is not "
                                      "configured on this server.", ok=False),
                            status_code=503)
    login = store.login(code)
    if login is None or auth.expired(login["created"]):
        return HTMLResponse(auth.page("That code has expired",
                                      "Go back to Starfront and press Sign in "
                                      "with Discord again.", ok=False),
                            status_code=400)
    return RedirectResponse(discord.authorize_url(state=code))


@app.get("/auth/discord/callback")
def auth_callback(code: str = Query(default=""), state: str = Query(default=""),
                  error: str = Query(default="")):
    """Discord sends the person back here. Check them, bind them to the code."""
    if error:
        return HTMLResponse(auth.page("Sign-in cancelled", f"Discord said: {error}",
                                      ok=False), status_code=400)
    login = store.login(state)
    if login is None or auth.expired(login["created"]):
        return HTMLResponse(auth.page("That code has expired",
                                      "Go back to Starfront and press Sign in "
                                      "with Discord again.", ok=False),
                            status_code=400)
    try:
        bearer = discord.exchange(code)
        identity = discord.identify(bearer)
    except auth.AuthError as exc:
        return HTMLResponse(auth.page("Could not sign you in", str(exc), ok=False),
                            status_code=403)
    token = auth.new_user_token()
    store.upsert_user(identity.id, token, identity.name, identity.avatar,
                      identity.roles)
    store.bind_login(state, identity.id)
    starter = ("You can start collaborations here."
               if DISCORD.role in identity.roles or not DISCORD.role
               else "You can join collaborations; starting one needs a role "
                    "you do not hold.")
    return HTMLResponse(auth.page(
        f"Signed in as {identity.name}",
        "Go back to Starfront - it has already noticed. " + starter))


@app.get("/api/v1/auth/poll")
def auth_poll(code: str = Query(default="", max_length=64)) -> dict[str, Any]:
    """Has anybody signed in on this code yet? The token comes back once."""
    login = store.login(code)
    if login is None or auth.expired(login["created"]):
        return {"state": "expired"}
    if not login["user"]:
        return {"state": "pending"}
    if login["claimed"]:
        return {"state": "claimed"}
    user = store.user(login["user"])
    if user is None:
        return {"state": "expired"}
    store.claim_login(code)
    owner = user["id"] in DISCORD.owners
    return {"state": "done", "token": user["token"],
            "user": {"id": user["id"], "name": user["name"],
                     "avatar": user.get("avatar") or "",
                     "admin": owner,
                     "canStart": may_start({"admin": owner, "id": user["id"],
                                            "roles": user["roles"]})}}


@app.get("/api/v1/auth/me")
def auth_me(who: dict[str, Any] = Depends(person_from)) -> dict[str, Any]:
    return {"id": who["id"], "name": who["name"], "admin": who["admin"],
            "canStart": may_start(who)}


@app.post("/api/v1/auth/logout")
def auth_logout(who: dict[str, Any] = Depends(person_from)) -> dict[str, Any]:
    if who["kind"] == "user":
        store.forget_user_token(who["id"])
    return {"signedOut": True}


# ---------------------------------------------------------------------------
# What arrives
# ---------------------------------------------------------------------------

class HelloRequest(BaseModel):
    """An agent saying what it is, and what it can do."""

    protocol: int = Field(default=collab.PROTOCOL)
    profile: dict[str, Any] = Field(default_factory=dict)
    #: Where the telescope is pointing and what it is doing, for the others
    #: to see on their charts: {ra (hours), dec, state, target, project}.
    #: Optional, and only what the rig chooses to say.
    presence: dict[str, Any] | None = None


#: How long since a check-in a telescope still counts as online. Rigs check
#: in every ten minutes by default; twice that plus slack.
ONLINE_SECONDS = 25 * 60


def _presence_of(agent: dict[str, Any], now: float) -> dict[str, Any]:
    """One telescope as the group sees it."""
    said = agent.get("presence") or {}
    age = max(0.0, now - float(agent.get("seen") or 0.0))
    ra = _number_or_none(said.get("ra"))
    dec = _number_or_none(said.get("dec"))
    return {
        "id": agent["id"],
        "name": agent.get("name") or "",
        "owner": agent.get("owner") or "",
        "ra": ra, "dec": dec,
        "state": str(said.get("state") or ""),
        "target": str(said.get("target") or "")[:80],
        "project": str(said.get("project") or ""),
        "ageSeconds": round(age),
        "online": age <= ONLINE_SECONDS,
    }


def _number_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _participants(project_id: str, now: float) -> dict[str, Any]:
    """How many telescopes are on a project, and how many are online now."""
    ids = store.agents_on(project_id)
    names: list[str] = []
    online = 0
    for agent_id in ids:
        agent = store.agent(agent_id)
        if agent is None:
            continue
        names.append(agent.get("name") or agent_id)
        if now - float(agent.get("seen") or 0.0) <= ONLINE_SECONDS:
            online += 1
    return {"participants": len(names), "participantsOnline": online,
            "participantNames": names}


class TaskStateRequest(BaseModel):
    state: str = Field(pattern="^(accepted|declined|complete)$")


class ReportRequest(BaseModel):
    """One night's work, one record per filter."""

    contributions: list[dict[str, Any]] = Field(default_factory=list,
                                                max_length=200)


class AgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    owner: str = Field(default="", max_length=80)


class ProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    coordinator: str = Field(default="", max_length=80)
    region: dict[str, Any]
    # "single": one object, everybody points at it, nobody tiles it.
    # "mosaic": a region to be covered, tiled by each rig with its own camera.
    kind: str = Field(default="mosaic", pattern="^(single|mosaic)$")
    requirements: dict[str, Any] = Field(default_factory=dict)
    # Depth wanted at every point inside the region, per filter, in hours.
    goals: dict[str, float] = Field(default_factory=dict)
    notes: str = Field(default="", max_length=2000)


class TaskRequest(BaseModel):
    agent: str
    region: dict[str, Any]
    filters: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=500)


# ---------------------------------------------------------------------------
# The agent's side: three calls, and that is the whole protocol
# ---------------------------------------------------------------------------

@app.post("/api/v1/agent/hello")
def hello(body: HelloRequest,
          agent: dict[str, Any] = Depends(agent_from)) -> dict[str, Any]:
    """Register a heartbeat and say what this rig is.

    The profile is what makes the pre-flight check possible — telling somebody
    their 7 nm Ha will be rejected before they spend a night on it rather than
    after.
    """
    if body.protocol > collab.PROTOCOL:
        raise HTTPException(
            status_code=409,
            detail=f"this server speaks protocol {collab.PROTOCOL}; the agent "
                   f"speaks {body.protocol} — update the server")
    store.seen(agent["id"], body.profile or {},
               body.presence if body.presence is not None else None)
    return {"agent": agent["id"], "name": agent["name"],
            "protocol": collab.PROTOCOL, "serverTime": time.time()}


@app.get("/api/v1/presence")
def presence(agent: dict[str, Any] = Depends(agent_from)) -> dict[str, Any]:
    """Who is on the sky right now: every telescope that has checked in
    lately, where it points and what it is doing, for the chart and the
    counts on the Collab tab. Telescopes silent for a day drop off."""
    now = time.time()
    store.seen(agent["id"])
    rows = [_presence_of(entry, now) for entry in store.agents()
            if now - float(entry.get("seen") or 0.0) <= 24 * 3600]
    rows.sort(key=lambda row: (not row["online"], row["ageSeconds"]))
    online = [row for row in rows if row["online"]]
    people = {(store.agent(row["id"]) or {}).get("owner_id") or row["id"]
              for row in online}
    return {"telescopes": rows, "online": len(online), "people": len(people),
            "onlineSeconds": ONLINE_SECONDS, "serverTime": now}


@app.get("/api/v1/agent/task")
def current_task(agent: dict[str, Any] = Depends(agent_from),
                 night: str = Query(default="", max_length=16)) -> dict[str, Any]:
    """What this telescope should be shooting.

    The whole point of the server, from an observatory's side. Returns the
    task and a version; an agent that already has that version has nothing to
    do, which keeps a poll every few minutes free.

    `night` is the night the rig is in, as it names its own nights. Its
    panels for a night are dealt once for that night and held — they must not
    move under a run because somebody else's frames arrived at 2 a.m. — and
    are dealt afresh the first time it asks in the next one. A client that
    does not say falls back to a list held for twenty hours.
    """
    store.seen(agent["id"])
    tasks = store.tasks_for(agent["id"])
    if not tasks:
        return {"task": None, "tasks": [], "version": 0,
                "protocol": collab.PROTOCOL}
    # A camera that has turned since its cells were cut gets them cut again
    # before anything is dealt on them.
    profile = collab.RigProfile.read(agent.get("profile") or {})
    for task in tasks:
        project = store.project(task["project"])
        if project is not None and task.get("cells"):
            _retile_if_turned(task, profile, project)
    # Shares move as others join and as frames come in, and a poll is when a
    # rig finds out. Cheap when nothing has changed: a share that is the same
    # is not rewritten, so the version stays put and the rig has nothing to do.
    for project_id in {task["project"] for task in tasks}:
        _redeal(project_id, {agent["id"]: night.strip()} if night.strip() else None)
    tasks = store.tasks_for(agent["id"])
    task = tasks[0]
    project = store.project(task["project"])
    return {
        "task": task,
        # All of them. A rig that has joined three collaborations has three
        # chunks to plan a night around, and handing back only the first made
        # the other two invisible to the machine that was supposed to shoot
        # them. `task` stays for the older client that expects one.
        "tasks": tasks,
        "requirementsByProject": {
            project_id: ((store.project(project_id) or {})
                         .get("payload", {}).get("requirements", {}))
            for project_id in {entry["project"] for entry in tasks}},
        "version": task.get("version", 1),
        "protocol": collab.PROTOCOL,
        # Sent with the task so the agent can judge its own data before
        # reporting it, and tell the operator why a night will be rejected
        # while there is still time to do something about it.
        "requirements": (project or {}).get("payload", {}).get("requirements", {}),
    }


@app.get("/api/v1/agent/projects")
def open_projects(agent: dict[str, Any] = Depends(agent_from)) -> dict[str, Any]:
    """Every project that is open, and whether this telescope can help.

    Browsing is how somebody finds a collaboration to join, and the judgement
    of whether they qualify is made here as well as in their own program —
    theirs so they are told before they waste a night, this one because a
    check that only runs on the client is not a check.
    """
    store.seen(agent["id"])
    profile = collab.RigProfile.read(agent.get("profile") or {})
    mine = {task["project"] for task in
            store.tasks_for(agent["id"], ("offered", "accepted", "complete"))}

    listed = []
    for project in store.projects():
        if project.get("status") != "open":
            continue
        payload = project.get("payload") or {}
        wants = collab.Requirements.read(payload.get("requirements") or {})
        listed.append({
            "id": project["id"],
            "name": project["name"],
            "coordinator": project.get("coordinator") or "",
            # Whose it is, so a program can offer Edit and Close only to the
            # person who may use them. The server enforces it either way.
            "ownerId": project.get("owner_id") or "",
            "region": payload.get("region") or {},
            "kind": payload.get("kind") or "mosaic",
            "requirements": wants.payload(),
            "goals": payload.get("goals") or {},
            "notes": payload.get("notes") or "",
            "compatibility": collab.compatibility(profile, wants),
            "joined": project["id"] in mine,
            # What everyone together has collected, so somebody choosing where
            # to point can choose the one that needs them.
            "collected": _collected(project["id"]),
            # Who is on it, and how many of them are about tonight.
            **_participants(project["id"], time.time()),
        })
    return {"projects": listed, "protocol": collab.PROTOCOL}


def _goals(goals: dict[str, float]) -> dict[str, float]:
    """Depth wanted per filter, keyed by the one spelling of each name."""
    out: dict[str, float] = {}
    for name, hours in (goals or {}).items():
        key = filters.canonical(name) or str(name)
        out[key] = out.get(key, 0.0) + float(hours)
    return out


def _collected(project_id: str) -> dict[str, float]:
    """Accepted hours per filter on a project."""
    hours: dict[str, float] = {}
    for row in store.contributions(project_id):
        if not row.get("accepted"):
            continue
        name = filters.canonical((row.get("payload") or {}).get("filterName") or "")
        hours[name] = round(hours.get(name, 0.0)
                            + float(row.get("seconds") or 0.0) / 3600.0, 2)
    return hours


class JoinRequest(BaseModel):
    """Signing up for a share of a project."""

    #: Hours this rig will give it per filter, capped by what the project wants.
    #: Zero or missing takes the project's own goal.
    hours: float = Field(default=0.0, ge=0.0, le=24.0)
    #: Sub length, for a filter with no entry in `exposures`. Zero takes the
    #: middle of what the project will accept.
    exposure: float = Field(default=0.0, ge=0.0, le=3600.0)
    #: The rig's own default sub length per filter - what its darks are
    #: built for. The share is dealt at these, so every light can be
    #: calibrated; a rig is assumed to have chosen them well.
    exposures: dict[str, float] = Field(default_factory=dict)


@app.post("/api/v1/agent/projects/{project_id}/join")
def join_project(project_id: str, body: JoinRequest,
                 agent: dict[str, Any] = Depends(agent_from)) -> dict[str, Any]:
    """Take a share of a project this telescope qualifies for.

    Nobody deals this out. A rig that meets the requirements asks, and the
    server hands it the part of the sky least spoken for — sized to *its own*
    field, because depth is integration time at a point on the sky and chunks
    from different cameras have no need to line up.

    The requirements are checked here, against the profile the rig last
    reported, rather than taken on the word of whoever is asking.
    """
    project = store.project(project_id)
    if project is None or project.get("status") != "open":
        raise HTTPException(status_code=404, detail="no such open project")
    payload = project.get("payload") or {}

    profile = collab.RigProfile.read(agent.get("profile") or {})
    field = profile.field()
    if field is None:
        raise HTTPException(
            status_code=400,
            detail="this telescope has not said what it can see - connect once "
                   "with its focal length, sensor size and pixel size set")

    wants = collab.Requirements.read(payload.get("requirements") or {})
    verdict = collab.compatibility(profile, wants)
    if not verdict["ok"]:
        raise HTTPException(status_code=409, detail=verdict["summary"])

    region = collab.Region.read(payload["region"])
    kind = "single" if payload.get("kind") == "single" else "mosaic"
    cells = _tile(region, kind, profile)
    if not cells:
        raise HTTPException(status_code=409,
                            detail="there is nothing left to hand out")

    goals = payload.get("goals") or {}
    names = list(wants.filters) or list(goals) or ["L"]
    fallback = body.exposure or _middle_exposure(wants)
    # The rig's own exposure per filter first: that is the length its darks
    # are built for, and a light with no dark of its own length is a light
    # nobody can calibrate. One outside what the project takes is refused
    # here, with the filter named, rather than shot for a night and rejected.
    own = {filters.canonical(name): float(value)
           for name, value in (body.exposures or {}).items()
           if value and float(value) > 0}
    tasks = []
    for name in names:
        exposure = own.get(filters.canonical(name)) or fallback
        if wants.minExposure is not None and exposure < wants.minExposure:
            raise HTTPException(
                status_code=409,
                detail=f"this telescope shoots {name} at {exposure:g}s and the "
                       f"project wants {wants.minExposure:g}s or longer - change "
                       "the default exposure under Equipment")
        if wants.maxExposure is not None and exposure > wants.maxExposure:
            raise HTTPException(
                status_code=409,
                detail=f"this telescope shoots {name} at {exposure:g}s and the "
                       f"project wants {wants.maxExposure:g}s or shorter - change "
                       "the default exposure under Equipment")
        hours = body.hours or float(goals.get(name) or 1.0)
        if hours > 0:
            tasks.append(collab.FilterTask(filter=name, exposure=exposure,
                                           hours=hours).payload())
    filters_payload = tasks

    task = collab.Task(
        id=collab.new_id(), project=project_id, projectName=project["name"],
        # The whole project's sky, tiled with this rig's own camera. Which of
        # those cells are its to shoot is dealt out below, alongside everyone
        # else's, so a rig that is the only one on a forty-panel mosaic holds
        # all forty rather than the one it used to be handed.
        agent=agent["id"], region=region, cells=cells, share=[], kind=kind,
        filters=[collab.FilterTask.read(entry) for entry in filters_payload],
        # Joined, not offered: the rig asked for this one. Making somebody
        # accept work they just volunteered for would be a dialog that only
        # ever has one answer.
        state="accepted", version=1, issued=time.time(),
        note=f"joined by {agent['name']}")
    stored = task.payload()
    # The angle the cells were cut for, so a camera that turns out to sit
    # somewhere else can be noticed and the cells cut again.
    stored["tiledRotation"] = profile.rotation
    store.add_task(stored)
    _redeal(project_id)
    return {"task": store.task(task.id), "requirements": wants.payload()}


def _tile(region: collab.Region, kind: str,
          profile: collab.RigProfile) -> list[dict[str, Any]]:
    """A rig's cells over a project: its own frame, or its own tiling.

    Tiled with what the camera really spans on the sky. A rig that cannot
    turn its camera reports the angle it sits at, and at 268 degrees its long
    axis runs north-south; cells cut with the raw width and height would be
    ones it could not cover in a frame. A single-target project is one cell:
    everybody points at the object, at whatever field each telescope has.
    """
    field = profile.field()
    if field is None:
        return []
    across, down = collab.footprint(field[0], field[1], profile.rotation)
    if kind == "single":
        return collab.single_cell(region, across, down)
    return collab.grid(region, across, down, 0.1)


#: How far a camera may turn before its cells are cut again. The same figure
#: the program uses before it re-lays a mosaic.
RETILE_DEGREES = 2.0


def _retile_if_turned(task: dict[str, Any], profile: collab.RigProfile,
                      project: dict[str, Any]) -> bool:
    """Cut a task's cells again when the camera no longer sits where they
    were cut for.

    A rig with no rotator lays its mosaic at the angle its camera really
    sits at, and the first plate solve of a night may find that angle is not
    the one in its settings. The rig's program lays its panels again and
    reports the new angle in its profile; here the cells follow, so that
    what the server deals still matches what the camera can cover. The share
    is cleared so tonight's list is dealt afresh on the new cells.
    """
    was = task.get("tiledRotation", "unset")
    now = profile.rotation
    if was == "unset":
        task["tiledRotation"] = now
        store.set_task(task)
        return False
    if was is None and now is None:
        return False
    turned = ((was is None) != (now is None)
              or abs(((float(was) - float(now) + 180.0) % 360.0) - 180.0) > RETILE_DEGREES)
    if not turned:
        return False
    payload = project.get("payload") or {}
    cells = _tile(collab.Region.read(payload["region"]),
                  task.get("kind") or "mosaic", profile)
    if not cells:
        return False
    task.update({"cells": cells, "share": [], "visit": {},
                 "tiledRotation": now, "assignedNight": "", "assignedAt": 0.0,
                 "version": int(task.get("version") or 1) + 1})
    store.set_task(task)
    return True


#: A rig that has not said how much of a night it gives is assumed to give
#: this much. Wrong for somebody, but wrong by a night rather than by a
#: season, and its list is recomputed tomorrow anyway.
DEFAULT_HOURS_PER_NIGHT = 6.0

#: How long a night's list holds for a rig that never says which night it is
#: in. A rig's panels must not move under it while it is shooting them —
#: other rigs' frames arriving at 2 a.m. would otherwise reshuffle its plan
#: mid-run — so without a night to anchor to, a list is kept this long.
ASSIGNMENT_HOLD_SECONDS = 20 * 3600.0


def _redeal(project_id: str, nights: dict[str, str] | None = None) -> int:
    """Give every rig on a project its panels for tonight, and say who changed.

    Called whenever a rig joins or asks what to shoot. It is what replaces a
    coordinator dealing chunks out by hand, and it is what makes each night
    different from the last: the panels a rig is sent to are chosen from what
    the whole collaboration has already collected — the depth map, built from
    every panel anybody has reported — and from where *this* rig has been.
    See `collab.assign` for the order the pulls are resolved in.

    A list, once made, holds for the night it was made for. `nights` says
    which night the asking rig is in; its list is remade the first time it
    asks in a new one, so everything moves on by default each night and nothing
    moves under a rig that is shooting. Everybody else's list is left as it is
    (or remade if it is twenty hours old and its rig never said which night it
    was in) and counts as spoken for tonight. A rig whose list changed has its
    task's version bumped, which is the signal its program watches for; one
    whose list held is left exactly alone, so an idle poll costs nothing.
    """
    project = store.project(project_id)
    if project is None:
        return 0
    payload = project.get("payload") or {}
    wants = collab.Requirements.read(payload.get("requirements") or {})
    goals = {str(name): float(hours)
             for name, hours in (payload.get("goals") or {}).items()}

    tasks = [task for task in store.tasks_in(project_id)
             if task.get("state") in ("offered", "accepted")]
    tasks.sort(key=lambda task: float(task.get("issued") or 0.0))
    if not tasks:
        return 0

    # Everything that has come in and been accepted, as the sky it covered.
    shot = [row["payload"] for row in store.contributions(project_id)
            if row.get("accepted") and (row.get("payload") or {}).get("footprint")]

    nights = nights or {}
    # The night being dealt, when the asking rig said which. Another rig's
    # list counts as spoken for tonight only if it was made for the same
    # night: last night's list is last night's, and dealing around it would
    # push a rig back onto the panels it has just done.
    dealing = next(iter(nights.values()), "")
    now = time.time()
    changed = 0
    tonight: list[collab.Region] = []          # what earlier rigs hold tonight
    for task in tasks:
        cells = task.get("cells") or []
        if not cells:
            continue
        agent_id = task["agent"]
        held = list(task.get("share") or [])
        made_at = float(task.get("assignedAt") or 0.0)
        made_for = str(task.get("assignedNight") or "")
        asked = nights.get(agent_id)
        profile = collab.RigProfile.read(
            (store.agent(agent_id) or {}).get("profile") or {})
        hours = float(profile.hoursPerNight or 0.0) or DEFAULT_HOURS_PER_NIGHT
        if dealing:
            current = bool(held) and made_for == dealing
        else:
            current = bool(held) and now - made_at < ASSIGNMENT_HOLD_SECONDS
        # The hours a rig gives tonight come from its own plan - the window
        # its target really has - and a list dealt for six hours is wrong for
        # a rig that now reports two. A panel is either reachable tonight or
        # it is not; a list that was, is dealt again when the hours move.
        if current and asked is not None:
            dealt_for = float(task.get("dealtHours") or 0.0)
            if dealt_for and abs(hours - dealt_for) > 0.15 * max(hours, dealt_for):
                current = False
        if current:
            tonight.extend(collab.Region.read(cells[i]) for i in held
                           if 0 <= i < len(cells))
            continue
        # Only the rig that asked is dealt afresh. Somebody else's list from
        # an earlier night is theirs until they ask in a new one - their
        # night may still be running - and it is simply not tonight's claim.
        if asked is None and held and dealing:
            continue
        depth, mine = collab.coverage(cells, shot)
        filters = [collab.FilterTask.read(f) for f in (task.get("filters") or [])]
        share, visit = collab.assign(cells, goals, depth, mine.get(agent_id, {}),
                                     tonight, hours, filters,
                                     wants.minFramesPerVisit)
        tonight.extend(collab.Region.read(cells[i]) for i in share)

        task["assignedAt"] = now
        task["assignedNight"] = asked if asked is not None else made_for
        task["dealtHours"] = hours
        if share != held or visit != (task.get("visit") or {}):
            task["share"] = share
            # Tonight's depth on each of those panels. The task's filters stay
            # the project's full depth - what a panel wants over all nights -
            # and this is the slice of it the rig gives tonight.
            task["visit"] = visit
            task["version"] = int(task.get("version") or 1) + 1
            changed += 1
        store.set_task(task)
    return changed


def _middle_exposure(wants: collab.Requirements) -> float:
    """A sub length the project will accept, when the rig has not named one."""
    low = wants.minExposure or 0.0
    high = wants.maxExposure or 0.0
    if low and high:
        return round((low + high) / 2.0, 1)
    return low or high or 300.0


@app.post("/api/v1/agent/task/{task_id}")
def set_task_state(task_id: str, body: TaskStateRequest,
                   agent: dict[str, Any] = Depends(agent_from)) -> dict[str, Any]:
    """Accept, decline or finish a task.

    Accepting is a real step rather than a formality: an assignment that
    silently rewrote what somebody's mount did tonight would be the software
    going rogue, however well meant.
    """
    task = store.task(task_id)
    if task is None or task["agent"] != agent["id"]:
        raise HTTPException(status_code=404, detail="no such task for this agent")
    task["state"] = body.state
    task["version"] = int(task.get("version", 1)) + 1
    store.set_task(task)
    return {"task": task}


@app.post("/api/v1/agent/report")
def report(body: ReportRequest,
           agent: dict[str, Any] = Depends(agent_from)) -> dict[str, Any]:
    """Hand back what was actually captured, and hear whether it counts.

    Judged here rather than taken on trust, because the rules belong to the
    project and an agent should not be able to mark its own homework. The
    verdict is advisory — a coordinator can overrule it.
    """
    store.seen(agent["id"])
    results = []
    for raw in body.contributions:
        entry = collab.Contribution.read({**raw, "agent": agent["id"]})
        task = store.task(entry.task) if entry.task else None
        if task is not None and task["agent"] != agent["id"]:
            raise HTTPException(status_code=403,
                                detail="that task belongs to another agent")
        if task is not None and not entry.project:
            entry.project = task["project"]

        project = store.project(entry.project) if entry.project else None
        wants = collab.Requirements.read(
            (project or {}).get("payload", {}).get("requirements", {}))
        verdict = collab.judge(entry, wants)
        row_id = collab.new_id()
        stored, fresh = store.add_contribution(row_id, entry.payload(), verdict)
        results.append({"id": stored["id"], "accepted": stored["accepted"],
                        "duplicate": not fresh, "verdict": stored["verdict"]})
    return {"recorded": results}


# ---------------------------------------------------------------------------
# The coordinator's side
# ---------------------------------------------------------------------------

@app.post("/api/v1/agents")
def create_agent(body: AgentRequest,
                 who: dict[str, Any] = Depends(person_from)) -> dict[str, Any]:
    """Enrol a telescope and mint its token.

    Anybody signed in may enrol their own telescopes - that is what signing in
    is for - and the telescope is theirs from then on. The token is shown once,
    here, and kept nowhere but the observatory it is for.
    """
    token = secrets.token_urlsafe(24)
    owner = body.owner or who["name"]
    agent = store.add_agent(collab.new_id(), token, body.name, owner,
                            owner_id=who["id"])
    return {"agent": {k: v for k, v in agent.items() if k != "token"},
            "token": token}


@app.get("/api/v1/agents")
def list_agents(who: dict[str, Any] = Depends(person_from)) -> dict[str, Any]:
    """The owner sees every telescope; a member sees their own."""
    rows = store.agents() if who["admin"] else store.agents_of(who["id"])
    return {"agents": [{k: v for k, v in agent.items() if k != "token"}
                       for agent in rows]}


@app.post("/api/v1/projects")
def create_project(body: ProjectRequest,
                   who: dict[str, Any] = Depends(person_from)) -> dict[str, Any]:
    if not may_start(who):
        raise HTTPException(status_code=403,
                            detail="starting a collaboration on this server needs "
                                   "a Discord role you do not hold")
    region = collab.Region.read(body.region)
    payload = {
        "region": region.payload(),
        "kind": body.kind,
        "requirements": collab.Requirements.read(body.requirements).payload(),
        "goals": _goals(body.goals),
        "notes": body.notes,
    }
    # The name on it is the person's Discord name unless the owner, on the
    # admin token, chose to write one.
    coordinator = who["name"] if not who["admin"] else (body.coordinator or who["name"])
    project = store.add_project(collab.new_id(), body.name, coordinator, payload,
                                owner_id=who["id"])
    return {"project": project}


class ProjectUpdateRequest(BaseModel):
    """Changing a project after it was started. Every field is optional."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    region: dict[str, Any] | None = None
    kind: str | None = Field(default=None, pattern="^(single|mosaic)$")
    requirements: dict[str, Any] | None = None
    goals: dict[str, float] | None = None
    notes: str | None = Field(default=None, max_length=2000)
    # open | closed. Closing takes it off everybody's list without deleting
    # the record of what was collected.
    status: str | None = Field(default=None, pattern="^(open|closed)$")


@app.put("/api/v1/projects/{project_id}")
def update_project(project_id: str, body: ProjectUpdateRequest,
                   who: dict[str, Any] = Depends(person_from)) -> dict[str, Any]:
    """Change what a project asks for.

    A coordinator learns as the season goes: the Ha limit was too strict, the
    region should have taken in the next nebula over, the depth goal was
    reached. Starting a new project for each of those would lose the ledger.
    Only what is sent is changed; a field left out is left alone.

    Contributors are not re-judged retroactively — a night accepted under the
    old rules stays accepted — and every rig on it finds out on its next poll,
    because the requirements travel with the task.
    """
    project = _require_project_owner(who, project_id)
    payload = dict(project.get("payload") or {})
    if body.region is not None:
        payload["region"] = collab.Region.read(body.region).payload()
    if body.kind is not None:
        payload["kind"] = body.kind
    if body.requirements is not None:
        payload["requirements"] = collab.Requirements.read(body.requirements).payload()
    if body.goals is not None:
        payload["goals"] = _goals(body.goals)
    if body.notes is not None:
        payload["notes"] = body.notes
    if body.name is not None:
        store.rename_project(project_id, body.name.strip())
    updated = store.set_project(project_id, payload, body.status)
    return {"project": updated}


@app.get("/api/v1/projects")
def list_projects() -> dict[str, Any]:
    """Open to anyone: browsing is how people find a collab to join."""
    return {"projects": store.projects()}


@app.get("/api/v1/projects/{project_id}")
def read_project(project_id: str) -> dict[str, Any]:
    project = store.project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such project")
    return {"project": project, "tasks": store.tasks_in(project_id),
            "contributions": store.contributions(project_id)}


@app.post("/api/v1/projects/{project_id}/tasks")
def create_task(project_id: str, body: TaskRequest,
                who: dict[str, Any] = Depends(person_from)) -> dict[str, Any]:
    """Delegate a chunk of sky to one telescope."""
    project = _require_project_owner(who, project_id)
    if store.agent(body.agent) is None:
        raise HTTPException(status_code=404, detail="no such agent")

    task = collab.Task(
        id=collab.new_id(), project=project_id, projectName=project["name"],
        agent=body.agent, region=collab.Region.read(body.region),
        filters=[collab.FilterTask.read(entry) for entry in body.filters],
        state="offered", version=1, issued=time.time(), note=body.note)
    return {"task": store.add_task(task.payload())}


@app.post("/api/v1/contributions/{row_id}/verdict")
def override_verdict(row_id: str, accepted: bool = Query(...),
                     reason: str = Query(default=""),
                     who: dict[str, Any] = Depends(person_from)) -> dict[str, Any]:
    """Overrule the automatic judgement.

    Seeing varies, and a night the numbers reject may be the only data anybody
    has on that patch of sky. A verdict that could not be overruled would make
    the rules more authoritative than the person who wrote them. The person
    who wrote them - the project's owner - is who may overrule.
    """
    row = store.contribution(row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such contribution")
    _require_project_owner(who, row["project"])
    verdict = dict(row["verdict"])
    verdict["overriddenBy"] = who["name"]
    verdict["overrideReason"] = reason
    verdict["summary"] = reason or ("accepted by the coordinator" if accepted
                                    else "rejected by the coordinator")
    return {"contribution": store.set_verdict(row_id, accepted, verdict)}


# ---------------------------------------------------------------------------
# Everything else
# ---------------------------------------------------------------------------

@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    return {"ok": True, "protocol": collab.PROTOCOL, "time": time.time(),
            "adminConfigured": bool(ADMIN_TOKEN),
            "discord": DISCORD.configured(),
            "roleRequired": bool(DISCORD.role)}


@app.exception_handler(Exception)
async def _unhandled(_request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500,
                        content={"detail": f"{type(exc).__name__}: {exc}"})
