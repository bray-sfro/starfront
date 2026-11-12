"""Progress of the coordinator's processing job (stacking, rendering), per collaboration.

Stacking and rendering run on the coordinator's own machine, not here. That
machine says how far it has got with `PUT /api/v1/releases/<project>/job`,
and the files page shows it to everybody from `GET` on the same path:

    <releases>/<project id>/JOB.json      the last status sent, as cleaned up here

Who may send: the server owner (admin token or an owner's Discord sign-in),
the collaboration's owner, or the job token in `job-token.txt` beside the
database - a credential that can do nothing but this, so a stacking box does
not need the admin token. The GET is public and holds nothing secret: only
the fields below survive, each cut to size.

A running job that has not been heard from for STALE_SECONDS is reported
`stale`; the page says so instead of a progress bar that never moves.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from server.personal import hidden_from

STATUSES = ("idle", "running", "failed", "done")
STEP_STATES = ("pending", "running", "done", "failed", "skipped")
STALE_SECONDS = 15 * 60
MAX_STEPS = 30
FILE = "JOB.json"
#: A request for a new stack, from the "Start new stack" button. The stacking
#: box polls it over ssh, claims it and runs the stack; see STACK-BUTTON.md.
REQUEST = "STACK_REQUEST.json"
REQUEST_STATES = ("queued", "claimed", "running", "done", "failed", "cancelled")
ACTIVE = ("queued", "claimed", "running")
#: A request nobody has touched for this long no longer blocks a new one.
REQUEST_EXPIRE_SECONDS = 6 * 3600


def _text(value: Any, size: int) -> str:
    return " ".join(str(value if value is not None else "").split())[:size]


def _when(value: Any) -> float | None:
    """Epoch seconds, from a number or an ISO 8601 string; None if neither."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return v if 0 < v < 1e11 else None
    try:
        from datetime import datetime
        s = str(value).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def clean(body: Any, now: float) -> dict[str, Any]:
    """The status as stored: known fields only, checked and cut to size. 400 on nonsense."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="send a JSON object")
    status = str(body.get("status") or "").lower()
    if status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {', '.join(STATUSES)}")
    steps_in = body.get("steps") or []
    if not isinstance(steps_in, list) or len(steps_in) > MAX_STEPS:
        raise HTTPException(status_code=400, detail=f"steps must be a list of at most {MAX_STEPS}")
    steps = []
    for s in steps_in:
        if isinstance(s, str):
            s = {"name": s}
        if not isinstance(s, dict) or not _text(s.get("name"), 80):
            raise HTTPException(status_code=400, detail="each step needs a name")
        state = str(s.get("state") or "pending").lower()
        if state not in STEP_STATES:
            raise HTTPException(status_code=400, detail=f"a step's state must be one of {', '.join(STEP_STATES)}")
        step = {"name": _text(s.get("name"), 80), "state": state}
        if s.get("detail"):
            step["detail"] = _text(s.get("detail"), 120)
        steps.append(step)
    current = body.get("step")
    if isinstance(current, str) and not current.isdigit():
        names = [s["name"] for s in steps]
        current = names.index(current) if current in names else None
    elif current is not None:
        try:
            current = int(current)
        except (TypeError, ValueError):
            current = None
    if current is not None and not 0 <= current < max(1, len(steps)):
        current = None
    percent = body.get("percent")
    if percent is not None:
        try:
            percent = max(0.0, min(100.0, float(percent)))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="percent must be a number") from None
        percent = round(percent, 1)
    version = _text(body.get("version"), 12)
    return {"status": status, "version": version, "title": _text(body.get("title"), 80),
            "steps": steps, "step": current, "percent": percent,
            "message": _text(body.get("message"), 300),
            "started": _when(body.get("started")), "eta": _when(body.get("eta")),
            "updated": now}


class Jobs:
    def __init__(self, app: FastAPI, store: Any, folder: Path,
                 who: Callable[[Request], dict[str, Any] | None],
                 owns_project: Callable[[dict[str, Any], dict[str, Any]], bool],
                 token_file: Path | None = None,
                 latest: Callable[[str], str] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.store, self.folder, self.who, self.owns_project = store, Path(folder), who, owns_project
        self.token_file, self.latest, self.clock = token_file, latest or (lambda pid: ""), clock
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._routes(app)

    def _job_token_ok(self, request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer ") or self.token_file is None:
            return False
        try:
            want = self.token_file.read_text("utf-8").strip()
        except OSError:
            return False
        return len(want) >= 24 and secrets.compare_digest(auth[7:].strip(), want)

    def read(self, project_id: str) -> dict[str, Any]:
        now = self.clock()
        hit = self._cache.get(project_id)
        if hit and now - hit[0] < 2.0:
            job = dict(hit[1])
        else:
            try:
                job = json.loads((self.folder / project_id / FILE).read_text("utf-8"))
                if not isinstance(job, dict):
                    job = {}
            except (OSError, ValueError):
                job = {}
            self._cache[project_id] = (now, job)
            job = dict(job)
        job.setdefault("status", "idle")
        updated = job.get("updated")
        job["stale"] = bool(job["status"] == "running" and isinstance(updated, (int, float))
                            and now - updated > STALE_SECONDS)
        job["published"] = self.latest(project_id) or ""
        job["now"] = round(now, 1)
        return job

    def read_request(self, project_id: str) -> dict[str, Any]:
        try:
            req = json.loads((self.folder / project_id / REQUEST).read_text("utf-8"))
            return req if isinstance(req, dict) else {}
        except (OSError, ValueError):
            return {}

    def stack_view(self, project_id: str) -> dict[str, Any]:
        """The request as the page shows it (no names), and whether it blocks a new one."""
        now = self.clock()
        req = self.read_request(project_id)
        state = str(req.get("state") or "")
        touched = max(float(req.get(k) or 0) for k in ("requested", "updated", "claimed")) if req else 0.0
        active = state in ACTIVE and now - touched < REQUEST_EXPIRE_SECONDS
        job = self.read(project_id)
        busy = active or (job["status"] == "running" and not job["stale"])
        view = {"state": state if req else "", "active": active, "busy": busy,
                "requested": req.get("requested"), "updated": req.get("updated"),
                "message": _text(req.get("message"), 300), "id": _text(req.get("id"), 40),
                "job": job["status"], "stale": job["stale"], "now": round(now, 1)}
        return view

    def _write_request(self, project_id: str, req: dict[str, Any]) -> None:
        folder = self.folder / project_id
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (REQUEST + ".tmp")
        tmp.write_text(json.dumps(req, indent=1), "utf-8")
        os.replace(tmp, folder / REQUEST)

    def _owner(self, request: Request, project: dict[str, Any]) -> dict[str, Any] | None:
        try:
            who = self.who(request)
        except HTTPException:
            who = None
        if who is None or who.get("kind") == "agent" or not self.owns_project(who, project):
            return None
        return who

    def _visible(self, project_id: str, request: Request) -> dict[str, Any]:
        """The project, or 404: also for a personal project (server/personal.py)
        asked about by anybody but its owner."""
        project = self.store.project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="no such collaboration")
        if project.get("scope") == "personal":
            try:
                who = self.who(request)
            except HTTPException:
                who = None
            # The processing machine reports progress on the job token.
            if hidden_from(project, who, self.owns_project) and not self._job_token_ok(request):
                raise HTTPException(status_code=404, detail="no such collaboration")
        return project

    def _routes(self, app: FastAPI) -> None:
        @app.get("/api/v1/releases/{project_id}/stack")
        def get_stack(project_id: str, request: Request) -> JSONResponse:
            """The stack button's state: whether you may press it, and the last request."""
            project = self._visible(project_id, request)
            view = self.stack_view(project_id)
            view["canStack"] = self._owner(request, project) is not None
            return JSONResponse(view, headers={"Cache-Control": "private, no-store"})

        @app.post("/api/v1/releases/{project_id}/stack")
        def post_stack(project_id: str, request: Request) -> JSONResponse:
            """The owner asks for a new stack. One at a time: 409 while one is queued or running."""
            project = self._visible(project_id, request)
            who = self._owner(request, project)
            if who is None:
                raise HTTPException(status_code=403, detail="only the collaboration's owner starts a stack")
            with self._lock:
                view = self.stack_view(project_id)
                if view["busy"]:
                    raise HTTPException(status_code=409, detail="a stack is already queued or running")
                now = self.clock()
                req = {"id": secrets.token_hex(6), "state": "queued", "project": project_id,
                       "requested": now, "updated": now, "by": _text(who.get("id"), 40),
                       "byName": _text(who.get("name"), 80), "message": "waiting for the stacking machine"}
                self._write_request(project_id, req)
            view = self.stack_view(project_id)
            view["canStack"] = True
            return JSONResponse(view, status_code=202, headers={"Cache-Control": "private, no-store"})

        @app.delete("/api/v1/releases/{project_id}/stack")
        def cancel_stack(project_id: str, request: Request) -> JSONResponse:
            """The owner takes back a request the stacking machine has not picked up yet."""
            project = self._visible(project_id, request)
            if self._owner(request, project) is None:
                raise HTTPException(status_code=403, detail="only the collaboration's owner cancels a stack")
            with self._lock:
                req = self.read_request(project_id)
                if req.get("state") != "queued":
                    raise HTTPException(status_code=409, detail="nothing queued to cancel (a started stack cannot be cancelled here)")
                req.update(state="cancelled", updated=self.clock(), message="cancelled")
                self._write_request(project_id, req)
            view = self.stack_view(project_id)
            view["canStack"] = True
            return JSONResponse(view, headers={"Cache-Control": "private, no-store"})

        @app.get("/api/v1/releases/{project_id}/job")
        def get_job(project_id: str, request: Request) -> JSONResponse:
            """What the coordinator's processing job is doing. Public, cheap, cached a few seconds."""
            project = self._visible(project_id, request)
            private = project.get("scope") == "personal"
            return JSONResponse(self.read(project_id), headers={
                "Cache-Control": "private, no-store" if private else "public, max-age=5"})

        @app.put("/api/v1/releases/{project_id}/job")
        async def put_job(project_id: str, request: Request) -> dict[str, Any]:
            project = self._visible(project_id, request)
            if not self._job_token_ok(request):
                try:
                    who = self.who(request)
                except HTTPException:
                    who = None
                if who is None or who.get("kind") == "agent" or not self.owns_project(who, project):
                    raise HTTPException(status_code=403, detail="only the collaboration's owner or the job token sets this")
            try:
                body = await request.json()
            except ValueError:
                raise HTTPException(status_code=400, detail="send JSON") from None
            job = clean(body, self.clock())
            folder = self.folder / project_id
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / (FILE + ".tmp")
            tmp.write_text(json.dumps(job, indent=1), "utf-8")
            os.replace(tmp, folder / FILE)
            self._cache.pop(project_id, None)
            return self.read(project_id)
