"""Where the subs go: the files side of a collaboration.

The ledger says what was shot; this is the data itself. A collaboration that
*collects files* takes every contributor's lights onto this server, one
folder per collaboration, one per person inside it, so the coordinator has
them in one place to stack.

Two ways in, one door. A browser: the files page at `/files`, where
somebody signs in with Discord, picks the collaboration, and drops the
night's folder on it. A program: the same `PUT`, on a telescope's token or a
person's, which is how the N.I.N.A. plugin and Starfront will send each
light the moment it is saved. The page exists so that people can start
tonight; the programs come after, and nothing here changes when they do.

A browser sign-in is its own credential - a *web token* in a cookie - so
that signing in here does not sign anybody's N.I.N.A. or Starfront out, as a
second program sign-in would.

What is kept is only ever written under the files folder, which lives beside
the database (`/var/lib/starfront/files` on the VPS, or wherever
`STARFRONT_FILES` points - a mounted volume, in time). Nothing is deleted by
the server on its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Request
from starlette.requests import ClientDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from server import uploadrules

#: What a light is. Compressed FITS (`.fits.fz`) ends in `.fz`; XISF is
#: PixInsight's. Nothing else: a stray `.txt` or a `.jpg` of the result is
#: not data for a stack.
ALLOWED = (".fits", ".fit", ".fts", ".fz", ".xisf")
#: The largest single file taken. A 61-megapixel sub is 120 MB; a 16-bit
#: full-frame at 2x2 binning is a quarter of that. Four gigabytes is nothing
#: anybody shoots in one exposure and keeps a runaway upload bounded.
MAX_BYTES = 4 * 1024 ** 3
#: Room left on the disk, always. The database lives on the same disk.
KEEP_FREE = 2 * 1024 ** 3
#: The most one collaboration may hold, unless STARFRONT_PROJECT_QUOTA_GB
#: says otherwise. A project already past it keeps everything it has and can
#: still be read and downloaded; it just takes no more.
PROJECT_QUOTA = int(float(os.environ.get("STARFRONT_PROJECT_QUOTA_GB", "40")) * 1024 ** 3)
#: Below this much free space (or this fraction of the disk) the server
#: warns: in the journal, in /api/v1/health and on the files page to owners.
LOW_FREE = 10 * 1024 ** 3
LOW_FRACTION = 0.10
#: An upload is written in pieces this big, off the event loop, so a
#: stream of files never holds up a telescope's or a coordinator's call.
WRITE_BUFFER = 1024 ** 2
_log = logging.getLogger("starfront.files")


async def _drain(request: Request, length: int) -> bool:
    """Read and drop a request body (up to its Content-Length). False if the
    client went away first."""
    got = 0
    try:
        async for chunk in request.stream():
            got += len(chunk)
            if got >= length:
                break
    except ClientDisconnect:
        return False
    return True

#: The cookie a browser sign-in lives in, and how long it lasts unused.
COOKIE = "starfront_web"
COOKIE_SECONDS = 90 * 86400

#: What a file is: a light, or one of the calibration frames a stack needs.
#: Each kind is its own folder under the person's, so a coordinator's
#: calibration is a matter of pointing the stacker at four folders.
KINDS = ("light", "flat", "dark", "bias", "darkflat")
KIND_LABELS = {"light": "Lights", "flat": "Flats", "dark": "Darks",
               "bias": "Bias", "darkflat": "Dark flats"}
#: What the files page offers for new uploads. Dark flats are no longer
#: collected (Bray, 2026-11-03): the rules never need them. "darkflat" stays
#: a kind so the ones already here still list and download, and so an older
#: client that sends it is not refused - its file is kept, apart, as a
#: flat-calibration aid that is never matched to a light.
UPLOAD_KINDS = ("light", "flat", "dark", "bias")
#: The folder each kind is kept in, under the person's.
KIND_FOLDERS = {"light": "lights", "flat": "flats", "dark": "darks",
                "bias": "bias", "darkflat": "darkflats"}

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_KIND_WORDS = (
    # N.I.N.A.'s $$IMAGETYPE$$ and the usual spellings, checked longest first
    # so "DARKFLAT" is not read as "DARK".
    (re.compile(r"dark[\s_\-]?flat|flat[\s_\-]?dark", re.I), "darkflat"),
    (re.compile(r"\bflats?\b|[_\-]flats?[_\-.]", re.I), "flat"),
    (re.compile(r"\bdarks?\b|[_\-]darks?[_\-.]", re.I), "dark"),
    (re.compile(r"\bbias\b|[_\-]bias[_\-.]|\boffset\b", re.I), "bias"),
)


def guess_kind(name: str) -> str:
    """What a file is, read from its name: N.I.N.A. writes the image type
    into the name by default. Anything unrecognised is a light."""
    for pattern, kind in _KIND_WORDS:
        if pattern.search(name or ""):
            return kind
    return "light"


def clean_kind(kind: str, name: str) -> str:
    """The kind asked for, or the one the name says when none was."""
    kind = (kind or "").strip().lower()
    if kind in KINDS:
        return kind
    if kind in ("", "auto"):
        return guess_kind(name)
    raise HTTPException(status_code=400,
                        detail="kind must be one of " + ", ".join(KINDS))


def clean_name(name: str) -> str:
    """A file name as it will be kept, or '' when it cannot be.

    The base name only, whatever path came with it; the characters no file
    system takes removed; and one of the extensions a light has.
    """
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _BAD_CHARS.sub("", base).strip(". ")
    if not base or len(base) > 200:
        return ""
    if not base.lower().endswith(ALLOWED):
        return ""
    return base


def looks_like_data(head: bytes, name: str) -> bool:
    """Whether the first bytes are what the extension says they are."""
    lower = name.lower()
    if lower.endswith(".xisf"):
        return head.startswith(b"XISF0100")
    if lower.endswith(".fz"):
        # A tile-compressed FITS is still a FITS: its primary header is plain.
        return head.startswith(b"SIMPLE")
    return head.startswith(b"SIMPLE")


class Files:
    """The routes, bound to the server's store and its idea of who is asking."""

    def __init__(self, app: FastAPI, store: Any, folder: Path,
                 person: Callable[[str], dict[str, Any] | None],
                 owns_project: Callable[[dict[str, Any], dict[str, Any]], bool],
                 owners: list[str], public_url: str,
                 new_login_code: Callable[[], str]) -> None:
        self.app = app
        self.store = store
        self.folder = Path(folder)
        self.person = person
        self.owns_project = owns_project
        self.owners = list(owners)
        self.public_url = public_url.rstrip("/")
        self.new_login_code = new_login_code
        self.folder.mkdir(parents=True, exist_ok=True)
        self._routes()

    # -- who is asking ---------------------------------------------------
    def who(self, request: Request) -> dict[str, Any] | None:
        """The person behind a request: a bearer token (a person's, or a
        telescope's, which stands for its owner), else the browser cookie."""
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if token:
            found = self.person(token)
            if found is not None:
                return found
            agent = self.store.agent_by_token(token)
            if agent is not None:
                owner = str(agent.get("owner_id") or "")
                user = self.store.user(owner) if owner else None
                if user is None:
                    raise HTTPException(status_code=403,
                                        detail="this telescope has no owner to file "
                                               "its lights under; sign in with Discord first")
                return {"kind": "agent", "id": user["id"], "name": user["name"],
                        "roles": [], "admin": user["id"] in self.owners,
                        "telescope": str(agent.get("name") or "")}
            return None
        cookie = request.cookies.get(COOKIE, "")
        user = self.store.user_by_web_token(cookie) if cookie else None
        if user is None:
            return None
        return {"kind": "web", "id": user["id"], "name": user["name"],
                "roles": list(user.get("roles") or []),
                "admin": user["id"] in self.owners}

    def require(self, request: Request) -> dict[str, Any]:
        who = self.who(request)
        if who is None:
            raise HTTPException(status_code=401, detail="not signed in")
        return who

    # -- the shape of things ----------------------------------------------
    def path_of(self, project_id: str, user_id: str, name: str, kind: str = "light") -> Path:
        return self.folder / project_id / user_id / KIND_FOLDERS[kind] / name

    def quota_of(self, project: dict[str, Any]) -> int:
        """The most this collaboration may hold: its own `fileQuotaGB` if
        the coordinator set one (0 = takes no files at all), else the
        server-wide default."""
        own = (project.get("payload") or {}).get("fileQuotaGB")
        try:
            return PROJECT_QUOTA if own is None else max(0, int(float(own) * 1024 ** 3))
        except (TypeError, ValueError):
            return PROJECT_QUOTA

    def collecting(self, project: dict[str, Any]) -> bool:
        return bool((project.get("payload") or {}).get("collectFiles"))

    def stats(self) -> dict[str, Any]:
        totals = self.store.file_totals()
        try:
            usage = shutil.disk_usage(self.folder)
            free, size = int(usage.free), int(usage.total)
        except OSError:
            free, size = 0, 0
        low = free < LOW_FREE or (size > 0 and free < size * LOW_FRACTION)
        if low:
            now = time.time()
            if now - getattr(self, "_warned", 0.0) > 3600:
                self._warned = now
                _log.warning("files disk is low: %.1f GB free of %.1f GB",
                             free / 1024 ** 3, size / 1024 ** 3)
        return {"count": totals["count"], "bytes": totals["bytes"], "free": free,
                "size": size, "low": low, "projectQuota": PROJECT_QUOTA}

    def _row(self, row: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
        user = str(row.get("user") or "")
        if user not in names:
            found = self.store.user(user)
            names[user] = str((found or {}).get("name") or user)
        status = str(row.get("status") or "")
        return {"name": row["name"], "kind": row.get("kind") or "light",
                "bytes": int(row.get("bytes") or 0),
                "received": float(row.get("received") or 0.0),
                "telescope": row.get("telescope") or "",
                "user": user, "uploader": names[user],
                "status": status,
                "statusLabel": uploadrules.STATUS_LABELS.get(status, status),
                "reasons": _json(row.get("reasons"), []),
                **_select_facts(row)}

    # -- the upload rules ---------------------------------------------------
    def recheck(self, project: dict[str, Any], user_id: str,
                only_light: str | None = None) -> dict[str, int]:
        """Judge every file one contributor has on a collaboration again, and
        store the statuses that changed. Header facts come from the database,
        so this never touches the files themselves. A new light changes no
        other file's status, so `only_light` judges just it (against the
        calibration) and keeps an upload's cost flat however many lights
        the contributor already has."""
        rules = uploadrules.rules_of(project)
        rows = self.store.files_in(project["id"], user_id)
        if only_light is not None:
            rows = [row for row in rows if row["kind"] != "light" or row["id"] == only_light]
        for row in rows:
            row["meta"] = _json(row.get("meta"), {})
            row["label"] = row.get("telescope") or ""
        verdicts = uploadrules.judge_all(rows, rules)
        changes = []
        moved = {"ready": 0, "pending": 0, "quarantine": 0}
        for row in rows:
            if row["id"] not in verdicts:
                continue
            status, reasons = verdicts[row["id"]]
            if status != (row.get("status") or "") or reasons != _json(row.get("reasons"), []):
                changes.append((row["id"], status, reasons))
                # What moved means to a person: lights that changed state.
                if status != (row.get("status") or "") and row["kind"] == "light":
                    moved[status] = moved.get(status, 0) + 1
        self.store.set_file_statuses(changes)
        return moved

    def gaps(self, project_id: str, user_id: str) -> list[dict[str, Any]]:
        """What one contributor's pending and quarantined lights are waiting
        for, each distinct reason once with how many lights it holds back."""
        counts: dict[tuple[str, str], int] = {}
        for row in self.store.files_in(project_id, user_id):
            status = row.get("status") or ""
            if status not in (uploadrules.PENDING, uploadrules.QUARANTINE):
                continue
            for reason in _json(row.get("reasons"), []):
                key = (status, reason)
                counts[key] = counts.get(key, 0) + 1
        return [{"status": status, "reason": reason, "files": n}
                for (status, reason), n in sorted(counts.items(), key=lambda kv: -kv[1])]

    def rules_view(self, project: dict[str, Any]) -> dict[str, Any]:
        rules = uploadrules.rules_of(project)
        return {"checklist": uploadrules.checklist(rules), "values": rules,
                "defaults": uploadrules.DEFAULTS,
                "spec": [{"name": n, "default": d, "type": k,
                          "choices": list(lo) if k == "choice" else None, "about": w}
                         for n, d, k, lo, _hi, w in uploadrules.RULES]}

    def _telescopes(self, project_id: str, user_id: str) -> dict[str, Any]:
        totals = getattr(self.store, "contribution_totals_of", None)
        return totals(project_id, user_id) if totals else {"sessions": 0, "hours": 0, "telescopes": 0, "filters": []}

    def collab_list(self) -> list[dict[str, Any]]:
        out = []
        for project in self.store.projects():
            closed = project.get("status") != "open"
            totals = self.store.file_totals(project["id"])
            collecting = self.collecting(project) and not closed
            if not totals["count"] and not collecting:
                continue
            out.append({"id": project["id"], "name": project["name"],
                        "coordinator": project.get("coordinator") or "",
                        "closed": closed, "collecting": collecting,
                        "files": totals["count"], "bytes": totals["bytes"]})
        # Open first (collecting before paused), then closed; biggest first.
        out.sort(key=lambda x: (x["closed"], not x["collecting"], -x["bytes"], x["name"].lower()))
        return out

    def _project(self, project_id: str) -> dict[str, Any]:
        project = self.store.project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="no such collaboration")
        return project

    # -- the routes --------------------------------------------------------
    def _routes(self) -> None:
        app = self.app

        @app.get("/files")
        def files_page() -> HTMLResponse:
            return HTMLResponse(_PAGE)

        @app.get("/files/signin")
        def files_signin():
            """Begin a browser sign-in that comes back here."""
            # Old, never-finished sign-ins are swept here too, as the
            # program's sign-in does, so the table cannot just grow.
            self.store.sweep_logins(time.time() - 2 * 600)
            code = self.new_login_code()
            self.store.add_login(code, back="/files")
            return RedirectResponse(f"/auth/discord/start?code={code}", status_code=303)

        @app.post("/files/signout")
        def files_signout(request: Request) -> JSONResponse:
            cookie = request.cookies.get(COOKIE, "")
            if cookie:
                self.store.forget_web_token(cookie)
            response = JSONResponse({"signedOut": True})
            response.delete_cookie(COOKIE, path="/")
            return response

        @app.get("/api/v1/files/projects")
        def files_projects(request: Request) -> dict[str, Any]:
            """The collaborations that collect files, with what each holds,
            and who is asking - the page's one call on load."""
            who = self.who(request)
            listed = []
            for project in self.store.projects():
                closed = project.get("status") != "open"
                owned = who is not None and self.owns_project(who, project)
                totals = self.store.file_totals(project["id"])
                mine = (self.store.file_totals_of(project["id"], who["id"])
                        if who is not None else {"count": 0, "bytes": 0})
                if closed:
                    # A closed collaboration takes nothing new, but what it
                    # collected stays downloadable: by its owner, and by each
                    # uploader for their own files.
                    if not totals["count"] or not (owned or mine["count"]):
                        continue
                elif not self.collecting(project) and not owned:
                    continue
                quota = self.quota_of(project)
                listed.append({
                    "id": project["id"], "name": project["name"],
                    "coordinator": project.get("coordinator") or "",
                    "kind": (project.get("payload") or {}).get("kind") or "mosaic",
                    "collecting": self.collecting(project),
                    "autoUpload": bool((project.get("payload") or {}).get("autoUpload")),
                    "notes": (project.get("payload") or {}).get("notes") or "",
                    "files": totals["count"], "bytes": totals["bytes"],
                    "kinds": self.store.file_kinds(project["id"]),
                    "myFiles": mine["count"], "myBytes": mine["bytes"],
                    "myTelescopes": (self._telescopes(project["id"], who["id"])
                                     if who is not None else None),
                    "owned": owned, "quota": quota, "closed": closed,
                    "full": totals["bytes"] >= quota,
                    # Lights by upload-rule status: only "ready" ones count.
                    "lights": self.store.file_status_counts(project["id"]),
                    "myLights": (self.store.file_status_counts(project["id"], who["id"])
                                 if who is not None else {}),
                })
            return {
                "signedIn": who is not None,
                "name": who["name"] if who else "",
                # Every collaboration worth picking, for everybody (signed
                # out too): open ones that take files or hold some, closed
                # ones that hold files. Totals only, never whose files.
                "collabs": self.collab_list(),
                "admin": bool(who and who["admin"]),
                "projects": listed,
                # Every kind, for labelling files already here; only
                # uploadKinds are offered for new uploads.
                "kinds": [{"id": kind, "label": KIND_LABELS[kind]} for kind in KINDS],
                "uploadKinds": list(UPLOAD_KINDS),
                "allowed": list(ALLOWED),
                "maxBytes": MAX_BYTES,
                "free": self.stats()["free"],
                # Only the owners are told the disk itself is filling up.
                "diskLow": bool(who and who["admin"] and self.stats()["low"]),
            }

        @app.get("/api/v1/projects/{project_id}/files")
        def list_files(project_id: str, request: Request) -> dict[str, Any]:
            """What has arrived: everything, to the coordinator; your own,
            to you."""
            who = self.require(request)
            project = self._project(project_id)
            owned = self.owns_project(who, project)
            rows = self.store.files_in(project_id, None if owned else who["id"])
            names: dict[str, str] = {}
            listed = [self._row(row, names) for row in rows]
            return {"project": project_id, "name": project["name"],
                    "collecting": self.collecting(project), "owned": owned,
                    "me": who["id"], "files": listed,
                    "bytes": sum(row["bytes"] for row in listed),
                    "free": self.stats()["free"],
                    "rules": self.rules_view(project),
                    "myLights": self.store.file_status_counts(project_id, who["id"]),
                    "lights": self.store.file_status_counts(project_id) if owned else {},
                    "gaps": self.gaps(project_id, who["id"]),
                    # Frames the person's own telescopes reported (N.I.N.A./
                    # Starfront): a contribution that is not a web upload.
                    "myTelescopes": self._telescopes(project_id, who["id"])}

        @app.get("/api/v1/projects/{project_id}/upload-rules")
        def get_rules(project_id: str) -> dict[str, Any]:
            """The rules a collaboration's uploads are held to. Public: a
            contributor should be able to read them before signing in."""
            return self.rules_view(self._project(project_id))

        @app.put("/api/v1/projects/{project_id}/collecting")
        async def put_collecting(project_id: str, request: Request) -> dict[str, Any]:
            """The coordinator turns file collection on or off from this page:
            `collectFiles` (uploads accepted at all) and `autoUpload` (rigs may
            send their lights on their own, as they are captured)."""
            who = self.require(request)
            project = self._project(project_id)
            if who.get("kind") == "agent" or not self.owns_project(who, project):
                raise HTTPException(status_code=403,
                                    detail="only the collaboration's coordinator turns collection on or off")
            try:
                body = await request.json()
            except ValueError:
                raise HTTPException(status_code=400, detail="send JSON") from None
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="send JSON")
            payload = dict(project.get("payload") or {})
            for key in ("collectFiles", "autoUpload"):
                if key in body:
                    payload[key] = bool(body[key])
            project = self.store.set_project(project_id, payload)
            return {"collecting": self.collecting(project),
                    "autoUpload": bool(payload.get("autoUpload"))}

        @app.put("/api/v1/projects/{project_id}/upload-rules")
        async def put_rules(project_id: str, request: Request) -> dict[str, Any]:
            """The coordinator changes the rules. Only the changes from the
            defaults are kept; every file on the collaboration is judged again
            on the stored header facts (no file is re-read)."""
            who = self.require(request)
            project = self._project(project_id)
            if who.get("kind") == "agent" or not self.owns_project(who, project):
                raise HTTPException(status_code=403,
                                    detail="only the collaboration's coordinator sets its upload rules")
            try:
                body = await request.json()
            except ValueError:
                raise HTTPException(status_code=400, detail="send the rules as JSON") from None
            try:
                own = uploadrules.clean_rules(body.get("rules") if isinstance(body, dict)
                                              and "rules" in body else body)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            payload = dict(project.get("payload") or {})
            if body.get("replace") if isinstance(body, dict) else False:
                payload["uploadRules"] = own
            else:
                payload["uploadRules"] = {**(payload.get("uploadRules") or {}), **own}
            # A value set back to its default is the default again.
            payload["uploadRules"] = {k: v for k, v in payload["uploadRules"].items()
                                      if uploadrules.DEFAULTS.get(k, object()) != v}
            project = self.store.set_project(project_id, payload)
            moved: dict[str, int] = {}
            for user in self.store.file_users(project_id):
                for key, n in self.recheck(project, user).items():
                    moved[key] = moved.get(key, 0) + n
            return {"rules": self.rules_view(project), "moved": moved}

        @app.post("/api/v1/projects/{project_id}/files/recheck")
        def recheck_all(project_id: str, request: Request) -> dict[str, Any]:
            """The coordinator has the files from before the rules checked:
            each one's header is read (header only) and every file judged.
            Nothing is deleted or moved on disk."""
            who = self.require(request)
            project = self._project(project_id)
            if who.get("kind") == "agent" or not self.owns_project(who, project):
                raise HTTPException(status_code=403, detail="only the coordinator can recheck")
            read = missing = 0
            for row in self.store.files_in(project_id):
                if _json(row.get("meta"), {}):
                    continue
                path = self.path_of(project_id, row["user"], row["name"], row.get("kind") or "light")
                if not path.is_file():
                    missing += 1
                    meta = {"error": "the file is not on the server's disk"}
                else:
                    meta = uploadrules.facts_of_file(path, row["name"])
                    read += 1
                self.store.set_file_meta(row["id"], meta)
            moved: dict[str, int] = {}
            for user in self.store.file_users(project_id):
                for key, n in self.recheck(project, user).items():
                    moved[key] = moved.get(key, 0) + n
            return {"read": read, "missing": missing, "moved": moved,
                    "lights": self.store.file_status_counts(project_id)}

        @app.put("/api/v1/projects/{project_id}/files/{name}")
        async def upload(project_id: str, name: str, request: Request,
                         telescope: str = Query(default="", max_length=60),
                         kind: str = Query(default="", max_length=12)) -> dict[str, Any]:
            """One file, streamed to disk as it arrives.

            `kind` says what it is - light, flat, dark, bias - or
            is left blank (a legacy `darkflat` is still taken, kept apart and
            never matched) for the name to say (N.I.N.A. writes the image
            type into the name). Idempotent by kind, name and size: a file
            already here at the same size is not taken again, which is what
            lets an upload that was cut off be run again over the whole
            folder. A different size replaces.
            """
            # Everything here but the body itself runs in a worker thread:
            # the checks, the database, the disk, the header and the
            # judging. The event loop only moves bytes, so open/close, task
            # offers and reports are never queued behind a file.
            def prepare() -> tuple[dict[str, Any], dict[str, Any], str, str, int,
                                   Path, dict[str, Any] | None]:
                who = self.require(request)
                project = self._project(project_id)
                if project.get("status") != "open":
                    raise HTTPException(status_code=409, detail="that collaboration is closed")
                if not self.collecting(project) and not self.owns_project(who, project):
                    raise HTTPException(status_code=409,
                                        detail="that collaboration is not collecting files")
                clean = clean_name(name)
                if not clean:
                    raise HTTPException(status_code=400,
                                        detail="not a frame: a file name ending in "
                                               + ", ".join(ALLOWED) + " is expected")
                what = clean_kind(kind, clean)
                try:
                    length = int(request.headers.get("content-length") or "-1")
                except ValueError:
                    length = -1
                if length < 0:
                    raise HTTPException(status_code=411, detail="the file's size must be sent")
                if length > MAX_BYTES:
                    raise HTTPException(status_code=413,
                                        detail=f"larger than {MAX_BYTES // 1024 ** 3} GB")
                if length == 0:
                    raise HTTPException(status_code=400, detail="an empty file")
                existing = self.store.file(project_id, who["id"], clean, what)
                final = self.path_of(project_id, who["id"], clean, what)
                if existing is not None and int(existing["bytes"]) == length and final.is_file():
                    return who, project, clean, what, length, final, \
                        {"file": self._row(existing, {}), "already": True}
                held = int(self.store.file_totals(project_id)["bytes"])
                replacing = int(existing["bytes"]) if existing is not None else 0
                quota = self.quota_of(project)
                if quota == 0:
                    raise HTTPException(status_code=409,
                                        detail="this collaboration is not taking files")
                if held - replacing + length > quota:
                    raise HTTPException(status_code=507,
                                        detail=f"this collaboration is at its storage limit "
                                               f"({quota / 1024 ** 3:g} GB); "
                                               "tell the coordinator")
                if self.stats()["free"] - length < KEEP_FREE:
                    raise HTTPException(status_code=507,
                                        detail="the server has no room left for files; "
                                               "tell the coordinator")
                final.parent.mkdir(parents=True, exist_ok=True)
                return who, project, clean, what, length, final, None

            who, project, clean, what, length, final, done = await asyncio.to_thread(prepare)
            if done is not None:
                # Already here: still take the body (to nowhere) before
                # answering, so the browser's request finishes cleanly; an
                # answer with the body unread can leave an HTTP/2 or HTTP/3
                # PUT hanging at 'sending'.
                if not await _drain(request, length):
                    return JSONResponse(status_code=499, content={"detail": "the upload was cut off"})
                return done
            part = final.with_name(final.name + ".part")
            written = 0
            head = b""
            handle = await asyncio.to_thread(open, part, "wb")
            try:
                try:
                    buffer = bytearray()
                    async for chunk in request.stream():
                        if not chunk:
                            continue
                        if len(head) < 80:
                            head += chunk[:80 - len(head)]
                        written += len(chunk)
                        if written > length:
                            raise HTTPException(status_code=400,
                                                detail="more arrived than the size said")
                        buffer += chunk
                        if len(buffer) >= WRITE_BUFFER:
                            data, buffer = bytes(buffer), bytearray()
                            await asyncio.to_thread(handle.write, data)
                    if buffer:
                        await asyncio.to_thread(handle.write, bytes(buffer))
                except ClientDisconnect:
                    _log.info("upload of %s cut off by the client after %d of %d bytes",
                              clean, written, length)
                    await asyncio.to_thread(handle.close)
                    with _quiet():
                        await asyncio.to_thread(part.unlink)
                    return JSONResponse(status_code=499, content={"detail": "the upload was cut off"})
                finally:
                    await asyncio.to_thread(handle.close)
                if written != length:
                    raise HTTPException(status_code=400,
                                        detail=f"the upload stopped short "
                                               f"({written} of {length} bytes)")
                if not looks_like_data(head, clean):
                    raise HTTPException(status_code=400,
                                        detail=f"{clean} does not start like a "
                                               f"{'XISF' if clean.lower().endswith('.xisf') else 'FITS'} file")
            except BaseException:
                with _quiet():
                    await asyncio.to_thread(part.unlink)
                raise

            def settle() -> dict[str, Any]:
                nonlocal what, final
                try:
                    # The header only - never the pixels - for the upload rules.
                    meta = uploadrules.facts_of_file(part, clean)
                    if (kind or "").strip().lower() in ("", "auto") and meta.get("headerKind") in KINDS \
                            and meta["headerKind"] != what:
                        # Nothing said what it is and the name guessed: the
                        # camera's own IMAGETYP is the better witness.
                        what = meta["headerKind"]
                        final = self.path_of(project_id, who["id"], clean, what)
                        final.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(part, final)
                except BaseException:
                    with _quiet():
                        part.unlink()
                    raise
                label = telescope.strip() or str(who.get("telescope") or "")
                row = self.store.add_file(secrets.token_hex(8), project_id, who["id"],
                                          clean, written, label[:60], what, meta)
                # A light is judged; a calibration frame may also release (or,
                # replaced, hold back) lights already here. Either way the whole
                # of this contributor's set is judged on stored facts - no disk.
                moved = self.recheck(project, who["id"],
                                     only_light=row["id"] if what == "light" else None)
                row = self.store.file(project_id, who["id"], clean, what)
                return {"file": self._row(row, {}), "already": False, "moved": moved,
                        "myLights": self.store.file_status_counts(project_id, who["id"])}

            return await asyncio.to_thread(settle)

        @app.get("/api/v1/projects/{project_id}/files/{user_id}/{kind}/{name}")
        def download(project_id: str, user_id: str, kind: str, name: str, request: Request):
            """A file back out: the coordinator's, or your own."""
            who = self.require(request)
            project = self._project(project_id)
            if who["id"] != user_id and not self.owns_project(who, project):
                raise HTTPException(status_code=403, detail="not your file")
            clean = clean_name(name)
            if not clean or kind not in KINDS:
                raise HTTPException(status_code=404, detail="no such file")
            row = self.store.file(project_id, user_id, clean, kind)
            path = self.path_of(project_id, user_id, clean, kind)
            if row is None or not path.is_file():
                raise HTTPException(status_code=404, detail="no such file")
            return FileResponse(path, media_type="application/octet-stream", filename=clean)

        @app.post("/api/v1/projects/{project_id}/files/remove")
        async def remove_many(project_id: str, request: Request) -> dict[str, Any]:
            """Take many files off at once: each your own, or, as the
            coordinator, anybody's. Calibration is rechecked once per
            contributor at the end, not once per file."""
            who = self.require(request)
            project = self._project(project_id)
            try:
                body = await request.json()
            except ValueError:
                raise HTTPException(status_code=400, detail="send JSON")
            items = body.get("files") if isinstance(body, dict) else None
            if not isinstance(items, list) or not items:
                raise HTTPException(status_code=400, detail="no files listed")
            if len(items) > 5000:
                raise HTTPException(status_code=400, detail="at most 5000 files at once")
            owned = self.owns_project(who, project)
            results, recheck_for = [], set()
            for item in items:
                item = item if isinstance(item, dict) else {}
                name = str(item.get("name") or "")
                kind = str(item.get("kind") or "light")
                target = str(item.get("user") or "") or who["id"]
                out = {"name": name, "kind": kind, "user": target}
                if target != who["id"] and not owned:
                    results.append({**out, "ok": False, "error": "not your file"})
                    continue
                clean = clean_name(name)
                if not clean or kind not in KINDS or self.store.file(project_id, target, clean, kind) is None:
                    results.append({**out, "ok": False, "error": "no such file"})
                    continue
                with _quiet():
                    self.path_of(project_id, target, clean, kind).unlink()
                self.store.remove_file(project_id, target, clean, kind)
                if kind != "light":
                    recheck_for.add(target)
                results.append({**out, "name": clean, "ok": True})
            moved = {user: self.recheck(project, user) for user in sorted(recheck_for)}
            done = sum(1 for r in results if r["ok"])
            return {"removed": done, "failed": len(results) - done, "results": results, "moved": moved}

        @app.delete("/api/v1/projects/{project_id}/files/{name}")
        def remove(project_id: str, name: str, request: Request,
                   user: str = Query(default="", max_length=64),
                   kind: str = Query(default="light", max_length=12)) -> dict[str, Any]:
            """Take a file off: your own, or, as the coordinator, anybody's."""
            who = self.require(request)
            project = self._project(project_id)
            target = user or who["id"]
            if target != who["id"] and not self.owns_project(who, project):
                raise HTTPException(status_code=403, detail="not your file")
            clean = clean_name(name)
            if not clean or kind not in KINDS or self.store.file(project_id, target, clean, kind) is None:
                raise HTTPException(status_code=404, detail="no such file")
            with _quiet():
                self.path_of(project_id, target, clean, kind).unlink()
            self.store.remove_file(project_id, target, clean, kind)
            moved = self.recheck(project, target) if kind != "light" else {}
            return {"removed": clean, "kind": kind, "moved": moved}


