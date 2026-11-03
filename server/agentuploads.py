"""Auto-upload: a telescope sends its collaboration lights, unattended.

The files page (`server/files.py`) is a person dropping a folder on a
browser. This is the same door for a program: Starfront or the N.I.N.A.
plugin, on the rig's own token, sending each light it shot for a
collaboration task, and the calibration masters that calibrate it, in the
daytime, behind a home connection that comes and goes.

So unlike the page's single `PUT` this is **resumable**: a session is
opened with the file's size and sha256, the bytes arrive in chunks at
stated offsets, and an upload cut off at 80 % carries on from 80 % - after
a reboot, the next day. The server checks the hash when the last byte is in,
so a file is never taken half-written or damaged.

What it will not take, and how it says so:

* Nothing unless the collaboration is open, collects files, has
  `autoUpload` switched on by its owner, and - when the owner keeps an
  allowlist (`autoUploadAgents`) - the rig is on it. 409.
* A light only from a rig with an accepted (or finished) task on that
  collaboration, and only of that collaboration: the header's `PROJID`
  (Starfront writes it) names it, or its RA/Dec lie within the field. 403.
* Nothing past the project's quota, counting the room other open sessions
  have reserved, and nothing that would leave the disk with less than
  `AGENT_KEEP_FREE` (25 GiB). 507 with `Retry-After`, which the clients
  treat as "pause for that long", never as "try again now".
* One chunk at a time per rig, and a handful across the server: 429 with
  `Retry-After`.

Every name is stored with the rig's id in front (`<agent8>-<name>`): one
owner's two telescopes both writing `M31_Ha_0001.fits` are two files, and
the `(project, user, kind, name)` index stays what it was. A file whose
content (sha256) is already held for this person is not taken twice.

Files land as the rig's owner's, under the rig's name, exactly as a page
upload by that owner would - so the upload rules, the files page, and the
contributor list treat them the same. Nothing on the rig is ever asked to
change or delete a file; nothing here deletes a kept file either.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from server import uploadrules
from server.files import ALLOWED, KINDS, MAX_BYTES, UPLOAD_KINDS, clean_name, looks_like_data

#: The disk floor for unattended uploads. A person at the page can be told;
#: forty rigs at dawn cannot, so they stop well before the database's disk
#: is in danger.
AGENT_KEEP_FREE = int(float(os.environ.get("STARFRONT_AGENT_KEEP_FREE_GB", "25")) * 1024 ** 3)
#: How long a rig is told to wait when the disk or the quota is full.
FULL_PAUSE = 6 * 3600
#: The largest chunk taken in one request, and the size clients are told to use.
CHUNK_MAX = 32 * 1024 ** 2
CHUNK_SIZE = 8 * 1024 ** 2
#: Uploads idle for this long are given up and their part file removed.
STALE = 72 * 3600
#: Concurrent chunk requests, across the server. One vCPU, one disk.
GLOBAL_STREAMS = int(os.environ.get("STARFRONT_UPLOAD_STREAMS", "6"))
#: Kinds a rig may send. Masters are flats, darks and bias like any other.
AGENT_KINDS = UPLOAD_KINDS
#: Margin around a collaboration's field for a light's pointing, in degrees.
FIELD_MARGIN = 1.5

_HEX = re.compile(r"^[0-9a-f]{64}$")


class UploadStart(BaseModel):
    project: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    kind: str = Field(default="light", max_length=12)
    bytes: int = Field(..., gt=0)
    sha256: str = Field(..., min_length=64, max_length=64)
    task: str = Field(default="", max_length=64)


def _sexagesimal(text: str, hours: bool) -> float | None:
    parts = re.split(r"[\s:hmsd°'\"]+", text.strip())
    parts = [p for p in parts if p]
    if not parts:
        return None
    try:
        sign = -1.0 if parts[0].startswith("-") else 1.0
        nums = [abs(float(p)) for p in parts[:3]]
    except ValueError:
        return None
    value = nums[0] + (nums[1] / 60 if len(nums) > 1 else 0) + (nums[2] / 3600 if len(nums) > 2 else 0)
    return sign * value * (15.0 if hours else 1.0)


def pointing(cards: dict[str, Any]) -> tuple[float, float] | None:
    """Where a light points, in degrees, from whatever its header carries."""
    def num(key: str) -> float | None:
        try:
            return float(str(cards.get(key)).strip())
        except (TypeError, ValueError):
            return None
    ra, dec = num("RA"), num("DEC")
    if ra is not None and dec is not None:
        return ra, dec
    ra, dec = num("CRVAL1"), num("CRVAL2")
    if ra is not None and dec is not None:
        return ra, dec
    if cards.get("OBJCTRA") and cards.get("OBJCTDEC"):
        ra = _sexagesimal(str(cards["OBJCTRA"]), True)
        dec = _sexagesimal(str(cards["OBJCTDEC"]), False)
        if ra is not None and dec is not None:
            return ra, dec
    return None


def separation(a: tuple[float, float], b: tuple[float, float]) -> float:
    ra1, d1, ra2, d2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(ra1 - ra2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def of_this_collaboration(cards: dict[str, Any], project: dict[str, Any]) -> tuple[bool, str]:
    """Whether a light is of this collaboration: by PROJID, else by pointing."""
    projid = str(cards.get("PROJID") or "").strip().strip("'").strip()
    if projid:
        if projid == project["id"]:
            return True, ""
        return False, f"the header's PROJID is {projid}, not this collaboration"
    region = (project.get("payload") or {}).get("region") or {}
    try:
        centre = (float(region["ra"]), float(region["dec"]))
        radius = math.hypot(float(region.get("width") or 0), float(region.get("height") or 0)) / 2
    except (KeyError, TypeError, ValueError):
        return True, ""      # a collaboration with no field: nothing to check against
    where = pointing(cards)
    if where is None:
        return False, "the header has no PROJID and no RA/DEC to place it"
    off = separation(where, centre)
    if off > radius + FIELD_MARGIN:
        return False, f"it points {off:.1f}° from the collaboration's field"
    return True, ""


class AgentUploads:
    def __init__(self, app: FastAPI, store: Any, files: Any) -> None:
        self.store = store
        self.files = files
        self.parts = Path(files.folder) / "_uploads"
        self.parts.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._busy_agents: set[str] = set()
        self._streams = 0
        self._swept = 0.0
        self._routes(app)

    # -- policy -----------------------------------------------------------
    def agent(self, request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        rig = self.store.agent_by_token(token) if token else None
        if rig is None:
            raise HTTPException(status_code=401, detail="unknown agent token")
        owner = str(rig.get("owner_id") or "")
        user = self.store.user(owner) if owner else None
        if user is None:
            raise HTTPException(status_code=403, detail="this telescope has no owner to file "
                                                        "its lights under; sign in with Discord first")
        return rig, user

    def enabled_for(self, project: dict[str, Any], rig: dict[str, Any]) -> tuple[bool, str]:
        payload = project.get("payload") or {}
        if project.get("status") != "open":
            return False, "that collaboration is closed"
        if not payload.get("collectFiles"):
            return False, "that collaboration is not collecting files"
        if not payload.get("autoUpload"):
            return False, "auto-upload is off on that collaboration"
        allow = payload.get("autoUploadAgents")
        if allow and rig["id"] not in allow:
            return False, "auto-upload is not on for this telescope on that collaboration"
        return True, ""

    def _project(self, project_id: str) -> dict[str, Any]:
        project = self.store.project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="no such collaboration")
        return project

    def _check(self, project: dict[str, Any], rig: dict[str, Any]) -> dict[str, Any]:
        ok, why = self.enabled_for(project, rig)
        if not ok:
            raise HTTPException(status_code=409, detail=why)
        task = self.store.agent_task_in(rig["id"], project["id"])
        if task is None:
            raise HTTPException(status_code=403,
                                detail="this telescope has no accepted task on that collaboration")
        return task

    def label(self, rig: dict[str, Any]) -> str:
        """The rig's name on the files page and for calibration matching.

        What the rig calls itself on the chart (its equipment profile), else
        its agent name. Many rigs are all called "Telescope 1" as agents;
        when another of the same owner's rigs has the same name, the agent
        id is added, so one rig's masters are never matched to another's
        lights."""
        def name_of(a: dict[str, Any]) -> str:
            seen = (a.get("presence") or {}).get("telescope") if isinstance(a.get("presence"), dict) else ""
            return str(seen or a.get("name") or "").strip()
        mine = name_of(rig) or str(rig["id"])[:8]
        owner = str(rig.get("owner_id") or "")
        twins = [a for a in self.store.agents_of(owner)
                 if a["id"] != rig["id"] and name_of(self.store.agent(a["id"]) or a) == mine] if owner else []
        return (f"{mine} [{str(rig['id'])[:8]}]" if twins else mine)[:60]

    def stored_name(self, rig: dict[str, Any], name: str) -> str:
        prefix = str(rig["id"])[:8] + "-"
        return name if name.startswith(prefix) else prefix + name

    def reserved(self, project_id: str | None = None) -> int:
        return sum(max(0, int(u["bytes"]) - int(u["received"]))
                   for u in self.store.uploads(project_id))

    def full(self, detail: str) -> HTTPException:
        return HTTPException(status_code=507, detail=detail,
                             headers={"Retry-After": str(FULL_PAUSE)})

    def part(self, upload_id: str) -> Path:
        return self.parts / f"{upload_id}.part"

    def sweep(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        gone = 0
        for row in self.store.uploads():
            if now - float(row["updated"]) > STALE:
                try:
                    self.part(row["id"]).unlink()
                except OSError:
                    pass
                self.store.remove_upload(row["id"])
                gone += 1
        self._swept = now
        return gone

    # -- throttles --------------------------------------------------------
    def acquire(self, agent_id: str) -> None:
        with self._lock:
            if agent_id in self._busy_agents:
                raise HTTPException(status_code=429, detail="one upload at a time per telescope",
                                    headers={"Retry-After": "30"})
            if self._streams >= GLOBAL_STREAMS:
                raise HTTPException(status_code=429, detail="the server is busy with other uploads",
                                    headers={"Retry-After": "60"})
            self._busy_agents.add(agent_id)
            self._streams += 1

    def release(self, agent_id: str) -> None:
        with self._lock:
            self._busy_agents.discard(agent_id)
            self._streams = max(0, self._streams - 1)

    # -- the routes ---------------------------------------------------------
    def _routes(self, app: FastAPI) -> None:

        @app.post("/api/v1/agent/uploads")
        def start(body: UploadStart, request: Request) -> dict[str, Any]:
            """Open (or resume) an upload: what, how big, and its sha256."""
            rig, user = self.agent(request)
            if time.time() - self._swept > 3600:
                self.sweep()
            project = self._project(body.project)
            task = self._check(project, rig)
            kind = (body.kind or "light").strip().lower()
            if kind not in AGENT_KINDS:
                raise HTTPException(status_code=400, detail="kind must be one of " + ", ".join(AGENT_KINDS))
            sha = body.sha256.lower()
            if not _HEX.match(sha):
                raise HTTPException(status_code=400, detail="sha256 must be 64 hex digits")
            clean = clean_name(body.name)
            if not clean:
                raise HTTPException(status_code=400, detail="not a frame: a file name ending in "
                                                            + ", ".join(ALLOWED) + " is expected")
            if body.bytes > MAX_BYTES:
                raise HTTPException(status_code=413, detail=f"larger than {MAX_BYTES // 1024 ** 3} GB")
            name = self.stored_name(rig, clean)
            same = self.store.file_by_hash(project["id"], user["id"], kind, sha)
            if same is not None:
                return {"already": True, "name": same["name"], "status": same.get("status") or ""}
            existing = self.store.file(project["id"], user["id"], name, kind)
            if existing is not None and (existing.get("sha256") or "") == sha:
                return {"already": True, "name": name, "status": existing.get("status") or ""}
            resume = self.store.upload_for(rig["id"], project["id"], kind, name, sha)
            if resume is not None and int(resume["bytes"]) == body.bytes:
                have = self.part(resume["id"]).stat().st_size if self.part(resume["id"]).is_file() else 0
                if have != int(resume["received"]):
                    self.store.set_upload_received(resume["id"], have)
                return {"already": False, "id": resume["id"], "offset": have,
                        "chunk": CHUNK_SIZE, "name": name}
            quota = self.files.quota_of(project)
            if quota == 0:
                raise HTTPException(status_code=409, detail="this collaboration is not taking files")
            held = int(self.store.file_totals(project["id"])["bytes"])
            replacing = int(existing["bytes"]) if existing is not None else 0
            if held - replacing + self.reserved(project["id"]) + body.bytes > quota:
                raise self.full(f"this collaboration is at its storage limit "
                                f"({quota / 1024 ** 3:g} GB)")
            free = self.files.stats()["free"]
            if free - self.reserved() - body.bytes < AGENT_KEEP_FREE:
                raise self.full("the server is keeping its last "
                                f"{AGENT_KEEP_FREE // 1024 ** 3} GB free; uploads are paused")
            row = self.store.add_upload({
                "id": secrets.token_hex(8), "project": project["id"], "agent": rig["id"],
                "user": user["id"], "kind": kind, "name": name, "original": clean,
                "bytes": body.bytes, "sha256": sha, "task": body.task or str(task["id"])})
            return {"already": False, "id": row["id"], "offset": 0, "chunk": CHUNK_SIZE, "name": name}

        def owned(upload_id: str, rig: dict[str, Any]) -> dict[str, Any]:
            row = self.store.upload(upload_id)
            if row is None or row["agent"] != rig["id"]:
                raise HTTPException(status_code=404, detail="no such upload (it may have expired; start again)")
            return row

        @app.get("/api/v1/agent/uploads/{upload_id}")
        def where(upload_id: str, request: Request) -> dict[str, Any]:
            rig, _ = self.agent(request)
            row = owned(upload_id, rig)
            part = self.part(upload_id)
            have = part.stat().st_size if part.is_file() else 0
            return {"id": upload_id, "offset": have, "bytes": int(row["bytes"])}

        @app.put("/api/v1/agent/uploads/{upload_id}")
        async def chunk(upload_id: str, request: Request,
                        offset: int = Query(..., ge=0)) -> dict[str, Any]:
            rig, _ = self.agent(request)
            row = owned(upload_id, rig)
            try:
                length = int(request.headers.get("content-length") or "-1")
            except ValueError:
                length = -1
            if length <= 0:
                raise HTTPException(status_code=411, detail="each chunk's size must be sent")
            if length > CHUNK_MAX:
                raise HTTPException(status_code=413, detail=f"chunks are at most {CHUNK_MAX // 1024 ** 2} MB")
            total = int(row["bytes"])
            part = self.part(upload_id)
            self.acquire(rig["id"])
            try:
                have = await asyncio.to_thread(lambda: part.stat().st_size if part.is_file() else 0)
                if offset != have:
                    return JSONResponse(status_code=409, content={
                        "detail": f"expected offset {have}", "offset": have})
                if have + length > total:
                    raise HTTPException(status_code=400, detail="more than the upload's size")
                data = bytearray()
                async for piece in request.stream():
                    data += piece
                    if len(data) > length:
                        raise HTTPException(status_code=400, detail="more arrived than the size said")
                if len(data) != length:
                    raise HTTPException(status_code=400, detail="the chunk stopped short")

                def write() -> int:
                    with open(part, "ab") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    return part.stat().st_size
                now_have = await asyncio.to_thread(write)
            finally:
                self.release(rig["id"])
            self.store.set_upload_received(upload_id, now_have)
            return {"offset": now_have, "bytes": total, "complete": now_have == total}

        @app.post("/api/v1/agent/uploads/{upload_id}/finish")
        async def finish(upload_id: str, request: Request) -> dict[str, Any]:
            rig, user = self.agent(request)
            row = owned(upload_id, rig)
            project = self._project(row["project"])
            self._check(project, rig)
            part = self.part(upload_id)
            total = int(row["bytes"])

            def digest() -> tuple[int, str, bytes]:
                h = hashlib.sha256()
                size = 0
                head = b""
                with open(part, "rb") as handle:
                    while True:
                        block = handle.read(4 * 1024 ** 2)
                        if not block:
                            break
                        if len(head) < 80:
                            head += block[:80 - len(head)]
                        size += len(block)
                        h.update(block)
                return size, h.hexdigest(), head
            if not part.is_file():
                raise HTTPException(status_code=409, detail="nothing received yet")
            size, sha, head = await asyncio.to_thread(digest)
            if size != total:
                return JSONResponse(status_code=409, content={
                    "detail": f"only {size} of {total} bytes are here", "offset": size})
            if sha != row["sha256"]:
                await asyncio.to_thread(part.unlink)
                self.store.remove_upload(upload_id)
                raise HTTPException(status_code=422, detail="the sha256 does not match what arrived; "
                                                            "send the file again")
            name, kind = row["name"], row["kind"]
            if not looks_like_data(head, name):
                await asyncio.to_thread(part.unlink)
                self.store.remove_upload(upload_id)
                raise HTTPException(status_code=400, detail=f"{name} does not start like a "
                                    f"{'XISF' if name.lower().endswith('.xisf') else 'FITS'} file")
            header = await asyncio.to_thread(_header_or_empty, part)
            meta = uploadrules.facts(header, name) if header else uploadrules.facts_of_file(part, name)
            if kind == "light":
                ok, why = of_this_collaboration(header.get("cards") or {}, project)
                if not ok:
                    await asyncio.to_thread(part.unlink)
                    self.store.remove_upload(upload_id)
                    raise HTTPException(status_code=403, detail="not a light of this collaboration: " + why)
            final = self.files.path_of(project["id"], user["id"], name, kind)
            await asyncio.to_thread(_place, part, final)
            label = self.label(rig)
            file_id = secrets.token_hex(8)
            self.store.add_file(file_id, project["id"], user["id"], name, size, label, kind, meta)
            stored = self.store.file(project["id"], user["id"], name, kind)
            self.store.set_file_origin(stored["id"], sha, rig["id"], row["task"])
            self.store.remove_upload(upload_id)
            moved = self.files.recheck(project, user["id"],
                                       only_light=stored["id"] if kind == "light" else None)
            stored = self.store.file(project["id"], user["id"], name, kind)
            return {"already": False, "name": name, "kind": kind,
                    "status": stored.get("status") or "",
                    "reasons": uploadrules_reasons(stored), "moved": moved}

        @app.get("/api/v1/agent/uploads")
        def status(request: Request, project: str = Query(..., max_length=64)) -> dict[str, Any]:
            """Whether this rig may auto-upload to a collaboration now, and what
            its pending lights still need: the calibration it should send."""
            rig, user = self.agent(request)
            found = self._project(project)
            enabled, why = self.enabled_for(found, rig)
            task = self.store.agent_task_in(rig["id"], found["id"])
            if task is None and enabled:
                enabled, why = False, "this telescope has no accepted task on that collaboration"
            label = self.label(rig)
            prefix = str(rig["id"])[:8] + "-"
            mine = [r for r in self.store.files_in(found["id"], user["id"])
                    if (r.get("agent") or "") == rig["id"] or r["name"].startswith(prefix)
                    or (r.get("telescope") or "") == label]
            counts: dict[str, int] = {}
            needs: dict[str, dict[str, Any]] = {}
            for r in mine:
                if r["kind"] != "light":
                    continue
                st = r.get("status") or ""
                counts[st] = counts.get(st, 0) + 1
                if st != uploadrules.PENDING:
                    continue
                m = _loads(r.get("meta"), {})
                for reason in uploadrules_reasons(r):
                    if not reason.startswith("needs "):
                        continue
                    want = {"filter": m.get("filter"), "filterRaw": m.get("filterRaw"), "exposure": m.get("exposure"),
                            "gain": m.get("gain"), "offset": m.get("offset"),
                            "temp": m.get("temp"), "xbin": m.get("xbin"), "ybin": m.get("ybin"),
                            "camera": m.get("camera"), "rotator": m.get("rotator"),
                            "kinds": (["flat"] if reason.startswith("needs flats") else
                                      ["dark", "bias"] if "bias at" in reason else ["dark"])}
                    key = repr(sorted((k, str(v)) for k, v in want.items()))
                    entry = needs.setdefault(key, {**want, "reason": reason, "lights": 0})
                    entry["lights"] += 1
            free = self.files.stats()["free"]
            return {"enabled": enabled, "why": why,
                    "task": (task or {}).get("id") or "",
                    "pausedSeconds": FULL_PAUSE if (free - self.reserved()) < AGENT_KEEP_FREE else 0,
                    "chunk": CHUNK_SIZE, "lights": counts,
                    "calibrationNeeds": sorted(needs.values(), key=lambda x: -x["lights"])}


def _header_or_empty(path: Path) -> dict[str, Any]:
    try:
        return uploadrules.read_header(path)
    except Exception:          # noqa: BLE001 - the rules quarantine what cannot be read
        return {}


def _place(part: Path, final: Path) -> None:
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(part, final)


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return value
    try:
        out = json.loads(value or "null")
    except (TypeError, ValueError):
        return default
    return out if isinstance(out, type(default)) else default


def uploadrules_reasons(row: dict[str, Any]) -> list[str]:
    return _loads(row.get("reasons"), [])
