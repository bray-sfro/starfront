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
* **Mosaics are one project.** A mosaic is one target with parts under it:
  each panel is its own project (`parent` = the target's id), so frames,
  calibration, quota and the stack button work on a panel unchanged, and
  the target holds them together. A panel comes from the rig saying so
  (`panel`), or from the name N.I.N.A. gives it ("M31 Panel 3"). Panels are
  the first kind of part; `PART_KINDS` is where another kind is added.
* **Other names for the same thing.** When the name is new but the rig says
  where it is pointing, a target within `NEAR_DEG` of an existing one is
  that target, and the new name is kept as one of its aliases.
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

import math
import os
import re
import secrets
import threading
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

#: What a personal project is called in the `scope` column.
SCOPE = "personal"

_KEY = re.compile(r"[^a-z0-9]+")

#: What a target can be split into, and the words a name uses for it.
#: Each kind is a project under the target (see `Personal.part`). Add a kind
#: here, and a word for it, to split targets some other way.
PART_KINDS: dict[str, tuple[str, ...]] = {
    "panel": ("panel", "pane", "tile", "p"),
}

# "M31 Panel 3", "M31_panel_03", "M31 - Tile B", "M31 P2". The word must be
# set apart from the name, so a name that simply ends in a letter or a number
# ("Sh2-101", "IC 1805") is never read as a part.
_PART_NAME = re.compile(
    r"^(?P<base>.*?\S)[\s_\-]+(?P<word>" + "|".join(
        sorted({w for words in PART_KINDS.values() for w in words}, key=len, reverse=True))
    + r")[\s_\-#]*(?P<key>[0-9]{1,3}|[a-z])$", re.IGNORECASE)

#: How close (degrees) a new name's pointing must be to an existing target to
#: be that target. Tight on purpose: M81 and M82 are 0.6 degrees apart and
#: are two targets; two names framed on the same spot are one.
NEAR_DEG = 0.2
_ID = re.compile(r"^[0-9a-f]{6,32}$")


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def target_key(name: str) -> str:
    """The same target however it is typed: "M 42", "m42" and "M-42" are one."""
    return _KEY.sub("", (name or "").lower())[:60]


def part_key(key: str) -> str:
    """A part's key however it is written: "03", "3" and "3 " are one."""
    key = (key or "").strip().lower()
    return str(int(key)) if key.isdigit() else key[:16]


def split_target(name: str) -> tuple[str, tuple[str, str] | None]:
    """The target a name is of, and the part of it, if the name says one:
    "M31 Panel 3" is M31, panel 3; "M31" is M31 and no part."""
    name = (name or "").strip()
    match = _PART_NAME.match(name)
    word = match["word"].lower() if match else ""
    # A bare "P" takes a number only: "M31 P2" is a panel, "Tadpoles Pa" is not.
    if not match or (len(word) == 1 and not match["key"].isdigit()):
        return name, None
    kind = next(k for k, words in PART_KINDS.items() if word in words)
    return match["base"].strip(" _-"), (kind, part_key(match["key"]))


def degrees_apart(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float:
    """Angle between two {ra, dec} in degrees; infinite if either is unknown."""
    if not a or not b or a.get("ra") is None or b.get("ra") is None:
        return math.inf
    ra1, dec1, ra2, dec2 = map(math.radians, (a["ra"], a["dec"], b["ra"], b["dec"]))
    cos = math.sin(dec1) * math.sin(dec2) + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))


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
    #: The mosaic panel this frame is of, when the rig knows it. Wins over a
    #: panel read from the name.
    panel: str | None = Field(default=None, min_length=1, max_length=16, pattern=r"^[0-9A-Za-z][0-9A-Za-z._\-]*$")


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
        # Finding-or-making is one step: two rigs starting the same target in
        # the same second must get the same project, not one each.
        self._making = threading.Lock()
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
                     ra: float | None = None, dec: float | None = None,
                     panel: str | None = None
                     ) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
        """The project a frame of `target` goes into, whether it was made
        now, and the target it is a part of (None if it is the target).

        The target is found by name, then by where the rig is pointing;
        made if neither. A panel (said by the rig, or read from the name)
        is then found or made under it."""
        with self._making:
            return self._find_or_make(user, target, ra, dec, panel)

    def _find_or_make(self, user: dict[str, Any], target: str, ra: float | None,
                      dec: float | None, panel: str | None
                      ) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
        base, part = split_target(target)
        if panel:
            part = ("panel", part_key(panel))
        key = target_key(base)
        if not key:
            raise HTTPException(status_code=400, detail="a target name with letters or digits is expected")
        # Only a frame of the target as a whole says where the target is; a
        # panel's pointing is off to one side of it.
        where = {"ra": ra, "dec": dec} if ra is not None and dec is not None else None
        owner = str(user["id"])
        tops = [p for p in self.store.personal_projects_of(owner) if not p.get("parent")]
        found = self._by_name(tops, key) or (None if part else self._by_place(tops, where))
        made = False
        if found is None:
            payload: dict[str, Any] = {
                "target": base[:80], "targetKey": key, "aliases": [],
                # The same switches a collaboration has, always on: the
                # person's own rigs send, and the server keeps what they send.
                "collectFiles": True, "autoUpload": True,
                # Whether new frames start a stack by themselves (when
                # processing is automatic). Off until the person turns it on.
                "autoProcess": False,
            }
            if where and not part:
                payload["region"] = dict(where, width=0, height=0)
            found = self.store.add_project(secrets.token_hex(6), base[:80], str(user.get("name") or ""),
                                           payload, owner_id=owner, scope=SCOPE)
            made = True
        else:
            payload = dict(found.get("payload") or {})
            changed = False
            if payload.get("targetKey") != key and key not in payload.get("aliases", []):
                payload["aliases"] = (payload.get("aliases") or []) + [key]     # found by place
                changed = True
            if where and not part and not payload.get("region"):
                payload["region"] = dict(where, width=0, height=0)
                changed = True
            if changed:
                found = self.store.set_project(found["id"], payload) or found
        if part is None:
            return found, made, None
        child, child_made = self.part(found, part[0], part[1], user, where)
        return child, made or child_made, found

    @staticmethod
    def _by_name(tops: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
        for project in tops:
            payload = project.get("payload") or {}
            if payload.get("targetKey") == key or key in (payload.get("aliases") or []):
                return project
        return None

    @staticmethod
    def _by_place(tops: list[dict[str, Any]], where: dict[str, Any] | None) -> dict[str, Any] | None:
        if where is None:
            return None
        best, nearest = None, NEAR_DEG
        for project in tops:
            apart = degrees_apart((project.get("payload") or {}).get("region"), where)
            if apart <= nearest:
                best, nearest = project, apart
        return best

    def part(self, target: dict[str, Any], kind: str, key: str, user: dict[str, Any],
             where: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
        """The `kind` part `key` of a target (panel 3 of M31), made on first use.

        A part is a personal project like any other, with `parent` set, so
        everything that works on a project works on it. It starts with the
        target's switches."""
        if kind not in PART_KINDS:
            raise HTTPException(status_code=400, detail=f"unknown part kind {kind!r}")
        for child in self.store.parts_of(target["id"]):
            got = (child.get("payload") or {}).get("part") or {}
            if got.get("kind") == kind and got.get("key") == key:
                return child, False
        parent = target.get("payload") or {}
        label = f"{kind.capitalize()} {key.upper() if key.isalpha() else key}"
        payload: dict[str, Any] = {
            "target": parent.get("target") or target["name"], "targetKey": parent.get("targetKey", ""),
            "part": {"kind": kind, "key": key, "label": label},
            "collectFiles": True, "autoUpload": True,
            "autoProcess": bool(parent.get("autoProcess")),
        }
        if where:
            payload["region"] = dict(where, width=0, height=0)
        child = self.store.add_project(secrets.token_hex(6), f"{target['name']} · {label}"[:80],
                                       str(user.get("name") or ""), payload,
                                       owner_id=str(user["id"]), scope=SCOPE, parent=target["id"])
        return child, True

    def rename(self, project: dict[str, Any], name: str) -> None:
        """Call a target, or one of its parts, something else.

        A target keeps answering to its old name, so a rig still saving
        under it keeps filling the same project, and answers to the new one
        too unless another target already does. Its parts follow it. A part
        takes the name as its label ("Panel 3" -> "North-east")."""
        payload = dict(project.get("payload") or {})
        if project.get("parent"):
            parent = self.store.project(project["parent"]) or {"name": payload.get("target", "")}
            payload["part"] = dict(payload.get("part") or {}, label=name)
            self.store.set_project(project["id"], payload)
            self.store.rename_project(project["id"], f"{parent['name']} · {name}"[:80])
            return
        key = target_key(name)
        if not key:
            raise HTTPException(status_code=400, detail="a name with letters or digits is expected")
        others = [p for p in self.store.personal_projects_of(str(project.get("owner_id") or ""))
                  if not p.get("parent") and p["id"] != project["id"]]
        if key != payload.get("targetKey") and key not in (payload.get("aliases") or []) \
                and self._by_name(others, key) is None:
            payload["aliases"] = (payload.get("aliases") or []) + [key]
        payload["target"] = name
        self.store.set_project(project["id"], payload)
        self.store.rename_project(project["id"], name)
        for child in self.store.parts_of(project["id"]):
            inner = dict(child.get("payload") or {})
            inner["target"] = name
            label = (inner.get("part") or {}).get("label") or child["name"]
            self.store.set_project(child["id"], inner)
            self.store.rename_project(child["id"], f"{name} · {label}"[:80])

    @staticmethod
    def _part_order(project: dict[str, Any]) -> tuple[int, str]:
        key = ((project.get("payload") or {}).get("part") or {}).get("key", "")
        return (int(key), "") if key.isdigit() else (10 ** 6, key)

    def view(self, project: dict[str, Any], who: dict[str, Any]) -> dict[str, Any]:
        """One personal project as the My imaging page shows it. A target
        with parts (a mosaic) lists them under `parts`, and its counts are
        the whole target's: its own frames and every part's."""
        out = self._view_one(project, who)
        # What sits on the target itself, before its parts are added in: on
        # a mosaic, frames of the whole field shot under the plain name.
        out["own"] = {"files": out["files"], "bytes": out["bytes"], "lights": dict(out["lights"])}
        parts = sorted(self.store.parts_of(project["id"]), key=self._part_order)
        out["parts"] = [self._view_one(child, who) for child in parts]
        out["kind"] = "mosaic" if out["parts"] else "single"
        for child in out["parts"]:
            out["files"] += child["files"]
            out["bytes"] += child["bytes"]
            for status, n in (child["lights"] or {}).items():
                out["lights"][status] = out["lights"].get(status, 0) + n
        return out

    def _view_one(self, project: dict[str, Any], who: dict[str, Any]) -> dict[str, Any]:
        payload = project.get("payload") or {}
        totals = self.store.file_totals(project["id"])
        out = {
            "id": project["id"], "name": project["name"],
            "target": payload.get("target") or project["name"],
            "created": project.get("created"), "closed": project.get("status") != "open",
            "autoProcess": bool(payload.get("autoProcess")),
            "region": payload.get("region"),
            "parent": project.get("parent") or "",
            "part": payload.get("part"),
            "aliases": payload.get("aliases") or [],
            "files": int(totals["count"]), "bytes": int(totals["bytes"]),
            "kinds": self.store.file_kinds(project["id"]),
            "lights": dict(self.store.file_status_counts(project["id"]) or {}),
            # What the person's rigs reported: sessions, hours, telescopes, filters.
            "reports": self.files._telescopes(project["id"], who["id"]),
        }
        if self.releases is not None:
            out["release"] = self.releases.summary(project["id"])
        if self.jobs is not None:
            out["stack"] = self.jobs.stack_view(project["id"])
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
            project, made, target = self.find_or_make(user, body.target, body.ra, body.dec, body.panel)
            return {"project": project["id"], "name": project["name"], "created": made,
                    "target": (target or project)["id"],
                    "part": (project.get("payload") or {}).get("part")}

        @app.get("/api/v1/me/personal")
        def my_projects(request: Request) -> dict[str, Any]:
            """The My imaging page: what this person may do, and their projects."""
            who = self._mine(request)
            allowed = self.entitlement(who)
            projects = [self.view(p, who) for p in self.store.personal_projects_of(str(who["id"]))
                        if not p.get("parent")]
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
                with self._making:
                    self.rename(self._own(project_id, who), body.name.strip())
                project = self._own(project_id, who)
            if body.autoProcess is not None:
                payload = dict(project.get("payload") or {})
                payload["autoProcess"] = bool(body.autoProcess)
                self.store.set_project(project_id, payload)
                # A target's switch is its parts' switch too.
                for child in self.store.parts_of(project_id):
                    inner = dict(child.get("payload") or {})
                    inner["autoProcess"] = bool(body.autoProcess)
                    self.store.set_project(child["id"], inner)
            return self.view(self._own(project_id, who), who)
