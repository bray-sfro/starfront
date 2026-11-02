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

import dataclasses
import json
import math
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import astrocontrol                                              # noqa: E402
from astrocontrol import catalog, collab, filters                # noqa: E402
from astrocontrol.config import data_root                        # noqa: E402
from server import auth                                          # noqa: E402
from server.files import COOKIE, COOKIE_SECONDS, Files           # noqa: E402
from server.store import Store                                   # noqa: E402

app = FastAPI(title="Starfront collaboration server", version="0.1.0")

# Beside everything else the rig keeps, and through the same function, so a
# machine that still has its folder under the old name is not left with its
# telescope's data in one place and its collaboration's in another.
DATA = Path(auth.setting("DATA") or data_root() / "collab")
store = Store(DATA / "collab.sqlite")

#: The server owner's credential. Can do anything, and is meant for the one
#: person running the server; everybody else signs in with Discord.
ADMIN_TOKEN = auth.setting("ADMIN_TOKEN")

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
    # A program's token, or the one a browser sign-in on the files page was
    # handed; both are the person.
    user = store.user_by_token(token) or store.user_by_web_token(token)
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
                       "(STARFRONT_ADMIN_TOKEN)")
        raise HTTPException(status_code=401, detail="not the server owner")
    return "coordinator"


def may_start(who: dict[str, Any]) -> bool:
    """Whether this person may start collaborations: anybody signed in.

    Being in the Discord server is the whole gate, and signing in already
    checks it - Discord refuses the member lookup for anyone who is not. A
    role requirement (DISCORD_ROLE) used to sit on top of that, so only
    customers could start one; it was dropped on 2026-10-12 so that anyone
    in the community can, customer or not. The setting is still read and
    reported, and still means nothing here.
    """
    return who is not None


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
            "roleRequired": False, "publicUrl": DISCORD.public_url}


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
    back = str(login.get("back") or "")
    if back:
        # Begun from a page on this server (the files page): the browser is
        # the client, and gets a credential of its own in a cookie rather
        # than a token the program's sign-in would be replaced by. The
        # person's record is refreshed, their program token left alone.
        existing = store.user(identity.id)
        store.upsert_user(identity.id,
                          existing["token"] if existing else auth.new_user_token(),
                          identity.name, identity.avatar, identity.roles)
        web = auth.new_user_token()
        store.add_web_token(web, identity.id)
        store.sweep_web_tokens(time.time() - COOKIE_SECONDS)
        store.bind_login(state, identity.id)
        store.claim_login(state)
        response = RedirectResponse(back if back.startswith("/") else "/files",
                                    status_code=303)
        response.set_cookie(COOKIE, web, max_age=COOKIE_SECONDS, httponly=True,
                            secure=DISCORD.public_url.startswith("https"),
                            samesite="lax", path="/")
        return response
    token = auth.new_user_token()
    store.upsert_user(identity.id, token, identity.name, identity.avatar,
                      identity.roles)
    store.bind_login(state, identity.id)
    return HTMLResponse(auth.page(
        f"Signed in as {identity.name}",
        "Go back to Starfront - it has already noticed. You can join "
        "collaborations and start your own."))


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


def _avatar_url(user: dict[str, Any] | None) -> str:
    """The Discord profile picture of a signed-in person, as a CDN link.

    Discord keeps the picture itself; the sign-in told us the hash. The link
    is public, is what Discord's own clients fetch, and sends the headers a
    browser canvas needs to draw it. Somebody without a picture gets "".
    """
    if not user:
        return ""
    user_id = str(user.get("id") or "")
    avatar = str(user.get("avatar") or "")
    if not user_id or not avatar:
        return ""
    kind = "gif" if avatar.startswith("a_") else "png"
    return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.{kind}?size=64"


