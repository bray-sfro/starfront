"""What a hub pushes back, and the routes that show it.

The hub owns everything from `uploaded` onward (plan 0.4): verdicts, the grid
after the first light, stream statistics, the rendered artefacts. It cannot be
reached from here, so it pushes them: `POST /api/v1/hubs/{id}/push`, hub token,
multipart.

* a part named `records`: JSON `{frames, projects, streams, events?}`
  - `frames`: `{frame_id, project_id, status, reject_reason?, reject_detail?,
    quarantine_reason?, metrics?, integration_s?, gold_seq?, verdict_at}`,
    merged into `stack_frames` (status column and `doc`);
  - `projects`: `{project_id, grid, grid_rev, mosaic?}`, into the project's `doc`;
  - `streams`: `{project_id, stream_key, stats, gold_seq?, gold_published_at?}`;
    a gold_seq above any seen announces the gold on Discord (server/notify.py);
  - `events`: `{project_id, ts, type, text, event_id?, stream_key?,
    display_name?, count?}`, the newest 200 kept per project.
* any number of file parts, named by their path under the project
  (`status.json`, `previews/x.png`, `masters/...`), written to
  `STARFRONT_DATA/stack/public/{project_id}/...`. With one project in the
  push the name is just the path; otherwise (or always, if you like) it is
  `{project_id}/{path}`. The deep-zoom pyramid is ONE part, `deepzoom.tar`,
  unpacked under `deepzoom/`.

Everything is checked before anything is written: a project not bound to this
hub is 409, a name or tar member that is absolute, has `..`, or is a link is
400, a push that would take the hub past its byte budget
(`STARFRONT_STACK_HUB_BUDGET_GB`, default 50) is 429. Files go in by temp file
and rename, and a file already identical is left alone, so the same push twice
changes nothing.

A master data file is too big for a multipart part (32 MiB cap), so the hub
sends each one alone: `PUT /api/v1/hubs/{id}/push/{project_id}/masters/{name}`
(hub token, `Content-Length` and `X-Content-SHA256` required, body streamed to
a temp file beside the destination, hashed on the way, renamed only when the
digest matches). Same name rules, project binding and byte budget as above;
the same name with the same digest is a 200 that rewrites nothing.

Masters are members-only whatever the licence (the contract), so they are NOT
served by the open `/stack/public` route: the masters list points at
`/stack/api/projects/{p}/masters/{file}`, which wants a grant or the cookie of
an active member.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from .auth import Auth
from .envelope import ApiError, ok, validation_failed
from .frames import FRAME_ID, origin_of
from .. import notify

#: The newest events kept per project.
EVENTS_KEPT = 200
_FILE_STATUS = {"preparing", "accepted", "rejected", "quarantined", "accumulated", "in_gold", "failed"}
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$")
_PROJECT = re.compile(r"^prj_[A-Za-z0-9]{1,40}$")
_STREAM = re.compile(r"^(L|R|G|B|HA|OIII|SII|HB|NII|CLEAR|DUO|IR|UV|U|V|I|Z|Y)(@osc)?(@(unk|[0-9]+(\.[0-9]+)?nm))?$")
_EVENT_ID = re.compile(r"^evt_[0-9a-hjkmnp-tv-z]{26}$")
_EVENT_TYPES = {"project_created", "project_status_changed", "member_joined", "frames_accepted",
                "gold_published", "filter_remapped"}
_SEGMENT = re.compile(r"^[A-Za-z0-9_@+=-][A-Za-z0-9_@+=.-]{0,127}$")
_SUFFIXES = {".json", ".png", ".jpg", ".jpeg", ".dzi", ".xml", ".xisf", ".fits"}
_MASTER_SUFFIXES = {".json", ".xisf", ".fits"}
_MASTER_DATA = {".xisf", ".fits", ".fz"}
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_MASTER_NAME = re.compile(r"^[A-Za-z0-9_@+=-][A-Za-z0-9_@+=.-]{0,127}$")
_LATE = ("status.json", "panels.json")      # written last, so a page never sees a half set


def budget_bytes() -> int:
    try:
        gb = float(os.environ.get("STARFRONT_STACK_HUB_BUDGET_GB") or 50)
    except ValueError:
        gb = 50.0
    return int(gb * (1 << 30))


def tree_bytes(root: Path) -> int:
    total = 0
    for base, dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _clean_path(path: str, *, what: str) -> list[str]:
    """The segments of a relative path, or 400. No `..`, no absolute, no odd
    characters, an allowed suffix."""
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path or "\0" in path:
        raise _bad(f"{what}: not a relative path")
    parts = path.split("/")
    for p in parts:
        if p in ("", ".", "..") or not _SEGMENT.match(p):
            raise _bad(f"{what}: a segment of the path is not allowed")
    if Path(parts[-1]).suffix.lower() not in _SUFFIXES:
        raise _bad(f"{what}: that kind of file is not accepted")
    if parts[0] == "masters" and (len(parts) != 2 or Path(parts[1]).suffix.lower() not in _MASTER_SUFFIXES):
        raise _bad(f"{what}: masters are flat .xisf, .fits and .json files")
    return parts


def _destination(root: Path, parts: list[str]) -> Path:
    """`root/parts`, refusing a path that runs through a link."""
    cur = root
    if cur.is_symlink():
        raise _bad("the project's folder is a link")
    for p in parts:
        cur = cur / p
        if cur.is_symlink():
            raise _bad("a path runs through a link")
    resolved = cur.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise _bad("a path leaves the project's folder")
    return cur


def _same(path: Path, fileobj: Any, size: int) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != size:
            return False
        with path.open("rb") as have:
            while True:
                a, b = have.read(1 << 20), fileobj.read(1 << 20)
                if a != b:
                    return False
                if not a:
                    return True
    except OSError:
        return False
    finally:
        fileobj.seek(0)


def _stamp_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(_STAMP.match(value))


class Pushed:
    """The push route, and the read routes of what was pushed."""

    def __init__(self, store: Any, public: Path) -> None:
        self.store = store
        self.public = public
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ push
    def install(self, app: FastAPI, hubs: Any) -> None:
        @app.post("/api/v1/hubs/{hub_id}/push")
        async def push(request: Request, hub_id: str) -> dict[str, Any]:
            hub = hubs.hub_as_itself(request, hub_id)
            self.store.hub_heard(hub["id"])
            try:
                form = await request.form(max_files=20000, max_fields=20000,
                                          max_part_size=32 << 20)
            except Exception:
                raise _bad("the body is not multipart/form-data")
            try:
                return await run_in_threadpool(self.ingest, hub, form)
            finally:
                await form.close()

        @app.put("/api/v1/hubs/{hub_id}/push/{project_id}/masters/{name}")
        async def push_master(request: Request, hub_id: str, project_id: str, name: str):
            hub = hubs.hub_as_itself(request, hub_id)
            self.store.hub_heard(hub["id"])
            row = self.store.stack_project(project_id) if _PROJECT.match(project_id) else None
            if row is None or row["hub"] != hub["id"]:
                raise HTTPException(status_code=409, detail=f"{project_id} is not a project of this hub")
            if not _MASTER_NAME.match(name) or Path(name).suffix.lower() not in _MASTER_DATA:
                raise _bad("a master is a flat .xisf, .fits or .fz file with a plain name")
            length = request.headers.get("content-length")
            if length is None or not length.isdigit():
                raise HTTPException(status_code=411, detail="Content-Length is required")
            size = int(length)
            digest = (request.headers.get("x-content-sha256") or "").strip().lower()
            if not _SHA256.match(digest):
                raise _bad("X-Content-SHA256 (64 hex digits) is required")
            dest = _destination(self.public / project_id, ["masters", name])
            if await run_in_threadpool(self._already, dest, size, digest):
                async for _ in request.stream():     # read it out, so the sender's write does not break
                    pass
                return {"ok": True, "written": False, "bytes": size}
            with self.lock:
                self._check_budget(hub, size, dest)
            await self._receive(request, dest, size, digest)
            return {"ok": True, "written": True, "bytes": size}

    def _check_budget(self, hub: dict[str, Any], added: int, dest: Path) -> None:
        replaced = dest.stat().st_size if dest.is_file() else 0
        used = sum(tree_bytes(self.public / pid) for pid in self._hub_projects(hub["id"]))
        if used - replaced + added > budget_bytes():
            raise HTTPException(status_code=429, detail="this hub has used its storage budget")

    @staticmethod
    def _already(dest: Path, size: int, digest: str) -> bool:
        try:
            if not dest.is_file() or dest.stat().st_size != size:
                return False
            h = hashlib.sha256()
            with dest.open("rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
            return h.hexdigest() == digest
        except OSError:
            return False

    async def _receive(self, request: Request, dest: Path, size: int, digest: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.tmp")
        h = hashlib.sha256()
        got = 0
        try:
            with tmp.open("wb") as out:
                async for chunk in request.stream():
                    got += len(chunk)
                    if got > size:
                        raise _bad("the body is longer than Content-Length")
                    h.update(chunk)
                    await run_in_threadpool(out.write, chunk)
            if got != size:
                raise _bad("the body is shorter than Content-Length")
            if h.hexdigest() != digest:
                raise HTTPException(status_code=422, detail="the body does not match X-Content-SHA256")
            with self.lock:
                self._check_budget_after(tmp, dest)
                os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)

    def _check_budget_after(self, tmp: Path, dest: Path) -> None:
        """Concurrent receives may each have passed the first check: the temp
        file is already in the tree, so the tree total is the truth."""
        replaced = dest.stat().st_size if dest.is_file() else 0
        hub = self.store.stack_project(dest.parent.parent.name)["hub"]
        used = sum(tree_bytes(self.public / pid) for pid in self._hub_projects(hub))
        if used - replaced > budget_bytes():
            raise HTTPException(status_code=429, detail="this hub has used its storage budget")

    def ingest(self, hub: dict[str, Any], form: Any) -> dict[str, Any]:
        records = self._records(form)
        names: dict[str, Any] = {}
        for key, value in form.multi_items():
            if key != "records":
                if key in names:
                    raise _bad(f"{key!r} is sent twice")
                if not hasattr(value, "file"):
                    raise _bad(f"{key!r} is not a file")
                names[key] = value
        # Which projects, and are they this hub's?
        wanted = {r["project_id"] for k in ("frames", "projects", "streams", "events")
                  for r in records[k]}
        for name in names:
            first = name.split("/", 1)[0]
            if _PROJECT.match(first):
                wanted.add(first)
        rows: dict[str, dict[str, Any]] = {}
        for pid in sorted(wanted):
            row = self.store.stack_project(pid) if _PROJECT.match(pid) else None
            if row is None or row["hub"] != hub["id"]:
                raise HTTPException(status_code=409, detail=f"{pid} is not a project of this hub")
            rows[pid] = row
        only = next(iter(rows)) if len(rows) == 1 else None
        plan = self._plan(names, rows, only)
        added = sum(p["size"] for p in plan)
        with self.lock:
            replaced = sum(p["dest"].stat().st_size for p in plan if p["dest"].is_file())
            used = sum(tree_bytes(self.public / pid) for pid in self._hub_projects(hub["id"]))
            if used - replaced + added > budget_bytes():
                raise HTTPException(status_code=429, detail="this hub has used its storage budget")
            written = self._write(plan)
            merged = self._merge(hub, rows, records)
            self._counts_from_status(rows, plan)
        self._announce(merged.pop("gold"))
        return {"ok": True, "files": {"written": written, "unchanged": len(plan) - written,
                                      "bytes": added}, **merged}

    def _hub_projects(self, hub_id: str) -> list[str]:
        return [r["project_id"] for r in self.store._all(
            "SELECT project_id FROM stack_projects WHERE hub = ?", hub_id)]

    # -- the records ------------------------------------------------------------
    def _records(self, form: Any) -> dict[str, list[dict[str, Any]]]:
        part = form.get("records")
        if part is None:
            raise _bad("a push needs a records part")
        try:
            raw = part.file.read() if hasattr(part, "file") else str(part).encode()
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise _bad("records is not JSON")
        if not isinstance(body, dict) or set(body) - {"frames", "projects", "streams", "events"}:
            raise _bad("records is an object with frames, projects, streams and events only")
        out: dict[str, list[dict[str, Any]]] = {}
        for key in ("frames", "projects", "streams", "events"):
            items = body.get(key) or []
            if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
                raise _bad(f"records.{key} must be a list of objects")
            for i in items:
                if not isinstance(i.get("project_id"), str):
                    raise _bad(f"records.{key}: every entry names a project_id")
            out[key] = items
        for f in out["frames"]:
            self._check_frame(f)
        for p in out["projects"]:
            if not isinstance(p.get("grid"), dict) or not isinstance(p.get("grid_rev"), int) \
                    or isinstance(p.get("grid_rev"), bool) or p["grid_rev"] < 0 \
                    or not (p.get("mosaic") is None or isinstance(p.get("mosaic"), dict)):
                raise _bad("records.projects: grid (object) and grid_rev (integer) are needed")
        for s in out["streams"]:
            self._check_stream(s)
        for e in out["events"]:
            self._check_event(e)
        return out

    @staticmethod
    def _check_frame(f: dict[str, Any]) -> None:
        if not isinstance(f.get("frame_id"), str) or not FRAME_ID.fullmatch(f["frame_id"]):
            raise _bad("records.frames: frame_id is not one of ours")
        if f.get("status") not in _FILE_STATUS:
            raise _bad("records.frames: status is not a verdict status")
        if not _stamp_ok(f.get("verdict_at")):
            raise _bad("records.frames: verdict_at must be a UTC timestamp")
        for key, limit in (("reject_reason", 64), ("quarantine_reason", 64), ("reject_detail", 500)):
            if f.get(key) is not None and not (isinstance(f[key], str) and len(f[key]) <= limit):
                raise _bad(f"records.frames: {key} must be text of up to {limit} characters")
        if f.get("metrics") is not None and (not isinstance(f["metrics"], dict)
                                             or len(json.dumps(f["metrics"])) > 4096):
            raise _bad("records.frames: metrics must be a small object")
        for key in ("integration_s",):
            if f.get(key) is not None and (isinstance(f[key], bool) or not isinstance(f[key], (int, float))):
                raise _bad(f"records.frames: {key} must be a number")
        if f.get("gold_seq") is not None and (isinstance(f["gold_seq"], bool) or not isinstance(f["gold_seq"], int)):
            raise _bad("records.frames: gold_seq must be an integer")

    @staticmethod
    def _check_stream(s: dict[str, Any]) -> None:
        if not isinstance(s.get("stream_key"), str) or not _STREAM.match(s["stream_key"]):
            raise _bad("records.streams: stream_key is not a stream key")
        if not isinstance(s.get("stats"), dict) or len(json.dumps(s["stats"])) > 8192:
            raise _bad("records.streams: stats must be a small object")
        if s.get("gold_seq") is not None and (isinstance(s["gold_seq"], bool)
                                              or not isinstance(s["gold_seq"], int) or s["gold_seq"] < 1):
            raise _bad("records.streams: gold_seq must be a positive integer")
        if s.get("gold_published_at") is not None and not _stamp_ok(s["gold_published_at"]):
            raise _bad("records.streams: gold_published_at must be a UTC timestamp")

    @staticmethod
    def _check_event(e: dict[str, Any]) -> None:
        if e.get("type") not in _EVENT_TYPES or not _stamp_ok(e.get("ts")) \
                or not isinstance(e.get("text"), str) or len(e["text"]) > 500:
            raise _bad("records.events: type, ts and text are needed (and valid)")
        if e.get("event_id") is not None and not (isinstance(e["event_id"], str) and _EVENT_ID.match(e["event_id"])):
            raise _bad("records.events: event_id is not an event id")
        if e.get("stream_key") is not None and not (isinstance(e["stream_key"], str) and _STREAM.match(e["stream_key"])):
            raise _bad("records.events: stream_key is not a stream key")
        for key in ("display_name",):
            if e.get(key) is not None and not (isinstance(e[key], str) and len(e[key]) <= 80):
                raise _bad(f"records.events: {key} must be short text")
        if e.get("count") is not None and (isinstance(e["count"], bool) or not isinstance(e["count"], int)):
            raise _bad("records.events: count must be an integer")

    # -- the files ---------------------------------------------------------------
    def _plan(self, names: dict[str, Any], rows: dict[str, Any], only: str | None) -> list[dict[str, Any]]:
        plan: list[dict[str, Any]] = []
        seen: set[Path] = set()

        def add(pid: str, parts: list[str], size: int, source: Any) -> None:
            root = self.public / pid
            dest = _destination(root, parts)
            if dest in seen:
                raise _bad("the same file is sent twice")
            seen.add(dest)
            plan.append({"pid": pid, "rel": [pid, *parts], "dest": dest, "size": size, "source": source})

        for name, part in names.items():
            head, _, rest = name.partition("/")
            if _PROJECT.match(head) and rest:
                pid, path = head, rest
            else:
                if only is None:
                    raise _bad(f"{name!r}: name the project ({{project_id}}/{{path}}); the push has none or several")
                pid, path = only, name
            if pid not in rows:
                raise HTTPException(status_code=409, detail=f"{pid} is not a project of this hub")
            if path == "deepzoom.tar":
                self._plan_tar(pid, part, add)
                continue
            part.file.seek(0, 2)
            size = part.file.tell()
            part.file.seek(0)
            add(pid, _clean_path(path, what=name), size, part.file)
        plan.sort(key=lambda p: (1 if len(p["rel"]) == 2 and p["rel"][1] in _LATE else 0, p["rel"]))
        return plan

    def _plan_tar(self, pid: str, part: Any, add: Any) -> None:
        part.file.seek(0)
        try:
            tar = tarfile.open(fileobj=part.file, mode="r:*")
            members = tar.getmembers()
        except (tarfile.TarError, EOFError, OSError):
            raise _bad("deepzoom.tar is not a tar archive")
        for m in members:
            if m.isdir():
                continue
            if not m.isreg():
                raise _bad("deepzoom.tar holds something that is not a plain file")
            name = m.name[2:] if m.name.startswith("./") else m.name
            if name.startswith("deepzoom/"):
                name = name[len("deepzoom/"):]
            parts = _clean_path("deepzoom/" + name, what="deepzoom.tar")
            add(pid, parts, m.size, (tar, m))

    def _write(self, plan: list[dict[str, Any]]) -> int:
        written = 0
        for item in plan:
            dest: Path = item["dest"]
            source = item["source"]
            if isinstance(source, tuple):
                tar, member = source
                fileobj = tar.extractfile(member)
            else:
                fileobj = source
            if fileobj is None:
                raise _bad("a file in the archive cannot be read")
            if _same(dest, fileobj, item["size"]):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.tmp")
            try:
                with tmp.open("wb") as out:
                    shutil.copyfileobj(fileobj, out, 1 << 20)
                os.replace(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
            written += 1
        return written

    def _counts_from_status(self, rows: dict[str, Any], plan: list[dict[str, Any]]) -> None:
        """The project card's counts come from a pushed status.json's totals."""
        for item in plan:
            if item["rel"][1:] != ["status.json"]:
                continue
            try:
                totals = json.loads(item["dest"].read_text("utf-8")).get("totals") or {}
            except (OSError, ValueError, AttributeError):
                continue
            counts = {"frames_accepted": totals.get("frames_accepted"),
                      "integration_s": totals.get("integration_s"),
                      "last_update": totals.get("last_update")}
            if not all(isinstance(counts[k], t) for k, t in (("frames_accepted", int), ("integration_s", (int, float)))):
                continue
            if counts["last_update"] is None:
                counts.pop("last_update")
            self._set(item["pid"], {"counts": counts})

    # -- the records into the database ---------------------------------------------
    def _set(self, pid: str, fields: dict[str, Any]) -> bool:
        """Set top-level keys of a project's doc, only those that differ."""
        row = self.store.stack_project(pid)
        changed = {k: v for k, v in fields.items() if row["doc"].get(k) != v}
        if not changed:
            return False
        expr, args = "doc", []
        for k, v in changed.items():
            expr = f"json_set({expr}, '$.{k}', json(?))"
            args.append(json.dumps(v))
        self.store._run(f"UPDATE stack_projects SET doc = {expr}, rev = rev + 1 WHERE project_id = ?",
                        *args, pid)
        return True

    def _merge(self, hub: dict[str, Any], rows: dict[str, Any], records: dict[str, Any]) -> dict[str, Any]:
        out = {"frames": {"merged": 0, "unchanged": 0, "skipped": 0}, "projects": 0, "streams": 0,
               "events": 0, "gold": []}
        for f in records["frames"]:
            out["frames"][self._merge_frame(f)] += 1
        for p in records["projects"]:
            row = self.store.stack_project(p["project_id"])
            if int(row["doc"].get("grid_rev") or 0) > p["grid_rev"]:
                continue
            fields = {"grid": p["grid"], "grid_rev": p["grid_rev"]}
            if "mosaic" in p:
                fields["mosaic"] = p["mosaic"]
            out["projects"] += self._set(p["project_id"], fields)
        by_project: dict[str, dict[str, Any]] = {}
        for s in records["streams"]:
            pid = s["project_id"]
            row = self.store.stack_project(pid)
            streams = by_project.setdefault(pid, dict(row["doc"].get("streams") or {}))
            had = int((streams.get(s["stream_key"]) or {}).get("gold_seq") or 0)
            now = {"stats": s["stats"], "gold_seq": s.get("gold_seq", had or None),
                   "gold_published_at": s.get("gold_published_at")}
            if (s.get("gold_seq") or 0) < had:      # an older push arriving late
                continue
            streams[s["stream_key"]] = now
            if s.get("gold_seq") and s["gold_seq"] > had:
                stats = s["stats"]
                out["gold"].append((pid, s["stream_key"], s["gold_seq"],
                                    float(stats.get("integration_s") or 0) / 3600.0,
                                    int(stats.get("contributors") or 0)))
        for pid, streams in by_project.items():
            out["streams"] += self._set(pid, {"streams": streams})
        events: dict[str, list[dict[str, Any]]] = {}
        fresh: dict[str, int] = {}
        for e in records["events"]:
            pid = e["project_id"]
            kept = events.setdefault(pid, list(self.store.stack_project(pid)["doc"].get("events") or []))
            known = {x["event_id"] for x in kept}
            ident = e.get("event_id") or "evt_" + self._ulid_like(pid, e)
            if ident in known:
                continue
            item = {"event_id": ident, "ts": e["ts"], "type": e["type"], "text": e["text"]}
            for key in ("stream_key", "display_name", "count"):
                if e.get(key) is not None:
                    item[key] = e[key]
            kept.append(item)
            known.add(ident)
            fresh[pid] = fresh.get(pid, 0) + 1
        for pid, kept in events.items():
            kept.sort(key=lambda x: (x["ts"], x["event_id"]), reverse=True)
            if self._set(pid, {"events": kept[:EVENTS_KEPT]}):
                out["events"] += fresh.get(pid, 0)
        return out

    @staticmethod
    def _ulid_like(pid: str, e: dict[str, Any]) -> str:
        """26 characters of the event id alphabet, from the event itself, so a
        resent event without an id is the same event."""
        alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
        digest = hashlib.sha256(json.dumps([pid, e["ts"], e["type"], e["text"], e.get("stream_key")],
                                           sort_keys=True).encode()).digest()
        return "".join(alphabet[b % 32] for b in digest[:26])

    def _merge_frame(self, f: dict[str, Any]) -> str:
        row = self.store._one("SELECT * FROM stack_frames WHERE frame_id = ?", f["frame_id"])
        if row is None or row["project_id"] != f["project_id"] \
                or row["status"] in ("uploading", "withdrawn"):
            return "skipped"
        doc = json.loads(row["doc"] or "{}")
        if doc.get("verdict_at") and doc["verdict_at"] > f["verdict_at"]:
            return "skipped"                        # an older verdict arriving late
        new = dict(doc)
        for key in ("reject_reason", "reject_detail", "quarantine_reason"):
            new[key] = f.get(key)
        for key in ("metrics", "integration_s", "gold_seq"):
            if f.get(key) is not None:
                new[key] = f[key]
        new["verdict_at"] = f["verdict_at"]
        new["updated_at"] = f["verdict_at"]
        if row["status"] == f["status"] and new == doc:
            return "unchanged"
        self.store._run("UPDATE stack_frames SET status = ?, doc = ?, rev = rev + 1 WHERE frame_id = ?",
                        f["status"], json.dumps(new), f["frame_id"])
        return "merged"

    def _announce(self, golds: list[tuple[str, str, int, float, int]]) -> None:
        for pid, stream, seq, hours, contributors in golds:
            row = self.store.stack_project(pid)
            notify.notify_gold_published(self.store, self.store.project(row["sf_project"]),
                                         stream, seq, hours, contributors)

    # ------------------------------------------------------------------ reads
    def routes(self, router: APIRouter, auth: Auth, projects: Any) -> None:
        def readable(request: Request, ref: str) -> tuple[dict[str, Any], Any]:
            """Read like the project itself: public and unlisted to anyone,
            private to its members (or a grant for it)."""
            who = auth.caller(request, required=False)
            row = auth.project(ref)
            if who is not None:
                auth.scoped(who, row)
            mine = projects.member(row["project_id"], who.user_id) if who else None
            if mine is not None and mine["doc"].get("status") == "removed":
                mine = None
            if row["doc"].get("visibility") == "private" and mine is None \
                    and not (who is not None and who.kind == "grant"):
                raise ApiError("not_found", "No such project.")
            return row, who

        def member_only(request: Request, ref: str) -> dict[str, Any]:
            who = auth.caller(request)
            row = auth.project(ref)
            auth.scoped(who, row)
            mine = projects.member(row["project_id"], who.user_id)
            if mine is None or mine["doc"].get("status") != "active":
                raise ApiError("not_member", "Only members of this project can download its masters.")
            return row

        @router.get("/projects/{project}/status")
        async def status(project: str, request: Request) -> dict[str, Any]:
            row, _ = readable(request, project)
            path = self.public / row["project_id"] / "status.json"
            try:
                doc = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                raise ApiError("not_found", "This project has no status document yet.")
            return ok(status_doc=doc)

        @router.get("/projects/{project}/events")
        async def events(project: str, request: Request) -> dict[str, Any]:
            row, _ = readable(request, project)
            q = request.query_params
            try:
                limit = int(q.get("limit", "50"))
            except ValueError:
                raise validation_failed(("limit", "must be an integer"))
            if not 1 <= limit <= 100:
                raise validation_failed(("limit", "must be 1 to 100"))
            items = list(row["doc"].get("events") or [])
            start = 0
            if q.get("cursor"):
                try:
                    after = base64.urlsafe_b64decode(q["cursor"] + "=" * (-len(q["cursor"]) % 4)).decode()
                except (ValueError, UnicodeError):
                    after = ""
                ids = [e["event_id"] for e in items]
                if after not in ids:
                    raise ApiError("invalid_request", "That cursor is not one this service gave you.")
                start = ids.index(after) + 1
            page = items[start:start + limit]
            more = start + limit < len(items)
            nxt = base64.urlsafe_b64encode(page[-1]["event_id"].encode()).decode().rstrip("=") \
                if more and page else None
            return ok(events=page, next_cursor=nxt)

        @router.get("/projects/{project}/mosaic")
        async def mosaic(project: str, request: Request) -> dict[str, Any]:
            row, _ = readable(request, project)
            return ok(mosaic=row["doc"].get("mosaic") or None)

        @router.get("/projects/{project}/masters")
        async def masters(project: str, request: Request) -> dict[str, Any]:
            row = member_only(request, project)
            base = f"{origin_of(request)}/stack/api/projects/{row['project_id']}/masters/"
            expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
            return ok(masters=self.masters_of(row, base, expires))

        @router.get("/projects/{project}/masters/{name}")
        async def master_file(project: str, name: str, request: Request):
            row = member_only(request, project)
            suffix = Path(name).suffix.lower()
            path = self.public / row["project_id"] / "masters" / name
            if not _MASTER_NAME.match(name) or suffix not in _MASTER_DATA \
                    or path.is_symlink() or not path.is_file():
                raise ApiError("not_found", "No such master.")
            return FileResponse(path, media_type="application/octet-stream", filename=name,
                                headers={"Cache-Control": "private, no-store"})

    def masters_of(self, row: dict[str, Any], base: str, expires: str) -> list[dict[str, Any]]:
        folder = self.public / row["project_id"] / "masters"
        if not folder.is_dir():
            return []
        best: dict[tuple[str, str], dict[str, Any]] = {}
        for record in sorted(folder.glob("*.json")):
            try:
                d = json.loads(record.read_text("utf-8"))
                fmt = d["format"]
                stream, seq = d["stream_key"], int(d["gold_seq"])
                if fmt not in ("xisf", "fits") or not _STREAM.match(stream) or isinstance(d["gold_seq"], bool):
                    continue
                name = d.get("file") if isinstance(d.get("file"), str) else f"{record.stem}.{fmt}"
                file = folder / name
                if not _MASTER_NAME.match(name) or file.is_symlink() or not file.is_file():
                    continue
                item = {"stream_key": stream, "gold_seq": seq, "format": fmt,
                        "size_bytes": file.stat().st_size, "integration_s": float(d.get("integration_s") or 0),
                        "created_at": d["created_at"] if _stamp_ok(d.get("created_at"))
                        else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(file.stat().st_mtime))}
                for key in ("frames", "sessions", "contributors"):
                    if isinstance(d.get(key), int) and not isinstance(d.get(key), bool):
                        item[key] = d[key]
                item["licence"] = d["licence"] if d.get("licence") in ("members_only", "cc_by", "cc_by_nc") \
                    else row["doc"].get("licence", "members_only")
                item["url"] = base + name
                item["url_expires_at"] = expires
            except (OSError, ValueError, KeyError, TypeError):
                continue
            key = (stream, fmt)
            if key not in best or best[key]["gold_seq"] < seq:
                best[key] = item
        return [best[k] for k in sorted(best)]
