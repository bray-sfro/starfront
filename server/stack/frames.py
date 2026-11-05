"""Frames: the contributor's upload, in the Collab contract's words, onto this
server's disk instead of S3.

`frames/init` registers a frame and answers with a **multipart** upload whose
part URLs are `/stack/s3/{upload_id}/{n}` on this server. The part PUT is the
local twin of S3's `UploadPart` (see Rapid Stacker Collab's
`backend/local/lib/s3-http.js`): the body is the part, the URL is the
credential (an `upload_id` is 32 random bytes), the answer is an `ETag` header
holding the quoted MD5 of the part. Parts land at `(n-1) * part_size` of
`STARFRONT_DATA/stack/relay/{project}/{user}/{frame_id}.{ext}.part`, so they
may arrive in any order and a part sent twice simply overwrites. `complete`
checks the ETags, that every part is there, the total size and the file's
magic, then renames the file into place and writes a `frame` row to the hub's
change feed, which is how the hub learns there is something to fetch (T406).

The verdict fields (`accepted`, `rejected`, metrics, ...) come from the hub's
push (T407). Until then a frame stays `uploaded`.

`Uploads` is the machinery the project frames and the rig library (library.py)
share: the plan of parts, the part PUT, verification, the sweep of expired
uploads. Everything is keyed by collab user ids; no Starfront id is written
here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import files as files_mod
from ..auth import setting as server_setting
from . import defaults
from .auth import Auth, Caller, json_body, now_iso
from .envelope import ApiError, ok, validation_failed
from .header import check as check_header
from .projects import Projects
from .rigs import Rigs, fingerprint, pixel_scale, rig_id_of

PART_SIZE = 16 * 1024 * 1024
MAX_BYTES = 512 * 1024 * 1024          # the contract: above this, frame_too_large
MAX_DIMENSION_PX = 20000
BITPIX_VALUES = (8, 16, 32, -32, -64)
URL_SECONDS = 3600                     # how long part URLs are good (the contract's "1 hour")
UPLOAD_SECONDS = 3 * 86400             # an `uploading` frame is aborted after this
WITHDRAW_SECONDS = 7 * 86400
EXTENSIONS = (".fits", ".fit", ".fts", ".fz", ".xisf")
SHA = re.compile(r"[0-9a-f]{64}")
FRAME_ID = re.compile(r"frm_[0-9a-hjkmnp-tv-z]{26}")
_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"

TYPE_NAMES = {
    "light": ("light", "lightframe", "lights", "object", "science"),
    "dark": ("dark", "darkframe", "darks"),
    "flat": ("flat", "flatframe", "flatfield", "flats", "skyflat", "domeflat", "twilightflat"),
    "bias": ("bias", "biasframe", "zero", "zeroframe", "offset", "offsetframe"),
    "dark_flat": ("darkflat", "flatdark", "darkflatframe", "flatdarkframe", "darkflats"),
    "master_dark": ("masterdark", "masterdarkframe"),
    "master_flat": ("masterflat", "masterflatframe", "masterflatfield"),
    "master_bias": ("masterbias", "masterbiasframe", "masterzero"),
    "master_dark_flat": ("masterdarkflat", "masterflatdark", "masterdarkflatframe"),
}
TYPE_OF = {n: t for t, names in TYPE_NAMES.items() for n in names}
CAL_TYPES = frozenset({"dark", "bias", "dark_flat", "master_dark", "master_bias", "master_dark_flat"})


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def new_frame_id() -> str:
    """`frm_` + a lower-case ULID: 48 bits of milliseconds, 80 random."""
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    return "frm_" + "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in range(25, -1, -1))


def iso_plus(seconds: float) -> str:
    t = time.time() + seconds
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


def frame_type_of(imagetyp: Any) -> str | None:
    """IMAGETYP -> FrameType; a header without it is a light; None if unknown."""
    if imagetyp is None:
        return "light"
    return TYPE_OF.get(re.sub(r"[^a-z]", "", str(imagetyp).lower()))


def extension_of(filename: str) -> str:
    low = filename.lower()
    for ext in EXTENSIONS:
        if low.endswith(ext):
            return ext
    return ""


def part_count(size: int) -> int:
    return -(-size // PART_SIZE)


def part_size_of(size: int, n: int) -> int:
    return PART_SIZE if n < part_count(size) else size - PART_SIZE * (part_count(size) - 1)


def norm_etag(text: Any) -> str:
    return str(text).strip().strip('"').lower()


def validate_request(body: dict[str, Any], *, library: bool) -> dict[str, Any]:
    """`FrameInitRequest` / `LibraryFrameInitRequest`, in the contract's order:
    the header allow-list first, then the shape, then the limits."""
    if isinstance(body.get("header"), dict):
        check_header(body["header"])
    allowed = {"sha256", "size", "filename", "header", "stripped"} | (set() if library else {"rig_hint"})
    problems = [(k, "unknown field") for k in sorted(set(body) - allowed)]
    sha = body.get("sha256")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        problems.append(("sha256", "must be 64 lower-case hex characters"))
    size = body.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        problems.append(("size", "must be an integer of at least 1"))
    name = body.get("filename")
    if not isinstance(name, str) or not 1 <= len(name) <= 255:
        problems.append(("filename", "must be text of 1 to 255 characters"))
    if not isinstance(body.get("header"), dict):
        problems.append(("header", "must be an object of FITS cards"))
    if "stripped" in body and not isinstance(body["stripped"], bool):
        problems.append(("stripped", "must be true or false"))
    hint = body.get("rig_hint")
    if hint is not None and (not isinstance(hint, dict) or set(hint) - {"rig_id"}):
        problems.append(("rig_hint", "must be an object with a rig_id"))
    if problems:
        raise validation_failed(*problems)
    header = check_header(body["header"])
    if body["size"] > MAX_BYTES:
        raise ApiError("frame_too_large", "Frames above 512 MiB are not accepted.")
    for card in ("NAXIS1", "NAXIS2"):
        if isinstance(header.get(card), int) and header[card] > MAX_DIMENSION_PX:
            raise ApiError("dimensions_too_large",
                           "Frames wider or taller than 20 000 px are not accepted.")
    if "BITPIX" in header and header["BITPIX"] not in BITPIX_VALUES:
        raise validation_failed(("header.BITPIX", "must be one of 8, 16, 32, -32, -64"))
    if not extension_of(body["filename"]):
        raise validation_failed(("filename", "must end in .fits, .fit, .fts, .fz or .xisf"))
    return body


def origin_of(request: Request) -> str:
    """Where part URLs point: STARFRONT_PUBLIC_URL when set, else the request's own
    scheme and host. Absolute, because the plugin's HttpClient has no base address."""
    public = (server_setting("PUBLIC_URL") or "").strip().rstrip("/")
    return public or str(request.base_url).rstrip("/")