def _presence_of(agent: dict[str, Any], now: float) -> dict[str, Any]:
    """One telescope as the group sees it."""
    said = agent.get("presence") or {}
    age = max(0.0, now - float(agent.get("seen") or 0.0))
    ra = _number_or_none(said.get("ra"))
    dec = _number_or_none(said.get("dec"))
    person = store.user(str(agent.get("owner_id") or "")) if agent.get("owner_id") else None
    # The name the rig gives itself on each check-in - its equipment
    # profile's - over the name it was enrolled under, which is "Telescope 1"
    # on nearly every install.
    called = str(said.get("telescope") or "").strip()[:60]
    return {
        "id": agent["id"],
        "name": called or agent.get("name") or "",
        "enrolledAs": agent.get("name") or "",
        "owner": agent.get("owner") or "",
        # Who the telescope belongs to, as Discord shows them: their current
        # name and their picture, for the chart.
        "ownerName": (person or {}).get("name") or agent.get("owner") or "",
        "avatar": _avatar_url(person),
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
        # The name the rig gives itself on check-in, over the one it was
        # enrolled under; see `_presence_of`.
        called = str((agent.get("presence") or {}).get("telescope") or "").strip()
        names.append(called or agent.get("name") or agent_id)
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
    # Whether contributors' lights are collected on this server (the files
    # page, and in time the programs themselves), for the coordinator to
    # stack. Off unless asked: gigabytes a night are nobody's by accident.
    collectFiles: bool = False


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


def _one_task_per_project(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retire duplicate shares a rig holds on one project, keeping the newest.

    Joining used to make a fresh task every time, so a rig that pressed the
    button twice held two shares of the same mosaic and was dealt panels for
    both. The older ones are marked superseded here, which takes them out of
    the dealing and the rig's list; the rig's program moves its target onto
    the one that is left.
    """
    kept: list[dict[str, Any]] = []
    newest: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if task.get("state") not in ("offered", "accepted"):
            kept.append(task)
            continue
        project = str(task.get("project") or "")
        other = newest.get(project)
        if other is None:
            newest[project] = task
            continue
        loser, winner = ((task, other) if float(task.get("issued") or 0.0)
                         <= float(other.get("issued") or 0.0) else (other, task))
        loser["state"] = "superseded"
        loser["version"] = int(loser.get("version") or 1) + 1
        store.set_task(loser)
        kept.append(loser)
        newest[project] = winner
    return kept + list(newest.values())


@app.get("/api/v1/agent/task")
def current_task(agent: dict[str, Any] = Depends(agent_from),
                 night: str = Query(default="", max_length=16),
                 moon: float | None = Query(default=None, ge=0.0, le=1.0),
                 moonUp: float | None = Query(default=None, ge=0.0, le=1.0)) -> dict[str, Any]:
    """What this telescope should be shooting.

    The whole point of the server, from an observatory's side. Returns the
    task and a version; an agent that already has that version has nothing to
    do, which keeps a poll every few minutes free.

    `night` is the night the rig is in, as it names its own nights. Its
    panels for a night are dealt once for that night and held — they must not
    move under a run because somebody else's frames arrived at 2 a.m. — and
    are dealt afresh the first time it asks in the next one. A client that
    does not say falls back to a list held for twenty hours.

    `moon` and `moonUp` are the rig's own sky tonight: how much of the Moon
    is lit, and the fraction of its dark hours the Moon is above its horizon.
    They decide whether tonight is a narrowband night for it; see
    `collab.choose_filter`. The server has no idea where any rig is, so the
    rig has to say.
    """
    store.seen(agent["id"])
    tasks = _one_task_per_project(store.tasks_for(agent["id"]))
    if not tasks:
        return {"task": None, "tasks": [], "version": 0,
                "protocol": collab.PROTOCOL}
    # A camera that has turned since its cells were cut gets them cut again
    # before anything is dealt on them.
    profile = collab.RigProfile.read(agent.get("profile") or {})
    for task in tasks:
        project = store.project(task["project"])
        if project is not None and task.get("cells"):
            _retile_if_turned(task, profile, project, night.strip())
    # Shares move as others join and as frames come in, and a poll is when a
    # rig finds out. Cheap when nothing has changed: a share that is the same
    # is not rewritten, so the version stays put and the rig has nothing to do.
    sky = ({"illumination": moon, "upFraction": moonUp}
           if moon is not None and moonUp is not None else None)
    for project_id in {task["project"] for task in tasks}:
        _redeal(project_id, {agent["id"]: night.strip()} if night.strip() else None,
                moon=sky)
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


@app.get("/api/v1/agent/projects/{project_id}/depth")
def project_depth(project_id: str,
                  agent: dict[str, Any] = Depends(agent_from)) -> dict[str, Any]:
    """The depth map of a project: everybody's seconds on every cell of the
    region, per filter, for the picture of the field on the Plan tab."""
    store.seen(agent["id"])
    project = store.project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such project")
    grid = _depth_grid(project_id, project.get("payload") or {})
    if grid is None:
        raise HTTPException(status_code=409, detail="this project has no region to map")
    return {"project": project_id, "name": project["name"], **grid,
            "progress": _progress(project_id, project.get("payload") or {})}


@app.get("/api/v1/agent/projects/{project_id}/people")
def project_people(project_id: str,
                   agent: dict[str, Any] = Depends(agent_from)) -> dict[str, Any]:
    """Who has put what into a project: each person's accepted hours, by
    filter, with their telescopes and their Discord name and picture.

    Grouped by the person rather than the telescope, because that is how
    the group thinks of it - one member with three telescopes contributed
    once, three times over. A telescope nobody has signed in for stands on
    its own under its enrolled name.
    """
    store.seen(agent["id"])
    project = store.project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="no such project")
    people: dict[str, dict[str, Any]] = {}
    refused = 0
    for row in store.contributions(project_id):
        if not row.get("accepted"):
            refused += 1
            continue
        rig = store.agent(row["agent"]) or {}
        owner_id = str(rig.get("owner_id") or "")
        person = store.user(owner_id) if owner_id else None
        key = owner_id or str(row["agent"])
        called = str((rig.get("presence") or {}).get("telescope") or "").strip()
        entry = people.setdefault(key, {
            "name": (person or {}).get("name") or rig.get("owner") or rig.get("name") or key,
            "avatar": _avatar_url(person),
            "telescopes": [],
            "byFilter": {},
            "frames": 0,
            "nights": set(),
            "lastReported": 0.0,
        })
        telescope = called or str(rig.get("name") or "")
        if telescope and telescope not in entry["telescopes"]:
            entry["telescopes"].append(telescope)
        name = filters.canonical(row.get("filter") or "") or str(row.get("filter") or "")
        entry["byFilter"][name] = entry["byFilter"].get(name, 0.0) + float(row.get("seconds") or 0.0)
        entry["frames"] += int((row.get("payload") or {}).get("frames") or 0)
        if row.get("night"):
            entry["nights"].add(str(row["night"]))
        entry["lastReported"] = max(entry["lastReported"], float(row.get("received") or 0.0))
    listed = []
    for entry in people.values():
        seconds = sum(entry["byFilter"].values())
        listed.append({
            "name": entry["name"],
            "avatar": entry["avatar"],
            "telescopes": entry["telescopes"],
            "hours": round(seconds / 3600.0, 2),
            "byFilter": {name: round(value / 3600.0, 2)
                         for name, value in sorted(entry["byFilter"].items())},
            "frames": entry["frames"],
            "nights": len(entry["nights"]),
            "lastReported": entry["lastReported"] or None,
        })
    listed.sort(key=lambda row: (-row["hours"], row["name"].lower()))
    return {"project": project_id, "name": project["name"], "people": listed,
            "hours": round(sum(row["hours"] for row in listed), 2),
            "refused": refused, "serverTime": time.time()}


#: How far back the activity feed looks by default.
ACTIVITY_HOURS = 48.0


@app.get("/api/v1/agent/activity")
def activity(agent: dict[str, Any] = Depends(agent_from),
             hours: float = Query(default=ACTIVITY_HOURS, ge=1.0, le=240.0)) -> dict[str, Any]:
    """What has happened lately across every open collaboration, newest
    first: frames accepted, with who shot them, on what, in which filter,
    and telescopes that joined. Plus, per project, what the last day
    brought - hours accepted, people who sent something, frames.

    The Collab tab is a list of projects with a number on each; this is
    the life behind the numbers, so somebody opening the tab sees a night
    in progress rather than a ledger.
    """
    store.seen(agent["id"])
    now = time.time()
    since = now - hours * 3600.0
    day = now - 24 * 3600.0
    events: list[dict[str, Any]] = []
    today: dict[str, dict[str, Any]] = {}
    people_cache: dict[str, tuple[str, str, str]] = {}

    def who(agent_id: str) -> tuple[str, str, str]:
        """(person's name, picture, telescope) for a rig, cached per call."""
        if agent_id in people_cache:
            return people_cache[agent_id]
        rig = store.agent(agent_id) or {}
        owner_id = str(rig.get("owner_id") or "")
        person = store.user(owner_id) if owner_id else None
        telescope = str((rig.get("presence") or {}).get("telescope") or rig.get("name") or "")[:60]
        name = (person or {}).get("name") or rig.get("owner") or rig.get("name") or ""
        people_cache[agent_id] = (str(name), _avatar_url(person), telescope)
        return people_cache[agent_id]

    for project in store.projects():
        if (project.get("status") or "open") != "open":
            continue
        pid = project["id"]
        summary = today.setdefault(pid, {"hours": 0.0, "frames": 0, "people": set()})
        for row in store.contributions(pid):
            at = float(row.get("received") or 0.0)
            if at < since or not row.get("accepted"):
                continue
            name, avatar, telescope = who(str(row["agent"]))
            payload = row.get("payload") or {}
            seconds = float(row.get("seconds") or 0.0)
            events.append({
                "kind": "frames", "at": round(at), "project": pid, "projectName": project["name"],
                "person": name, "avatar": avatar, "telescope": telescope,
                "filter": filters.canonical(row.get("filter") or "") or str(row.get("filter") or ""),
                "frames": int(payload.get("frames") or 0), "seconds": round(seconds, 1),
            })
            if at >= day:
                summary["hours"] += seconds / 3600.0
                summary["frames"] += int(payload.get("frames") or 0)
                summary["people"].add(name or str(row["agent"]))
        for task in store.tasks_in(pid):
            issued = float(task.get("issued") or 0.0)
            if issued < since or task.get("state") not in ("offered", "accepted"):
                continue
            name, avatar, telescope = who(str(task.get("agent") or ""))
            events.append({
                "kind": "joined", "at": round(issued), "project": pid, "projectName": project["name"],
                "person": name, "avatar": avatar, "telescope": telescope,
            })
    events.sort(key=lambda e: -e["at"])
    return {
        "events": events[:80],
        "serverTime": round(now), "since": round(since),
        "projects": {pid: {"hoursToday": round(s["hours"], 2), "framesToday": s["frames"],
                           "peopleToday": len(s["people"])}
                     for pid, s in today.items()},
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
        collected, progress = _figures(project["id"], payload)
        listed.append({
            "id": project["id"],
            "name": project["name"],
            "coordinator": project.get("coordinator") or "",
            # Whose it is, so a program can offer Edit and Close only to the
            # person who may use them. The server enforces it either way.
            "ownerId": project.get("owner_id") or "",
            "created": float(project.get("created") or 0.0),
            "region": payload.get("region") or {},
            "kind": payload.get("kind") or "mosaic",
            "requirements": wants.payload(),
            "goals": payload.get("goals") or {},
            "notes": payload.get("notes") or "",
            # Whether the lights themselves are collected here, and where
            # a person sends them.
            "collectFiles": bool(payload.get("collectFiles")),
            "filesUrl": (f"{DISCORD.public_url.rstrip('/')}/files#{project['id']}"
                         if payload.get("collectFiles") and DISCORD.public_url else ""),
            "compatibility": collab.compatibility(profile, wants),
            "joined": project["id"] in mine,
            # What everyone together has collected, so somebody choosing where
            # to point can choose the one that needs them.
            "collected": collected,
            # How much of the *field* is at the goal, per filter. On a mosaic
            # this is the number that means anything; see `_progress`.
            "progress": progress,
            # Who is on it, and how many of them are about tonight.
            **_participants(project["id"], time.time()),
        })
    return {"projects": listed, "protocol": collab.PROTOCOL}


#: The collected hours and the progress of every open project, kept from
#: one listing to the next. Every rig lists every project at every
#: check-in, and the progress is a depth map laid over the whole field from
#: every frame anybody has reported - cheap for ten projects, not for five
#: hundred polled by a few hundred rigs. A project's figures change only
#: when a night lands on it or its goals or region move, so they are
#: remembered against exactly that.
_figures_cache: dict[str, tuple[Any, dict[str, float], dict[str, dict[str, float]]]] = {}
_figures_lock = threading.Lock()


def _figures(project_id: str, payload: dict[str, Any]
             ) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """A project's collected hours per filter and its progress, cached
    until a contribution lands or the project's goals or region change."""
    rows = store.contributions(project_id)
    accepted = [r for r in rows if r.get("accepted")]
    stamp = (len(rows), len(accepted),
             max((float(r.get("received") or 0.0) for r in rows), default=0.0),
             json.dumps({"goals": payload.get("goals"), "region": payload.get("region"),
                         "kind": payload.get("kind")}, sort_keys=True, default=str))
    with _figures_lock:
        held = _figures_cache.get(project_id)
        if held is not None and held[0] == stamp:
            return held[1], held[2]
    collected = _collected(project_id)
    progress = _progress(project_id, payload)
    with _figures_lock:
        _figures_cache[project_id] = (stamp, collected, progress)
    return collected, progress


def _goals(goals: dict[str, float]) -> dict[str, float]:
    """Depth wanted per filter, keyed by the one spelling of each name."""
    out: dict[str, float] = {}
    for name, hours in (goals or {}).items():
        key = filters.canonical(name) or str(name)
        out[key] = out.get(key, 0.0) + float(hours)
    return out


#: How finely a region is cut when progress is measured across it: the
#: longer side in this many cells. Sixteen across a fifteen-degree mosaic
#: is a cell under a degree, a quarter of a panel - fine enough that a
#: panel's worth of frames counts where it landed and not where it did not.
PROGRESS_CELLS = 16

#: The fraction of the goal a cell must hold to count as at it.
AT_GOAL = 0.9


#: How far beyond its region a single target's map may reach to take in the
#: frames around it, as a multiple of the region's size. A frame that landed
#: in the wrong part of the sky altogether must not stretch the map to it.
SINGLE_EXTENT_LIMIT = 3.0


def _footprints(shot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Everybody's accepted frames as the fields of view they were shot
    with, merged where one telescope shot the same field at the same angle
    through the same filter: where it was, how big, which way it was
    turned, and the seconds that went into it."""
    merged: dict[tuple, dict[str, Any]] = {}
    for row in shot:
        try:
            region = collab.Region.read(row["footprint"])
        except (KeyError, TypeError, ValueError):
            continue
        agent = str(row.get("agent") or "")
        key = (agent, collab._key(row.get("filterName")), round(region.ra, 3), round(region.dec, 3),
               round(region.width, 3), round(region.height, 3), round(region.rotation, 1))
        entry = merged.get(key)
        if entry is None:
            telescope = store.agent(agent) or {}
            said = telescope.get("presence") or {}
            entry = merged[key] = {
                "agent": agent,
                "telescope": str(said.get("telescope") or telescope.get("name") or "")[:60],
                "filter": filters.canonical(row.get("filterName") or ""),
                **region.payload(), "seconds": 0.0, "frames": 0,
            }
        entry["seconds"] += float(row.get("seconds") or 0.0)
        entry["frames"] += int(row.get("frames") or 0)
    out = sorted(merged.values(), key=lambda f: (-f["seconds"], f["telescope"]))
    for entry in out:
        entry["seconds"] = round(entry["seconds"], 1)
    return out


def _single_extent(region: collab.Region,
                   prints: list[dict[str, Any]]) -> collab.Region:
    """The sky a single target's map shows: the region and every frame
    around it, with a margin, so the union of everybody's fields of view is
    in the picture and not only the part of it inside the box."""
    cosine = max(math.cos(math.radians(region.dec)), 1e-6)
    xs = [-region.width / 2.0, region.width / 2.0]
    ys = [-region.height / 2.0, region.height / 2.0]
    for f in prints:
        across, down = collab.footprint(f["width"], f["height"], f.get("rotation"))
        x = (((f["ra"] - region.ra + 180.0) % 360.0) - 180.0) * cosine
        y = f["dec"] - region.dec
        xs += [x - across / 2.0, x + across / 2.0]
        ys += [y - down / 2.0, y + down / 2.0]
    limit_x = region.width * SINGLE_EXTENT_LIMIT / 2.0
    limit_y = region.height * SINGLE_EXTENT_LIMIT / 2.0
    x0, x1 = max(min(xs), -limit_x), min(max(xs), limit_x)
    y0, y1 = max(min(ys), -limit_y), min(max(ys), limit_y)
    width, height = (x1 - x0) * 1.1, (y1 - y0) * 1.1
    return collab.Region(ra=(region.ra + (x0 + x1) / 2.0 / cosine) % 360.0,
                         dec=region.dec + (y0 + y1) / 2.0,
                         width=width, height=height, rotation=region.rotation)


def _depth_grid(project_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """The field cut fine, with everybody's accepted seconds on every cell.

    The one depth map behind both the progress figures and the picture of
    the field: cells north-up, and per filter the seconds each cell has been
    given, credited from every accepted frame's footprint in proportion to
    how much of the cell it covers, the frame turned as it really was.

    A mosaic's cells tile its region. A single target's reach out to take
    in every frame shot around it - the fields of view of a dozen cameras
    at a dozen angles do not stop at the box - and each cell says whether
    it is inside the region, which is what progress is measured over. The
    frames themselves come back as `footprints`, so the picture can draw
    each camera's field at its angle over the depth it made.
    """
    goals = _goals(payload.get("goals") or {})
    try:
        region = collab.Region.read(payload.get("region") or {})
    except (KeyError, TypeError, ValueError):
        return None
    if region.width <= 0 or region.height <= 0:
        return None
    shot = [row["payload"] for row in store.contributions(project_id)
            if row.get("accepted") and (row.get("payload") or {}).get("footprint")]
    prints = _footprints(shot)
    single = payload.get("kind") == "single"
    extent = _single_extent(region, prints) if single else region
    cell = max(abs(extent.width), abs(extent.height)) / PROGRESS_CELLS
    cells = collab.grid(extent, cell, cell, 0.0)
    cosine = max(math.cos(math.radians(region.dec)), 1e-6)
    for c in cells:
        dx = (((c["ra"] - region.ra + 180.0) % 360.0) - 180.0) * cosine
        dy = c["dec"] - region.dec
        c["inside"] = (abs(dx) <= region.width / 2.0 + 1e-9
                       and abs(dy) <= region.height / 2.0 + 1e-9)
    depth, _ = collab.coverage(cells, shot)
    names = list(goals) or sorted({name for i in depth for name in depth[i]})
    seconds = {name: [round(depth.get(i, {}).get(collab._key(name), 0.0), 1)
                      for i in range(len(cells))] for name in names}
    return {
        "region": region.payload(),
        "extent": extent.payload(),
        "kind": "single" if single else "mosaic",
        "columns": (max(int(c.get("column", 0)) for c in cells) + 1) if cells else 0,
        "rows": (max(int(c.get("row", 0)) for c in cells) + 1) if cells else 0,
        "cells": [{"ra": c["ra"], "dec": c["dec"], "width": c["width"], "height": c["height"],
                   "row": c.get("row", 0), "column": c.get("column", 0),
                   "inside": bool(c.get("inside", True))} for c in cells],
        "seconds": seconds,
        "goals": goals,
        "footprints": prints,
    }


def _progress(project_id: str, payload: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Per filter, how much of the region is at the goal depth, across everybody.

    A total of hours says nothing about a mosaic: ten hours on one panel and
    none on the other fourteen is ten hours. What the coordinator set out was
    a depth at *every point* of the region, so that is what is measured.
    The region is cut into a fine grid; every accepted frame's footprint
    credits the cells it covers with its seconds, in proportion to how much
    of each it covers; and each filter then reports the fraction of the
    field at full depth, the field's average against the goal, and its
    thinnest cell. A project is done when the first of those is one.
    """
    grid = _depth_grid(project_id, payload)
    if grid is None or not grid["goals"]:
        return {}
    # Progress is over the region the coordinator drew. A single target's
    # map reaches further, to show the frames around the field, and the sky
    # outside the box is not part of what was asked for.
    inside = [i for i, c in enumerate(grid["cells"]) if c.get("inside", True)] \
        or list(range(len(grid["cells"])))
    count = max(1, len(inside))
    out: dict[str, dict[str, float]] = {}
    for name, hours in grid["goals"].items():
        goal = float(hours) * 3600.0
        if goal <= 0:
            continue
        column = grid["seconds"].get(name) or [0.0] * len(grid["cells"])
        fractions = [min(1.0, column[i] / goal) for i in inside]
        out[name] = {
            "goalHours": round(float(hours), 2),
            # Within a tenth of the goal counts as there: the outermost sliver
            # of a frame is its overlap with the next, and the flat-sky
            # arithmetic the overlap is measured with is itself a few percent
            # out at the corners of a tall region.
            "atGoal": round(sum(1 for f in fractions if f >= AT_GOAL) / count, 3),
            "average": round(sum(fractions) / count, 3),
            "thinnest": round(min(fractions), 3),
        }
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
    #: Which night the rig is in and what its Moon is doing, so the first
    #: deal is tonight's and made with the Moon in mind. A join that dealt
    #: blind got one filter and the first poll another, under a running
    #: sequence.
    night: str = Field(default="", max_length=16)
    moon: float | None = Field(default=None, ge=0.0, le=1.0)
    moonUp: float | None = Field(default=None, ge=0.0, le=1.0)


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

    # Joining twice is one share, not two. A second task for the same rig on
    # the same project was dealt panels as though it were another telescope,
    # claimed sky nobody was on, and left the rig with two lists to confuse
    # its plan with. The one it already holds is handed back.
    already = [task for task in store.tasks_for(agent["id"])
               if task.get("project") == project_id
               and task.get("state") in ("offered", "accepted")]
    nights = {agent["id"]: body.night.strip()} if body.night.strip() else None
    sky = ({"illumination": body.moon, "upFraction": body.moonUp}
           if body.moon is not None and body.moonUp is not None else None)
    if already:
        _redeal(project_id, nights, moon=sky)
        wants = collab.Requirements.read(payload.get("requirements") or {})
        return {"task": store.task(already[0]["id"]), "requirements": wants.payload(),
                "alreadyJoined": True}

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
    # Only the project's filters this telescope carries: a rig with R, G and
    # B on an H, O, R, G, B project is dealt R, G and B and nothing else, and
    # the deal sends the H and O to the rigs that have them.
    names = list(verdict.get("usable") or []) or list(wants.filters) or list(goals) or ["L"]
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
    _redeal(project_id, nights, moon=sky)
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
    if profile.rotation is not None:
        # No rotator: the grid the rig's own program lays, cell for panel,
        # so what is dealt here is exactly what is shot there.
        return collab.camera_grid(region, field[0], field[1], float(profile.rotation), 0.1)
    return collab.grid(region, across, down, 0.1)


#: How far a camera may turn before its cells are cut again. The same figure
#: the program uses before it re-lays a mosaic.
RETILE_DEGREES = 2.0


#: How far a cell's centre may sit from where the tiling would put it now
#: before the cells count as cut by a different rule. A fifth of a degree:
#: a twentieth of a panel. The camera's angle in a rig's profile moves by a
#: few hundredths of a degree with every plate solve, which swings the
#: outermost cells of a wide mosaic by a hundredth or two - and a tolerance
#: that caught that re-cut the cells, cleared the share and restarted the
#: rig's run every couple of hours, all night.
SAME_CELL_DEGREES = 0.2


def _same_cells(stored: list[dict[str, Any]], fresh: list[dict[str, Any]]) -> bool:
    """Whether a task's cells are the ones the tiling would cut now.

    Count and centres, to within `SAME_CELL_DEGREES`; the sizes follow from
    the same camera and need no checking of their own. This tells a tiling
    rule that has changed from the same rule applied to a camera angle that
    has wobbled by a solve's worth.
    """
    if len(stored) != len(fresh):
        return False
    for a, b in zip(stored, fresh):
        try:
            cosine = max(0.05, math.cos(math.radians(float(a["dec"]))))
            east = (((float(a["ra"]) - float(b["ra"]) + 180.0) % 360.0) - 180.0) * cosine
            if abs(east) > SAME_CELL_DEGREES or abs(float(a["dec"]) - float(b["dec"])) > SAME_CELL_DEGREES:
                return False
        except (KeyError, TypeError, ValueError):
            return False
    return True


def _retile_if_turned(task: dict[str, Any], profile: collab.RigProfile,
                      project: dict[str, Any], night: str = "") -> bool:
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
    payload = project.get("payload") or {}
    if was == "unset":
        task["tiledRotation"] = now
        store.set_task(task)
        return False
    # Half a turn is the same rectangle on the sky, so the cells are the
    # same cells: only the remainder past a half-turn counts as having moved.
    turned = ((was is None) != (now is None)
              or (was is not None and now is not None
                  and abs(((float(was) - float(now) + 90.0) % 180.0) - 90.0) > RETILE_DEGREES))
    cells = _tile(collab.Region.read(payload["region"]),
                  task.get("kind") or "mosaic", profile)
    if not cells:
        return False
    # Cells cut by an older rule are cut again too: the tiling of a fixed
    # camera changed to match the rig's own panels, and a task tiled the old
    # way would otherwise keep its twelve cells against the rig's fifteen
    # for as long as the project ran. But not under a list dealt for the
    # night in hand - that waits for the night to turn, like everything else.
    held_tonight = bool(task.get("share")) and night and task.get("assignedNight") == night
    if not turned and not held_tonight and not _same_cells(task.get("cells") or [], cells):
        turned = True
    if not turned:
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


def _redeal(project_id: str, nights: dict[str, str] | None = None,
            moon: dict[str, Any] | None = None) -> int:
    """Give every rig on a project its panels for tonight, and say who changed.

    Called whenever a rig joins or asks what to shoot. It is what replaces a
    coordinator dealing chunks out by hand, and it is what makes each night
    different from the last: the panels a rig is sent to are chosen from what
    the whole collaboration has already collected — the depth map, built from
    every panel anybody has reported — and from where *this* rig has been.
    See `collab.assign` for the order the pulls are resolved in.

    A list, once made, holds for the night it was made for - without
    exception. `nights` says which night the asking rig is in; its list is
    remade the first time it asks in a new one, so everything moves on by
    default each night and nothing moves under a rig that is shooting.
    Everybody else's list is left as it is (or remade if it is twenty hours
    old and its rig never said which night it was in) and counts as spoken
    for tonight. A rig whose list changed has its task's version bumped,
    which is the signal its program watches for; one whose list held is
    left exactly alone, so an idle poll costs nothing.
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
    # Seconds committed tonight per filter by the rigs already dealt, so the
    # next rig is sent to the filter that is still thinnest once those are
    # counted. See `collab.choose_filter`.
    spoken: dict[str, float] = {}

    def commit(task: dict[str, Any]) -> None:
        visit = task.get("visit") or {}
        panels = len(task.get("share") or [])
        entries = {collab._key(f.get("filter")): collab.FilterTask.read(f)
                   for f in (task.get("filters") or [])}
        for name, count in (visit.get("frames") or {}).items():
            key = collab._key(name)
            entry = entries.get(key)
            seconds = int(count) * (entry.exposure if entry else 0.0) * panels
            # A sub through a dual-band filter is a sub in each of its
            # channels, so it is spoken for in both.
            for channel in (entry.keys() if entry else [key]):
                spoken[channel] = spoken.get(channel, 0.0) + seconds

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
        # And that is the whole rule. A list dealt for a night holds for
        # that night, whatever arrives: a rig's hours shrinking, another rig
        # joining, frames landing, a better idea about the filters. Every
        # one of those is reconsidered when the night turns and the list is
        # dealt afresh, and not before. A sequence drawn up at dusk - here,
        # or in another program that only reads the deal once - must be the
        # sequence that runs; a server that redrew it at two in the morning
        # cost a slew and a refocus every time it did, and nothing was wrong.
        if current:
            tonight.extend(collab.Region.read(cells[i]) for i in held
                           if 0 <= i < len(cells))
            commit(task)
            continue
        # Only the rig that asked is dealt afresh. Somebody else's list from
        # an earlier night is theirs until they ask in a new one - their
        # night may still be running - and it is simply not tonight's claim.
        if asked is None and held and dealing:
            continue
        depth, mine = collab.coverage(cells, shot)
        if _sync_task_filters(task, wants, goals, profile):
            task["version"] = int(task.get("version") or 1) + 1
        filters = [collab.FilterTask.read(f) for f in (task.get("filters") or [])]
        # Only what this rig can shoot. A telescope with one filter in a
        # drawer carries exactly that filter tonight, and dealing it OIII
        # because OIII is thinnest gets the project a night of Ha labelled
        # as a disappointment. A rig that has said nothing about its filters
        # is offered the lot, as before.
        carried = {collab._key(name) for name in (profile.filters or {})}
        if carried:
            able = [f for f in filters if collab._key(f.filter) in carried]
            if able:
                filters = able
        night_goals = goals
        chosen = None
        need = None
        if (task.get("kind") or "mosaic") == "mosaic" and len(filters) > 1:
            # A mosaic night is one filter per telescope, and which one is
            # the collaboration's call: the filter thinnest across the field
            # once what the other rigs are putting in tonight is counted.
            chosen = collab.choose_filter(filters, goals, depth, cells, spoken, hours,
                                          moon if asked is not None else None)
            if chosen is not None:
                filters = [chosen]
                # The goals of the channels the chosen filter delivers: a
                # dual-band night is measured against Ha and OIII both.
                night_goals = {name: value for name, value in goals.items()
                               if collab._key(name) in chosen.keys()}
                if not night_goals:
                    night_goals = {chosen.filter: chosen.hours}
        elif len(filters) > 1:
            # A single field shoots several filters in one night, and the
            # night goes to the thinnest of them first: what the field has
            # already, and what the rigs dealt before this one are adding
            # tonight, decide the split and the order. An even split by the
            # goals left one filter at a third of the others' depth, last in
            # every rig's night and the first thing dawn cut.
            need = collab.night_need(filters, goals, depth, cells, spoken,
                                     moon if asked is not None else None)
        share, visit = collab.assign(cells, night_goals, depth, mine.get(agent_id, {}),
                                     tonight, hours, filters,
                                     wants.minFramesPerVisit, need)
        if chosen is not None:
            visit["filter"] = chosen.filter
        if need is not None and visit.get("frames"):
            # The rig shoots its filters in the task's order; put the night's
            # order there, thinnest first, with the rest behind unchanged.
            shot_order = list(visit["frames"])
            by_key = {collab._key(name): index for index, name in enumerate(shot_order)}
            task["filters"] = sorted(task.get("filters") or [],
                                     key=lambda f: by_key.get(collab._key(f.get("filter")),
                                                              len(shot_order)))
        if moon is not None and asked is not None:
            # What the choice was made under, so a list made blind to the
            # Moon can be told from one that was not.
            visit["moon"] = collab.moon_badness(moon)
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
        commit(task)
    return changed


def _sync_task_filters(task: dict[str, Any], wants: collab.Requirements,
                       goals: dict[str, float], profile: collab.RigProfile) -> bool:
    """Bring a task's filters up to date with its project's.

    A task's filters were fixed the night the rig joined, so a project
    that added luminance a week later never dealt it to anyone already on
    it: every rig's list went on saying H, O, R, G and B until it left and
    joined again. The project's filters the rig carries are what it holds
    now - added at the rig's own exposure for that filter, or the middle
    of what the project takes - and a filter the project dropped is
    dropped. True when anything changed.
    """
    names = list(wants.filters) or list(goals)
    if not names:
        return False
    # The channels the project wants, with their bandpass limits; a goal
    # with no filter entry wants its channel with no limit.
    wanted = collab.wanted_channels(wants) if wants.filters else {
        collab._key(name): None for name in goals}
    goal_of = {collab._key(name): float(hours) for name, hours in goals.items()}
    held = list(task.get("filters") or [])
    by_key = {collab._key(f.get("filter")): f for f in held}
    fallback = float(held[0].get("exposure") or 0.0) if held else 0.0
    fallback = fallback or _middle_exposure(wants)

    def clamp(exposure: float) -> float:
        if wants.minExposure is not None and exposure < wants.minExposure:
            exposure = float(wants.minExposure)
        if wants.maxExposure is not None and exposure > wants.maxExposure:
            exposure = float(wants.maxExposure)
        return exposure

    fresh = []
    if profile.filters or profile.colour:
        # One entry per filter the rig carries that delivers something the
        # project wants, named as the rig names it - "H" on a mono wheel,
        # "L-eXtreme" on a colour camera - with the channels a sub through
        # it counts towards. A filter wider than the project's limit for a
        # channel does not deliver that channel here.
        own = dict(profile.filters or {})
        if profile.colour and not own:
            own = {"RGB": None}
        for name, band in own.items():
            letters = []
            for letter in profile.delivers(name):
                limit = wanted.get(collab._key(letter), "absent")
                if limit == "absent":
                    continue
                if limit is not None and (band is None or band > limit):
                    continue
                letters.append(letter)
            if not letters:
                continue
            key = collab._key(name)
            hours = max((goal_of.get(collab._key(c), 0.0) for c in letters), default=0.0) or 1.0
            if key in by_key:
                kept = dict(by_key[key])
                kept["channels"] = letters
                kept["hours"] = hours
                fresh.append(kept)
                continue
            exposure = clamp(float((profile.exposures or {}).get(name)
                                   or (profile.exposures or {}).get(key) or 0.0) or fallback)
            fresh.append(collab.FilterTask(filter=name, exposure=exposure, hours=hours,
                                           channels=letters).payload())
    else:
        # A rig that has said nothing about its filters is offered the lot,
        # one entry per channel, as before.
        for key in wanted:
            name = collab._normal(key) or key
            if key in by_key:
                fresh.append(by_key[key])
                continue
            exposure = clamp(float((profile.exposures or {}).get(key) or 0.0) or fallback)
            fresh.append(collab.FilterTask(filter=name, exposure=exposure,
                                           hours=goal_of.get(key) or 1.0,
                                           channels=[name] if collab._normal(name) else []).payload())
    if not fresh:
        return False
    # In the project's order, whatever order the rig lists its filters in:
    # a deal that has nothing else to go on - equal progress, no Moon -
    # ties to the first, and the first is the coordinator's to choose.
    order = list(wanted)
    fresh.sort(key=lambda f: min((order.index(collab._key(c)) for c in (f.get("channels") or [])
                                  if collab._key(c) in order), default=len(order)))
    if [collab._key(f.get("filter")) for f in fresh] == list(by_key) \
            and all(sorted(f.get("channels") or []) == sorted(by_key[collab._key(f.get("filter"))].get("channels") or [])
                    for f in fresh):
        return False
    task["filters"] = fresh
    return True


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
        # The channels the night carries. A client that said nothing sent a
        # mono filter by its letter, which is its one channel; a filter the
        # table knows - an L-eXtreme - is its lines. A night through a
        # filter that carries two channels is recorded once per channel,
        # each with the whole night's seconds, so the depth map and the
        # progress count it in both, and the filter it really came through
        # travels with each row.
        letters = list(entry.channels) or collab.filter_channels(entry.filterName, entry.colour)
        rows = []
        if len(letters) <= 1 and (not letters or collab._key(letters[0]) == collab._key(entry.filterName)):
            if letters and not entry.channels:
                entry.channels = letters
            rows.append(entry)
        else:
            for letter in letters:
                rows.append(dataclasses.replace(entry, filterName=letter, channels=[letter],
                                                sourceFilter=entry.sourceFilter or entry.filterName))
        verdict = collab.judge(entry, wants)
        first = None
        for row in rows:
            stored, fresh = store.add_contribution(collab.new_id(), row.payload(), verdict)
            if first is None:
                first = {"id": stored["id"], "accepted": stored["accepted"],
                         "duplicate": not fresh, "verdict": stored["verdict"]}
        results.append(first)
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
        "collectFiles": bool(body.collectFiles),
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
    collectFiles: bool | None = None
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
    if body.collectFiles is not None:
        payload["collectFiles"] = bool(body.collectFiles)
    if body.name is not None:
        store.rename_project(project_id, body.name.strip())
    updated = store.set_project(project_id, payload, body.status)
    return {"project": updated}


@app.post("/api/v1/projects/{project_id}/redeal")
def redeal_project(project_id: str,
                   who: dict[str, Any] = Depends(person_from)) -> dict[str, Any]:
    """Deal every rig on the project afresh the next time it asks.

    A list dealt for a night holds for that night, and rightly: nothing
    moves under a rig that is shooting. The coordinator is the exception.
    When the rules of the deal change - as they did the day the thinnest
    filter started coming first - the lists already made for tonight were
    made the old way, and holding them is a night lost. This forgets which
    night each live list was made for, so each rig's next poll is dealt
    afresh for its own night. A rig that never polls keeps the list it
    has, so nothing is pulled out from under anyone mid-run.
    """
    _require_project_owner(who, project_id)
    forgotten = 0
    for task in store.tasks_in(project_id):
        if task.get("state") not in ("offered", "accepted"):
            continue
        if not task.get("assignedNight") and not task.get("assignedAt"):
            continue
        task["assignedNight"] = ""
        task["assignedAt"] = 0.0
        store.set_task(task)
        forgotten += 1
    return {"project": project_id, "forgotten": forgotten}


@app.get("/api/v1/projects")
def list_projects() -> dict[str, Any]:
    """Open to anyone: browsing is how people find a collab to join."""
    return {"projects": store.projects()}


@app.get("/api/v1/lookup")
def lookup_object(name: str = Query(default="", max_length=80),
                  who: dict[str, Any] = Depends(person_from)) -> dict[str, Any]:
    """An object by name, for somebody starting a collaboration from a
    program that has no catalog of its own.

    The bundled table first - the Messier catalog and the usual showpieces
    - then CDS Sesame for anything else. Right ascension comes back in
    degrees, as everything about a region does on this server, and the
    size in arcminutes where the table knows it, which is a fair first
    guess at how big a single field or a mosaic ought to be.
    """
    found = catalog.lookup(name, limit=12)
    results = []
    for hit in found.get("results") or []:
        results.append({
            "id": hit.get("id") or "",
            "name": (hit.get("name") or hit.get("id") or "").strip(),
            "type": hit.get("type") or "",
            "ra": round(float(hit["ra"]) * 15.0, 5),
            "dec": round(float(hit["dec"]), 5),
            "sizeArcmin": hit.get("size"),
            "constellation": hit.get("constellation") or "",
            "source": hit.get("source") or found.get("source") or "catalog",
        })
    return {"results": results, "source": found.get("source") or "catalog"}


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
# The N.I.N.A. plugin, served from here
# ---------------------------------------------------------------------------

#: Where the N.I.N.A. plugin's releases sit: the manifests N.I.N.A.'s plugin
#: manager reads, and the zips they point at. N.I.N.A. lets anyone add a
#: *plugin repository* by URL (Options > General > Plugin Repositories) and
#: fetches `<url>/plugins/manifests` from it, so this server is one: people
#: who add `https://<host>/nina` see the plugin in their plugin manager and
#: install and update it there, with no review queue and no wait between a
#: fix and its release - which the central manifest repository, with its
#: review and its public listing, does not give.
NINA_DIR = Path(auth.setting("NINA_DIR") or Path(__file__).resolve().parent / "nina_plugin")

_NINA_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.zip$")


def _manifest_version(data: dict[str, Any]) -> tuple[int, int, int, int]:
    version = data.get("Version") or {}
    try:
        return tuple(int(version.get(k, 0)) for k in ("Major", "Minor", "Patch", "Build"))  # type: ignore[return-value]
    except (TypeError, ValueError):
        return (0, 0, 0, 0)


def _nina_manifests() -> list[dict[str, Any]]:
    """The newest manifest of each plugin in the folder.

    Older releases left in the folder are not offered: N.I.N.A. would pick
    the newest anyway, and a feed listing six versions of one plugin reads
    as a mess to anybody who looks at it.
    """
    newest: dict[str, dict[str, Any]] = {}
    if not NINA_DIR.is_dir():
        return []
    for path in sorted(NINA_DIR.glob("*.manifest.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not (isinstance(data, dict) and data.get("Name") and data.get("Identifier")):
            continue
        key = str(data["Identifier"]).upper()
        if key not in newest or _manifest_version(data) > _manifest_version(newest[key]):
            newest[key] = data
    return sorted(newest.values(), key=lambda m: str(m.get("Name")))


@app.get("/nina/plugins/manifests")
def nina_manifests() -> list[dict[str, Any]]:
    """What N.I.N.A.'s plugin manager asks a plugin repository for: a list
    of manifests, each saying where its zip is and what its checksum is."""
    return _nina_manifests()


@app.get("/nina/download/{name}")
def nina_download(name: str):
    """A plugin release, by the file name its manifest gives."""
    if not _NINA_FILE.match(name):
        raise HTTPException(status_code=404, detail="no such file")
    path = NINA_DIR / name
    if not path.is_file() or path.resolve().parent != NINA_DIR.resolve():
        raise HTTPException(status_code=404, detail="no such file")
    return FileResponse(path, media_type="application/zip", filename=name)


@app.get("/nina/guide")
def nina_guide():
    """The plugin's instruction guide as a PDF, staged beside the releases,
    for a link on the website and in Discord."""
    path = NINA_DIR / "Starfront Collaboration for NINA.pdf"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no guide")
    return FileResponse(path, media_type="application/pdf",
                        filename="Starfront Collaboration for NINA.pdf",
                        content_disposition_type="inline")


@app.get("/nina/start")
def nina_getting_started():
    """The first-night walk-through for people new to N.I.N.A.'s Advanced
    Sequencer, staged beside the releases like the guide."""
    path = NINA_DIR / "Getting started with Starfront in NINA.pdf"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no guide")
    return FileResponse(path, media_type="application/pdf",
                        filename="Getting started with Starfront in NINA.pdf",
                        content_disposition_type="inline")


@app.get("/nina/changelog")
def nina_changelog():
    """The plugin's change log, which its manifests link to. A copy of
    nina/CHANGELOG.md staged beside the releases, since the source is not
    somewhere the public can read it."""
    path = NINA_DIR / "CHANGELOG.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no change log")
    return FileResponse(path, media_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# The download: Starfront itself, for the people the observatory gives it to
# ---------------------------------------------------------------------------
#
# The program's source is private. What people get is the installer, and
# they get it from here rather than from a code-hosting site that would put
# the source beside it. The folder holds the installers; the newest version
# is the download, and the page says only that one. An older build stays
# where its own link points without being offered.

DOWNLOAD_DIR = Path(auth.setting("DOWNLOAD_DIR") or Path(__file__).resolve().parent / "downloads")
_SETUP_FILE = re.compile(r"^Starfront-Setup-(\d+)\.(\d+)\.(\d+)\.exe$")


def _setups() -> list[dict[str, Any]]:
    """The installers in the folder, newest version first."""
    found: list[dict[str, Any]] = []
    if not DOWNLOAD_DIR.is_dir():
        return found
    for path in DOWNLOAD_DIR.glob("Starfront-Setup-*.exe"):
        match = _SETUP_FILE.match(path.name)
        if not match or not path.is_file():
            continue
        stat = path.stat()
        found.append({"file": path.name, "version": ".".join(match.groups()),
                      "key": tuple(int(part) for part in match.groups()),
                      "size": stat.st_size, "modified": stat.st_mtime})
    found.sort(key=lambda setup: setup["key"], reverse=True)
    return found


def _latest_setup() -> dict[str, Any] | None:
    setups = _setups()
    return setups[0] if setups else None


@app.get("/download/latest.json")
def download_latest() -> dict[str, Any]:
    """What the newest download is, for a page or for a program that checks."""
    latest = _latest_setup()
    if latest is None:
        return {"version": None, "file": None, "url": None, "size": 0, "published": None}
    return {"version": latest["version"], "file": latest["file"], "size": latest["size"],
            "url": f"/download/{latest['file']}",
            "published": time.strftime("%Y-%m-%d", time.gmtime(latest["modified"]))}


@app.get("/download/latest")
def download_latest_file():
    """A link that always fetches the newest installer."""
    latest = _latest_setup()
    if latest is None:
        raise HTTPException(status_code=404, detail="nothing to download yet")
    return RedirectResponse(f"/download/{latest['file']}", status_code=302)


@app.get("/download/{name}")
def download_file(name: str):
    """One installer, by its file name. Only installers are served."""
    if not _SETUP_FILE.match(name):
        raise HTTPException(status_code=404, detail="no such file")
    path = DOWNLOAD_DIR / name
    if not path.is_file() or path.resolve().parent != DOWNLOAD_DIR.resolve():
        raise HTTPException(status_code=404, detail="no such file")
    return FileResponse(path, media_type="application/octet-stream", filename=name)


_DOWNLOAD_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Download Starfront</title>
<style>
  body {{ margin: 0; background: #05070c; color: #dfe5f0; font-family: system-ui, sans-serif; }}
  main {{ max-width: 640px; margin: 0 auto; padding: 56px 20px; }}
  h1 {{ font-size: 2rem; margin: 0 0 6px; }}
  .sub {{ color: #8f9bb3; margin: 0 0 32px; }}
  .btn {{ display: inline-block; background: #7ee7a5; color: #06130c; font-weight: 600;
          padding: 14px 26px; border-radius: 10px; text-decoration: none; font-size: 1.1rem; }}
  .meta {{ color: #8f9bb3; font-size: 0.9rem; margin: 12px 0 40px; }}
  h2 {{ font-size: 1.05rem; margin: 28px 0 8px; }}
  p, li {{ line-height: 1.5; }}
  a {{ color: #9fe4bd; }}
  code {{ background: #11161f; padding: 2px 6px; border-radius: 4px; }}
</style>
</head>
<body>
<main>
  <h1>Starfront</h1>
  <p class="sub">Telescope control and collaborative imaging for Windows.</p>
  {button}
  <h2>Installing</h2>
  <p>Run the installer. It puts Starfront under your own Programs folder with a
  Start-menu shortcut and an uninstaller, and asks for no administrator rights.
  Windows may say it protected your PC the first time: <em>More info &rarr; Run
  anyway</em>. An upgrade installs over the top and keeps your settings, targets
  and plan.</p>
  <h2>Staying on N.I.N.A.?</h2>
  <p>There is a plugin instead. In N.I.N.A., Options &rarr; General &rarr; Plugin
  Repositories, press + and enter <code>{host}/nina</code>, then install
  &ldquo;Starfront&rdquo; from the Plugins tab.</p>
  <p><a href="/sky">Tonight at Starfront</a>: where the observatory's telescopes are pointed right now.</p>
</main>
</body>
</html>
"""


@app.get("/download")
def download_page(request: Request) -> HTMLResponse:
    """The page with the one button."""
    latest = _latest_setup()
    if latest is None:
        button = "<p class=\"meta\">There is no download yet.</p>"
    else:
        size = latest["size"] / (1024 * 1024)
        button = (f"<a class=\"btn\" href=\"/download/{latest['file']}\">Download Starfront "
                  f"{latest['version']} for Windows</a>"
                  f"<p class=\"meta\">{latest['file']} &middot; {size:.0f} MB &middot; "
                  f"Windows 10 and 11, 64-bit</p>")
    host = DISCORD.public_url.rstrip("/") if DISCORD.public_url else f"{request.url.scheme}://{request.url.netloc}"
    return HTMLResponse(_DOWNLOAD_PAGE.format(button=button, host=host))


# ---------------------------------------------------------------------------
# The sky: where everybody is pointing, for anybody's web page
# ---------------------------------------------------------------------------
#
# A public picture of the night: each telescope that shares its position,
# where it points now, what it is on, and the track it has drawn across the
# sky since dusk. Meant for a page on the observatory's own website, so it
# is served with permissive CORS and no sign-in, and it carries nothing but
# what the telescope chose to say - its name, its target, its pointing. No
# ids, no owners, no pictures of people. A rig that has position sharing off
# never sends a pointing and never appears.

SKY_DIR = Path(__file__).resolve().parent / "sky"
#: How far back a telescope's track is drawn by default: a whole night.
TRAIL_HOURS = 14.0
#: The files the widget is made of, and nothing else from that folder.
_SKY_FILES = {"widget.js": "application/javascript; charset=utf-8",
              "stars.json": "application/json",
              "index.html": "text/html; charset=utf-8",
              "starfront-sky.liquid": "text/plain; charset=utf-8"}
_SKY_HEADERS = {"Access-Control-Allow-Origin": "*"}


@app.get("/api/v1/sky")
def sky(hours: float = Query(default=TRAIL_HOURS, ge=0.0, le=48.0)) -> JSONResponse:
    """Every telescope on the sky tonight, with its track. Open to anyone."""
    now = time.time()
    trails = store.presence_trails(now - hours * 3600.0) if hours > 0 else {}
    rows: list[dict[str, Any]] = []
    for entry in store.agents():
        age = now - float(entry.get("seen") or 0.0)
        if age > 24 * 3600:
            continue
        said = entry.get("presence") or {}
        ra = _number_or_none(said.get("ra"))
        dec = _number_or_none(said.get("dec"))
        if ra is None or dec is None:
            continue
        called = str(said.get("telescope") or "").strip()[:60]
        rows.append({
            "name": called or str(entry.get("name") or "")[:60],
            "state": str(said.get("state") or "")[:40],
            "target": str(said.get("target") or "")[:80],
            "ra": round(ra, 4), "dec": round(dec, 4),
            "online": age <= ONLINE_SECONDS,
            "ageSeconds": round(age),
            "trail": [[round(float(row["at"])), round(float(row["ra"]), 4),
                       round(float(row["dec"]), 4), str(row.get("target") or "")[:80]]
                      for row in trails.get(entry["id"], [])],
        })
    rows.sort(key=lambda row: (not row["online"], row["name"].lower()))
    online = [row for row in rows if row["online"]]
    return JSONResponse(
        {"telescopes": rows, "online": len(online),
         # The plugin says "imaging"; Starfront says "exposing" between the
         # frames of a run. Both are a camera open on the sky.
         "imaging": sum(1 for row in online if row["state"] in ("imaging", "exposing")),
         "serverTime": round(now), "trailHours": hours,
         "onlineSeconds": ONLINE_SECONDS},
        headers={**_SKY_HEADERS, "Cache-Control": "public, max-age=30"})


@app.get("/sky")
def sky_page():
    """The chart on its own, for a look or for an iframe."""
    return sky_file("index.html")


@app.get("/sky/{name}")
def sky_file(name: str):
    """The widget's own files: the script, the stars, the page."""
    media = _SKY_FILES.get(name)
    path = SKY_DIR / name
    if media is None or not path.is_file():
        raise HTTPException(status_code=404, detail="no such file")
    return FileResponse(path, media_type=media, headers={
        **_SKY_HEADERS,
        # The stars never change. The script and the page are embedded by
        # their bare names, with nothing to bust a cache, so they are
        # revalidated on every load: the browser sends what it has and the
        # server answers "unchanged" in a few bytes, or hands over the new
        # one the moment it is deployed. An hour's cache meant the person
        # who had just deployed a new sky saw the old one.
        "Cache-Control": ("public, max-age=86400" if name == "stars.json"
                          else "no-cache"),
    })


# ---------------------------------------------------------------------------
# The files: contributors' lights, collected for the coordinator to stack
# ---------------------------------------------------------------------------
#
# Beside the database unless STARFRONT_FILES says otherwise - a mounted
# volume, once a season's subs outgrow the VPS's own disk. The routes, the
# page and the rules are in server/files.py.

FILES_DIR = Path(auth.setting("FILES") or DATA / "files")
files = Files(app, store, FILES_DIR, _person, owns_project, DISCORD.owners,
              DISCORD.public_url, auth.new_login_code)


# ---------------------------------------------------------------------------
# Everything else
# ---------------------------------------------------------------------------

@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    return {"ok": True, "protocol": collab.PROTOCOL, "time": time.time(),
            # What the files folder holds and how much room is left, so a
            # disk filling up is seen from outside before it is full.
            "files": files.stats(),
            # Which build is running, so an update can be checked from outside.
            "version": astrocontrol.__version__,
            "adminConfigured": bool(ADMIN_TOKEN),
            "discord": DISCORD.configured(),
            "roleRequired": False,
            # The N.I.N.A. plugin releases this server offers, so a deploy
            # can be checked from outside.
            "ninaPlugins": [f'{m.get("Name")} {".".join(str(m["Version"][k]) for k in ("Major", "Minor", "Patch", "Build"))}'
                            for m in _nina_manifests() if isinstance(m.get("Version"), dict)],
            # The Starfront installer on offer at /download, for the same reason.
            "download": (_latest_setup() or {}).get("version")}


@app.exception_handler(Exception)
async def _unhandled(_request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500,
                        content={"detail": f"{type(exc).__name__}: {exc}"})
