"""Personal projects: one person's own imaging of one target.

A collaboration is many people's frames of one patch of sky. A personal
project is the same thing with one person in it: their own telescopes send
the lights they shoot of a target, and the masters that calibrate them,
and the server keeps and processes them for that person alone. It is not
listed, not dealt out, and nobody else can see it is there.

It is deliberately *the same* object as a collaboration - a row in
`projects` with `scope = 'personal'` - so the upload path
(`server/agentuploads.py`), the calibration rules (`server/uploadrules.py`),
the stack button (`server/jobs.py`) and the releases (`server/releases.py`)
all work on it unchanged. What this module adds:

* **One project per person per target**, found or made on demand. A rig
  asks `POST /api/v1/agent/personal` with the target's name (and where it
  is, if it knows) and gets the project id to upload into; the same name
  from any of that person's rigs, on any night, is the same project.
* **Who may.** `entitlement()` is the single place that answers "may this
  person keep personal projects, and how much room do they get". Today
  that is a switch and a beta list; when payments arrive it reads the plan,
  and nothing else in the server has to change.
* **Privacy.** `hidden_from()` is the rule every by-id route applies: a
  personal project is visible to its owner and the server's owners only,
  and is a plain 404 to anybody else.

Off unless `STARFRONT_PERSONAL_UPLOADS=1`. With it off every route here
answers that personal projects are not open yet, and nothing changes for
anybody.

| | |
|---|---|
| `STARFRONT_PERSONAL_UPLOADS` | `1` to open personal projects at all (default off) |
| `STARFRONT_PERSONAL_BETA` | Discord ids, comma-separated, allowed in before plans exist |
| `STARFRONT_PERSONAL_STORAGE_GB` | room per person across all their personal projects (default 200) |
"""

from __future__ import annotations

import os
import re
import secrets
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

#: What a personal project is called in the `scope` column.
SCOPE = "personal"

_KEY = re.compile(r"[^a-z0-9]+")
_ID = re.compile(r"^[0-9a-f]{6,32}$")


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def target_key(name: str) -> str:
    """The same target however it is typed: "M 42", "m42" and "M-42" are one."""
    return _KEY.sub("", (name or "").lower())[:60]


def is_personal(project: dict[str, Any] | None) -> bool:
    return bool(project) and project.get("scope") == SCOPE


def hidden_from(project: dict[str, Any] | None, who: dict[str, Any] | None,
                owns_project: Callable[[dict[str, Any], dict[str, Any]], bool]) -> bool:
    """Whether this project must look like it does not exist to this caller.

    Collaborations are never hidden here (their own rules decide what a
    caller sees of them). A personal project is hidden from everybody but
    its owner and the server's owners."""
    if not is_personal(project):
        return False
    if who is None:
        return True
    return not owns_project(who, project)


class PersonalTarget(BaseModel):
    target: str = Field(min_length=1, max_length=80)
    ra: float | None = Field(default=None, ge=0, lt=360)
    dec: float | None = Field(default=None, ge=-90, le=90)


class PersonalUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    autoProcess: bool | None = None


