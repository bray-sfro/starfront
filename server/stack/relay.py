"""The relay: frame bytes waiting on this server for the hub to fetch.

A contributor's frame lands in `STARFRONT_DATA/stack/relay` (frames.py). The
hub, which stacks it, cannot be reached from here, so it asks (with its hub
token, like every call in server/hubs.py; these are the Starfront hub API, in
plain `{"detail"}` errors, not the Collab envelope of `/stack/api`):

* `GET  /api/v1/hubs/{id}/frames?state=uploaded&limit=N[&kind=library]`:
  frames of the projects bound to THIS hub that are `uploaded` and not yet
  relayed, oldest first. `kind=library` lists the rig-library frames instead
  (`ready`, not yet relayed to this hub). Two lists, not one mixed list,
  because the hub treats them differently (a library frame belongs to a
  user's rig, not to a project, and feeds no stack) and their ids never mix.
  A library frame is this hub's when its owner is a member of a project bound
  to the hub.
* `GET  /api/v1/hubs/{id}/frames/{frame_id}/bytes[?kind=library]`: the file,
  with `Range` (206 and `Content-Range`), so a hub can resume.
* `POST /api/v1/hubs/{id}/frames/{frame_id}/ack {"sha256"}`: the hub has the
  bytes and they hash to what we hold (else 409). The frame is marked relayed
  (a timestamp), which takes it out of the list. No feed row: the hub caused
  it. Repeating a good ack is harmless.

Another hub's frame is 404, never 403: ids are not worth confirming.

Retention (`sweep`, run by the `stack-sweep` thread): the file of a relayed
frame is deleted `STARFRONT_STACK_RELAY_KEEP_DAYS` days after its ack (default
7; 0 deletes at the ack itself), and a withdrawn frame's file at its
`raw_delete_after` (D004: raw is purged, masters are the hub's and stay). The
row stays either way, with `relay_deleted` in its document, so the dashboard
keeps its status and metadata.

`collectFiles`: a collaboration that collects files wants the coordinator to
see each frame on `/files`. `collect` hard-links the relay file into
`files/<collaboration>/<starfront person>/<kind>/<name>` (copying when the two
folders are on different volumes) and registers it, at upload completion
(frames.py calls it) and again at ack as a backstop for a project that turned
collecting on later. A hard link costs no space and the collected file
survives the relay copy's deletion; both are the same bytes at the same time.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from calendar import timegm
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .. import files as files_mod
from . import deletion
from ..auth import setting as server_setting

KEEP_DAYS_DEFAULT = 7
LIST_MAX = 500
KIND_OF_TYPE = {"light": "light", "flat": "flat", "dark": "dark", "bias": "bias",
                "dark_flat": "darkflat", "master_dark": "dark", "master_flat": "flat",
                "master_bias": "bias", "master_dark_flat": "darkflat"}


def keep_seconds() -> float:
    """How long a relayed file is kept, read each time so a change takes effect."""
    try:
        days = float(server_setting("STACK_RELAY_KEEP_DAYS", str(KEEP_DAYS_DEFAULT)))
    except ValueError:
        days = KEEP_DAYS_DEFAULT
    return max(0.0, days) * 86400


def iso_epoch(text: Any) -> float | None:
    try:
        return float(timegm(time.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


class Relay:
    def __init__(self, store: Any, hubs: Any, front: FastAPI, files: Any) -> None:
        self.store = store
        self.hubs = hubs
        self.frames = front.state.frames
        self.up = front.state.uploads
        self.files = files

    # -- which rows are a hub's -----------------------------------------------
    def frame_of(self, hub: dict[str, Any], frame_id: str) -> dict[str, Any] | None:
        row = self.frames.row(frame_id)
        if row is None:
            return None
        project = self.store.stack_project(row["project_id"])
        return row if project is not None and project["hub"] == hub["id"] else None

    def library_of(self, hub: dict[str, Any], frame_id: str) -> dict[str, Any] | None:
        rows = self.store._all(self._library_sql("AND l.frame_id = ?"), hub["id"], frame_id)
        if not rows:
            return None
        row = self.store._one("SELECT * FROM stack_library WHERE frame_id = ?", frame_id)
        row["doc"] = json.loads(row["doc"] or "{}")
        return row

    @staticmethod
    def _library_sql(extra: str = "") -> str:
        """Library frames a hub may have: the owner is a member of a project bound to it."""
        return ("SELECT l.frame_id FROM stack_library l WHERE l.status IN ('ready', 'withdrawn')"
                " AND l.user_id IN (SELECT m.user_id FROM stack_members m JOIN stack_projects p"
                " ON p.project_id = m.project_id WHERE p.hub = ?"
                " AND COALESCE(json_extract(m.doc, '$.status'), '') <> 'removed') "
                + extra + " ORDER BY l.frame_id")

    # -- records ---------------------------------------------------------------
    def frame_record(self, row: dict[str, Any]) -> dict[str, Any]:
        out = self.frames.wire(row)
        d = row["doc"]
        out.update(kind="frame", user_id=row["user_id"], header=d.get("header") or {},
                   stripped=bool(d.get("stripped")), rig_id=d.get("rig_id"))
        return out

    @staticmethod
    def library_record(row: dict[str, Any]) -> dict[str, Any]:
        d = row["doc"]
        return {"kind": "library", "frame_id": row["frame_id"], "user_id": row["user_id"],
                "rig_id": row["rig_id"], "frame_type": d.get("frame_type"),
                "filename": d.get("filename", ""), "size": d.get("size", 0),
                "sha256": d.get("sha256", ""), "header": d.get("header") or {},
                "stripped": True, "uploaded_at": d.get("uploaded_at")}

    # -- the file --------------------------------------------------------------
    def file_of(self, row: dict[str, Any]) -> Path:
        path = self.up.final_file(row["path"])
        if row["doc"].get("relay_deleted") or not path.is_file():
            raise HTTPException(status_code=404, detail="the relay copy is gone")
        return path

    # -- collectFiles ------------------------------------------------------------
    def collect(self, project: dict[str, Any], row: dict[str, Any]) -> bool:
        """Put an uploaded frame in the coordinator's `/files` area when the
        collaboration collects files. Idempotent. True when it is there."""
        if self.files is None or row["status"] in ("uploading", "rejected", "withdrawn"):
            return False
        sf_project = self.store.project(project["sf_project"])
        if sf_project is None or not self.files.collecting(sf_project):
            return False
        mapped = self.store._one("SELECT user_id FROM stack_identities WHERE collab_user_id = ?",
                                 row["user_id"])
        source = self.up.final_file(row["path"])
        if mapped is None or not source.is_file():
            return False
        d = row["doc"]
        name = files_mod.clean_name(d.get("filename", "")) or files_mod.clean_name(
            row["frame_id"] + Path(row["path"]).suffix)
        kind = KIND_OF_TYPE.get(d.get("frame_type", "light"), "light")
        who, size = mapped["user_id"], int(row["size"])
        have = self.store.file(project["sf_project"], who, name, kind)
        if have is not None and int(have["bytes"]) != size:
            # Another file with this name: keep both, as the frame's own id tells them apart.
            name = files_mod.clean_name(row["frame_id"] + "_" + name) or name
            have = self.store.file(project["sf_project"], who, name, kind)
        target = self.files.path_of(project["sf_project"], who, name, kind)
        if have is None or not target.is_file() or target.stat().st_size != size:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + ".part")
            try:
                temp.unlink()
            except OSError:
                pass
            try:
                os.link(source, temp)
            except OSError:
                shutil.copyfile(source, temp)
            os.replace(temp, target)
        self.store.add_file(os.urandom(8).hex(), project["sf_project"], who, name, size, "", kind)
        return True

    # -- ack ---------------------------------------------------------------------
    def ack_frame(self, hub: dict[str, Any], row: dict[str, Any], sha: str) -> None:
        if sha != row["sha256"]:
            raise HTTPException(status_code=409, detail="the sha256 differs from the one stored")
        now = time.time()
        if not row["relayed"]:
            self.store._run("UPDATE stack_frames SET relayed = ?, rev = rev + 1"
                            " WHERE frame_id = ? AND relayed = 0", now, row["frame_id"])
        project = self.store.stack_project(row["project_id"])
        try:
            self.collect(project, row)
        except OSError:
            pass            # the hub has its copy; the coordinator's is not worth failing the ack
        if keep_seconds() == 0:
            self.drop_frame(row["frame_id"])

    def ack_library(self, hub: dict[str, Any], row: dict[str, Any], sha: str) -> None:
        if sha != row["doc"].get("sha256"):
            raise HTTPException(status_code=409, detail="the sha256 differs from the one stored")
        with self.up.lock:
            fresh = self.store._one("SELECT doc FROM stack_library WHERE frame_id = ?", row["frame_id"])
            acks = json.loads(fresh["doc"] or "{}").get("relayed")
            acks = dict(acks) if isinstance(acks, dict) else {}
            acks.setdefault(hub["id"], time.time())
            self.store._run("UPDATE stack_library SET doc = json_set(doc, '$.relayed', json(?)),"
                            " rev = rev + 1 WHERE frame_id = ?", json.dumps(acks), row["frame_id"])
        if keep_seconds() == 0:
            self.sweep_library(time.time())

    # -- retention -----------------------------------------------------------------
    def drop_frame(self, frame_id: str) -> bool:
        """Delete a frame's relay file, keep the row. True when a file was removed."""
        with self.up.lock:
            row = self.frames.row(frame_id)
            if row is None or not row["path"] or row["doc"].get("relay_deleted"):
                return False
            removed = True
            try:
                self.up.final_file(row["path"]).unlink()
            except FileNotFoundError:
                removed = False
            except OSError:
                return False
            self.store._run("UPDATE stack_frames SET doc = json_set(doc, '$.relay_deleted', ?),"
                            " rev = rev + 1 WHERE frame_id = ?", int(time.time()), frame_id)
            return removed

    def sweep(self, now: float | None = None) -> int:
        """Delete relay files that are due. Returns how many."""
        now = time.time() if now is None else now
        keep = keep_seconds()
        gone = 0
        for r in self.store._all(
                "SELECT frame_id, relayed, status, json_extract(doc, '$.raw_delete_after') AS due"
                " FROM stack_frames WHERE json_extract(doc, '$.relay_deleted') IS NULL"
                " AND path <> '' AND (relayed > 0 OR status = 'withdrawn')"):
            due_at = iso_epoch(r["due"]) if r["status"] == "withdrawn" else None
            if (r["relayed"] > 0 and now - r["relayed"] >= keep) or (due_at is not None and now >= due_at):
                gone += self.drop_frame(r["frame_id"])
        return gone + self.sweep_library(now) + self.purge_deleted(now)

    def purge_deleted(self, now: float) -> int:
        """D004: once a deleted project's 7 days are over, delete the relay bytes
        that are only its own (its frames; library frames of people who are in no
        other live hub project). Rows, `status.json` and `masters/` stay."""
        gone = 0
        for row in deletion.due_projects(self.store, now):
            for r in self.store._all("SELECT frame_id FROM stack_frames WHERE project_id = ?"
                                     " AND path <> ''", row["project_id"]):
                gone += self.drop_frame(r["frame_id"])
            for frame_id in deletion.library_only_for(self.store, row):
                gone += self.drop_library(frame_id)
            doc = dict(row["doc"])
            doc["relay_purged_at"] = deletion.stamp(now)
            self.store.set_stack_project(row["project_id"], row["hub"], doc)
        return gone

    def drop_library(self, frame_id: str) -> bool:
        with self.up.lock:
            row = self.store._one("SELECT * FROM stack_library WHERE frame_id = ?", frame_id)
            if row is None or not row["path"]:
                return False
            removed = True
            try:
                self.up.final_file(row["path"]).unlink()
            except FileNotFoundError:
                removed = False
            except OSError:
                return False
            self.store._run("UPDATE stack_library SET doc = json_set(doc, '$.relay_deleted', ?),"
                            " rev = rev + 1 WHERE frame_id = ?", int(time.time()), frame_id)
            return removed

    def sweep_library(self, now: float) -> int:
        keep = keep_seconds()
        gone = 0
        for r in self.store._all("SELECT frame_id FROM stack_library WHERE status IN ('ready', 'withdrawn')"
                                 " AND path <> '' AND json_extract(doc, '$.relay_deleted') IS NULL"):
            with self.up.lock:
                row = self.store._one("SELECT * FROM stack_library WHERE frame_id = ?", r["frame_id"])
                d = json.loads(row["doc"] or "{}")
                if d.get("relay_deleted"):
                    continue
                due = False
                if row["status"] == "withdrawn":
                    at = iso_epoch(d.get("raw_delete_after"))
                    due = at is not None and now >= at
                else:
                    acks = d.get("relayed") if isinstance(d.get("relayed"), dict) else {}
                    hubs = {h["hub"] for h in self.store._all(
                        "SELECT DISTINCT p.hub AS hub FROM stack_members m JOIN stack_projects p"
                        " ON p.project_id = m.project_id WHERE m.user_id = ? AND p.hub <> ''"
                        " AND COALESCE(json_extract(m.doc, '$.status'), '') <> 'removed'", row["user_id"])}
                    due = bool(acks) and hubs <= set(acks) and now - max(acks.values()) >= keep
                if not due:
                    continue
                removed = True
                try:
                    self.up.final_file(row["path"]).unlink()
                except FileNotFoundError:
                    removed = False
                except OSError:
                    continue
                self.store._run("UPDATE stack_library SET doc = json_set(doc, '$.relay_deleted', ?),"
                                " rev = rev + 1 WHERE frame_id = ?", int(now), r["frame_id"])
                gone += removed
        return gone

    # -- routes --------------------------------------------------------------------
    def mount(self, app: FastAPI) -> None:
        @app.get("/api/v1/hubs/{hub_id}/frames")
        def listing(request: Request, hub_id: str, state: str = Query("uploaded"),
                    kind: str = Query("frame"), limit: int = Query(100, ge=1, le=LIST_MAX)) -> dict[str, Any]:
            hub = self.hubs.hub_as_itself(request, hub_id)
            if kind not in ("frame", "library"):
                raise HTTPException(status_code=400, detail="kind is frame or library")
            if kind == "library":
                out = []
                for r in self.store._all(self._library_sql(), hub["id"]):
                    row = self.store._one("SELECT * FROM stack_library WHERE frame_id = ?", r["frame_id"])
                    row["doc"] = json.loads(row["doc"] or "{}")
                    acks = row["doc"].get("relayed")
                    if (row["status"] != "ready" or row["doc"].get("relay_deleted")
                            or (isinstance(acks, dict) and hub["id"] in acks)):
                        continue
                    out.append(self.library_record(row))
                    if len(out) >= limit:
                        break
                return {"kind": "library", "frames": out}
            if state != "uploaded":
                raise HTTPException(status_code=400, detail="only state=uploaded is listed")
            rows = self.store._all(
                "SELECT f.frame_id FROM stack_frames f JOIN stack_projects p ON p.project_id = f.project_id"
                " WHERE p.hub = ? AND f.status = 'uploaded' AND f.relayed = 0 AND f.path <> ''"
                " ORDER BY f.received, f.frame_id LIMIT ?", hub["id"], limit)
            return {"kind": "frame", "frames": [self.frame_record(self.frames.row(r["frame_id"]))
                                                for r in rows]}

        @app.get("/api/v1/hubs/{hub_id}/frames/{frame_id}/bytes")
        def raw(request: Request, hub_id: str, frame_id: str, kind: str = Query("frame")):
            hub = self.hubs.hub_as_itself(request, hub_id)
            row = (self.library_of(hub, frame_id) if kind == "library"
                   else self.frame_of(hub, frame_id))
            if row is None or row["status"] in ("uploading", "withdrawn"):
                raise HTTPException(status_code=404, detail="no such frame")
            path = self.file_of(row)
            name = (row["doc"].get("filename") or path.name)
            # FileResponse answers `Range` itself: 206 with Content-Range, 416 when unsatisfiable.
            return FileResponse(path, media_type="application/octet-stream", filename=name)

        @app.post("/api/v1/hubs/{hub_id}/frames/{frame_id}/ack")
        def ack(request: Request, hub_id: str, frame_id: str, body: dict[str, Any],
                kind: str = Query("frame")) -> dict[str, Any]:
            hub = self.hubs.hub_as_itself(request, hub_id)
            row = (self.library_of(hub, frame_id) if kind == "library"
                   else self.frame_of(hub, frame_id))
            if row is None or row["status"] in ("uploading", "withdrawn"):
                raise HTTPException(status_code=404, detail="no such frame")
            sha = str(body.get("sha256") or "").lower()
            if kind == "library":
                self.ack_library(hub, row, sha)
            else:
                self.ack_frame(hub, row, sha)
            return {"ok": True, "frame_id": frame_id}


def install(app: FastAPI, store: Any, hubs: Any, front: FastAPI, files: Any) -> Relay:
    """Mount the relay routes (before the `/stack` static mount) and let the
    front door call `collect` at upload completion and `sweep` on its thread."""
    relay = Relay(store, hubs, front, files)
    relay.mount(app)
    front.state.relay = relay
    return relay
