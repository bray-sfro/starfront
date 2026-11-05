"""The rig calibration library: a contributor's bias, dark and dark-flat frames,
uploaded once per rig, stripped of their location cards, and shared by every
project that contributor flies the rig in.

The routes and bodies are the contract's (`libraryGet`, `libraryFramesInit`,
`libraryFramesComplete`, `libraryFramesWithdraw`; README open points 98-104);
the bytes go the same way as a project frame's (frames.py `Uploads`), to
`STARFRONT_DATA/stack/relay/lib/{user}/{rig}/`.

Decision D001 (open point 115): **one library per backend**. This server's
library serves hub projects only; a rig with a library on the cloud backend has
a second, separate one there. The plugin routes rig-scoped calls to the backend
the rig mostly uses, so nothing here looks at the other backend.

A ready frame is announced to the hub as a `library` row, key
`user_id/rig_id/frame_id`, on the grant's hub and on the hub of every project
the caller is an active member of, so a hub can fetch it (T406).
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Request

from .auth import Auth, Caller, json_body, now_iso
from .envelope import ApiError, ok, validation_failed
from .frames import (FRAME_ID, origin_of, UPLOAD_SECONDS, URL_SECONDS, WITHDRAW_SECONDS, AbortUpload, Uploads,
                     extension_of, frame_type_of, iso_plus, new_frame_id, validate_request)
from .rigs import Rigs

#: README open point 100: 30 GiB and 1 500 frames per user, over all their rigs.
MAX_BYTES = 30 * 1024 ** 3
MAX_FRAMES = 1500
TYPES = ("bias", "dark", "dark_flat", "master_bias", "master_dark", "master_dark_flat")


def num(v: Any) -> float | int | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class Library:
    def __init__(self, store: Any, auth: Auth, rigs: Rigs, uploads: Uploads, router: APIRouter) -> None:
        self.store = store
        self.auth = auth
        self.rigs = rigs
        self.up = uploads
        uploads.finders.append(self.target)
        self._routes(router)

    # -- rows ----------------------------------------------------------------
    def row(self, frame_id: str) -> dict[str, Any] | None:
        row = self.store._one("SELECT * FROM stack_library WHERE frame_id = ?", frame_id)
        if row is not None:
            row["doc"] = json.loads(row["doc"] or "{}")
        return row

    def target(self, upload_id: str) -> dict[str, Any] | None:
        row = self.store._one("SELECT * FROM stack_library WHERE json_extract(doc, '$.upload_id') = ?",
                              upload_id)
        if row is None:
            return None
        doc = json.loads(row["doc"])
        return {"kind": "library", "id": row["frame_id"], "status": row["status"], "size": doc["size"],
                "filename": doc.get("filename", ""), "path": row["path"],
                "url_expires": doc.get("url_expires", 0), "expires": doc.get("expires", 0)}

    def save(self, row: dict[str, Any], **columns: Any) -> None:
        sets = ["doc = ?", "rev = rev + 1"] + [f"{c} = ?" for c in columns]
        self.store._run(f"UPDATE stack_library SET {', '.join(sets)} WHERE frame_id = ?",
                        json.dumps(row["doc"]), *columns.values(), row["frame_id"])

    def usage(self, user_id: str) -> dict[str, int]:
        used = self.store._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(json_extract(doc, '$.size')), 0) AS b FROM stack_library"
            " WHERE user_id = ? AND status = 'ready'", user_id)
        return {"bytes_used": int(used["b"]), "frames_used": int(used["n"]),
                "max_bytes": MAX_BYTES, "max_frames": MAX_FRAMES}

    def announce(self, who: Caller, row: dict[str, Any]) -> None:
        hubs = {who.hub} if who.hub else set()
        for r in self.store._all(
                "SELECT p.hub AS hub, m.doc AS doc FROM stack_members m JOIN stack_projects p"
                " ON p.project_id = m.project_id WHERE m.user_id = ?", who.user_id):
            if r["hub"] and json.loads(r["doc"] or "{}").get("status") == "active":
                hubs.add(r["hub"])
        for hub in sorted(hubs):
            self.store.append_change(hub, "library", f"{row['user_id']}/{row['rig_id']}/{row['frame_id']}")

    # -- the wire ------------------------------------------------------------
    def wire(self, row: dict[str, Any]) -> dict[str, Any]:
        d = row["doc"]
        h = d.get("header") or {}
        binning = num(h.get("XBINNING", 1))
        temp = num(h.get("SET-TEMP"))
        return {"frame_id": row["frame_id"], "frame_type": d["frame_type"], "status": row["status"],
                "sha256": d["sha256"], "size": d["size"], "filename": d["filename"],
                "exposure_s": num(h.get("EXPTIME")), "gain": num(h.get("GAIN")),
                "offset": num(h.get("OFFSET")), "binning": None if binning is None else round(binning),
                "set_temp_c": temp, "date_obs": h.get("DATE-OBS") if isinstance(h.get("DATE-OBS"), str) else None,
                "uploaded_at": d.get("uploaded_at"), "usable": None, "problem": None}

    # -- access --------------------------------------------------------------
    def access(self, request: Request, rig_id: str, *, write: bool) -> tuple[Caller, dict[str, Any]]:
        who = self.auth.caller(request, write=write)
        rig = self.rigs.get(who.user_id, rig_id)
        if rig is None:
            raise ApiError("not_found", "No such rig.")
        return who, rig

    # -- init ----------------------------------------------------------------
    def do_init(self, who: Caller, rig: dict[str, Any], body: dict[str, Any], origin: str) -> dict[str, Any]:
        body = validate_request(body, library=True)
        if "stripped" not in body:
            raise validation_failed(("stripped", "is required"))
        if body["stripped"] is not True:
            raise ApiError("strip_required", "A library frame is always uploaded with its location cards"
                                             " removed; set `stripped: true`.",
                           errors=[{"field": "stripped", "message": "must be true"}])
        header = body["header"]
        frame_type = frame_type_of(header.get("IMAGETYP"))
        if frame_type not in TYPES:
            raise ApiError("unsupported_frame_type", "A calibration library holds bias, dark and dark-flat"
                                                     " frames (raw or master), not lights or flats.")
        problems = []
        if str(header.get("INSTRUME", "")).strip().casefold() != str(rig["camera"]).strip().casefold():
            problems.append(("header.INSTRUME", "must name the camera of this rig"))
        if (header.get("XBINNING") or 1) != rig["binning"]:
            problems.append(("header.XBINNING", "must equal the binning of this rig"))
        for card, key in (("NAXIS1", "width_px"), ("NAXIS2", "height_px")):
            if rig.get(key) and isinstance(header.get(card), int) and header[card] != rig[key]:
                problems.append((f"header.{card}", "must equal the sensor size of this rig"))
        if problems:
            raise validation_failed(*problems)
        with self.up.lock:
            sha = body["sha256"]
            rows = [self.row(r["frame_id"]) for r in self.store._all(
                "SELECT frame_id FROM stack_library WHERE user_id = ? AND rig_id = ?"
                " AND status <> 'withdrawn' AND json_extract(doc, '$.sha256') = ?",
                who.user_id, rig["rig_id"], sha)]
            for row in rows:
                if row["status"] == "ready":
                    raise ApiError("duplicate", "This frame is already in the library. Treat it as uploaded.",
                                   status="ready")
            if rows:
                mine = rows[0]
                if mine["doc"]["size"] != body["size"]:
                    raise validation_failed(("size", "does not match the upload already in progress"
                                                     " for this sha256"))
                self.up.prepare_resume(mine["doc"]["upload_id"], mine["path"])
                mine["doc"]["url_expires"] = time.time() + URL_SECONDS
                mine["doc"]["expires"] = time.time() + UPLOAD_SECONDS
                self.save(mine)
                return self.init_response(mine, True, origin)
            self.quota(who, body["size"])
            frame_id = new_frame_id()
            rel = f"lib/{who.user_id}/{rig['rig_id']}/{frame_id}{extension_of(body['filename'])}"
            doc = {"frame_type": frame_type, "sha256": sha, "size": body["size"],
                   "filename": body["filename"], "header": header, "stripped": True,
                   "upload_id": secrets.token_hex(32), "created_at": now_iso(), "uploaded_at": None,
                   "url_expires": time.time() + URL_SECONDS, "expires": time.time() + UPLOAD_SECONDS}
            self.store._run("INSERT INTO stack_library (frame_id, user_id, rig_id, status, path, doc)"
                            " VALUES (?, ?, ?, 'uploading', ?, ?)",
                            frame_id, who.user_id, rig["rig_id"], rel, json.dumps(doc))
            return self.init_response(self.row(frame_id), False, origin)

    def quota(self, who: Caller, size: int) -> None:
        used = self.usage(who.user_id)
        if used["bytes_used"] + size > MAX_BYTES:
            raise ApiError("quota_exceeded", "This upload would exceed your calibration library quota.",
                           quota="library_bytes", retry_after_s=3600)
        if used["frames_used"] + 1 > MAX_FRAMES:
            raise ApiError("quota_exceeded", "You have reached the library's frame limit.",
                           quota="library_frames", retry_after_s=3600)
        if not self.up.room(size):
            raise ApiError("quota_exceeded", "The server has no room left for frames; tell the coordinator.",
                           quota="library_bytes", retry_after_s=3600)

    def init_response(self, row: dict[str, Any], resumed: bool, origin: str) -> dict[str, Any]:
        d = row["doc"]
        return ok(message="Upload resumed." if resumed else "Upload started.", frame_id=row["frame_id"],
                  status="uploading", resumed=resumed, frame_type=d["frame_type"], rig_id=row["rig_id"],
                  upload=self.up.instructions(d["upload_id"], d["size"], origin))

    # -- complete and withdraw -----------------------------------------------
    def owned(self, who: Caller, rig: dict[str, Any], frame_id: str) -> dict[str, Any]:
        row = self.row(frame_id) if FRAME_ID.fullmatch(frame_id) else None
        if row is None or row["user_id"] != who.user_id or row["rig_id"] != rig["rig_id"]:
            raise ApiError("not_found", "No such frame.")
        return row

    def do_complete(self, who: Caller, rig: dict[str, Any], frame_id: str,
                    body: dict[str, Any]) -> dict[str, Any]:
        row = self.owned(who, rig, frame_id)
        if row["status"] != "uploading":
            if row["status"] == "withdrawn":
                raise ApiError("frame_state", "This frame was withdrawn.", status="withdrawn")
            return ok(message="Already complete.", frame_id=frame_id, status=row["status"],
                      already_complete=True)
        with self.up.lock:
            row = self.row(frame_id)
            if row["status"] != "uploading":
                return ok(message="Already complete.", frame_id=frame_id, status=row["status"],
                          already_complete=True)
            d = row["doc"]
            try:
                self.up.verify(d["upload_id"], row["path"], d["size"], d["filename"], body.get("etags"))
            except AbortUpload as abort:
                self.up.discard(d["upload_id"], row["path"])
                self.store._run("DELETE FROM stack_library WHERE frame_id = ?", frame_id)
                raise abort.error
            try:
                self.quota_commit(who, d["size"])
            except ApiError:
                self.up.discard(d["upload_id"], row["path"])
                self.store._run("DELETE FROM stack_library WHERE frame_id = ?", frame_id)
                raise
            self.up.finish(d["upload_id"], row["path"])
            for key in ("url_expires", "expires"):
                d.pop(key, None)
            d["upload_id"] = ""
            d["uploaded_at"] = now_iso()
            self.save(row, status="ready")
        self.announce(who, row)
        return ok(message="Upload complete. The frame is in your library.", frame_id=frame_id,
                  status="ready", already_complete=False)

    def quota_commit(self, who: Caller, size: int) -> None:
        used = self.usage(who.user_id)
        if used["bytes_used"] + size > MAX_BYTES or used["frames_used"] + 1 > MAX_FRAMES:
            raise ApiError("quota_exceeded", "Another upload took the room in your library meanwhile.",
                           quota="library_bytes" if used["bytes_used"] + size > MAX_BYTES else "library_frames",
                           retry_after_s=3600)

    def do_withdraw(self, who: Caller, rig: dict[str, Any], frame_id: str) -> dict[str, Any]:
        row = self.owned(who, rig, frame_id)
        d = row["doc"]
        if row["status"] == "withdrawn":
            return ok(frame_id=frame_id, status="withdrawn", raw_delete_after=d.get("raw_delete_after", now_iso()))
        with self.up.lock:
            if row["status"] == "uploading":
                self.up.discard(d["upload_id"], row["path"])
                d["raw_delete_after"] = now_iso()
                d["upload_id"] = ""
                self.save(row, status="withdrawn")
            else:
                d["raw_delete_after"] = iso_plus(WITHDRAW_SECONDS)
                self.save(row, status="withdrawn")
                self.announce(who, row)
        return ok(message="Frame withdrawn from the library.", frame_id=frame_id, status="withdrawn",
                  raw_delete_after=d["raw_delete_after"])

    def sweep(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        gone = 0
        for r in self.store._all("SELECT frame_id FROM stack_library WHERE status = 'uploading'"):
            with self.up.lock:
                row = self.row(r["frame_id"])
                if row is None or row["status"] != "uploading" or row["doc"].get("expires", now + 1) > now:
                    continue
                self.up.discard(row["doc"]["upload_id"], row["path"])
                self.store._run("DELETE FROM stack_library WHERE frame_id = ?", row["frame_id"])
                gone += 1
        return gone

    # -- routes --------------------------------------------------------------
    def _routes(self, router: APIRouter) -> None:
        @router.get("/rigs/{rig_id}/library")
        async def get(rig_id: str, request: Request) -> dict[str, Any]:
            who, rig = self.access(request, rig_id, write=False)
            q = request.query_params
            try:
                limit = int(q.get("limit", "50"))
            except ValueError:
                raise validation_failed(("limit", "must be an integer"))
            if not 1 <= limit <= 100:
                raise validation_failed(("limit", "must be 1 to 100"))
            after = None
            if q.get("cursor"):
                try:
                    c = q["cursor"]
                    after = base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)).decode()
                except (ValueError, binascii.Error, UnicodeError):
                    after = None
                if after is None or not FRAME_ID.fullmatch(after):
                    raise ApiError("invalid_request", "That cursor is not one this service gave you.")
            sql = ("SELECT * FROM stack_library WHERE user_id = ? AND rig_id = ? AND status IN"
                   " ('uploading', 'ready')")
            args: list[Any] = [who.user_id, rig["rig_id"]]
            if after:
                sql += " AND frame_id < ?"
                args.append(after)
            rows = self.store._all(sql + " ORDER BY frame_id DESC LIMIT ?", *args, limit + 1)
            for r in rows:
                r["doc"] = json.loads(r["doc"] or "{}")
            more = len(rows) > limit
            rows = rows[:limit]
            nxt = base64.urlsafe_b64encode(rows[-1]["frame_id"].encode()).decode().rstrip("=") if more else None
            return ok(rig_id=rig["rig_id"], usage=self.usage(who.user_id), frames=[self.wire(r) for r in rows],
                      masters=[], built_at=None, next_cursor=nxt)

        @router.post("/rigs/{rig_id}/library/frames/init")
        async def init(rig_id: str, request: Request) -> dict[str, Any]:
            who, rig = self.access(request, rig_id, write=True)
            return self.do_init(who, rig, await json_body(request), origin_of(request))

        @router.post("/rigs/{rig_id}/library/frames/{frame_id}/complete")
        async def complete(rig_id: str, frame_id: str, request: Request) -> dict[str, Any]:
            who, rig = self.access(request, rig_id, write=True)
            return self.do_complete(who, rig, frame_id, await json_body(request))

        @router.post("/rigs/{rig_id}/library/frames/{frame_id}/withdraw")
        async def withdraw(rig_id: str, frame_id: str, request: Request) -> dict[str, Any]:
            who, rig = self.access(request, rig_id, write=True)
            return self.do_withdraw(who, rig, frame_id)