class Personal:
    def __init__(self, app: FastAPI, store: Any, files: Any,
                 agent: Callable[[Request], tuple[dict[str, Any], dict[str, Any]]],
                 who: Callable[[Request], dict[str, Any] | None],
                 owners: list[str] | None = None, *,
                 enabled: bool | None = None, beta: list[str] | None = None,
                 storage_gb: float | None = None,
                 releases: Any = None, jobs: Any = None) -> None:
        self.store = store
        self.files = files
        self.agent = agent
        self.who = who
        self.owners = set(owners or [])
        self.enabled = _flag("STARFRONT_PERSONAL_UPLOADS") if enabled is None else enabled
        listed = os.environ.get("STARFRONT_PERSONAL_BETA", "") if beta is None else ",".join(beta)
        self.beta = {part.strip() for part in listed.split(",") if part.strip()}
        if storage_gb is None:
            try:
                storage_gb = float(os.environ.get("STARFRONT_PERSONAL_STORAGE_GB", "200"))
            except ValueError:
                storage_gb = 200.0
        self.storage = int(max(0.0, storage_gb) * 1024 ** 3)
        # Set after construction where they are built later (see app.py).
        self.releases = releases
        self.jobs = jobs
        self._routes(app)

    # -- who may ------------------------------------------------------------
    def entitlement(self, user: dict[str, Any] | None) -> dict[str, Any]:
        """What this person may do with personal projects.

        The one place payments plug in: today a server-wide switch and a
        beta list; later, the person's plan (see the payments notes in
        server/README.md). Everything else asks this and nothing else.
        """
        if not self.enabled:
            return {"personal": False, "plan": "", "storageBytes": 0,
                    "why": "personal projects are not open yet"}
        if user is None:
            return {"personal": False, "plan": "", "storageBytes": 0,
                    "why": "sign in with Discord first"}
        uid = str(user.get("id") or "")
        if uid in self.owners or uid in self.beta:
            return {"personal": True, "plan": "beta", "storageBytes": self.storage, "why": ""}
        # Payments: look up this person's plan here and return its room.
        return {"personal": False, "plan": "", "storageBytes": 0,
                "why": "personal projects need a Starfront plan"}

    def used(self, owner_id: str, *, besides: str = "") -> int:
        """Bytes this person keeps across their personal projects."""
        return sum(int(self.store.file_totals(p["id"])["bytes"])
                   for p in self.store.personal_projects_of(owner_id) if p["id"] != besides)

    def quota_for(self, project: dict[str, Any], user: dict[str, Any]) -> int:
        """The most this one personal project may hold: the person's room,
        less what their other personal projects already take. Their own
        room, not the project's, because storage is what a plan pays for."""
        room = int(self.entitlement(user)["storageBytes"])
        return max(0, room - self.used(str(user["id"]), besides=project["id"]))

    def check_upload(self, project: dict[str, Any], rig: dict[str, Any],
                     user: dict[str, Any]) -> None:
        """Whether this rig may send into this personal project, as the
        upload path's 409/403/402. No task is needed: it is the person's own."""
        if project.get("status") != "open":
            raise HTTPException(status_code=409, detail="that personal project is closed")
        if str(project.get("owner_id") or "") != str(user["id"]):
            raise HTTPException(status_code=404, detail="no such collaboration")
        allowed = self.entitlement(user)
        if not allowed["personal"]:
            # 402 is the one a client shows as "this needs a plan" and stops
            # retrying; 403 while the feature is simply not open.
            raise HTTPException(status_code=402 if self.enabled else 403,
                                detail=allowed["why"])

    # -- the projects -------------------------------------------------------
    def find_or_make(self, user: dict[str, Any], target: str,
                     ra: float | None = None, dec: float | None = None) -> tuple[dict[str, Any], bool]:
        key = target_key(target)
        if not key:
            raise HTTPException(status_code=400, detail="a target name with letters or digits is expected")
        for project in self.store.personal_projects_of(str(user["id"])):
            if (project.get("payload") or {}).get("targetKey") == key:
                if ra is not None and dec is not None and not (project["payload"] or {}).get("region"):
                    payload = dict(project["payload"])
                    payload["region"] = {"ra": ra, "dec": dec, "width": 0, "height": 0}
                    project = self.store.set_project(project["id"], payload) or project
                return project, False
        payload: dict[str, Any] = {
            "target": target.strip()[:80], "targetKey": key,
            # The same switches a collaboration has, always on: the person's
            # own rigs send, and the server keeps what they send.
            "collectFiles": True, "autoUpload": True,
            # Whether new frames start a stack by themselves (when processing
            # is automatic). Off until the person turns it on.
            "autoProcess": False,
        }
        if ra is not None and dec is not None:
            payload["region"] = {"ra": ra, "dec": dec, "width": 0, "height": 0}
        project = self.store.add_project(secrets.token_hex(6), target.strip()[:80],
                                         str(user.get("name") or ""), payload,
                                         owner_id=str(user["id"]), scope=SCOPE)
        return project, True

    def view(self, project: dict[str, Any], who: dict[str, Any]) -> dict[str, Any]:
        """One personal project as the My imaging page shows it."""
        payload = project.get("payload") or {}
        totals = self.store.file_totals(project["id"])
        out = {
            "id": project["id"], "name": project["name"],
            "target": payload.get("target") or project["name"],
            "created": project.get("created"), "closed": project.get("status") != "open",
            "autoProcess": bool(payload.get("autoProcess")),
            "region": payload.get("region"),
            "files": totals["count"], "bytes": totals["bytes"],
            "kinds": self.store.file_kinds(project["id"]),
            "lights": self.store.file_status_counts(project["id"]),
            "telescopes": self.files._telescopes(project["id"], who["id"]),
        }
        if self.releases is not None:
            out["release"] = self.releases.summary(project["id"])
        if self.jobs is not None:
            out["stack"] = self.jobs.stack_view(project["id"])
            out["job"] = self.jobs.read(project["id"])
        return out

    def _mine(self, request: Request) -> dict[str, Any]:
        try:
            who = self.who(request)
        except HTTPException:
            who = None
        if who is None:
            raise HTTPException(status_code=401, detail="sign in with Discord first")
        return who

    def _own(self, project_id: str, who: dict[str, Any]) -> dict[str, Any]:
        project = self.store.project(project_id) if _ID.match(project_id or "") else None
        if project is None or not is_personal(project) or \
                str(project.get("owner_id") or "") != str(who["id"]):
            raise HTTPException(status_code=404, detail="no such personal project")
        return project

    # -- the routes -----------------------------------------------------------
    def _routes(self, app: FastAPI) -> None:

        @app.get("/api/v1/agent/personal")
        def agent_status(request: Request) -> dict[str, Any]:
            """Whether this telescope's owner may upload personal projects,
            and how much room they have. The plugin asks before offering it."""
            _rig, user = self.agent(request)
            allowed = self.entitlement(user)
            used = self.used(str(user["id"])) if allowed["personal"] else 0
            return {"available": allowed["personal"], "why": allowed["why"],
                    "plan": allowed["plan"], "storageBytes": allowed["storageBytes"],
                    "usedBytes": used}

        @app.post("/api/v1/agent/personal")
        def agent_target(body: PersonalTarget, request: Request) -> dict[str, Any]:
            """The personal project for this target, made on first use. The
            rig then sends into it through /api/v1/agent/uploads like any
            collaboration, with this id as the project."""
            _rig, user = self.agent(request)
            allowed = self.entitlement(user)
            if not allowed["personal"]:
                raise HTTPException(status_code=402 if self.enabled else 403, detail=allowed["why"])
            project, made = self.find_or_make(user, body.target, body.ra, body.dec)
            return {"project": project["id"], "name": project["name"], "created": made}

        @app.get("/api/v1/me/personal")
        def my_projects(request: Request) -> dict[str, Any]:
            """The My imaging page: what this person may do, and their projects."""
            who = self._mine(request)
            allowed = self.entitlement(who)
            projects = [self.view(p, who) for p in self.store.personal_projects_of(str(who["id"]))]
            return {"available": allowed["personal"], "why": allowed["why"],
                    "plan": allowed["plan"], "storageBytes": allowed["storageBytes"],
                    "usedBytes": sum(int(p["bytes"]) for p in projects),
                    "projects": projects}

        @app.get("/api/v1/me/personal/{project_id}")
        def my_project(project_id: str, request: Request) -> dict[str, Any]:
            who = self._mine(request)
            return self.view(self._own(project_id, who), who)

        @app.put("/api/v1/me/personal/{project_id}")
        def update_project(project_id: str, body: PersonalUpdate, request: Request) -> dict[str, Any]:
            """Rename it, or turn automatic processing on or off."""
            who = self._mine(request)
            project = self._own(project_id, who)
            if body.name is not None:
                self.store.rename_project(project_id, body.name.strip())
            if body.autoProcess is not None:
                payload = dict(project.get("payload") or {})
                payload["autoProcess"] = bool(body.autoProcess)
                self.store.set_project(project_id, payload)
            return self.view(self._own(project_id, who), who)