_PANEL = re.compile(r"(?:panel|pane|tile|mosaic)[ _-]?(\d{1,3})|(?:^|[_ -])p(\d{1,3})(?=[_ .-])", re.I)


def _select_facts(row: dict[str, Any]) -> dict[str, str]:
    """Filter, night and mosaic panel of a file, for picking files in bulk."""
    meta = _json(row.get("meta"), {}) or {}
    found = _PANEL.search(str(row.get("name") or ""))
    return {"filter": str(meta.get("filter") or ""),
            "night": str(meta.get("date") or "")[:10],
            "panel": (found.group(1) or found.group(2)).lstrip("0") or "0" if found else ""}


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


class _quiet:
    """`with _quiet(): ...` - a cleanup that must not raise over the real error."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: Any) -> bool:
        return True


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Starfront Files</title>
<style>
  :root { color-scheme: dark; }
  [hidden] { display: none !important; }
  body { margin: 0; background: #0d1017; color: #dbe3ee; font: 15px/1.5 system-ui, sans-serif; }
  main { max-width: 1080px; margin: 0 auto; padding: 28px 20px 60px; }
  h1 { font-size: 24px; margin: 0 0 4px; }
  h2 { font-size: 16px; margin: 26px 0 8px; }
  p { margin: 6px 0; } .muted { color: #8f9bb0; } .small { font-size: 13px; }
  a { color: #9fe4bd; }
  .btn { display: inline-block; background: #7ee7a5; color: #0d1017; font-weight: 600; padding: 9px 16px;
         border-radius: 8px; border: 0; cursor: pointer; text-decoration: none; font-size: 15px; }
  .btn.ghost { background: #1a2130; color: #dbe3ee; }
  .btn:disabled { opacity: .5; cursor: default; }
  select, input[type=text] { background: #11161f; color: #dbe3ee; border: 1px solid #2a3344; border-radius: 8px;
         padding: 8px 10px; font-size: 15px; min-width: 260px; }
  .drop { margin: 12px 0; padding: 16px 12px; border: 2px dashed #2a3344; border-radius: 12px; text-align: center; }
  .drop.over { border-color: #7ee7a5; background: #11161f; }
  .zones { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 10px 0; }
  .zone { margin: 0; padding: 16px 12px 10px; }
  .zone h3 { margin: 0 0 2px; font-size: 16px; }
  .zone p.small { min-height: 2.6em; margin: 0 0 8px; font-size: 12px; }
  .zone .btn { padding: 7px 12px; font-size: 14px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; margin: 8px 0; }
  th, td { text-align: left; padding: 5px 6px; border-bottom: 1px solid #1f2634; vertical-align: top; }
  th { color: #8f9bb0; font-weight: 500; }
  td.num, th.num { text-align: right; white-space: nowrap; }
  .bar { height: 8px; background: #1a2130; border-radius: 4px; overflow: hidden; }
  .bar > i { display: block; height: 100%; background: #7ee7a5; width: 0; }
  .ok { color: #7ee7a5; } .no { color: #ff8a7a; } .wait { color: #e5c07b; }
  .pickbar { margin: 6px 0; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .pickbar select, .pickbar button { font-size: 12px; }
  .pickbar button.rm { background: none; border: 1px solid #5a6478; color: #c9d1de; border-radius: 4px; padding: 2px 8px; cursor: pointer; }
  .pickbar button.rm:disabled { opacity: .4; cursor: default; }
  .x { background: none; border: 0; color: #8f9bb0; cursor: pointer; font-size: 14px; }
  .x:hover { color: #ff8a7a; }
  #queue tr.done td { color: #8f9bb0; }
  code { background: #11161f; padding: 1px 5px; border-radius: 4px; }
  .rules { background: #11161f; border: 1px solid #2a3344; border-radius: 12px; padding: 12px 16px; margin: 12px 0; }
  .rules h2 { margin-top: 0; }
  .rules ul { margin: 6px 0; padding-left: 0; list-style: none; }
  .rules li { margin: 4px 0; padding-left: 24px; position: relative; }
  .rules li::before { content: '☐'; position: absolute; left: 0; color: #7ee7a5; }
  .pill { display: inline-block; padding: 0 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }
  .pill.ready { background: #16351f; color: #7ee7a5; } .pill.pending { background: #3a3116; color: #e5c07b; }
  .pill.quarantine { background: #3b1a17; color: #ff8a7a; } .pill.unchecked { background: #1a2130; color: #8f9bb0; }
  .why { font-size: 12px; color: #8f9bb0; margin: 2px 0 0; }
  .why.pending { color: #e5c07b; } .why.quarantine { color: #ff8a7a; }
  #gaps li { margin: 3px 0; }
  .ruleform { display: grid; grid-template-columns: minmax(200px, 1fr) 140px; gap: 6px 12px; align-items: center; font-size: 13px; margin: 8px 0; }
  .ruleform input[type=text], .ruleform select { min-width: 0; width: 100%; padding: 4px 6px; font-size: 13px; }
  .topbar { position: sticky; top: 0; z-index: 5; background: #0b0e14ee; border-bottom: 1px solid #2a3344; backdrop-filter: blur(4px); }
  .tin { max-width: 1080px; margin: 0 auto; padding: 10px 20px; display: flex; gap: 12px; align-items: center; justify-content: space-between; }
  .brand { color: #dbe3ee; font-weight: 700; font-size: 17px; text-decoration: none; letter-spacing: .01em; }
  .tright { font-size: 13px; color: #8f9bb0; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
  .tright .btn { padding: 4px 10px; font-size: 13px; }
  .collabs { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; margin: 0 0 20px; }
  .clabel { font-size: 12px; color: #8f9bb0; text-transform: uppercase; letter-spacing: .06em; }
  #collabSel { width: auto; min-width: 0; max-width: 100%; box-sizing: border-box; font-size: 14px; padding: 6px 10px; font-weight: 600; }
  .curmeta { margin: 0; font-size: 13px; color: #8f9bb0; }
  #curName .badge { margin-left: 0; }
  .badge { font-size: 11px; padding: 1px 7px; border-radius: 8px; margin-left: 6px; background: #16351f; color: #7ee7a5; font-weight: 600; vertical-align: middle; }
  .badge.closed { background: #1a2130; color: #8f9bb0; } .badge.paused { background: #3a3116; color: #e5c07b; }
  .badge.rel { background: #1d2740; color: #9fc3ff; }
  details.allc { margin-left: auto; }
  details.allc[open] { flex-basis: 100%; margin-left: 0; }
  details.allc > summary { cursor: pointer; font-size: 13px; color: #c3ccd9; }
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px; margin: 8px 0; }
  .ccard { display: block; border: 1px solid #2a3344; border-radius: 10px; padding: 8px 10px; background: #0f141d; color: #dbe3ee; text-decoration: none; }
  .ccard:hover, .ccard:focus-visible { border-color: #7ee7a5; outline: none; }
  .ccard[aria-current="true"] { border-color: #7ee7a5; background: #13231a; }
  .ccard b { display: block; } .ccard .small { color: #8f9bb0; }
  .cgroup { font-size: 12px; color: #8f9bb0; text-transform: uppercase; letter-spacing: .05em; margin: 10px 0 0; }
  #hero { margin: 0 0 28px; border: 1px solid #2a3344; border-radius: 12px; overflow: hidden; background: #0f141d; }
  #hero .shot { position: relative; display: block; cursor: zoom-in; background: #000; }
  #hero .shot:focus-visible { outline: 2px solid #9fe4bd; outline-offset: -2px; }
  #viewer { position: fixed; inset: 0; z-index: 50; background: #000; }
  #viewer[hidden] { display: none; }
  #viewerStage { position: absolute; inset: 0; overflow: hidden; touch-action: none; cursor: grab; }
  #viewerStage:active { cursor: grabbing; }
  #viewerImg { position: absolute; left: 0; top: 0; transform-origin: 0 0; max-width: none; user-select: none; -webkit-user-select: none; -webkit-user-drag: none; -webkit-touch-callout: none; }
  #viewerClose { position: absolute; top: 10px; right: 10px; z-index: 2; width: 44px; height: 44px; border-radius: 22px; font-size: 22px; line-height: 1; background: #1a2130dd; color: #fff; border: 1px solid #2a3344; cursor: pointer; }
  #viewerBar { position: absolute; left: 0; right: 0; bottom: 0; z-index: 2; padding: 10px 12px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
               background: linear-gradient(transparent, #000d); color: #cfd8e6; font-size: 13px; pointer-events: none; }
  #viewerBar button { pointer-events: auto; background: #1a2130dd; color: #fff; border: 1px solid #2a3344; border-radius: 8px; min-width: 40px; height: 36px; font-size: 16px; cursor: pointer; }
  #viewerBar #viewerTools { pointer-events: auto; display: inline-flex; flex-wrap: wrap; gap: 8px; max-width: 100%; min-width: 0; }
  #viewerTools:empty { display: none; }
  /* Contrast: a display-only CSS filter on the two preview <img> elements.
     Never a canvas re-encode, never a request: the bytes on disk and the
     downloads are untouched. The value is one number for the page's life,
     shared by the embedded preview and the full-screen viewer, and it
     survives switching images (see applyContrast). */
  #hero .shot img, #hero .shot .ghost, #viewerImg { filter: var(--img-filter, none); }
  .contrast { display: inline-flex; align-items: center; gap: 8px; min-height: 44px; }
  .contrast label { font-size: 13px; font-weight: 600; color: #dbe3ee; white-space: nowrap; }
  .contrast input[type="range"] { width: clamp(96px, 24vw, 200px); height: 44px; margin: 0; accent-color: #7ee7a5; cursor: pointer; touch-action: pan-y; }
  .contrast input[type="range"]:focus-visible { outline: 2px solid #9fe4bd; outline-offset: 2px; }
  .contrast output { min-width: 4ch; font-size: 13px; font-variant-numeric: tabular-nums; color: #dbe3ee; }
  #starfrontDisplayFilters { position: absolute; width: 0; height: 0; overflow: hidden; }
  #viewerBar .contrast { pointer-events: auto; background: #1a2130dd; border: 1px solid #2a3344; border-radius: 8px; padding: 0 8px; }
  #viewerBar .contrast button { font-size: 13px; min-width: 44px; height: 42px; }
  #hero .contrast { background: #141a25; border: 1px solid #2a3344; border-radius: 8px; padding: 0 10px; }
  #hero .contrast button { background: transparent; border: 1px solid #2a3344; border-radius: 6px; color: #dbe3ee; font-size: 13px; min-width: 44px; min-height: 32px; cursor: pointer; }
  /* Contrast and Black point sit side by side, but on a phone each takes its
     own row: the pair must never push a Reset off the screen. */
  #viewerContrast { display: flex; flex-wrap: wrap; gap: 8px; max-width: 100%; min-width: 0; }
  @media (max-width: 700px) {
    #hero .contrast input[type="range"], #viewerContrast input[type="range"] { width: clamp(80px, 22vw, 160px); }
  }
  @media (max-width: 520px) { .contrast input[type="range"] { width: clamp(88px, 26vw, 160px); } }
  .seg { display: inline-flex; border: 1px solid #2a3344; border-radius: 8px; overflow: hidden; vertical-align: middle; background: #141a25; }
  .seg button, #viewerBar .seg button { background: transparent; color: #c3ccd9; border: 0; border-radius: 0; min-width: 0; height: auto; min-height: 32px; padding: 4px 14px; font-size: 13px; font-weight: 500; cursor: pointer; }
  .seg button + button { border-left: 1px solid #2a3344; }
  .seg button[aria-pressed="true"], #viewerBar .seg button[aria-pressed="true"] { background: #e6ebf2; color: #0d1017; font-weight: 600; }
  .seg button:focus-visible { outline: 2px solid #9fe4bd; outline-offset: -2px; }
  #hero figure { margin: 0; }
  .btn.dl { padding: 3px 10px; font-size: 13px; }
  .btn.dl.locked { background: #1a2130; color: #8f9bb0; cursor: not-allowed; }
  .lockline { color: #e5c07b; }
  table.masters td, table.people td { vertical-align: middle; }
  table.masters td.dlc { white-space: nowrap; }
  tr.tot td { border-bottom: 0; }
  #hero .shot img { display: block; width: 100%; height: min(760px, max(320px, calc(100vh - 250px))); object-fit: contain; background: #000; }
  #hero .shot .ghost { position: absolute; inset: 0; pointer-events: none; transition: opacity .28s ease; }
  #hero .htools { flex: 1 1 100%; display: flex; align-items: center; justify-content: space-between; gap: 10px 16px; min-width: 0; }
  #hero .htools:empty { display: none; }
  .views { display: flex; gap: 6px; min-width: 0; overflow-x: auto; scrollbar-width: none; }
  .views::-webkit-scrollbar { display: none; }
  .views .seg { flex: none; }
  .views .seg button { padding: 4px 11px; }
  .seg button:disabled { color: #566173; cursor: not-allowed; }
  #viewerBar .views { max-width: 100%; }
  #hero .hbar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px 24px; padding: 14px 18px; border-top: 1px solid #1f2634; }
  #hero .ttl { margin: 0; font-size: 16px; font-weight: 600; color: #e6ebf2; letter-spacing: .01em; }
  #hero .sub { margin: 2px 0 0; font-size: 13px; color: #8f9bb0; }
  #hero .acts { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px; }
  .btn.quiet { background: transparent; border: 1px solid #2a3344; color: #dbe3ee; padding: 6px 12px; font-size: 13px; font-weight: 500; min-height: 32px; box-sizing: border-box; }
  .btn.quiet:hover { border-color: #4a5466; }
  .lnk { color: #c3ccd9; font-size: 13px; text-decoration: underline; text-decoration-color: #4a5466; text-underline-offset: 3px; }
  .lnk:hover { color: #e6ebf2; }
  .job { padding: 12px 18px 13px; border-top: 1px solid #1f2634; background: #111722; font-size: 13px; color: #c3ccd9; }
  .job.solo { border: 1px solid #2a3344; border-radius: 12px; background: #0f141d; margin: 0 0 20px; }
  .job .jl { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 4px 16px; }
  .job .jt { color: #e6ebf2; font-weight: 600; }
  .job .jm { color: #8f9bb0; }
  .job .track { height: 3px; border-radius: 2px; background: #1f2634; margin: 9px 0 0; overflow: hidden; }
  .job .fill { height: 100%; width: 0; background: #c3ccd9; border-radius: 2px; transition: width .6s ease; }
  .job.indet .fill { width: 30%; animation: jobslide 1.6s ease-in-out infinite; }
  @keyframes jobslide { from { transform: translateX(-100%); } to { transform: translateX(340%); } }
  @media (prefers-reduced-motion: reduce) { .job.indet .fill { animation: none; width: 100%; opacity: .4; } }
  .job.failed .fill { background: #b86b6b; } .job.failed .jt { color: #e8b4b4; }
  .job.stale .fill { background: #5b6577; }
  .job.done .fill { background: #8fc9a5; }
  .stackctl { padding: 10px 18px; font-size: 13px; display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
  .stackctl button[disabled] { opacity: .55; cursor: default; }
  .job details { margin-top: 8px; } .job summary { cursor: pointer; color: #8f9bb0; font-size: 12px; }
  .job ol { margin: 6px 0 0; padding: 0; list-style: none; display: grid; gap: 3px; }
  .job li { display: flex; gap: 10px; align-items: baseline; font-size: 12px; color: #8f9bb0; }
  .job li::before { content: ""; flex: none; width: 7px; height: 7px; border-radius: 50%; border: 1px solid #4a5466; transform: translateY(1px); }
  .job li.done { color: #c3ccd9; } .job li.done::before { background: #8f9bb0; border-color: #8f9bb0; }
  .job li.running { color: #e6ebf2; font-weight: 600; } .job li.running::before { background: #e6ebf2; border-color: #e6ebf2; }
  .job li.failed { color: #e8b4b4; } .job li.failed::before { background: #b86b6b; border-color: #b86b6b; }
  .job li .sd { margin-left: auto; font-weight: 400; color: #8f9bb0; }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
  details.view { scroll-margin-top: 64px; border: 1px solid #2a3344; border-radius: 10px; margin: 10px 0; background: #0f141d; }
  details.view > summary { cursor: pointer; padding: 11px 14px; font-weight: 600; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
  details.view > summary::-webkit-details-marker { display: none; }
  details.view > summary::before { content: '▸'; color: #7ee7a5; width: 1em; flex: none; transition: transform .15s; }
  details.view[open] > summary::before { transform: rotate(90deg); }
  details.view > summary:focus-visible { outline: 2px solid #7ee7a5; outline-offset: -2px; border-radius: 10px; }
  details.view > summary .sum { font-weight: 400; color: #8f9bb0; font-size: 13px; }
  details.view > .inner { padding: 0 14px 12px; }
  details.kind { margin: 6px 0; border-top: 1px solid #1f2634; }
  details.kind > summary { cursor: pointer; padding: 6px 0; font-size: 14px; }
  #release > h2:first-child { font-size: 14px; margin-top: 4px; }
  .signcard { display: flex; align-items: center; justify-content: space-between; gap: 12px 24px; flex-wrap: wrap; background: #11161f; border: 1px solid #2a3344; border-radius: 12px; padding: 14px 18px; margin: 10px 0 14px; }
  .signcard p { margin: 0; } .signcard .st { font-weight: 600; color: #e6ebf2; margin-bottom: 2px; }
  .upload { background: #11161f; border: 1px solid #2a3344; border-radius: 12px; padding: 12px 14px; margin: 10px 0 14px; }
  .picker .lbl { font-weight: 600; }
  .picks { justify-content: center; }
  .scroll { overflow-x: auto; }
  #telescope { min-width: 200px; }
  @media (max-width: 640px) {
    .tin { padding: 8px 12px; }
    .cards { grid-template-columns: 1fr; }
    .clabel { display: none; }
    #collabSel { flex: 1 1 100%; width: 100%; }
    .curmeta .by { display: none; }
    #hero { margin: 0 -12px 22px; border-radius: 0; border-left: 0; border-right: 0; }
    #hero .hbar { padding: 12px; }
    #hero .hbar { gap: 12px; }
    #hero .shot img { height: min(calc(100vw / var(--ar, 1)), 62vh); }
    #hero .acts { width: 100%; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; order: 3; }
    #hero .htools { flex-direction: column; align-items: stretch; gap: 8px; order: 2; }
    #hero .views { margin: 0 -12px; padding: 0 12px; }
    #hero .views.more { -webkit-mask-image: linear-gradient(to right, #000 82%, transparent); mask-image: linear-gradient(to right, #000 82%, transparent); }
    #hero .htools > .seg { display: flex; } #hero .htools > .seg button { flex: 1; }
    #hero .acts .btn { text-align: center; }
    .curmeta { flex: 1 1 0; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .signcard .btn { width: 100%; text-align: center; box-sizing: border-box; }
    main { padding: 16px 12px 40px; }
    select, input[type=text], #telescope { min-width: 0; width: 100%; box-sizing: border-box; }
    .zones { grid-template-columns: 1fr 1fr; gap: 8px; }
    .zone { padding: 10px 8px 8px; }
    .zone p.small { display: none; }
    .picks { flex-direction: column; gap: 6px; }
    .picks .btn { width: 100%; }
    table { font-size: 12px; }
    #release table, .inner table { display: block; overflow-x: auto; }
    table.masters td.dlc { white-space: normal; }
    table.masters td.dlc .small { display: block; }
    .ruleform { grid-template-columns: 1fr; }
  }
</style></head>
<body>
<header class="topbar"><div class="tin">
  <a class="brand" href="/files">Starfront Files</a>
  <span class="tright" id="topWho"></span>
</div></header>
<main>
<section id="collabs" class="collabs" aria-label="Collaboration" hidden>
  <label class="clabel" for="collabSel">Collaboration</label>
  <select id="collabSel"></select>
  <span id="curName"></span>
  <span class="curmeta" id="curMeta"></span>
  <details class="allc" id="allCollabs"><summary>All collabs (<span id="nAll">0</span>)</summary>
    <div id="cardList"></div>
  </details>
</section>
<section id="hero" hidden aria-label="Latest image"></section>
<p class="muted small">Upload a collaboration's lights and the flats, darks or bias that calibrate them (FITS or XISF). Files already here are skipped, so a folder can be sent again. Lights only count once their calibration is here too.</p>

<section id="signin" class="signcard" hidden>
  <div><p class="st">Sign in to upload your frames</p>
  <p class="small muted"><span id="signinNote"></span> Use the Discord account you use for Starfront; this does not sign your N.I.N.A. or Starfront out.</p></div>
  <a class="btn" href="/files/signin">Sign in with Discord</a>
</section>

<section id="app" hidden>

  <div class="upload">
    <select id="project" hidden aria-hidden="true"></select>
    <p class="row small" id="curLine"></p>
    <p class="small muted" id="projectNote"></p>
    <p id="none" class="muted" hidden>No collaboration is collecting files right now.</p>

    <div id="uploader" hidden>
      <div class="zones">
        <div class="drop zone" data-kind="light">
          <h3>Lights</h3>
          <p class="small muted">The subs, as N.I.N.A. saved them</p>
          <p class="row picks"><button class="btn" data-pick="files">Choose files</button><button class="btn ghost" data-pick="folder">Folder</button></p>
        </div>
        <div class="drop zone" data-kind="flat">
          <h3>Flats</h3>
          <p class="small muted">One set per filter, same camera angle</p>
          <p class="row picks"><button class="btn" data-pick="files">Choose files</button><button class="btn ghost" data-pick="folder">Folder</button></p>
        </div>
        <div class="drop zone" data-kind="dark">
          <h3>Darks</h3>
          <p class="small muted">Same gain, offset, binning, temperature, exposure</p>
          <p class="row picks"><button class="btn" data-pick="files">Choose files</button><button class="btn ghost" data-pick="folder">Folder</button></p>
        </div>
        <div class="drop zone" data-kind="bias">
          <h3>Bias</h3>
          <p class="small muted">Same gain, offset and binning</p>
          <p class="row picks"><button class="btn" data-pick="files">Choose files</button><button class="btn ghost" data-pick="folder">Folder</button></p>
        </div>
      </div>
      <input type="file" id="files" multiple hidden>
      <input type="file" id="folder" webkitdirectory multiple hidden>
      <p class="row"><input type="text" id="telescope" placeholder="Telescope (optional)"> <span class="small muted">written against each file, so rigs can be told apart. Drop a folder on a box, or press its button; keep this tab open while it uploads.</span></p>
      <p id="progress" hidden><span id="progressText"></span></p>
      <div class="bar" id="allBar" hidden><i></i></div>
      <div class="scroll"><table id="queue" hidden><thead><tr><th>File</th><th>Kind</th><th class="num">Size</th><th>State</th></tr></thead><tbody></tbody></table></div>
    </div>
  </div>
</section>

<div id="views">
  <details class="view" id="sec-release" hidden>
    <summary><span class="t">Masters and contributors</span> <span class="sum" id="sumRelease"></span></summary>
    <div class="inner"><section id="release" hidden></section></div>
  </details>

  <details class="view" id="sec-rules" hidden>
    <summary><span class="t">Upload rules</span> <span class="sum" id="sumRules"></span></summary>
    <div class="inner rules">
      <p class="small muted">Every light needs its calibration on the server. The server checks each file and says what is missing.</p>
      <ul id="rules"></ul>
      <details id="ownerRules" hidden>
        <summary class="small">Change the rules (coordinator)</summary>
        <div class="ruleform" id="ruleForm"></div>
        <p class="row"><button class="btn ghost" id="saveRules" style="padding:5px 10px;font-size:13px">Save rules</button> <span class="small muted" id="rulesSaved"></span></p>
      </details>
    </div>
  </details>

  <details class="view" id="sec-needed" hidden>
    <summary><span class="t" id="neededTitle">Still needed</span> <span class="sum" id="sumNeeded"></span></summary>
    <div class="inner">
      <p id="myStatus"></p>
      <div id="gapBox" hidden>
        <p class="small"><b>Still needed before these lights count:</b></p>
        <ul id="gaps" class="small"></ul>
      </div>
    </div>
  </details>

  <details class="view" id="sec-mine" hidden>
    <summary><span class="t">My contribution</span> <span class="sum" id="sumMine"></span></summary>
    <div class="inner">
      <div id="mineScopes"></div>
      <p class="small muted" id="mineNote"></p>
      <div id="mineKinds"></div>
    </div>
  </details>

  <details class="view" id="sec-others" hidden>
    <summary><span class="t">Contributors' files</span> <span class="sum" id="sumOthers"></span></summary>
    <div class="inner">
      <p class="small muted">Only you, as coordinator, see this. Tick bad frames and remove them.</p>
      <div id="othersKinds"></div>
    </div>
  </details>
</div>
<!-- Display-only black point: a linear stretch of each RGB channel,
     out = (in - bp) / (1 - bp), clamped by the browser. Referenced by the
     previews' CSS filter; it changes no bytes on the server. Kept in the
     body at zero size rather than display:none, which some browsers refuse
     to resolve a CSS url(#...) filter against. -->
<svg id="starfrontDisplayFilters" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" focusable="false">
  <filter id="sfBlackPoint" color-interpolation-filters="sRGB">
    <feComponentTransfer>
      <feFuncR id="sfBpR" type="linear" slope="1" intercept="0"/>
      <feFuncG id="sfBpG" type="linear" slope="1" intercept="0"/>
      <feFuncB id="sfBpB" type="linear" slope="1" intercept="0"/>
    </feComponentTransfer>
  </filter>
</svg>
<div id="viewer" hidden role="dialog" aria-modal="true" aria-label="Image viewer">
  <div id="viewerStage"><img id="viewerImg" alt="" draggable="false"></div>
  <button type="button" id="viewerClose" aria-label="Close viewer" title="Close (Esc)">✕</button>
  <div id="viewerBar">
    <button type="button" data-z="in" aria-label="Zoom in">+</button><button type="button" data-z="out" aria-label="Zoom out">−</button><button type="button" data-z="fit" aria-label="Fit to screen">Fit</button>
    <span id="viewerTools"></span>
    <span class="contrast" id="viewerContrast"></span>
    <span id="viewerNote"></span>
    <span class="muted">Scroll or pinch to zoom · drag to pan · double-tap to zoom · S: stars on/off · [ ]: image · Esc to close</span>
  </div>
</div>
<script src="/releases/page.js" defer></script>

<script>
(function () {
  const $ = (id) => document.getElementById(id);
  const state = { projects: [], allowed: [], kinds: [], project: null, owned: false, running: false, queue: [], picking: 'light' };

  // -- Contrast (Bray, 2026-11-06) ----------------------------------------
  // Display only: a CSS contrast() filter on the preview images. 100% is the
  // image exactly as published. Nothing is written, fetched or re-encoded;
  // downloads and the files on the server are byte for byte the same. The
  // value is kept in this variable for the life of the page, so it carries
  // across images (Color / Ha / stars-starless, another release) and across
  // the embedded preview and the full-screen viewer. It is deliberately not
  // stored: a new tab starts at 100%.
  let contrastPct = 100;
  // Black point, as a fraction of full range (0-0.5). Same display-only idea:
  // an inline SVG feComponentTransfer linear stretch, referenced from the CSS
  // filter alongside contrast(), so the two combine in one pass.
  let blackPoint = 0;
  function applyDisplay() {
    // Contrast first (CSS's own contrast is about mid grey), then the black
    // point stretch; both off means the image exactly as published.
    const parts = [];
    if (contrastPct !== 100) parts.push(`contrast(${contrastPct / 100})`);
    if (blackPoint > 0) parts.push('url(#sfBlackPoint)');
    const f = parts.join(' ');
    const slope = String(1 / (1 - blackPoint)), intercept = String(-blackPoint / (1 - blackPoint));
    for (const id of ['sfBpR', 'sfBpG', 'sfBpB']) {
      const fn = document.getElementById(id);
      if (fn) { fn.setAttribute('slope', slope); fn.setAttribute('intercept', intercept); }
    }
    document.getElementById('hero').style.setProperty('--img-filter', f);
    document.getElementById('viewer').style.setProperty('--img-filter', f);
    const readouts = [['[data-contrast]', contrastPct, contrastPct + '%'],
                      ['[data-blackpoint]', Math.round(blackPoint * 100), Math.round(blackPoint * 100) + '%']];
    for (const [sel, value, text] of readouts) {
      document.querySelectorAll(sel).forEach((r) => {
        if (Number(r.value) !== value) r.value = String(value);
        r.setAttribute('aria-valuetext', text.replace('%', ' percent'));
        const out = r.closest('.contrast').querySelector('output');
        if (out) out.textContent = text;
      });
    }
  }
  function applyContrast(pct) {
    contrastPct = Math.min(300, Math.max(50, Math.round(Number(pct) || 100)));
    applyDisplay();
  }
  function applyBlackPoint(pct) {
    blackPoint = Math.min(50, Math.max(0, Math.round(Number(pct) || 0))) / 100;
    applyDisplay();
  }
  // Two labelled sliders (embedded preview, full-screen viewer), one value.
  // Each has its own Reset; the title says which control it puts back.
  function contrastControl(which) {
    return `<span class="contrast"><label for="contrast${which}">Contrast</label>` +
      `<input type="range" id="contrast${which}" data-contrast min="50" max="300" step="5" value="${contrastPct}" aria-describedby="contrastHint${which}">` +
      `<output id="contrastHint${which}" for="contrast${which}">${contrastPct}%</output>` +
      `<button type="button" data-contrast-reset aria-label="Reset contrast" title="Reset contrast to the published image (100%)">Reset</button></span>` +
      `<span class="contrast"><label for="blackpoint${which}">Black point</label>` +
      `<input type="range" id="blackpoint${which}" data-blackpoint min="0" max="50" step="1" value="${Math.round(blackPoint * 100)}" aria-describedby="blackpointHint${which}">` +
      `<output id="blackpointHint${which}" for="blackpoint${which}">${Math.round(blackPoint * 100)}%</output>` +
      `<button type="button" data-blackpoint-reset aria-label="Reset black point" title="Reset black point to 0% (no clipping)">Reset</button></span>`;
  }
  document.addEventListener('input', (e) => {
    const d = e.target && e.target.dataset;
    if (!d) return;
    if ('contrast' in d) applyContrast(e.target.value);
    else if ('blackpoint' in d) applyBlackPoint(e.target.value);
  });
  document.addEventListener('click', (e) => {
    const d = e.target && e.target.dataset;
    if (!d) return;
    if ('contrastReset' in d) applyContrast(100);
    else if ('blackpointReset' in d) applyBlackPoint(0);
  });
  const label = (k) => (state.kinds.find((x) => x.id === k) || { label: k }).label;
  const kindSummary = (kinds) => Object.entries(kinds || {}).map(([k, n]) => `${n} ${label(k).toLowerCase()}`).join(', ');

  const mb = (n) => n >= 1024 ** 3 ? (n / 1024 ** 3).toFixed(2) + ' GB' : (n / 1024 ** 2).toFixed(1) + ' MB';
  const when = (t) => t ? new Date(t * 1000).toLocaleString() : '';
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const api = async (path, options) => {
    const r = await fetch(path, options);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || r.statusText);
    return body;
  };
  const allowed = (name) => state.allowed.some((ext) => name.toLowerCase().endsWith(ext));
  const LABELS = { ready: 'Ready', pending: 'Pending calibration', quarantine: 'Quarantined', '': 'Not checked' };
  const pill = (st) => `<span class="pill ${st || 'unchecked'}">${LABELS[st || ''] || esc(st)}</span>`;
  const whyList = (st, reasons) => (reasons || []).map((r) => `<div class="why ${st || ''}">${esc(r)}</div>`).join('');
  const statusCell = (f) => pill(f.status) + whyList(f.status, f.reasons);
  const lightLine = (c) => {
    c = c || {};
    const parts = [`${c.ready || 0} ready`, `${c.pending || 0} pending calibration`, `${c.quarantine || 0} quarantined`];
    if (c['']) parts.push(`${c['']} not checked (uploaded before the rules)`);
    return 'Lights: ' + parts.join(' · ') + '. Only ready lights go into the stack.';
  };

  // -- the collapsible views: closed unless opened, each remembered -------
  const OPEN_KEY = 'starfront-files-open';
  const remembered = () => { try { return JSON.parse(localStorage.getItem(OPEN_KEY) || '{}') || {}; } catch (e) { return {}; } };
  function setOpen(el, open) { if (el.open !== open) { el.dataset.auto = '1'; el.open = open; } }
  function restore(el) { const r = remembered(); if (el.id in r) setOpen(el, !!r[el.id]); }
  document.querySelectorAll('details.view').forEach((el) => {
    restore(el);
    el.addEventListener('toggle', () => {
      if (el.dataset.auto) { delete el.dataset.auto; return; }   // opened by the page, not the person
      const r = remembered(); r[el.id] = el.open; localStorage.setItem(OPEN_KEY, JSON.stringify(r));
    });
  });
  function show(id, summary) { const el = $(id); el.hidden = false; if (summary !== undefined) el.querySelector('.sum').textContent = summary; return el; }
  function deepLink() {
    const h = location.hash.slice(1);
    const target = h.startsWith('release-') ? $('sec-release') : (h.startsWith('sec-') ? $(h) : null);
    if (!target || target.hidden) return false;
    setOpen(target, true);
    target.scrollIntoView({ block: 'start' });
    return true;
  }
  const s = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  // The release section draws itself (/releases/page.js) and says when.
  // -- full-screen viewer: wheel/pinch/double-tap zoom, drag to pan --------
  // It always shows what #heroImg shows: its src first, then the image named
  // by #heroImg's data-full (full resolution) once that has loaded. Anything
  // that swaps #heroImg (a stars on/off toggle) should set src and data-full;
  // the viewer follows, keeping its zoom and position.
  const V = { open: false, s: 1, x: 0, y: 0, w0: 0, h0: 0, ptrs: new Map(), pinch: null, tap: 0, down: null };
  const vImg = () => $('viewerImg');
  function vApply() { vImg().style.transform = `translate(${V.x}px, ${V.y}px) scale(${V.s})`; }
  function vClamp() {
    const W = innerWidth, H = innerHeight, w = V.w0 * V.s, h = V.h0 * V.s;
    V.x = w <= W ? (W - w) / 2 : Math.min(0, Math.max(W - w, V.x));
    V.y = h <= H ? (H - h) / 2 : Math.min(0, Math.max(H - h, V.y));
  }
  function vFit(ratio) {
    ratio = ratio || (V.w0 && V.h0 ? V.w0 / V.h0 : 1.5);
    V.w0 = Math.min(innerWidth, innerHeight * ratio); V.h0 = V.w0 / ratio;
    vImg().style.width = V.w0 + 'px'; vImg().style.height = V.h0 + 'px';
    V.s = 1; vClamp(); vApply();
  }
  // A new image of another shape: same zoom, same spot (as a fraction of the image) in the middle.
  function vReshape(r) {
    if (!V.w0 || !V.h0 || Math.abs(r - V.w0 / V.h0) < 0.01 * r) return;
    const W = innerWidth, H = innerHeight, s = V.s;
    const fx = (W / 2 - V.x) / (V.w0 * s), fy = (H / 2 - V.y) / (V.h0 * s);
    vFit(r);
    if (s > 1) { V.s = s; V.x = W / 2 - fx * V.w0 * s; V.y = H / 2 - fy * V.h0 * s; vClamp(); vApply(); }
  }
  function vZoom(f, px, py) {
    const s = Math.min(16, Math.max(1, V.s * f)), k = s / V.s;
    V.x = px - (px - V.x) * k; V.y = py - (py - V.y) * k; V.s = s; vClamp(); vApply();
  }
  // Images already fetched (full-res ones are big): url -> Image, kept for the page's life.
  const imgCache = new Map();
  function preload(url) {
    if (!url) return null;
    let im = imgCache.get(url);
    if (!im) { im = new Image(); im.decoding = 'async'; im.src = url; imgCache.set(url, im); }
    return im;
  }
  const ready = (im) => !!(im && im.complete && im.naturalWidth);
  function vLoad() {
    const hero = $('heroImg'); if (!hero) return;
    const img = vImg(), base = hero.getAttribute('src') || '', full = hero.dataset.full || '';
    const note = (hi) => { $('viewerNote').textContent = `${/_(5k|phone)\.jpe?g$/i.test(hi.src) ? 'Phone-size detail' : 'Full resolution'} · ${hi.naturalWidth.toLocaleString()} × ${hi.naturalHeight.toLocaleString()} px`; };
    if (img.dataset.base !== base) {
      const first = !img.dataset.base;
      img.dataset.base = base; img.dataset.full = '';
      // Already have the full-res of the new image (a flip back): straight to it.
      const hi = full ? imgCache.get(full) : null;
      if (ready(hi)) { img.src = full; img.dataset.full = full; note(hi); }
      else img.src = base;
      const lo = imgCache.get(base);
      const r = ready(lo) ? lo.naturalWidth / lo.naturalHeight : (hero.naturalWidth && hero.naturalHeight ? hero.naturalWidth / hero.naturalHeight : 1.5);
      if (first) vFit(r); else vReshape(r);
    }
    if (full && img.dataset.full !== full) {
      img.dataset.full = full; $('viewerNote').textContent = 'Loading full resolution…';
      const hi = preload(full);
      const done = () => { if (img.dataset.full !== full || img.dataset.base !== base) return; img.src = full; note(hi); };
      if (ready(hi)) done(); else { hi.addEventListener('load', done, { once: true }); hi.addEventListener('error', () => { if (img.dataset.full === full) $('viewerNote').textContent = ''; }, { once: true }); }
    } else if (!full) $('viewerNote').textContent = '';
  }
  function viewerOpen() {
    if (!$('heroImg')) return;
    V.open = true; $('viewer').hidden = false; document.body.style.overflow = 'hidden';
    delete vImg().dataset.base; vLoad(); $('viewerClose').focus();
    setTimeout(() => { const v = views[viewIdx]; if (v && v.starless) preload(v[curMode() === 'stars' ? 'starless' : 'stars'].full); }, 2000);
    document.dispatchEvent(new CustomEvent('starfront:viewer', { detail: { open: true } }));
  }
  function viewerClose() {
    V.open = false; $('viewer').hidden = true; document.body.style.overflow = '';
    const o = $('heroOpen'); if (o) o.focus();
    document.dispatchEvent(new CustomEvent('starfront:viewer', { detail: { open: false } }));
  }
  window.starfrontViewer = { open: viewerOpen, close: viewerClose, zoom: (f, x, y) => vZoom(f, x == null ? innerWidth / 2 : x, y == null ? innerHeight / 2 : y), refresh: () => V.open && vLoad() };
  (function wireViewer() {
    const stage = $('viewerStage');
    $('viewerContrast').innerHTML = contrastControl('Viewer');
    applyDisplay();
    $('viewerClose').addEventListener('click', viewerClose);
    document.addEventListener('keydown', (e) => {
      if (!V.open) return;
      if (e.key === 'Escape') viewerClose();
      else if (e.key === '+' || e.key === '=') vZoom(1.5, innerWidth / 2, innerHeight / 2);
      else if (e.key === '-') vZoom(1 / 1.5, innerWidth / 2, innerHeight / 2);
      else if (e.key === '0') vFit();
      else if ((e.key === 't' || e.key === 'T' || e.key === 's' || e.key === 'S') && !e.ctrlKey && !e.metaKey && !e.altKey) setStars(curMode() === 'stars' ? 'starless' : 'stars');
      else if ((e.key === '[' || e.key === ']') && views.length > 1) setView((viewIdx + (e.key === ']' ? 1 : views.length - 1)) % views.length);
    });
    $('viewerBar').addEventListener('click', (e) => {
      const z = e.target && e.target.dataset ? e.target.dataset.z : '';
      if (z === 'in') vZoom(1.6, innerWidth / 2, innerHeight / 2);
      else if (z === 'out') vZoom(1 / 1.6, innerWidth / 2, innerHeight / 2);
      else if (z === 'fit') vFit();
    });
    stage.addEventListener('wheel', (e) => { e.preventDefault(); vZoom(Math.exp(-e.deltaY * (e.deltaMode ? 0.05 : 0.0015)), e.clientX, e.clientY); }, { passive: false });
    const mid = () => { const p = Array.from(V.ptrs.values()); return { x: (p[0].x + p[1].x) / 2, y: (p[0].y + p[1].y) / 2, d: Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) || 1 }; };
    stage.addEventListener('pointerdown', (e) => {
      try { stage.setPointerCapture(e.pointerId); } catch (x) { /* synthetic pointer */ }
      V.ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (V.ptrs.size === 2) V.pinch = mid();
      V.down = V.ptrs.size === 1 ? { x: e.clientX, y: e.clientY, t: Date.now() } : null;
    });
    stage.addEventListener('pointermove', (e) => {
      const p = V.ptrs.get(e.pointerId); if (!p) return;
      const dx = e.clientX - p.x, dy = e.clientY - p.y;
      p.x = e.clientX; p.y = e.clientY;
      if (V.ptrs.size === 1) { V.x += dx; V.y += dy; vClamp(); vApply(); }
      else if (V.ptrs.size === 2 && V.pinch) {
        const m = mid();
        vZoom(m.d / V.pinch.d, m.x, m.y);
        V.x += m.x - V.pinch.x; V.y += m.y - V.pinch.y; vClamp(); vApply();
        V.pinch = m;
      }
    });
    const up = (e) => {
      if (!V.ptrs.has(e.pointerId)) return;
      V.ptrs.delete(e.pointerId);
      if (V.ptrs.size < 2) V.pinch = null;
      const d = V.down; V.down = null;
      if (e.type !== 'pointerup' || !d || V.ptrs.size) return;
      if (Math.hypot(e.clientX - d.x, e.clientY - d.y) > 10 || Date.now() - d.t > 300) { V.tap = 0; return; }
      const now = Date.now();
      if (now - V.tap < 320) { V.tap = 0; if (V.s > 1.5) vFit(); else vZoom(3, e.clientX, e.clientY); }
      else V.tap = now;
    };
    stage.addEventListener('pointerup', up); stage.addEventListener('pointercancel', up);
    addEventListener('resize', () => { if (V.open) { const s = V.s; vFit(); if (s > 1) vZoom(s, innerWidth / 2, innerHeight / 2); } });
    // Follow #heroImg when something swaps it (a new release, a stars toggle).
    new MutationObserver(() => { if (V.open) vLoad(); }).observe($('hero'), { subtree: true, childList: true, attributes: true, attributeFilter: ['src', 'data-full'] });
  })();

  // The preview, shown once. #heroImg is the one image; a control that swaps
  // its src (e.g. stars on/off) can go in #heroTools.
  // Small screens get the ~5000 px version of a full-res image when the
  // release has one (its `small`), big screens the full thing.
  const smallScreen = () => matchMedia('(pointer: coarse)').matches && Math.min(screen.width, screen.height) <= 900;
  // What the preview can show: one entry per preview image in release.json
  // (role "preview", not a starless twin), each { key, label, title, stars, starless }
  // where stars/starless are { src, full, alt, title } (starless null when there is
  // no twin). The picker shows when there are two or more; Stars/Starless when any
  // has a twin. The choice lives in the URL hash: #view=Ha&stars=0.
  let views = [], viewIdx = 0, wantStarless = false;
  const curMode = () => { const v = views[viewIdx]; return v && v.starless && wantStarless ? 'starless' : 'stars'; };
  let swapTok = 0;
  // Swap #heroImg once the new image is in, with a short crossfade. The viewer follows (MutationObserver).
  function swapHero(m) {
    const img = $('heroImg'); if (!img || !m) return;
    const set = () => { img.src = m.src; img.dataset.full = m.full; img.alt = m.alt; img.title = m.title; };
    if (img.getAttribute('src') === m.src) { set(); return; }
    const tok = ++swapTok, pre = preload(m.src);
    let done = false;
    const go = () => {
      if (done || tok !== swapTok) return; done = true;
      if (img.complete && img.naturalWidth && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
        const g = img.cloneNode(); g.removeAttribute('id'); g.className = 'ghost'; g.alt = ''; g.setAttribute('aria-hidden', 'true'); g.removeAttribute('loading');
        img.parentNode.appendChild(g);
        requestAnimationFrame(() => requestAnimationFrame(() => { g.style.opacity = '0'; }));
        setTimeout(() => g.remove(), 450);
      }
      set();
    };
    if (ready(pre)) go(); else { pre.addEventListener('load', go, { once: true }); pre.addEventListener('error', go, { once: true }); setTimeout(go, 6000); }
  }
  function writeHash() {
    const v = views[viewIdx]; if (!v) return;
    history.replaceState(history.state, '', '#' + new URLSearchParams({ view: v.key, stars: wantStarless ? '0' : '1' }).toString());
  }
  function readHash() {
    const h = location.hash.slice(1);
    if (!/(^|&)(view|stars)=/.test(h)) return null;
    const p = new URLSearchParams(h);
    return { view: (p.get('view') || '').toLowerCase(), stars: p.get('stars') };
  }
  function applyView(user) {
    const v = views[viewIdx]; if (!v) return;
    const mode = curMode();
    swapHero(v[mode]);
    document.querySelectorAll('[data-view]').forEach((b) => b.setAttribute('aria-pressed', String(+b.dataset.view === viewIdx)));
    document.querySelectorAll('[data-stars]').forEach((b) => {
      b.setAttribute('aria-pressed', String(b.dataset.stars === mode));
      if (b.dataset.stars === 'starless') {
        b.disabled = !v.starless;
        b.title = v.starless ? 'Stars removed (S)' : `No starless version of ${v.label}`;
      }
    });
    if (user) writeHash();
    // The other half of the pair, so the next flip is instant.
    if (v.starless) { const o = v[mode === 'stars' ? 'starless' : 'stars']; preload(o.src); if (V.open) preload(o.full); }
  }
  function setStars(mode) {
    const v = views[viewIdx]; if (!v) return;
    if (mode === 'starless' && !v.starless) return;
    wantStarless = mode === 'starless'; applyView(true);
  }
  function setView(i) {
    if (!views[i]) return;
    viewIdx = i; applyView(true);
    document.querySelectorAll(`[data-view="${i}"]`).forEach((b) => b.scrollIntoView({ block: 'nearest', inline: 'nearest' }));
  }
  function fromHash() {
    const h = readHash(); if (!h || !views.length) return false;
    const i = views.findIndex((v) => [v.key, v.kind, v.filter].some((k) => k && String(k).toLowerCase() === h.view));
    if (i >= 0) viewIdx = i;
    if (h.stars != null) wantStarless = h.stars === '0';
    return true;
  }
  function drawStarTools() {
    const pick = views.length > 1;
    const any = views.some((v) => v.starless);
    const btn = (v, i) => `<button type="button" data-view="${i}" aria-pressed="${i === viewIdx}" title="${esc(v.title)}">${esc(v.label)}</button>`;
    const groups = [views.map((v, i) => [v, i]).filter(([v]) => !v.mono), views.map((v, i) => [v, i]).filter(([v]) => v.mono)].filter((g) => g.length);
    const picker = pick ? `<span class="views" role="group" aria-label="Which image">${groups.map((g) => `<span class="seg">${g.map(([v, i]) => btn(v, i)).join('')}</span>`).join('')}</span>` : '';
    const stars = any ? `<span class="seg" role="group" aria-label="Stars on or off"><button type="button" data-stars="stars" aria-pressed="true">Stars</button><button type="button" data-stars="starless" aria-pressed="false">Starless</button></span>` : '';
    for (const id of ['heroTools', 'viewerTools']) {
      const el = $(id); if (!el) continue;
      el.innerHTML = picker + stars;
      el.querySelectorAll('[data-stars]').forEach((b) => {
        b.addEventListener('click', (e) => { e.stopPropagation(); setStars(b.dataset.stars); });
        // Hover or focus: fetch it ahead of a flip.
        const warm = () => { const v = views[viewIdx]; const m = v && v[b.dataset.stars]; if (m) { preload(m.src); if (V.open) preload(m.full); } };
        b.addEventListener('pointerenter', warm); b.addEventListener('focus', warm);
      });
      el.querySelectorAll('[data-view]').forEach((b) => {
        b.addEventListener('click', (e) => { e.stopPropagation(); setView(+b.dataset.view); });
        const warm = () => { const v = views[+b.dataset.view]; const m = v && v[curMode() === 'starless' && v.starless ? 'starless' : 'stars']; if (m) { preload(m.src); if (V.open) preload(m.full); } };
        b.addEventListener('pointerenter', warm); b.addEventListener('focus', warm);
      });
      const sc = el.querySelector('.views');
      if (sc) { const edge = () => sc.classList.toggle('more', sc.scrollLeft + sc.clientWidth < sc.scrollWidth - 2); sc.addEventListener('scroll', edge, { passive: true }); requestAnimationFrame(edge); addEventListener('resize', edge); }
    }
    applyView(false);
  }
  window.addEventListener('hashchange', () => { if (fromHash()) applyView(false); });
  function drawHero(rel) {
    const box = $('hero');
    const files = rel ? (rel.files || []) : [];
    const byName = new Map(files.map((x) => [x.name, x]));
    // Never a starless twin as the main image.
    const previews = files.filter((x) => x.role === 'preview' && x.starless !== true && !x.starlessOf);
    const hero = previews.find((x) => x.hero) || previews[0];
    views = []; viewIdx = 0;
    if (!hero) { box.hidden = true; box.innerHTML = ''; drawStarTools(); return; }
    // Its full-resolution twin, for the viewer only (never offered as a download).
    const fullOf = (prev) => {
      if (!prev) return null;
      if (prev.fullres && byName.get(prev.fullres)) return byName.get(prev.fullres);
      const m = String(prev.name || '').match(/^(.*)_preview\.jpe?g$/i);
      return m ? (byName.get(m[1] + '_fullres.jpg') || byName.get(m[1] + '_fullres.jpeg') || null) : null;
    };
    // The phone copy: `small` (make_5k.py) or the _phone twin build_release.py writes (size "phone").
    const phoneOf = (f) => (f.small && byName.get(f.small)) || byName.get(String(f.name || '').replace(/_fullres\.(jpe?g)$/i, '_phone.$1')) || null;
    const viewUrl = (f) => { if (!f) return ''; const sm = phoneOf(f); return (smallScreen() && sm && sm !== f ? sm : f).url; };
    const full = fullOf(hero);
    // Hero first ("Color" unless it has a label), then the other colour kinds, then single filters in the release's filter order.
    // No kind/filter field (v1): the bit before _vN in M31_RGB_v1_preview.jpg.
    const nameKind = (n) => { const m = /^(?:.*_)?([^_]+)_v\d+(?:_[^_]+)*?_preview\.jpe?g$/i.exec(n || ''); return m ? m[1] : String(n || '').replace(/_preview\.jpe?g$/i, ''); };
    const order = (rel.filters || []).map((f) => f.filter);
    const isMono = (x) => x !== hero && (x.group ? x.group === 'filter' : (!!x.filter && (!x.kind || x.kind === x.filter)));
    const rank = (x) => x === hero ? -1 : (isMono(x) ? 1 : 0);
    const seen = new Set();
    for (const p of previews.slice().sort((a, b) => rank(a) - rank(b) || (rank(a) === 1 ? order.indexOf(a.filter) - order.indexOf(b.filter) : 0))) {
      const label = p.label || (p === hero ? 'Color' : (p.kind || p.filter || nameKind(p.name)));
      if (seen.has(label.toLowerCase())) continue; seen.add(label.toLowerCase());
      const f = fullOf(p);
      const tw = typeof p.starless === 'string' ? byName.get(p.starless) : null;
      const twf = tw ? ((f && typeof f.starless === 'string' && byName.get(f.starless)) || fullOf(tw)) : null;
      views.push({ key: label, label, kind: p.kind || '', filter: p.filter || '', mono: isMono(p), title: p.title || label,
        stars: { src: p.thumb || p.url, full: viewUrl(f), alt: p.title || 'Latest preview', title: p.title || '' },
        starless: tw ? { src: tw.thumb || tw.url, full: viewUrl(twf), alt: tw.title || 'Latest preview, starless', title: tw.title || '' } : null });
    }
    fromHash();
    const first = views[viewIdx][curMode()];
    const name = String(rel.title || '').replace(/\s+coadd\s+v\d+\s*$/i, '') || rel.projectName || '';
    const ymd = /^(\d{4})-(\d\d)-(\d\d)$/.exec(rel.released || '');
    const date = ymd ? new Date(+ymd[1], +ymd[2] - 1, +ymd[3]).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : (rel.released || '');
    const people = (rel.contributors || []).length;
    const filters = (rel.filters || []).map((f) => f.filter).join(', ');
    const sub = [rel.totalHours != null ? `${Number(rel.totalHours).toFixed(0)} h` : '', people ? `${people} contributor${people === 1 ? '' : 's'}` : '', filters].filter(Boolean).join(' · ');
    box.innerHTML = `<figure>
      <div class="shot" id="heroOpen" role="button" tabindex="0" aria-label="View full size"><img id="heroImg" decoding="async" width="2400" height="1600" src="${esc(first.src)}" data-full="${esc(first.full)}" alt="${esc(first.alt)}" title="${esc(first.title)}"></div>
      <figcaption class="hbar">
        <div><p class="ttl">${esc([name, rel.version, date].filter(Boolean).join(' · '))}</p><p class="sub">${esc(sub)}</p></div>
        <div class="acts"><button type="button" class="btn quiet" id="heroView">View full size</button><a class="btn quiet" href="#sec-release">Masters &amp; contributors</a></div>
        <div class="htools" id="heroTools"></div>
        ${contrastControl('Hero')}
      </figcaption></figure>`;
    box.hidden = false;
    applyDisplay();
    drawStarTools();
    // The starless twin's 2400 px: fetch it once the page has settled.
    setTimeout(() => { const v = views[viewIdx]; if (v && v.starless) preload(v.starless.src); }, 1500);
    // Phones: the frame takes the first image's shape (between 3:2 and 4:5); the others fit inside it.
    const hi = $('heroImg');
    const shape = () => { if (hi.naturalWidth) opener.style.setProperty('--ar', Math.min(1.5, Math.max(0.8, hi.naturalWidth / hi.naturalHeight)).toFixed(3)); };
    const opener = $('heroOpen');
    if (hi.complete) shape(); else hi.addEventListener('load', shape, { once: true });
    opener.addEventListener('click', () => viewerOpen());
    $('heroView').addEventListener('click', () => viewerOpen());
    opener.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); viewerOpen(); } });
    // The caption's link opens the Masters and contributors section, every time.
    const go = box.querySelector('a[href="#sec-release"]');
    if (go) go.addEventListener('click', (e) => {
      e.preventDefault();
      const sec = $('sec-release');
      if (sec.hidden) return;
      setOpen(sec, true);
      history.replaceState(history.state, '', '#sec-release');
      sec.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
    box.dispatchEvent(new CustomEvent('starfront:hero', { bubbles: true, detail: { rel, hero } }));
  }
  document.addEventListener('starfront:release', (e) => {
    if (!e.detail || (state.current && e.detail.project !== state.current)) { $('sec-release').hidden = true; drawHero(null); return; }
    const rel = e.detail;
    drawHero(rel);
    const locked = (rel.files || []).some((x) => x.locked);
    const hours = rel.totalHours == null ? '' : ` · ${Number(rel.totalHours).toFixed(0)} h`;
    show('sec-release', `${rel.version || ''}${hours} · ${s((rel.filters || []).length, 'filter')} · ${s((rel.contributors || []).length, 'contributor')}` + (locked ? ' · downloads for contributors' : ' · downloads'));
    if (location.hash.startsWith('#release-')) deepLink();
  });
  window.addEventListener('hashchange', deepLink);

  // -- the coordinator's processing job (stacking, rendering on their own machine) --
  // GET /api/v1/releases/<project>/job. Shown while running, for a day after a failure,
  // and once built until it is published. Polled every 15 s while running and visible.
  const JOB = { pid: '', timer: 0, data: null, open: false, seq: 0 };
  const ago = (sec) => sec < 60 ? 'just now' : (sec < 3600 ? `${Math.round(sec / 60)} min ago` : (sec < 86400 ? `${Math.round(sec / 3600)} h ago` : `${Math.round(sec / 86400)} d ago`));
  const mins = (sec) => sec < 3600 ? `${Math.max(1, Math.round(sec / 60))} min` : `${(sec / 3600).toFixed(1)} h`;
  function jobVisible(j) {
    if (!j) return false;
    const age = (j.now || Date.now() / 1000) - (j.updated || 0);
    if (j.status === 'running') return true;
    if (j.status === 'failed') return age < 86400;
    if (j.status === 'done') return !!j.version && j.version !== j.published && age < 14 * 86400;
    return false;
  }
  function drawJob() {
    const j = JOB.data, hero = $('hero');
    let el = $('job');
    if (!jobVisible(j) || !hero) { if (el) el.remove(); return; }
    if (!el) { el = document.createElement('div'); el.id = 'job'; el.setAttribute('role', 'status'); }
    const fig = !hero.hidden && hero.querySelector('figure');
    // Between the image and its caption: it is news about the next version, so it comes first.
    const cap = fig && fig.querySelector('figcaption');
    if (fig) { if (el.nextSibling !== cap) fig.insertBefore(el, cap); }
    else if (el.nextSibling !== hero) hero.parentNode.insertBefore(el, hero);
    const now = j.now || Date.now() / 1000, age = now - (j.updated || now);
    const steps = j.steps || [], n = steps.length, i = Number.isInteger(j.step) ? j.step : -1;
    const cur = i >= 0 && steps[i] ? `${steps[i].name} (${i + 1}/${n})` : '';
    const v = j.version || '';
    let title, bits = [], pct = j.percent;
    if (j.status === 'done') { title = `${v || 'New version'} ready, awaiting approval`; bits = [`built ${ago(age)}`]; pct = 100; }
    else if (j.status === 'failed') { title = `${v ? v + ' build' : 'Build'} failed`; bits = [cur, `${ago(age)}`]; }
    else {
      title = j.title || (v ? `Building ${v}` : 'Processing');
      bits = [cur, pct != null ? `${Math.round(pct)}%` : '', j.stale ? `no update for ${mins(age)}` : `updated ${ago(age)}`];
      if (!j.stale && j.eta && j.eta > now) bits.push(`about ${mins(j.eta - now)} left`);
    }
    el.className = 'job' + (fig ? '' : ' solo') + (j.status === 'failed' ? ' failed' : '') + (j.status === 'done' ? ' done' : '') + (j.stale ? ' stale' : '') + (j.status === 'running' && pct == null && !j.stale ? ' indet' : '');
    const msg = j.message && j.message !== (steps[i] || {}).name ? j.message : '';
    const list = n ? `<details${JOB.open ? ' open' : ''}><summary>Steps</summary><ol>${steps.map((st) => `<li class="${esc(st.state)}"><span>${esc(st.name)}</span>${st.detail ? `<span class="sd">${esc(st.detail)}</span>` : (st.state === 'running' || st.state === 'failed' ? `<span class="sd">${esc(st.state)}</span>` : '')}</li>`).join('')}</ol></details>` : '';
    el.innerHTML = `<div class="jl"><span><span class="jt">${esc(title)}</span>${bits.filter(Boolean).map((b) => ` <span class="jm">· ${esc(b)}</span>`).join('')}</span>${msg ? `<span class="jm">${esc(msg)}</span>` : ''}</div>
      <div class="track" aria-hidden="true"><div class="fill" style="width:${pct != null ? Math.max(2, Math.min(100, pct)) : (j.status === 'running' ? 30 : 100)}%"></div></div>${list}`;
    const d = el.querySelector('details'); if (d) d.addEventListener('toggle', () => { JOB.open = d.open; });
    if (typeof drawStack === 'function') drawStack();
  }
  async function jobPoll() {
    clearTimeout(JOB.timer); JOB.timer = 0;
    const pid = JOB.pid, seq = ++JOB.seq;
    if (!pid) return;
    try {
      const r = await fetch(`/api/v1/releases/${encodeURIComponent(pid)}/job`, { credentials: 'omit' });
      if (seq !== JOB.seq || pid !== JOB.pid) return;
      JOB.data = r.ok ? await r.json() : null;
    } catch (e) { if (seq !== JOB.seq) return; }
    drawJob();
    // Keep polling only while something is running; a hidden tab waits for visibilitychange.
    if (JOB.data && JOB.data.status === 'running' && document.visibilityState === 'visible') JOB.timer = setTimeout(jobPoll, 15000);
  }
  // -- "Start new stack": owners only. POST /api/v1/releases/<project>/stack queues a request the
  // stacking machine picks up; disabled while one is queued or a job is running.
  const STACK = { data: null, timer: 0, seq: 0, sending: false };
  function drawStack() {
    const d = STACK.data, hero = $('hero');
    let el = $('stackCtl');
    if (!d || !d.canStack || !hero) { if (el) el.remove(); return; }
    if (!el) { el = document.createElement('div'); el.id = 'stackCtl'; el.className = 'stackctl'; }
    const job = $('job');
    const anchor = job || hero;
    if (anchor.nextSibling !== el) anchor.parentNode.insertBefore(el, anchor.nextSibling);
    const now = d.now || Date.now() / 1000;
    let label = 'Start new stack', note = '', dis = STACK.sending || d.busy;
    if (STACK.sending) label = 'Starting…';
    else if (d.active && d.state === 'queued') { label = 'Stack queued'; note = `requested ${ago(now - (d.requested || now))}; waiting for the stacking machine` + (now - (d.requested || now) > 900 ? ' (not picked up yet: is it running?)' : ''); }
    else if (d.active || (d.job === 'running' && !d.stale)) { label = 'Stacking…'; note = d.message || 'progress is shown above'; }
    else if (d.state === 'failed') note = `last request failed: ${d.message || 'see the stacking machine'}`;
    else if (d.state === 'done') note = `last stack finished ${ago(now - (d.updated || now))}`;
    el.innerHTML = `<button type="button" id="stackBtn"${dis ? ' disabled' : ''}>${esc(label)}</button>` +
      (d.active && d.state === 'queued' ? ' <button type="button" class="link" id="stackCancel">Cancel</button>' : '') +
      (note ? ` <span class="jm">${esc(note)}</span>` : '');
    const b = $('stackBtn');
    if (b) b.addEventListener('click', async () => {
      if (!confirm('Start a new stack of everything collected so far? It runs on the stacking machine and the new version goes live when it is done.')) return;
      STACK.sending = true; drawStack();
      try { STACK.data = await api(`/api/v1/releases/${encodeURIComponent(JOB.pid)}/stack`, { method: 'POST' }); }
      catch (e) { alert(e.message); }
      STACK.sending = false; stackPoll(); jobPoll();
    });
    const c = $('stackCancel');
    if (c) c.addEventListener('click', async () => {
      try { STACK.data = await api(`/api/v1/releases/${encodeURIComponent(JOB.pid)}/stack`, { method: 'DELETE' }); } catch (e) { alert(e.message); }
      stackPoll();
    });
  }
  async function stackPoll() {
    clearTimeout(STACK.timer); STACK.timer = 0;
    const pid = JOB.pid, seq = ++STACK.seq;
    if (!pid) return;
    try {
      const r = await fetch(`/api/v1/releases/${encodeURIComponent(pid)}/stack`, { credentials: 'same-origin' });
      if (seq !== STACK.seq || pid !== JOB.pid) return;
      STACK.data = r.ok ? await r.json() : null;
    } catch (e) { if (seq !== STACK.seq) return; }
    drawStack();
    if (STACK.data && STACK.data.canStack && STACK.data.busy && document.visibilityState === 'visible') {
      STACK.timer = setTimeout(stackPoll, 15000);
      if (!JOB.timer) JOB.timer = setTimeout(jobPoll, 15000);   // the bar appears once the machine starts
    }
  }
  document.addEventListener('starfront:hero', drawStack);
  document.addEventListener('starfront:release', () => setTimeout(drawStack, 0));
  function jobWatch(pid) { if (pid === JOB.pid) return; JOB.pid = pid; JOB.data = null; STACK.data = null; drawJob(); drawStack(); jobPoll(); stackPoll(); }
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && JOB.data && JOB.data.status === 'running' && !JOB.timer) jobPoll(); });
  document.addEventListener('starfront:hero', drawJob);
  document.addEventListener('starfront:release', () => setTimeout(drawJob, 0));

  // -- the collab picker, in the page: open collabs first, closed below --
  const STATUS = (p) => p.closed ? 'closed' : (p.collecting ? 'collecting' : 'paused');
  const STATUS_WORD = (p) => p.closed ? 'closed' : (p.collecting ? 'collecting' : 'not collecting');
  const gb = (n) => n >= 1024 ** 3 ? (n / 1024 ** 3).toFixed(1) + ' GB' : (n >= 1024 ** 2 ? (n / 1024 ** 2).toFixed(0) + ' MB' : (n ? '<1 MB' : '0'));
  const facts = (x) => [x.coordinator ? 'by ' + x.coordinator : '', x.files ? `${x.files.toLocaleString()} files` : 'no files yet', x.files ? gb(x.bytes) : ''].filter(Boolean).join(' · ');
  function buildSwitch(info, pub, rel) {
    const byId = new Map();
    const released = new Set((rel.releases || []).map((r) => r.project));
    for (const p of info.collabs || []) byId.set(p.id, Object.assign({}, p));
    // Your own collabs (e.g. a paused one you coordinate) and any with a master out.
    for (const p of info.projects || []) if (!byId.has(p.id)) byId.set(p.id, { id: p.id, name: p.name, coordinator: p.coordinator || '', closed: !!p.closed, collecting: !p.closed && !!p.collecting, files: p.files || 0, bytes: p.bytes || 0 });
    for (const r of rel.releases || []) if (!byId.has(r.project)) {
      const p = (pub.projects || []).find((q) => q.id === r.project) || {};
      byId.set(r.project, { id: r.project, name: r.name || p.name || r.project, coordinator: p.coordinator || '', closed: p.status ? p.status !== 'open' : false, collecting: false, files: 0, bytes: 0 });
    }
    for (const x of byId.values()) x.release = released.has(x.id);
    const all = Array.from(byId.values());
    const order = (a, b) => (a.collecting === b.collecting ? 0 : (a.collecting ? -1 : 1)) || (b.bytes - a.bytes) || a.name.localeCompare(b.name);
    return { open: all.filter((x) => !x.closed).sort(order), closed: all.filter((x) => x.closed).sort(order) };
  }
  function drawSwitcher() {
    const sw = state.switch;
    const opt = (x) => `<option value="${esc(x.id)}"${x.id === state.current ? ' selected' : ''}>${esc(x.name)}</option>`;
    $('collabSel').innerHTML = (sw.open.length ? `<optgroup label="Open">${sw.open.map(opt).join('')}</optgroup>` : '') +
      (sw.closed.length ? `<optgroup label="Closed">${sw.closed.map(opt).join('')}</optgroup>` : '');
    const card = (x) => `<a class="ccard" href="/files?project=${encodeURIComponent(x.id)}" data-id="${esc(x.id)}" aria-current="${x.id === state.current}"><b>${esc(x.name)}<span class="badge ${STATUS(x)}">${STATUS_WORD(x)}</span>${x.release ? '<span class="badge rel">master</span>' : ''}</b><span class="small">${esc(facts(x))}</span></a>`;
    $('cardList').innerHTML = (sw.open.length ? `<p class="cgroup">Open</p><div class="cards">${sw.open.map(card).join('')}</div>` : '') +
      (sw.closed.length ? `<p class="cgroup">Closed</p><div class="cards">${sw.closed.map(card).join('')}</div>` : '');
    $('nAll').textContent = sw.open.length + sw.closed.length;
    $('collabs').hidden = !(sw.open.length + sw.closed.length);
    $('cardList').querySelectorAll('a[data-id]').forEach((a) => a.addEventListener('click', (e) => {
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;   // new tab: let the link work
      e.preventDefault(); choose(a.dataset.id, true).catch((x) => alert(x.message));
    }));
  }
  function drawCurrent(x) {
    $('curName').innerHTML = `<span class="badge ${STATUS(x)}">${STATUS_WORD(x)}</span>`;
    $('curMeta').innerHTML = (x.coordinator ? `<span class="by">by ${esc(x.coordinator)} · </span>` : '') + esc(facts({ ...x, coordinator: '' }));
  }
  async function choose(id, push) {
    const all = state.switch.open.concat(state.switch.closed);
    const x = all.find((p) => p.id === id);
    if (!x) return;
    const prevCurrent = state.current;
    state.current = id;
    localStorage.setItem('starfront-files-project', id);
    const url = new URL(location.href);
    url.searchParams.set('project', id);
    const keepView = /^#(view|stars)=/.test(url.hash) && (!prevCurrent || prevCurrent === id);
    if (url.hash && !keepView && !url.hash.startsWith('#release-') && !url.hash.startsWith('#sec-')) url.hash = '';
    if (url.hash.startsWith('#release-') && url.hash.slice(9) !== id) url.hash = '';
    history[push ? 'pushState' : 'replaceState']({ project: id }, '', url);
    document.title = `${x.name} · Starfront Files`;
    drawSwitcher();
    drawCurrent(x);
    jobWatch(id);
    document.dispatchEvent(new CustomEvent('starfront:project', { detail: { id } }));
    const mine = state.projects.find((p) => p.id === id);
    const badge = `<span class="badge ${STATUS(x)}">${STATUS_WORD(x)}</span>`;
    if (!state.signedIn) {
      $('signinNote').textContent = x.collecting ? 'This collaboration is collecting frames.' : (x.closed ? 'This collaboration is closed and takes no new frames.' : 'This collaboration is not taking uploads right now.');
      return;
    }
    ['sec-rules', 'sec-needed', 'sec-mine'].forEach((k) => { $(k).hidden = !mine; });
    if (!mine) $('sec-others').hidden = true;
    if (!mine) {
      $('uploader').hidden = true;
      $('none').hidden = false;
      $('none').textContent = x.closed ? `${x.name} is closed and you have no files on it.` : `${x.name} is not taking uploads right now.`;
      $('curLine').innerHTML = '';
      $('projectNote').textContent = '';
      return;
    }
    $('none').hidden = true;
    $('uploader').hidden = !!mine.closed;
    drawCollect(mine, x);
    $('curLine').innerHTML = mine.closed ? '<span class="muted">Closed: download only.</span>' : (mine.full ? '<span class="no">Storage limit reached for this collaboration.</span>' : `<span class="muted">Uploading to <b>${esc(x.name)}</b>${myLine(mine)}</span>`);
    $('project').value = id;
    await pick();
  }
  window.addEventListener('popstate', () => {
    const id = new URL(location.href).searchParams.get('project');
    if (id && id !== state.current) choose(id, false).catch((e) => alert(e.message));
  });

  async function load() {
    const [info, pub, rel] = await Promise.all([
      api('/api/v1/files/projects'),
      api('/api/v1/projects').catch(() => ({ projects: [] })),
      api('/api/v1/releases').catch(() => ({ releases: [] }))]);
    state.projects = info.projects; state.allowed = info.allowed; state.kinds = info.kinds || [];
    state.signedIn = info.signedIn;
    state.switch = buildSwitch(info, pub, rel);
    $('signin').hidden = info.signedIn; $('app').hidden = !info.signedIn;
    $('topWho').innerHTML = info.signedIn
      ? `<span>Signed in as <b>${esc(info.name)}</b></span> <button class="btn ghost" id="signout">Sign out</button>`
      : '<a class="btn" href="/files/signin">Sign in</a>';
    if (info.signedIn) $('signout').addEventListener('click', async () => { await fetch('/files/signout', { method: 'POST' }); location.reload(); });
    if (info.signedIn) {
      let warn = document.getElementById('diskwarn');
      if (!warn) { warn = document.createElement('p'); warn.id = 'diskwarn'; warn.className = 'small'; warn.style.color = '#ff8a7a'; $('app').prepend(warn); }
      warn.textContent = info.diskLow ? `Server disk is low: ${mb(info.free)} free. Uploads stop when it is full.` : '';
      warn.hidden = !info.diskLow;
      const select = $('project');
      select.innerHTML = info.projects.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
    }
    const all = state.switch.open.concat(state.switch.closed);
    const url = new URL(location.href);
    const hash = location.hash.slice(1).replace(/^release-/, '');
    // First visit: a collab with a master out, then one taking frames, then anything open.
    const firstOpen = (state.switch.open.find((x) => x.release) || state.switch.open.find((x) => x.collecting)
                       || state.switch.open[0] || all[0] || {}).id;
    const id = [url.searchParams.get('project'), hash, localStorage.getItem('starfront-files-project'), firstOpen]
      .find((c) => c && all.some((p) => p.id === c));
    if (!id) { drawSwitcher(); $('none').hidden = !info.signedIn; $('uploader').hidden = true; return; }
    await choose(id, false);
  }

  async function pick() {
    const id = $('project').value;
    state.project = state.projects.find((p) => p.id === id) || null;
    if (!state.project) return;
    localStorage.setItem('starfront-files-project', id);
    const p = state.project;
    $('projectNote').textContent = p.notes || '';
    const listing = await api(`/api/v1/projects/${encodeURIComponent(id)}/files`);
    state.listing = listing;
    state.owned = listing.owned;
    drawRules(listing);
    drawMine(listing);
    drawOthers(listing);
    deepLink();
  }

  function drawRules(listing) {
    const r = listing.rules || { checklist: [], spec: [], values: {} };
    const v = r.values || {};
    const parts = [];
    if (v.enforce === false) parts.push('calibration not checked');
    else {
      parts.push({ darkOrBias: 'darks or bias', dark: 'darks', none: 'no darks needed' }[v.darkPolicy] || 'darks or bias');
      if (v.requireFlats !== false) parts.push('flats per filter');
    }
    const box = show('sec-rules', parts.join(', '));
    box.hidden = !r.checklist.length;
    $('rules').innerHTML = r.checklist.map((x) => `<li>${esc(x)}</li>`).join('');
    $('ownerRules').hidden = !listing.owned;
    if (!listing.owned) return;
    $('ruleForm').innerHTML = r.spec.map((s) => {
      const v = r.values[s.name];
      let input;
      if (s.type === 'bool') input = `<select data-rule="${s.name}" data-type="bool"><option value="true"${v ? ' selected' : ''}>yes</option><option value="false"${v ? '' : ' selected'}>no</option></select>`;
      else if (s.type === 'choice') input = `<select data-rule="${s.name}" data-type="choice">${s.choices.map((c) => `<option${c === v ? ' selected' : ''}>${esc(c)}</option>`).join('')}</select>`;
      else input = `<input type="text" data-rule="${s.name}" data-type="${s.type}" value="${v === null || v === undefined ? '' : esc(v)}" placeholder="${s.type === 'float_or_none' ? 'off' : ''}">`;
      return `<label for="">${esc(s.about)} <span class="muted">(${esc(s.name)}, default ${s.default === null ? 'off' : esc(s.default)})</span></label>${input}`;
    }).join('');
  }

  async function saveRules() {
    const rules = {};
    $('ruleForm').querySelectorAll('[data-rule]').forEach((el) => {
      const t = el.dataset.type, v = el.value.trim();
      rules[el.dataset.rule] = t === 'bool' ? v === 'true' : t === 'choice' ? v : (v === '' ? null : Number(v));
    });
    try {
      const out = await api(`/api/v1/projects/${encodeURIComponent(state.project.id)}/upload-rules`, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ rules, replace: true }) });
      const m = out.moved || {};
      $('rulesSaved').textContent = `Saved. Rechecked every file: ${m.ready || 0} became ready, ${m.pending || 0} pending, ${m.quarantine || 0} quarantined.`;
      await pick();
    } catch (e) { $('rulesSaved').textContent = e.message; }
  }

  function collectText(m) {
    if (m.closed) return 'Closed: takes no new files.';
    if (!m.collecting) return 'Uploads are off: nobody can add files until the coordinator turns collecting on.';
    if (m.full) return 'Uploads open, but the storage limit is reached.';
    return m.autoUpload ? 'Uploads open. Rig auto-upload is on: rigs send each light as it is captured, day or night.'
                        : 'Uploads open from this page. Rig auto-upload is off.';
  }
  function drawCollect(m, x) {
    let el = document.getElementById('collectLine');
    if (!el) { el = document.createElement('p'); el.id = 'collectLine'; el.className = 'row small'; $('curLine').before(el); }
    const sw = (key, label, on) => `<label class="inline small"><input type="checkbox" data-k="${key}"${on ? ' checked' : ''}> ${label}: ${on ? 'On' : 'Off'}</label>`;
    el.innerHTML = `<span class="muted">${esc(collectText(m))}</span>` + (m.owned && !m.closed
      ? ' ' + sw('collectFiles', 'Collecting files', !!m.collecting) + ' ' + sw('autoUpload', 'Rig auto-upload', !!m.autoUpload) : '');
    el.querySelectorAll('input[data-k]').forEach((box) => box.addEventListener('change', async () => {
      box.disabled = true;
      try {
        const r = await api(`/api/v1/projects/${encodeURIComponent(m.id)}/collecting`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ [box.dataset.k]: box.checked }) });
        m.collecting = r.collecting; m.autoUpload = r.autoUpload; x.collecting = r.collecting;
        drawCurrent(x); drawSwitcher(); drawCollect(m, x);
      } catch (e) { alert(e.message); box.checked = !box.checked; box.disabled = false; }
    }));
  }
  function myLine(m) {
    const t = m.myTelescopes || {};
    const parts = [];
    if (t.sessions) parts.push(`${Number(t.hours).toFixed(1)} h from your telescopes`);
    if (m.myFiles) parts.push(`${m.myFiles.toLocaleString()} files uploaded (${mb(m.myBytes)})`);
    return parts.length ? ' · you have ' + parts.join(' and ') : '';
  }
  function drawMine(listing) {
    const my = listing.files.filter((f) => f.user === listing.me);
    const c = listing.myLights || {};
    const lights = my.filter((f) => f.kind === 'light').length;
    const cal = my.length - lights;
    const counts = {};
    for (const f of my) counts[f.kind] = (counts[f.kind] || 0) + 1;
    const parts = [`${c.ready || 0} ready`, `${c.pending || 0} pending`];
    if (c.quarantine) parts.push(`${c.quarantine} quarantined`);
    if (c['']) parts.push(`${c['']} not checked`);
    const t = listing.myTelescopes || {};
    const sum = [];
    if (t.sessions) sum.push(`${Number(t.hours).toFixed(1)} h from your telescopes`);
    if (my.length) sum.push(parts.join(' · ') + ` · ${cal} calibration · ${mb(my.reduce((x, f) => x + f.bytes, 0))} uploaded`);
    show('sec-mine', sum.length ? sum.join(' · ') : 'nothing yet');
    $('mineScopes').innerHTML = t.sessions ? `<p>Your telescopes (N.I.N.A. / Starfront): <b>${Number(t.hours).toFixed(1)} h</b> accepted over ${t.sessions} session${t.sessions === 1 ? '' : 's'} from ${t.telescopes} telescope${t.telescopes === 1 ? '' : 's'}.</p>
      <div class="scroll"><table><thead><tr><th>Filter</th><th class="num">Hours</th><th class="num">Sessions</th></tr></thead><tbody>${(t.filters || []).map((f) => `<tr><td>${esc(({ H: 'Ha', O: 'OIII', S: 'SII' })[f.filter] || f.filter)}</td><td class="num">${Number(f.hours).toFixed(1)} h</td><td class="num">${f.sessions}</td></tr>`).join('')}</tbody></table></div>` : '';
    $('mineNote').textContent = my.length ? `Uploaded here: ${kindSummary(counts)}. Lights: only ready ones go into the stack.` : (t.sessions ? 'No files uploaded through this page yet; your telescopes\' reports are above.' : 'Nothing contributed to this collaboration yet.');
    // One folding table per kind, so a season of lights does not bury the calibration.
    const order = ['light', 'flat', 'dark', 'bias', 'darkflat'];
    const kinds = order.filter((k) => counts[k]).concat(Object.keys(counts).filter((k) => !order.includes(k)));
    drawPicker('mineKinds', my, false);
    // What is holding lights back, opened by itself when something is.
    const gaps = listing.gaps || [];
    const needed = show('sec-needed');
    needed.hidden = !my.length;
    $('myStatus').textContent = my.length ? lightLine(c) : '';
    $('gapBox').hidden = !gaps.length;
    $('gaps').innerHTML = gaps.map((g) => `<li>${pill(g.status)} ${esc(g.reason)} <span class="muted">(${g.files} file${g.files === 1 ? '' : 's'})</span></li>`).join('');
    const held = (c.pending || 0) + (c.quarantine || 0);
    $('neededTitle').textContent = held ? 'Still needed' : 'Lights status';
    needed.querySelector('.sum').textContent = held
      ? `${s(c.pending || 0, 'light')} pending` + (c.quarantine ? ` · ${c.quarantine} quarantined` : '')
      : (lights ? `all ${lights} ready` : 'no lights yet');
    const r = remembered();
    if (c.pending && !('sec-needed' in r && r['sec-needed'] === false)) setOpen(needed, true);
  }

  // Tick-box tables with bulk remove, for your own files and (coordinator) everyone else's.
  function drawPicker(boxId, files, others) {
    const box = $(boxId);
    const order = ['light', 'flat', 'dark', 'bias', 'darkflat'];
    const counts = {};
    for (const f of files) counts[f.kind] = (counts[f.kind] || 0) + 1;
    const kinds = order.filter((k) => counts[k]).concat(Object.keys(counts).filter((k) => !order.includes(k)));
    const key = (f) => JSON.stringify([f.user, f.kind, f.name]);
    const byKey = {};
    files.forEach((f) => { byKey[key(f)] = f; });
    const opts = (field, title) => {
      const vals = [...new Set(files.map((f) => f[field]).filter(Boolean))].sort((a, b) => (Number(a) - Number(b)) || String(a).localeCompare(String(b)));
      return vals.length ? `<optgroup label="${title}">${vals.map((v) => `<option value="${field}:${esc(v)}">${title} ${esc(v)}</option>`).join('')}</optgroup>` : '';
    };
    const uploaders = others ? opts('uploader', 'Contributor') : '';
    const shortcut = opts('filter', 'Filter') + opts('panel', 'Panel') + opts('night', 'Date') + uploaders;
    const bar = files.length ? `<div class="pickbar small"><span class="muted" data-count>0 selected</span>
      ${shortcut ? `<select data-by><option value="">Select by…</option>${shortcut}</select>` : ''}
      <button type="button" class="rm" data-clear disabled>Clear</button>
      <button type="button" class="rm" data-rm disabled>Remove selected (0)</button></div>` : '';
    box.innerHTML = bar + kinds.map((k) => {
      const rows = files.filter((f) => f.kind === k);
      const st = {};
      for (const f of rows) st[f.status || ''] = (st[f.status || ''] || 0) + 1;
      const stText = Object.entries(st).map(([x, n]) => `${n} ${(LABELS[x] || x).toLowerCase()}`).join(' · ');
      return `<details class="kind" data-kind="${esc(k)}"><summary>${esc(label(k))} <span class="muted small">(${rows.length}) ${esc(stText)}</span></summary>
        <div class="scroll"><table><thead><tr><th><input type="checkbox" data-all="${esc(k)}" title="Select all ${esc(label(k)).toLowerCase()}" aria-label="Select all"></th><th>File</th>${others ? '<th>Contributor</th>' : ''}<th>Telescope</th><th class="num">Size</th><th>Status</th><th>Received</th><th></th></tr></thead><tbody>${rows.map((f) => `<tr><td><input type="checkbox" data-pick="${esc(key(f))}" aria-label="Select ${esc(f.name)}"></td><td>${esc(f.name)}</td>${others ? `<td>${esc(f.uploader)}</td>` : ''}<td>${esc(f.telescope)}</td><td class="num">${mb(f.bytes)}</td><td>${statusCell(f)}</td><td>${when(f.received)}</td>
        <td><button class="x" data-del="${esc(key(f))}" title="Take it off the server" aria-label="Remove ${esc(f.name)}">✕</button></td></tr>`).join('')}</tbody></table></div></details>`;
    }).join('');
    const picks = () => [...box.querySelectorAll('[data-pick]')];
    const chosen = () => picks().filter((c) => c.checked).map((c) => byKey[c.dataset.pick]);
    const sync = () => {
      const sel = chosen();
      const size = sel.reduce((x, f) => x + f.bytes, 0);
      const c = box.querySelector('[data-count]'); if (c) c.textContent = sel.length ? `${sel.length} selected · ${mb(size)}` : '0 selected';
      const b = box.querySelector('[data-rm]'); if (b) { b.disabled = !sel.length; b.textContent = `Remove selected (${sel.length})`; }
      const cl = box.querySelector('[data-clear]'); if (cl) cl.disabled = !sel.length;
      box.querySelectorAll('[data-all]').forEach((a) => {
        const mine = picks().filter((c) => byKey[c.dataset.pick].kind === a.dataset.all);
        const n = mine.filter((c) => c.checked).length;
        a.checked = n > 0 && n === mine.length; a.indeterminate = n > 0 && n < mine.length;
      });
    };
    const removeList = async (list) => {
      const size = list.reduce((x, f) => x + f.bytes, 0);
      const who = new Set(list.map((f) => f.uploader));
      if (!confirm(`Remove ${list.length} file${list.length === 1 ? '' : 's'} (${mb(size)})${others ? ` from ${[...who].join(', ')}` : ''} from the server? This cannot be undone.`)) return;
      try {
        const r = await api(`/api/v1/projects/${encodeURIComponent(state.project.id)}/files/remove`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ files: list.map((f) => ({ name: f.name, kind: f.kind, user: f.user })) }) });
        if (r.failed) alert(`${r.removed} removed, ${r.failed} not removed:\n` + r.results.filter((x) => !x.ok).slice(0, 10).map((x) => `${x.name}: ${x.error}`).join('\n'));
        await pick();
      } catch (e) { alert(e.message); }
    };
    picks().forEach((c) => c.addEventListener('change', sync));
    box.querySelectorAll('[data-all]').forEach((a) => a.addEventListener('change', () => {
      picks().filter((c) => byKey[c.dataset.pick].kind === a.dataset.all).forEach((c) => { c.checked = a.checked; });
      sync();
    }));
    const by = box.querySelector('[data-by]');
    if (by) by.addEventListener('change', () => {
      const v = by.value; by.value = '';
      if (!v) return;
      const i = v.indexOf(':'); const field = v.slice(0, i); const val = v.slice(i + 1);
      picks().forEach((c) => { if (String(byKey[c.dataset.pick][field]) === val) c.checked = true; });
      box.querySelectorAll('details.kind').forEach((d) => { if (d.querySelector('[data-pick]:checked')) d.open = true; });
      sync();
    });
    const cl = box.querySelector('[data-clear]');
    if (cl) cl.addEventListener('click', () => { picks().forEach((c) => { c.checked = false; }); sync(); });
    const rm = box.querySelector('[data-rm]');
    if (rm) rm.addEventListener('click', () => removeList(chosen()));
    box.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', () => removeList([byKey[b.dataset.del]])));
  }

  function drawOthers(listing) {
    const sec = $('sec-others');
    const rest = listing.owned ? listing.files.filter((f) => f.user !== listing.me) : [];
    sec.hidden = !rest.length;
    if (!rest.length) { $('othersKinds').innerHTML = ''; return; }
    const people = new Set(rest.map((f) => f.user)).size;
    sec.querySelector('.sum').textContent = `${rest.length.toLocaleString()} files from ${people} contributor${people === 1 ? '' : 's'} · ${mb(rest.reduce((x, f) => x + f.bytes, 0))}`;
    drawPicker('othersKinds', rest, true);
  }

  function take(fileList, kind) {
    if (!state.project) return;
    const files = Array.from(fileList).filter((f) => allowed(f.name));
    const skipped = fileList.length - files.length;
    if (!files.length) { alert('None of those are FITS or XISF files.'); return; }
    for (const f of files) state.queue.push({ file: f, kind: kind, state: 'waiting', sent: 0 });
    if (skipped) $('progressText').textContent = `${skipped} file${skipped === 1 ? '' : 's'} left out (not FITS or XISF).`;
    drawQueue();
    run();
  }

  function drawQueue() {
    $('queue').hidden = !state.queue.length;
    const body = $('queue').querySelector('tbody');
    body.innerHTML = state.queue.map((q) => `<tr class="${q.state === 'done' || q.state === 'already' ? 'done' : ''}"><td>${esc(q.file.name)}</td><td>${esc(label(q.kind))}</td><td class="num">${mb(q.file.size)}</td>
      <td class="${q.state === 'failed' ? 'no' : (q.state === 'done' || q.state === 'already' ? 'ok' : 'wait')}">${esc(q.text || q.state)}${q.state === 'done' || q.state === 'already' ? ' ' + pill(q.status) + whyList(q.status, q.reasons) : ''}</td></tr>`).join('');
    const total = state.queue.reduce((s, q) => s + q.file.size, 0);
    const sent = state.queue.reduce((s, q) => s + (q.state === 'done' || q.state === 'already' ? q.file.size : q.sent), 0);
    $('allBar').hidden = !state.queue.length;
    $('allBar').firstElementChild.style.width = total ? (100 * sent / total).toFixed(1) + '%' : '0';
    const left = state.queue.filter((q) => q.state === 'waiting' || q.state === 'sending').length;
    $('progress').hidden = false;
    if (left) $('progressText').textContent = `${left} to go · ${mb(sent)} of ${mb(total)}`;
    else if (state.queue.length) {
      const n = (st) => state.queue.filter((q) => q.kind === 'light' && (q.state === 'done' || q.state === 'already') && q.status === st).length;
      $('progressText').textContent = `All done: ${state.queue.filter((q) => q.state === 'done').length} uploaded, ${state.queue.filter((q) => q.state === 'already').length} already here, ${state.queue.filter((q) => q.state === 'failed').length} failed. Lights in this batch: ${n('ready')} ready, ${n('pending')} pending calibration, ${n('quarantine')} quarantined (reasons in each row, and under Still needed).`;
    }
  }

  // A file of mine already on the server at the same name and size (and kind,
  // when the box said one) is not sent again: the server would skip it anyway.
  function alreadyHere(q) {
    const l = state.listing;
    if (!l || !state.project || l.project !== state.project.id) return null;
    return (l.files || []).find((f) => f.user === l.me && f.name === q.file.name && f.bytes === q.file.size
      && (!q.kind || f.kind === q.kind)) || null;
  }

  function send(q, attempt) {
    return new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      // At least 2 minutes, plus about 10 minutes per 50 MB: a stalled
      // request becomes a retry instead of 'sending' for ever.
      xhr.timeout = 120000 + Math.ceil(q.file.size / (50 * 1024 * 1024) * 600000);
      xhr.ontimeout = () => resolve(false);
      const tel = encodeURIComponent($('telescope').value.trim());
      xhr.open('PUT', `/api/v1/projects/${encodeURIComponent(state.project.id)}/files/${encodeURIComponent(q.file.name)}?telescope=${tel}&kind=${encodeURIComponent(q.kind)}`);
      xhr.upload.onprogress = (e) => { q.sent = e.loaded; drawQueue(); };
      xhr.onload = () => {
        let body = {};
        try { body = JSON.parse(xhr.responseText || '{}'); } catch (e) { /* not json */ }
        if (xhr.status >= 200 && xhr.status < 300) {
          q.state = body.already ? 'already' : 'done';
          const f = body.file || {};
          q.status = f.status || ''; q.reasons = f.reasons || [];
          q.text = (body.already ? 'already here' : 'uploaded');
          const m = body.moved || {};
          if (q.kind !== 'light' && m.ready) q.reasons = q.reasons.concat([`${m.ready} of your lights became ready`]);
          resolve(true);
        }
        else if (xhr.status === 507 || xhr.status === 400 || xhr.status === 409 || xhr.status === 413) { q.state = 'failed'; q.text = body.detail || xhr.statusText; resolve(true); }
        else resolve(false);
      };
      xhr.onerror = () => resolve(false);
      xhr.send(q.file);
    });
  }

  async function run() {
    if (state.running) return;
    state.running = true;
    try {
      for (const q of state.queue) {
        if (q.state !== 'waiting') continue;
        const have = alreadyHere(q);
        if (have) { q.state = 'already'; q.text = 'already here'; q.status = have.status || ''; q.reasons = have.reasons || []; drawQueue(); continue; }
        q.state = 'sending'; q.text = 'sending'; drawQueue();
        let ok = false;
        for (let attempt = 1; attempt <= 3 && !ok; attempt++) {
          ok = await send(q, attempt);
          if (!ok) { q.text = `retrying (${attempt})`; drawQueue(); await new Promise((r) => setTimeout(r, 3000 * attempt)); }
        }
        if (!ok) { q.state = 'failed'; q.text = 'could not reach the server; drop the folder again later'; }
        drawQueue();
      }
      await pick();
    } finally {
      state.running = false;
    }
  }

  $('collabSel').addEventListener('change', (e) => { if (e.target.value) choose(e.target.value, true).catch((x) => alert(x.message)); });
  $('saveRules').addEventListener('click', () => saveRules());
  // One box per kind of frame. Its buttons open the shared pickers with the
  // box's kind remembered; a drop on it takes the box's kind.
  $('files').addEventListener('change', (e) => { take(e.target.files, state.picking); e.target.value = ''; });
  $('folder').addEventListener('change', (e) => { take(e.target.files, state.picking); e.target.value = ''; });
  document.querySelectorAll('.zone').forEach((zone) => {
    const kind = zone.dataset.kind;
    zone.querySelectorAll('[data-pick]').forEach((b) => b.addEventListener('click', () => {
      state.picking = kind;
      $(b.dataset.pick).click();
    }));
    zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('over'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('over'));
    zone.addEventListener('drop', async (e) => {
      e.preventDefault(); zone.classList.remove('over');
      const items = Array.from(e.dataTransfer.items || []);
      const files = [];
      const walk = async (entry) => {
        if (entry.isFile) { await new Promise((r) => entry.file((f) => { files.push(f); r(); }, r)); }
        else if (entry.isDirectory) {
          const reader = entry.createReader();
          const all = [];
          for (;;) { const batch = await new Promise((r) => reader.readEntries(r, () => r([]))); if (!batch.length) break; all.push(...batch); }
          for (const child of all) await walk(child);
        }
      };
      if (items.length && items[0].webkitGetAsEntry) { for (const it of items) { const entry = it.webkitGetAsEntry(); if (entry) await walk(entry); } }
      else files.push(...e.dataTransfer.files);
      take(files, kind);
    });
  });
  window.addEventListener('beforeunload', (e) => { if (state.running) { e.preventDefault(); e.returnValue = ''; } });

  load().catch((e) => { document.body.insertAdjacentHTML('beforeend', `<p class="no">${esc(e.message)}</p>`); });
})();
</script>
</main></body></html>
"""