def xml_error(status: int, code: str, message: str) -> Response:
    """S3's error body, which is what a part-PUT client expects."""
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>' + code + "</Code><Message>"
            + message.replace("&", "&amp;").replace("<", "&lt;") + "</Message></Error>")
    return Response(content=body, status_code=status, media_type="application/xml")


def free_bytes(folder: Path) -> int:
    probe = folder
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


# ---------------------------------------------------------------------------
# The shared upload machinery
# ---------------------------------------------------------------------------

class Uploads:
    """Parts on disk, for project frames and library frames alike.

    A *target* is a dict `{kind, id, status, size, filename, path, url_expires,
    expires}` describing the frame an `upload_id` belongs to; `finders` are the
    two lookups (project frames, library frames) that produce one.
    """

    def __init__(self, store: Any, root: Path) -> None:
        self.store = store
        self.root = root
        self.finders: list[Any] = []
        self.lock = threading.RLock()

    # -- space ---------------------------------------------------------------
    def room(self, size: int) -> bool:
        """Whether `size` more bytes leave the volume above `files.KEEP_FREE`."""
        return free_bytes(self.root) - size >= files_mod.KEEP_FREE

    # -- plan ----------------------------------------------------------------
    def parts(self, upload_id: str) -> dict[int, dict[str, Any]]:
        return {r["part_number"]: r for r in self.store._all(
            "SELECT * FROM stack_parts WHERE upload_id = ?", upload_id)}

    def instructions(self, upload_id: str, size: int, origin: str) -> dict[str, Any]:
        """The contract's `UploadInstructions`: what is stored, and URLs for the rest."""
        count = part_count(size)
        have = self.parts(upload_id)
        completed, urls = [], []
        for n in range(1, count + 1):
            row = have.get(n)
            if row is not None and row["size"] == part_size_of(size, n):
                completed.append({"part_number": n, "etag": f'"{row["etag"]}"'})
            else:
                urls.append({"part_number": n, "url": f"{origin}/stack/s3/{upload_id}/{n}",
                             "size": part_size_of(size, n)})
        return {"type": "multipart", "upload_id": upload_id, "part_size": PART_SIZE,
                "part_count": count, "part_urls": urls, "completed_parts": completed,
                "expires_at": iso_plus(URL_SECONDS)}

    def prepare_resume(self, upload_id: str, rel: str) -> None:
        """A resumed upload whose partial file is gone has nothing: forget its parts."""
        if not self.part_file(rel).exists():
            self.store._run("DELETE FROM stack_parts WHERE upload_id = ?", upload_id)

    # -- paths ---------------------------------------------------------------
    def part_file(self, rel: str) -> Path:
        return self.root / (rel + ".part")

    def final_file(self, rel: str) -> Path:
        return self.root / rel

    def discard(self, upload_id: str, rel: str) -> None:
        self.store._run("DELETE FROM stack_parts WHERE upload_id = ?", upload_id)
        for path in (self.part_file(rel), self.final_file(rel)):
            try:
                path.unlink()
            except OSError:
                pass

    # -- the part PUT --------------------------------------------------------
    def find(self, upload_id: str) -> dict[str, Any] | None:
        for finder in self.finders:
            target = finder(upload_id)
            if target is not None:
                return target
        return None

    async def put_part(self, request: Request, upload_id: str, n: int) -> Response:
        target = self.find(upload_id) if re.fullmatch(r"[0-9a-f]{64}", upload_id) else None
        if target is None or target["status"] != "uploading" or time.time() > target["url_expires"]:
            return xml_error(403, "AccessDenied", "That upload is unknown or its URLs have expired.")
        size = target["size"]
        if not 1 <= n <= part_count(size):
            return xml_error(400, "InvalidArgument", "No such part number for this upload.")
        want = part_size_of(size, n)
        try:
            length = int(request.headers.get("content-length", ""))
        except ValueError:
            return xml_error(411, "MissingContentLength", "The part's size must be sent.")
        if length != want:
            return xml_error(400, "InvalidRequest", f"Part {n} is exactly {want} bytes.")
        part = self.part_file(target["path"])
        part.parent.mkdir(parents=True, exist_ok=True)
        md5 = hashlib.md5(usedforsecurity=False)
        written = 0
        # O_BINARY: Windows opens a bare descriptor in text mode and turns every 0x0A
        # into 0x0D 0x0A, so the file outgrows the part (absent elsewhere).
        fd = os.open(part, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.lseek(fd, (n - 1) * PART_SIZE, os.SEEK_SET)
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > want:
                    return xml_error(400, "InvalidRequest", "More arrived than the size said.")
                md5.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        if written != want:
            return xml_error(400, "IncompleteBody", f"Part {n} stopped short ({written} of {want} bytes).")
        etag = md5.hexdigest()
        current = self.find(upload_id)
        if current is None or current["status"] != "uploading":
            # Withdrawn or aborted while the bytes were arriving: leave nothing behind.
            self.discard(upload_id, target["path"])
            return xml_error(403, "AccessDenied", "That upload is gone.")
        self.store._run("INSERT OR REPLACE INTO stack_parts (upload_id, part_number, size, etag)"
                        " VALUES (?, ?, ?, ?)", upload_id, n, want, etag)
        return Response(status_code=200, headers={"ETag": f'"{etag}"'})

    # -- complete ------------------------------------------------------------
    def verify(self, upload_id: str, rel: str, size: int, filename: str,
               etags: Any) -> None:
        """Raises the contract's error unless every part is here and right.

        `etags` is the client's list. `size_mismatch` and a bad magic abort the
        upload (the caller removes the frame): the bytes cannot be repaired.
        """
        if not isinstance(etags, list) or not etags:
            raise ApiError("invalid_request", "etags must be a non-empty list.")
        count = part_count(size)
        supplied: dict[int, str] = {}
        for item in etags:
            if (not isinstance(item, dict) or set(item) != {"part_number", "etag"}
                    or isinstance(item["part_number"], bool) or not isinstance(item["part_number"], int)
                    or item["part_number"] < 1 or not isinstance(item["etag"], str) or not item["etag"]):
                raise ApiError("invalid_request", "etags must be objects of part_number and etag.")
            if item["part_number"] > count or item["part_number"] in supplied:
                raise ApiError("bad_etags", f"The part list does not match this upload (parts are"
                                            f" numbered 1 to {count}, each once).")
            supplied[item["part_number"]] = item["etag"]
        have = self.parts(upload_id)
        for n, etag in supplied.items():
            row = have.get(n)
            if row is not None and norm_etag(row["etag"]) != norm_etag(etag):
                raise ApiError("bad_etags", f"The ETag for part {n} does not match what was stored.")
        missing = [n for n in range(1, count + 1) if n not in have]
        if missing:
            raise ApiError("upload_incomplete", "Some parts have not been uploaded. Call init again"
                                                " to resume.", outstanding_parts=missing)
        if sum(have[n]["size"] for n in range(1, count + 1)) != size:
            raise AbortUpload(ApiError("size_mismatch", "The uploaded size does not match the declared"
                                                        " size. Start again with init."))
        part = self.part_file(rel)
        try:
            actual = part.stat().st_size
        except OSError:
            raise ApiError("upload_incomplete", "The upload is gone. Call init to start it again.",
                           outstanding_parts=list(range(1, count + 1)))
        if actual != size:
            raise AbortUpload(ApiError("size_mismatch", "The uploaded size does not match the declared"
                                                        " size. Start again with init."))
        with open(part, "rb") as handle:
            head = handle.read(80)
        if not files_mod.looks_like_data(head, filename):
            kind = "XISF" if filename.lower().endswith(".xisf") else "FITS"
            raise AbortUpload(validation_failed(("file", f"does not start like a {kind} file")))

    def finish(self, upload_id: str, rel: str) -> None:
        """Rename the assembled file into place and drop the part rows."""
        os.replace(self.part_file(rel), self.final_file(rel))
        self.store._run("DELETE FROM stack_parts WHERE upload_id = ?", upload_id)


class AbortUpload(Exception):
    """A completion that cannot be repaired: remove the frame, then answer with `error`."""

    def __init__(self, error: ApiError) -> None:
        super().__init__(error.message)
        self.error = error


# ---------------------------------------------------------------------------
# Rigs a frame belongs to
# ---------------------------------------------------------------------------

def rig_fields(header: dict[str, Any]) -> dict[str, Any]:
    binning = header.get("XBINNING") or 1
    pixel = header["XPIXSZ"] if (header.get("XPIXSZ") or 0) > 0 else 1
    focal = header["FOCALLEN"] if (header.get("FOCALLEN") or 0) > 0 else 0
    if not focal and (header.get("PIXSCALE") or 0) > 0 and (header.get("XPIXSZ") or 0) > 0:
        focal = 206.265 * header["XPIXSZ"] * binning / header["PIXSCALE"]
    return {"camera": str(header.get("INSTRUME") or "").strip() or "Unknown camera",
            "telescope": str(header.get("TELESCOP") or "").strip() or "Unknown telescope",
            "focal_length_mm": focal or 1, "pixel_size_um": pixel, "binning": binning,
            "bayer_pattern": str(header["BAYERPAT"]).upper()[:4] if header.get("BAYERPAT") else None,
            "width_px": header.get("NAXIS1"), "height_px": header.get("NAXIS2")}


def optics_unknown(header: dict[str, Any]) -> bool:
    named = isinstance(header.get("TELESCOP"), str) and header["TELESCOP"].strip() != ""
    focal = (header.get("FOCALLEN") or 0) > 0 or ((header.get("PIXSCALE") or 0) > 0
                                                  and (header.get("XPIXSZ") or 0) > 0)
    return not named or not focal


# ---------------------------------------------------------------------------
# The project frames
# ---------------------------------------------------------------------------

class Frames:
    def __init__(self, store: Any, auth: Auth, projects: Projects, rigs: Rigs,
                 uploads: Uploads, router: APIRouter) -> None:
        self.store = store
        self.auth = auth
        self.projects = projects
        self.rigs = rigs
        self.up = uploads
        #: `after_upload(project, row)`, set by the relay: the collectFiles registration (relay.py).
        self.after_upload: Any = None
        uploads.finders.append(self.target)
        self._routes(router)

    # -- rows ----------------------------------------------------------------
    def row(self, frame_id: str) -> dict[str, Any] | None:
        row = self.store._one("SELECT * FROM stack_frames WHERE frame_id = ?", frame_id)
        if row is not None:
            row["doc"] = json.loads(row["doc"] or "{}")
        return row

    def target(self, upload_id: str) -> dict[str, Any] | None:
        row = self.store._one("SELECT * FROM stack_frames WHERE upload_id = ?", upload_id)
        if row is None:
            return None
        doc = json.loads(row["doc"] or "{}")
        return {"kind": "frame", "id": row["frame_id"], "status": row["status"], "size": row["size"],
                "filename": doc.get("filename", ""), "path": row["path"],
                "url_expires": doc.get("url_expires", 0), "expires": doc.get("expires", 0)}

    def save(self, row: dict[str, Any], **columns: Any) -> None:
        """Write `doc` (and any columns) back, bumping `rev`."""
        row["doc"]["updated_at"] = now_iso()
        sets = ["doc = ?", "rev = rev + 1"] + [f"{c} = ?" for c in columns]
        self.store._run(f"UPDATE stack_frames SET {', '.join(sets)} WHERE frame_id = ?",
                        json.dumps(row["doc"]), *columns.values(), row["frame_id"])

    def announce(self, project: dict[str, Any], frame_id: str) -> None:
        if project.get("hub"):
            self.store.append_change(project["hub"], "frame", f"{project['project_id']}/{frame_id}")

    # -- the wire ------------------------------------------------------------
    def wire(self, row: dict[str, Any], contributor: str | None = None) -> dict[str, Any]:
        d = row["doc"]
        out = {"frame_id": row["frame_id"], "project_id": row["project_id"], "status": row["status"],
               "frame_type": d.get("frame_type", "light"), "filter": d.get("filter", "UNKNOWN"),
               "filter_raw": d.get("filter_raw"), "target_streams": list(d.get("target_streams") or []),
               "filename": d.get("filename", ""), "size": row["size"], "sha256": row["sha256"],
               "exposure_s": d.get("exposure_s"), "date_obs": d.get("date_obs"),
               "rig_id": d.get("rig_id"), "session_id": d.get("session_id"),
               "reject_reason": d.get("reject_reason"), "reject_detail": d.get("reject_detail"),
               "quarantine_reason": d.get("quarantine_reason"), "flags": list(d.get("flags") or []),
               "gold_seq": d.get("gold_seq"), "created_at": d.get("created_at", ""),
               "uploaded_at": d.get("uploaded_at"), "updated_at": d.get("updated_at", "")}
        for key in ("metrics", "calibration"):
            if d.get(key) is not None:
                out[key] = d[key]
        if contributor is not None:
            out["contributor"] = contributor
        return out

    # -- access --------------------------------------------------------------
    def access(self, request: Request, ref: str, *, write: bool) -> tuple[Caller, dict[str, Any], dict[str, Any]]:
        who = self.auth.caller(request, write=write)
        row = self.auth.project(ref)
        self.auth.scoped(who, row)
        member = self.projects._own(row, who)
        return who, row, member

    @staticmethod
    def is_maintainer(member: dict[str, Any]) -> bool:
        return member["doc"].get("role") in ("owner", "maintainer")

    @staticmethod
    def require_active(member: dict[str, Any]) -> None:
        status = member["doc"].get("status", "active")
        if status == "pending":
            raise ApiError("membership_pending", "The coordinator has not approved your membership yet.")
        if status != "active":
            raise ApiError("not_member", "You are not a member of this project.")

    # -- filter resolution (plan 5.2, the part init needs) -------------------
    def resolve_filter(self, member: dict[str, Any], frame_type: str,
                       header: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        raw = header["FILTER"] if isinstance(header.get("FILTER"), str) else None
        if frame_type in CAL_TYPES:
            return "CAL", {"raw": raw or "", "canonical": "UNKNOWN", "confidence": None,
                           "source": "none", "bandwidth_nm": None, "vendor": None,
                           "quarantine_reason": None}
        canonical, source, confidence = "UNKNOWN", "none", None
        if raw is not None:
            wanted = raw.strip().casefold()
            for entry in member["doc"].get("filter_mapping") or []:
                if str(entry.get("raw", "")).strip().casefold() == wanted:
                    if entry.get("canonical") == "SKIP":
                        raise validation_failed(("header.FILTER", "is mapped to SKIP in your membership"))
                    canonical, source, confidence = entry["canonical"], "contributor_mapping", "user"
                    break
            else:
                if raw.strip().upper() in defaults.CANONICAL_FILTERS:
                    canonical, source, confidence = raw.strip().upper(), "alias_table", "exact"
                else:
                    found = defaults.contract_filters([raw])
                    if found:
                        canonical, source, confidence = found[0], "pattern", "pattern"
        if canonical == "UNKNOWN" and header.get("BAYERPAT"):
            canonical, source, confidence = "OSC", "sensor_context", "inferred"
        return canonical, {"raw": raw or "", "canonical": canonical, "confidence": confidence,
                           "source": source, "bandwidth_nm": None, "vendor": None,
                           "quarantine_reason": None}

    # -- the rig a frame belongs to ------------------------------------------
    def rig_for(self, who: Caller, body: dict[str, Any], frame_type: str) -> str:
        hint = (body.get("rig_hint") or {}).get("rig_id")
        if hint and self.rigs.get(who.user_id, hint):
            return hint
        header = body["header"]
        fields = rig_fields(header)
        osc = fields["bayer_pattern"] is not None
        if frame_type != "light" and header.get("INSTRUME") and optics_unknown(header):
            camera = fields["camera"].lower()
            tenths = f"{round(fields['pixel_size_um'] * 10) / 10:.1f}"
            fits = []
            for r in self.store._all("SELECT doc FROM stack_rigs WHERE user_id = ?", who.user_id):
                rig = json.loads(r["doc"])
                if (str(rig["camera"]).strip().lower() == camera
                        and (not (header.get("XPIXSZ") or 0) > 0
                             or f"{round(rig['pixel_size_um'] * 10) / 10:.1f}" == tenths)
                        and int(rig["binning"]) == fields["binning"] and bool(rig.get("osc")) == osc
                        and (not rig.get("width_px") or not fields["width_px"] or rig["width_px"] == fields["width_px"])
                        and (not rig.get("height_px") or not fields["height_px"] or rig["height_px"] == fields["height_px"])
                        and rig["telescope"] != "Unknown telescope" and rig["focal_length_mm"] > 1):
                    fits.append(rig["rig_id"])
            if len(fits) == 1:
                return fits[0]
        rid = rig_id_of(fingerprint(fields["camera"], fields["telescope"], fields["focal_length_mm"],
                                    fields["pixel_size_um"], fields["binning"], osc))
        if self.rigs.get(who.user_id, rid) is None:
            doc = {"rig_id": rid, "camera": fields["camera"], "telescope": fields["telescope"],
                   "focal_length_mm": fields["focal_length_mm"], "pixel_size_um": fields["pixel_size_um"],
                   "binning": fields["binning"], "osc": osc,
                   "pixel_scale_arcsec": pixel_scale(fields["pixel_size_um"], fields["binning"],
                                                     fields["focal_length_mm"]),
                   "frame_count": 0, "created_at": now_iso(), "confirmed": False,
                   "name": f"{fields['camera']} on {fields['telescope']}"[:80]}
            if osc:
                doc["bayer_pattern"] = fields["bayer_pattern"]
            for key in ("width_px", "height_px"):
                if fields[key]:
                    doc[key] = fields[key]
            self.rigs._save(who, doc, True)
        return rid

    # -- quota ---------------------------------------------------------------
    def quota(self, project: dict[str, Any], who: Caller, size: int) -> None:
        quotas = {**defaults.QUOTAS, **(project["doc"].get("quotas") or {})}
        mine = self.store._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM stack_frames WHERE project_id = ?"
            " AND user_id = ? AND status NOT IN ('withdrawn', 'rejected')", project["project_id"], who.user_id)
        every = self.store._one(
            "SELECT COALESCE(SUM(size), 0) AS b FROM stack_frames WHERE project_id = ?"
            " AND status NOT IN ('withdrawn', 'rejected')", project["project_id"])
        if mine["b"] + size > quotas["contributor_max_bytes"]:
            raise ApiError("quota_exceeded", "This upload would exceed your storage quota in this project.",
                           quota="contributor_bytes", retry_after_s=3600)
        if mine["n"] + 1 > quotas["contributor_max_frames"]:
            raise ApiError("quota_exceeded", "You have reached the frame limit for this project.",
                           quota="contributor_frames", retry_after_s=3600)
        if every["b"] + size > quotas["project_max_bytes"]:
            raise ApiError("quota_exceeded", "This upload would exceed the project's storage quota.",
                           quota="project_bytes", retry_after_s=3600)
        if not self.up.room(size):
            raise ApiError("quota_exceeded", "The server has no room left for frames; tell the coordinator.",
                           quota="project_bytes", retry_after_s=3600)

    # -- init ----------------------------------------------------------------
    def init_response(self, row: dict[str, Any], resumed: bool, upload: dict[str, Any]) -> dict[str, Any]:
        d = row["doc"]
        return ok(message="Upload resumed." if resumed else "Upload started.",
                  frame_id=row["frame_id"], status="uploading", resumed=resumed,
                  frame_type=d["frame_type"], filter=d["filter"], resolution=d["resolution"],
                  rig_id=d.get("rig_id"), upload=upload)

    def do_init(self, who: Caller, project: dict[str, Any], member: dict[str, Any],
                body: dict[str, Any], origin: str) -> dict[str, Any]:
        d = project["doc"]
        if d.get("status") == "deleted":
            raise ApiError("project_deleted", "This project is scheduled for deletion.")
        if d.get("status", "open") != "open":
            raise ApiError("project_not_open", "This project is not open for uploads.")
        self.require_active(member)
        body = validate_request(body, library=False)
        header = body["header"]
        frame_type = frame_type_of(header.get("IMAGETYP"))
        if frame_type is None:
            raise ApiError("unsupported_frame_type", "IMAGETYP is not a frame type this service"
                                                     " accepts (light, dark, flat, bias, dark flat or a master of those).")
        policy = {**defaults.POLICY, **(d.get("policy") or {})}
        if policy.get("require_coordinate_strip") and body.get("stripped") is not True:
            raise ApiError("strip_required", "This project requires location cards to be stripped before"
                                             " upload; set `stripped: true` after removing them.",
                           errors=[{"field": "stripped", "message": "must be true for this project"}])
        if frame_type == "light":
            if isinstance(header.get("EXPTIME"), (int, float)) and header["EXPTIME"] < policy["min_exposure_s"]:
                raise ApiError("policy_violation", f"Exposures under {policy['min_exposure_s']} s are not"
                                                   " accepted in this project.", reason="below_min_exposure")
            if header.get("BAYERPAT") and not policy["osc_allowed"]:
                raise ApiError("policy_violation", "This project does not accept one-shot-colour (Bayer)"
                                                   " frames.", reason="osc_not_allowed")
        filt, resolution = self.resolve_filter(member, frame_type, header)
        with self.up.lock:
            sha = body["sha256"]
            rows = self.store._all("SELECT frame_id FROM stack_frames WHERE project_id = ? AND sha256 = ?"
                                   " AND status <> 'withdrawn'", project["project_id"], sha)
            frames = [f for f in (self.row(r["frame_id"]) for r in rows)
                      if f["doc"].get("reject_reason") != "duplicate"]
            frames.sort(key=lambda r: r["frame_id"])
            mine = next((f for f in frames if f["user_id"] == who.user_id and f["status"] == "uploading"), None)
            if mine is not None:
                if mine["size"] != body["size"]:
                    raise validation_failed(("size", "does not match the upload already in progress"
                                                     " for this sha256"))
                self.up.prepare_resume(mine["upload_id"], mine["path"])
                mine["doc"]["url_expires"] = time.time() + URL_SECONDS
                mine["doc"]["expires"] = time.time() + UPLOAD_SECONDS
                self.save(mine)
                return self.init_response(mine, True, self.up.instructions(mine["upload_id"], mine["size"], origin))
            if frames:
                other = next((f for f in frames if f["user_id"] == who.user_id), frames[0])
                extra: dict[str, Any] = {"status": other["status"]}
                if other["user_id"] == who.user_id:
                    extra["existing_frame"] = {"frame_id": other["frame_id"], "status": other["status"],
                                               "filter": other["doc"].get("filter", "UNKNOWN"),
                                               "uploaded_at": other["doc"].get("uploaded_at")}
                raise ApiError("duplicate", "A frame with this SHA-256 is already in the project."
                                            " Treat it as uploaded.", **extra)
            self.quota(project, who, body["size"])
            frame_id = new_frame_id()
            upload_id = secrets.token_hex(32)
            rel = f"{project['project_id']}/{who.user_id}/{frame_id}{extension_of(body['filename'])}"
            now = now_iso()
            doc = {"frame_type": frame_type, "filter": filt, "filter_raw": resolution["raw"] if resolution["raw"] else None,
                   "resolution": resolution, "filename": body["filename"],
                   "exposure_s": header.get("EXPTIME") if isinstance(header.get("EXPTIME"), (int, float)) else None,
                   "date_obs": header.get("DATE-OBS") if isinstance(header.get("DATE-OBS"), str) else None,
                   "rig_id": self.rig_for(who, body, frame_type), "stripped": body.get("stripped") is True,
                   "header": header, "created_at": now, "uploaded_at": None, "updated_at": now,
                   "url_expires": time.time() + URL_SECONDS, "expires": time.time() + UPLOAD_SECONDS}
            self.store._run(
                "INSERT INTO stack_frames (frame_id, project_id, user_id, status, sha256, size, path,"
                " upload_id, doc) VALUES (?, ?, ?, 'uploading', ?, ?, ?, ?, ?)",
                frame_id, project["project_id"], who.user_id, sha, body["size"], rel, upload_id, json.dumps(doc))
            row = self.row(frame_id)
            return self.init_response(row, False, self.up.instructions(upload_id, body["size"], origin))

    # -- complete ------------------------------------------------------------
    def do_complete(self, who: Caller, project: dict[str, Any], member: dict[str, Any],
                    frame_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if project["doc"].get("status") == "deleted":
            raise ApiError("project_deleted", "This project is scheduled for deletion.")
        self.require_active(member)
        row = self.row(frame_id) if FRAME_ID.fullmatch(frame_id) else None
        if row is None or row["user_id"] != who.user_id or row["project_id"] != project["project_id"]:
            raise ApiError("not_found", "No such frame.")
        if row["status"] != "uploading":
            return self.already(row)
        with self.up.lock:
            row = self.row(frame_id)
            if row["status"] != "uploading":
                return self.already(row)
            try:
                self.up.verify(row["upload_id"], row["path"], row["size"], row["doc"]["filename"],
                               body.get("etags"))
            except AbortUpload as abort:
                self.up.discard(row["upload_id"], row["path"])
                self.store._run("DELETE FROM stack_frames WHERE frame_id = ?", frame_id)
                raise abort.error
            older = self.store._one(
                "SELECT 1 AS x FROM stack_frames WHERE project_id = ? AND sha256 = ? AND frame_id < ?"
                " AND status <> 'withdrawn' AND COALESCE(json_extract(doc, '$.reject_reason'), '') <> 'duplicate'",
                project["project_id"], row["sha256"], frame_id)
            now = now_iso()
            row["doc"]["uploaded_at"] = now
            if older is not None:
                row["doc"].update(reject_reason="duplicate", reject_detail="an identical frame was uploaded earlier")
                self.up.discard(row["upload_id"], row["path"])
                self.save(row, status="rejected", upload_id="")
                return ok(message="An identical frame was uploaded earlier, so this one was rejected"
                                  " as a duplicate.", frame_id=frame_id, status="rejected", already_complete=False)
            self.up.finish(row["upload_id"], row["path"])
            row["doc"].pop("url_expires", None)
            row["doc"].pop("expires", None)
            self.save(row, status="uploaded", upload_id="", received=time.time())
        self.announce(project, frame_id)
        if self.after_upload is not None:
            try:
                self.after_upload(project, self.row(frame_id))
            except OSError:
                pass        # a collected copy is a convenience; the upload is done
        return ok(message="Upload complete. The frame is queued for analysis.", frame_id=frame_id,
                  status="uploaded", already_complete=False)

    @staticmethod
    def already(row: dict[str, Any]) -> dict[str, Any]:
        if row["status"] == "withdrawn":
            raise ApiError("frame_state", "This frame was withdrawn.", status="withdrawn")
        return ok(message="Already complete.", frame_id=row["frame_id"], status=row["status"],
                  already_complete=True)

    # -- withdraw and the sweep ----------------------------------------------
    def do_withdraw(self, who: Caller, project: dict[str, Any], member: dict[str, Any],
                    frame_id: str) -> dict[str, Any]:
        row = self.row(frame_id) if FRAME_ID.fullmatch(frame_id) else None
        if row is None or row["project_id"] != project["project_id"] or (
                row["user_id"] != who.user_id and not self.is_maintainer(member)):
            raise ApiError("not_found", "No such frame.")
        d = row["doc"]
        if row["status"] == "withdrawn":
            return ok(frame_id=frame_id, status="withdrawn", raw_delete_after=d.get("raw_delete_after", now_iso()))
        with self.up.lock:
            if row["status"] == "uploading":
                self.up.discard(row["upload_id"], row["path"])
                d["raw_delete_after"] = now_iso()
                self.save(row, status="withdrawn", upload_id="")
            else:
                d["raw_delete_after"] = iso_plus(WITHDRAW_SECONDS)
                d["withdrawn_at"] = now_iso()
                d["withdrawn_by"] = who.user_id
                self.save(row, status="withdrawn")
                self.announce(project, frame_id)
        return ok(message="Frame withdrawn.", frame_id=frame_id, status="withdrawn",
                  raw_delete_after=d["raw_delete_after"])

    def sweep(self, now: float | None = None) -> int:
        """Abort `uploading` frames nobody has touched for three days."""
        now = time.time() if now is None else now
        gone = 0
        for r in self.store._all("SELECT frame_id FROM stack_frames WHERE status = 'uploading'"):
            with self.up.lock:
                row = self.row(r["frame_id"])
                if row is None or row["status"] != "uploading" or row["doc"].get("expires", now + 1) > now:
                    continue
                self.up.discard(row["upload_id"], row["path"])
                self.store._run("DELETE FROM stack_frames WHERE frame_id = ?", row["frame_id"])
                gone += 1
        return gone

    # -- routes --------------------------------------------------------------
    def _routes(self, router: APIRouter) -> None:
        @router.post("/projects/{project}/frames/init")
        async def init(project: str, request: Request) -> dict[str, Any]:
            who, row, member = self.access(request, project, write=True)
            return self.do_init(who, row, member, await json_body(request), origin_of(request))

        @router.post("/projects/{project}/frames/{frame_id}/complete")
        async def complete(project: str, frame_id: str, request: Request) -> dict[str, Any]:
            who, row, member = self.access(request, project, write=True)
            return self.do_complete(who, row, member, frame_id, await json_body(request))

        @router.post("/projects/{project}/frames/{frame_id}/withdraw")
        async def withdraw(project: str, frame_id: str, request: Request) -> dict[str, Any]:
            who, row, member = self.access(request, project, write=True)
            return self.do_withdraw(who, row, member, frame_id)

        @router.get("/projects/{project}/frames/{frame_id}")
        async def get(project: str, frame_id: str, request: Request) -> dict[str, Any]:
            who, row, member = self.access(request, project, write=False)
            frame = self.row(frame_id) if FRAME_ID.fullmatch(frame_id) else None
            if frame is None or frame["project_id"] != row["project_id"] or (
                    frame["user_id"] != who.user_id and not self.is_maintainer(member)):
                raise ApiError("not_found", "No such frame.")
            name = None
            if frame["user_id"] != who.user_id:
                owner = self.projects.member(row["project_id"], frame["user_id"])
                name = owner["doc"].get("display_name", "") if owner else ""
            return ok(frame=self.wire(frame, name))

        @router.get("/projects/{project}/frames")
        async def listing(project: str, request: Request) -> dict[str, Any]:
            who, row, member = self.access(request, project, write=False)
            q = request.query_params
            maintainer = self.is_maintainer(member)
            mine = q.get("mine") == "1" or not maintainer
            contributor = q.get("contributor")
            if contributor is not None:
                if not maintainer:
                    raise ApiError("not_maintainer", "Only the owner and maintainers can list another"
                                                     " member's frames.")
                if not 2 <= len(contributor) <= 40:
                    raise validation_failed(("contributor", "must be 2 to 40 characters"))
                mine = False
            if q.get("mine") not in (None, "0", "1"):
                raise validation_failed(("mine", "must be 0 or 1"))
            status = q.get("status")
            if status is not None and status not in (
                    "uploading", "uploaded", "preparing", "accepted", "rejected", "quarantined",
                    "accumulated", "in_gold", "failed", "withdrawn"):
                raise validation_failed(("status", "is not a frame status"))
            try:
                limit = int(q.get("limit", "50"))
            except ValueError:
                raise validation_failed(("limit", "must be an integer"))
            if not 1 <= limit <= 100:
                raise validation_failed(("limit", "must be 1 to 100"))
            cursor = q.get("cursor")
            after = None
            if cursor:
                try:
                    after = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
                except (ValueError, binascii.Error, UnicodeError):
                    after = None
                if after is None or not FRAME_ID.fullmatch(after):
                    raise ApiError("invalid_request", "That cursor is not one this service gave you.")
            since = q.get("since")
            if since is not None and not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z", since):
                raise validation_failed(("since", "must be an ISO-8601 UTC timestamp"))
            names = {m["user_id"]: m["doc"].get("display_name", "")
                     for m in self.projects._members(row["project_id"])}
            sql, args = "SELECT * FROM stack_frames WHERE project_id = ?", [row["project_id"]]
            if mine:
                sql += " AND user_id = ?"
                args.append(who.user_id)
            elif contributor is not None:
                ids = [u for u, n in names.items() if n.casefold() == contributor.casefold()
                       and (self.projects.member(row["project_id"], u) or {"doc": {}})["doc"].get("status") != "removed"]
                if not ids:
                    return ok(frames=[], next_cursor=None)
                sql += " AND user_id = ?"
                args.append(ids[0])
            if status:
                sql += " AND status = ?"
                args.append(status)
            if after:
                sql += " AND frame_id < ?"
                args.append(after)
            sql += " ORDER BY frame_id DESC"
            out, more = [], False
            for r in self.store._all(sql, *args):
                r["doc"] = json.loads(r["doc"] or "{}")
                if q.get("filter") and r["doc"].get("filter") != q["filter"]:
                    continue
                if since and r["doc"].get("created_at", "") < since:
                    continue
                if len(out) == limit:
                    more = True
                    break
                out.append(r)
            frames = [self.wire(r, None if r["user_id"] == who.user_id or mine else names.get(r["user_id"], ""))
                      for r in out]
            nxt = base64.urlsafe_b64encode(out[-1]["frame_id"].encode()).decode().rstrip("=") if more else None
            return ok(frames=frames, next_cursor=nxt)
