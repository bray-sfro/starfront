#!/usr/bin/env python3
"""Server-side import of a contributor's historical frames into a collaboration.

For frames that were shot by one of a person's own telescopes but never sent
(an old season sitting in Dropbox, or on a rig's disk), when sending them
through the rig's own auto-upload is not practical. The result is the same
as the rig's auto-upload (server/agentuploads.py, `finish`) would give: the
file lands at Files.path_of under the rig's owner, the name gets the rig's
`<agent8>-` prefix (AgentUploads.stored_name), the telescope tag is the rig's
label (AgentUploads.label), the header facts are read the same way, the
sha256 and the rig and its accepted task are recorded (set_file_origin), and
the upload rules judge it (Files.recheck). The page's PUT rules are kept as
well: a file already here at the same size is not taken again, and nothing
is taken past the project's quota or the disk's KEEP_FREE (plus a margin).

Every call is the app's own: this imports server.store, server.files,
server.agentuploads and server.uploadrules and repeats the few lines of the
finish handler that run after the bytes are written. Nothing is reimplemented.

Run as the service user, with the app's venv, at low priority, e.g.:

    sudo -u starfront env STARFRONT_DATA=/var/lib/starfront \
        nice -n 19 ionice -c3 /opt/starfront/venv/bin/python \
        /opt/starfront/tools/import_historical.py --manifest batch.csv \
        --project d07d82eb861e --user 328005736212725762 --dry-run

Manifest (CSV, header row): rig,kind,filter,night,size,target_name and, per
row, exactly one of url (a temporary download link; single-use, never
logged, never touched in --dry-run) or src_path (a file already on this
machine). Optional: content_hash (Dropbox's content hash, checked when
given), sha256 (checked when given), dropbox_path / local_path (logged only).

`rig` is an agent id, its first 8 hex digits, or the rig's label as the
files page shows it (fsq, fsq106, vsd100, rasa; "twin" means fsq). It must
be one of --user's own telescopes. `target_name` is the frame's file name
as it should be kept, without the rig prefix (it is added).

Safe to run again: rows already in the database at the same size are
skipped before anything is downloaded, content already held (by sha256)
is never stored twice, and the state file keeps what happened to each row.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402

from server import auth, uploadrules  # noqa: E402
from server import agentuploads as agentuploads_mod  # noqa: E402
from server.agentuploads import (AgentUploads, _header_or_empty, _place,  # noqa: E402
                                 of_this_collaboration)
from server.files import (KEEP_FREE, MAX_BYTES, UPLOAD_KINDS, Files,  # noqa: E402
                          clean_name, looks_like_data)
from server.store import Store  # noqa: E402

GiB = 1024 ** 3
BLOCK = 4 * 1024 ** 2          # Dropbox content_hash block size, and our read size
REQUIRED = ("rig", "kind", "filter", "night", "size", "target_name")
ALIASES = {"twin": "fsq", "twinfsq": "fsq", "twin-fsq": "fsq", "twin_fsq": "fsq",
           "twin fsq": "fsq"}
TEST_MARK = "IMPORTTEST"
#: Link schemes taken in a manifest (the tests add file://).
URL_SCHEMES = ("https://",)


class Stop(Exception):
    """Stop the whole run cleanly (disk, quota, project closed)."""


class RowFail(Exception):
    """This row cannot be imported; carry on with the next."""


# ---------------------------------------------------------------------------
# The app's objects, bound to a database and a files folder
# ---------------------------------------------------------------------------

def _readonly_store(db: Path) -> Store:
    """A Store whose connection cannot write: for --dry-run, so not even the
    schema statements the constructor runs touch the live database."""
    store = Store.__new__(Store)
    store.path = Path(db)
    store._lock = threading.RLock()
    store._db = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False,
                                timeout=10.0)
    store._db.row_factory = sqlite3.Row
    store._db.execute("PRAGMA busy_timeout=10000")
    return store


def build(db: Path, folder: Path, readonly: bool) -> tuple[Store, Files, AgentUploads]:
    store = _readonly_store(db) if readonly else Store(db)
    app = FastAPI()      # throwaway: the routes are registered on it and never served
    files = Files(app, store, folder, lambda _t: None, lambda _w, _p: False, [], "",
                  lambda: "")
    up = AgentUploads(app, store, files)
    return store, files, up


def rigs_of(store: Store, up: AgentUploads, user_id: str) -> dict[str, dict[str, Any]]:
    """Every way a manifest may name one of the person's telescopes. Only the
    agent's id, name, presence and owner are used; nothing else of the row."""
    out: dict[str, list[dict[str, Any]]] = {}
    for a in store.agents_of(user_id):
        rig = {"id": a["id"], "name": a.get("name"), "presence": a.get("presence"),
               "owner_id": a.get("owner_id")}
        label = up.label(store.agent(a["id"]) or a)
        rig["label"] = label
        for key in {a["id"], a["id"][:8], label.lower()}:
            out.setdefault(key, []).append(rig)
    return {k: v[0] for k, v in out.items() if len(v) == 1}


def resolve_rig(rigs: dict[str, dict[str, Any]], text: str) -> dict[str, Any] | None:
    key = (text or "").strip().lower()
    key = ALIASES.get(key, key)
    return rigs.get(key)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class State:
    def __init__(self, folder: Path, write: bool) -> None:
        self.folder = Path(folder)
        self.write = write
        self.path = self.folder / "state.json"
        self.log_path = self.folder / "log.jsonl"
        self.rows: dict[str, Any] = {}
        if self.path.is_file():
            self.rows = json.loads(self.path.read_text() or "{}")
        self._lock_handle = None
        if write:
            self.folder.mkdir(parents=True, exist_ok=True)
            self._lock_handle = open(self.folder / ".lock", "w")
            try:
                fcntl.flock(self._lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit("another import is running (state folder is locked)")

    def note(self, key: str, status: str, **info: Any) -> None:
        entry = {"status": status, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **info}
        if not self.write:
            return
        self.rows[key] = entry
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.rows, indent=1, sort_keys=True))
        os.replace(tmp, self.path)
        with open(self.log_path, "a") as log:
            log.write(json.dumps({"key": key, **entry}) + "\n")

    def event(self, **info: Any) -> None:
        if self.write:
            with open(self.log_path, "a") as log:
                log.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **info}) + "\n")


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _night(text: str) -> dt.date | None:
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def read_manifest(path: Path, rigs: dict[str, dict[str, Any]], up: AgentUploads
                  ) -> tuple[list[dict[str, Any]], list[str]]:
    rows, errors = [], []
    with open(path, newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            return [], ["manifest is missing columns: " + ", ".join(missing)]
        if "url" not in reader.fieldnames and "src_path" not in reader.fieldnames:
            return [], ["manifest needs a url or a src_path column"]
        for line, raw in enumerate(reader, start=2):
            r = {k: (v or "").strip() for k, v in raw.items() if k}
            r["line"] = line
            why = []
            rig = resolve_rig(rigs, r["rig"])
            if rig is None:
                why.append(f"rig {r['rig']!r} is not one of this person's telescopes (or is ambiguous)")
            kind = r["kind"].lower()
            if kind not in UPLOAD_KINDS:
                why.append(f"kind {r['kind']!r} is not one of {', '.join(UPLOAD_KINDS)}")
            clean = clean_name(r["target_name"])
            if not clean:
                why.append(f"target_name {r['target_name']!r} is not a frame name")
            try:
                size = int(r["size"])
            except ValueError:
                size = 0
            if size <= 0 or size > MAX_BYTES:
                why.append(f"size {r['size']!r} is not between 1 and {MAX_BYTES}")
            if r["night"] and _night(r["night"]) is None:
                why.append(f"night {r['night']!r} is not YYYY-MM-DD")
            url, src = r.get("url", ""), r.get("src_path", "")
            if bool(url) == bool(src):
                why.append("exactly one of url and src_path must be given")
            if url and not url.lower().startswith(URL_SCHEMES):
                why.append("url must be https")
            name = up.stored_name(rig, clean) if rig and clean else ""
            r.update(kind=kind, clean=clean, size=size, rig_obj=rig, name=name,
                     key=f"{rig['id'] if rig else r['rig']}|{kind}|{name or r['target_name']}",
                     source=r.get("dropbox_path") or r.get("local_path") or src or "(url)")
            if why:
                errors.append(f"line {line}: " + "; ".join(why))
                r["invalid"] = "; ".join(why)
            rows.append(r)
    seen: dict[str, int] = {}
    for r in rows:
        if r.get("invalid"):
            continue
        if r["key"] in seen:
            msg = (f"line {r['line']}: {r['name']} ({r['kind']}) is also on line {seen[r['key']]}"
                   " - two frames would share one stored name; make target_name unique"
                   " (for example put the night in it)")
            errors.append(msg)
            r["invalid"] = "duplicate stored name in manifest"
        else:
            seen[r["key"]] = r["line"]
    return rows, errors


# ---------------------------------------------------------------------------
# One row
# ---------------------------------------------------------------------------

def _night_consistent(row_meta: dict[str, Any], night: str) -> bool:
    """An existing row of the same name and size is the same frame only if it
    was shot that night (DATE-OBS, UTC, is the night's date or the next)."""
    want = _night(night)
    date = str((row_meta or {}).get("date") or "")[:10]
    have = _night(date)
    if want is None or have is None:
        return True
    return have in (want, want + dt.timedelta(days=1))


def _loads(text: Any) -> dict[str, Any]:
    if isinstance(text, dict):
        return text
    try:
        out = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def precheck(r: dict[str, Any], store: Store, files: Files, project_id: str, user_id: str,
             allow_replace: bool) -> tuple[str, str]:
    """What would happen to a row before any byte is moved:
    ('import'|'already'|'fail', why)."""
    if r.get("invalid"):
        return "fail", r["invalid"]
    existing = store.file(project_id, user_id, r["name"], r["kind"])
    final = files.path_of(project_id, user_id, r["name"], r["kind"])
    if existing is not None and final.is_file():
        if int(existing["bytes"]) == r["size"]:
            if _night_consistent(_loads(existing.get("meta")), r["night"]):
                return "already", "a file of this name and size is already here (the PUT's already rule)"
            return "fail", (f"name collision: {r['name']} is already here from another night "
                            f"({str(_loads(existing.get('meta')).get('date'))[:10]}); give it a unique target_name")
        if not allow_replace:
            return "fail", (f"{r['name']} is already here at another size "
                            f"({existing['bytes']} bytes); not replaced without --allow-replace")
    return "import", ""


def room(store: Store, files: Files, up: AgentUploads, project: dict[str, Any],
         size: int, replacing: int, floor: int) -> None:
    free = files.stats()["free"]
    if free - up.reserved() - size < floor:
        raise Stop(f"disk: {free / GiB:.1f} GiB free, {up.reserved() / GiB:.1f} GiB reserved by open"
                   f" uploads, and the floor is {floor / GiB:.1f} GiB; stopping")
    quota = files.quota_of(project)
    held = int(store.file_totals(project["id"])["bytes"])
    if quota == 0 or held - replacing + up.reserved(project["id"]) + size > quota:
        raise Stop(f"quota: the project holds {held / GiB:.1f} GiB of {quota / GiB:g} GiB; stopping")


class Hashes:
    def __init__(self) -> None:
        self.sha = hashlib.sha256()
        self.dbx = hashlib.sha256()
        self.size = 0
        self.head = b""

    def block(self, data: bytes) -> None:
        if len(self.head) < 80:
            self.head += data[:80 - len(self.head)]
        self.sha.update(data)
        self.dbx.update(hashlib.sha256(data).digest())
        self.size += len(data)


def _read_blocks(stream, limit: int):
    """Exactly BLOCK-sized pieces (the last may be short), so the Dropbox
    content hash is computed on the same boundaries Dropbox uses."""
    while True:
        buf = bytearray()
        while len(buf) < BLOCK:
            piece = stream.read(BLOCK - len(buf))
            if not piece:
                break
            buf += piece
        if not buf:
            return
        yield bytes(buf)
        if len(buf) < BLOCK:
            return


def fetch_url(url: str, part: Path, size: int, rate: str, timeout: int) -> Hashes:
    """Stream a URL into the part file through curl, hashing as it lands.
    The URL goes to curl on stdin (a config line), never on a command line."""
    cmd = ["curl", "-K", "-", "-fsS", "--location", "--max-time", str(timeout),
           "--connect-timeout", "30", "--limit-rate", rate, "-o", "-"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    assert proc.stdin and proc.stdout and proc.stderr
    proc.stdin.write(('url = "' + url.replace('\\', '\\\\').replace('"', '\\"') + '"\n').encode())
    proc.stdin.close()
    h = Hashes()
    try:
        with open(part, "wb") as out:
            for data in _read_blocks(proc.stdout, size):
                h.block(data)
                if h.size > size:
                    raise RowFail(f"more arrived than the manifest size ({size})")
                out.write(data)
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    err = proc.stderr.read().decode(errors="replace").strip()
    code = proc.wait()
    if code != 0:
        err = err.replace(url, "<url>")[:300]
        raise RowFail(f"download failed (curl exit {code}: {err}); the link may be used up or"
                      " expired - get a new one and run again")
    return h


def fetch_path(src: Path, part: Path, size: int, move: bool, rate_bytes: int) -> Hashes:
    """A file already on this machine: moved into place when asked and on the
    same disk (no second copy), else copied at the rate cap."""
    h = Hashes()
    if move and src.stat().st_dev == part.parent.stat().st_dev:
        os.replace(src, part)
        with open(part, "rb") as handle:
            for data in _read_blocks(handle, size):
                h.block(data)
        return h
    started = time.monotonic()
    with open(src, "rb") as handle, open(part, "wb") as out:
        for data in _read_blocks(handle, size):
            h.block(data)
            out.write(data)
            if rate_bytes > 0:
                ahead = h.size / rate_bytes - (time.monotonic() - started)
                if ahead > 0:
                    time.sleep(ahead)
    return h


def _rate_bytes(rate: str) -> int:
    rate = rate.strip().upper()
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}.get(rate[-1:], 1)
    num = rate[:-1] if rate[-1:] in "KMG" else rate
    return int(float(num) * mult)


def import_row(r: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Move, check, and register one frame; ('done'|'skipped', info)."""
    store, files, up = ctx["store"], ctx["files"], ctx["up"]
    project, user_id = ctx["project"], ctx["user"]
    rig, name, kind, size = r["rig_obj"], r["name"], r["kind"], r["size"]
    task = store.agent_task_in(rig["id"], project["id"])
    if task is None:
        # The rig's own auto-upload is refused without one (403); so is this.
        raise RowFail(f"{rig['label']} has no accepted task on this collaboration")
    existing = store.file(project["id"], user_id, name, kind)
    replacing = int(existing["bytes"]) if existing is not None else 0
    room(store, files, up, project, size, replacing, ctx["floor"])
    final = files.path_of(project["id"], user_id, name, kind)
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")          # where the PUT writes it
    src = Path(r["src_path"]) if r.get("src_path") else None
    moved_src = False
    ctx["current_part"] = part

    def give_back() -> None:
        """Undo the transfer: a moved source goes back, a download is dropped."""
        if moved_src and part.exists() and src is not None and not src.exists():
            os.replace(part, src)
        else:
            try:
                part.unlink()
            except OSError:
                pass

    try:
        if src is not None:
            if not src.is_file():
                raise RowFail(f"src_path {src} is not a file")
            if src.stat().st_size != size:
                raise RowFail(f"src_path is {src.stat().st_size} bytes, the manifest says {size}")
            moved_src = ctx["move_src"] and src.stat().st_dev == final.parent.stat().st_dev
            h = fetch_path(src, part, size, ctx["move_src"], ctx["rate_bytes"])
        else:
            h = fetch_url(r["url"], part, size, ctx["rate"], ctx["timeout"])
        if h.size != size:
            raise RowFail(f"only {h.size} of {size} bytes arrived")
        sha, dbx = h.sha.hexdigest(), h.dbx.hexdigest()
        if r.get("content_hash") and r["content_hash"].lower() != dbx:
            raise RowFail("the Dropbox content_hash does not match what arrived")
        if r.get("sha256") and r["sha256"].lower() != sha:
            raise RowFail("the sha256 does not match what arrived")
        if not looks_like_data(h.head, name):
            raise RowFail(f"{name} does not start like a "
                          f"{'XISF' if name.lower().endswith('.xisf') else 'FITS'} file")
        # As AgentUploads.start: content already held for this person is not taken twice.
        same = store.file_by_hash(project["id"], user_id, kind, sha)
        if same is not None:
            give_back()
            return "skipped", {"reason": f"same content (sha256) is already here as {same['name']}",
                               "sha256": sha, "name": name}
        # As AgentUploads finish, from here on.
        header = _header_or_empty(part)
        meta = uploadrules.facts(header, name) if header else \
            uploadrules.facts_of_file(part, name)
        if kind == "light":
            ok, why = of_this_collaboration(header.get("cards") or {}, project)
            if not ok:
                raise RowFail("not a light of this collaboration: " + why)
        warn = ""
        if meta.get("headerKind") in UPLOAD_KINDS and meta["headerKind"] != kind:
            warn = f"header says {meta['headerKind']}, manifest says {kind}; kept as {kind}"
        _place(part, final)
    except BaseException:
        give_back()
        ctx["current_part"] = None
        raise
    ctx["current_part"] = None
    label = up.label(store.agent(rig["id"]) or rig)
    file_id = secrets.token_hex(8)
    store.add_file(file_id, project["id"], user_id, name, size, label, kind, meta)
    stored = store.file(project["id"], user_id, name, kind)
    store.set_file_origin(stored["id"], sha, rig["id"], str(task["id"]))
    moved = files.recheck(project, user_id, only_light=stored["id"] if kind == "light" else None)
    stored = store.file(project["id"], user_id, name, kind)
    return "done", {"name": name, "sha256": sha, "bytes": size, "telescope": label,
                    "fileStatus": stored.get("status") or "", "moved": moved,
                    "warning": warn, "source": r["source"]}


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    data = Path(auth.setting("DATA") or "/var/lib/starfront")
    db = Path(args.db or data / "collab.sqlite")
    folder = Path(args.files or auth.setting("FILES") or data / "files")
    readonly = args.dry_run
    store, files, up = build(db, folder, readonly)
    project = store.project(args.project)
    if project is None:
        print(f"no such project {args.project}", file=sys.stderr)
        return 2
    if store.user(args.user) is None:
        print(f"no such user {args.user}", file=sys.stderr)
        return 2
    if project.get("status") != "open":
        print("that collaboration is closed", file=sys.stderr)
        return 2
    floor = KEEP_FREE + int(args.margin_gb * GiB)
    if args.agent_floor:
        floor = max(floor, agentuploads_mod.AGENT_KEEP_FREE + 5 * GiB)
    rigs = rigs_of(store, up, args.user)
    rows, errors = read_manifest(Path(args.manifest), rigs, up)
    state = State(Path(args.state_dir), write=not args.dry_run)
    ctx = {"store": store, "files": files, "up": up, "project": project, "user": args.user,
           "floor": floor, "rate": args.rate, "rate_bytes": _rate_bytes(args.rate),
           "timeout": args.timeout, "move_src": args.move_src,
           "current_part": None}

    def on_term(_sig, _frm):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)

    counts = {"import": 0, "already": 0, "fail": 0, "done": 0, "skipped": 0, "failed": 0}
    planned_bytes = 0
    out_rows = []
    for e in errors:
        print("manifest:", e, file=sys.stderr)
    if errors and not args.dry_run and not args.skip_invalid:
        print(f"{len(errors)} manifest problem(s); fix them, or pass --skip-invalid", file=sys.stderr)
        return 2

    if args.dry_run:
        free = files.stats()["free"] - up.reserved()
        quota_left = files.quota_of(project) - int(store.file_totals(project["id"])["bytes"]) \
            - up.reserved(project["id"])
        for r in rows:
            what, why = precheck(r, store, files, args.project, args.user, args.allow_replace)
            if what == "import":
                if args.limit and counts["import"] >= args.limit:
                    continue
                if free - planned_bytes - r["size"] < floor:
                    what, why = "fail", "would stop here: disk floor"
                elif planned_bytes + r["size"] > quota_left:
                    what, why = "fail", "would stop here: project quota"
                elif store.agent_task_in(r["rig_obj"]["id"], args.project) is None:
                    what, why = "fail", f"{r['rig_obj']['label']} has no accepted task"
                else:
                    planned_bytes += r["size"]
            counts[what] += 1
            out_rows.append({"line": r["line"], "action": what, "name": r["name"] or r["target_name"],
                             "kind": r["kind"], "telescope": (r["rig_obj"] or {}).get("label", ""),
                             "night": r["night"], "why": why})
        for o in out_rows:
            print(json.dumps(o))
        print(json.dumps({"dryRun": True, "rows": len(rows), "wouldImport": counts["import"],
                          "already": counts["already"], "fail": counts["fail"],
                          "bytes": planned_bytes, "freeGiB": round(free / GiB, 1),
                          "floorGiB": round(floor / GiB, 1),
                          "quotaLeftGiB": round(quota_left / GiB, 1),
                          "rigs": sorted({r["rig_obj"]["label"] for r in rows if r.get("rig_obj")})}))
        return 0 if not errors else 2

    state.event(event="start", manifest=str(args.manifest), rows=len(rows), project=args.project,
                user=args.user, floorGiB=round(floor / GiB, 1), limit=args.limit)
    imported_since_recheck = 0
    stopped = ""
    users = {args.user}
    try:
        for r in rows:
            if args.limit and counts["done"] >= args.limit:
                break
            what, why = precheck(r, store, files, args.project, args.user, args.allow_replace)
            if what == "already":
                counts["skipped"] += 1
                state.note(r["key"], "skipped", reason=why, name=r["name"], source=r["source"])
                continue
            if what == "fail":
                counts["failed"] += 1
                state.note(r["key"], "failed", reason=why, name=r["name"], source=r["source"])
                print(f"failed line {r['line']}: {why}", file=sys.stderr)
                continue
            try:
                project = store.project(args.project) or project
                ctx["project"] = project
                result, info = import_row(r, ctx)
            except RowFail as exc:
                counts["failed"] += 1
                state.note(r["key"], "failed", reason=str(exc), name=r["name"], source=r["source"])
                print(f"failed line {r['line']}: {exc}", file=sys.stderr)
                continue
            if result == "done":
                counts["done"] += 1
                imported_since_recheck += 1
                state.note(r["key"], "done", **info)
                print(f"done {info['name']} [{info['fileStatus']}]")
            else:
                counts["skipped"] += 1
                state.note(r["key"], "skipped", **info)
                print(f"skipped {r['name']}: {info['reason']}")
            if imported_since_recheck >= args.batch_size:
                moved = files.recheck(store.project(args.project), args.user)
                state.event(event="recheck", user=args.user, moved=moved)
                imported_since_recheck = 0
    except Stop as exc:
        stopped = str(exc)
        print("stopped:", stopped, file=sys.stderr)
    except KeyboardInterrupt:
        stopped = "interrupted"
        print("interrupted", file=sys.stderr)
    finally:
        project = store.project(args.project) or project
        final_moves = {u: files.recheck(project, u) for u in sorted(users)}
        lights = store.file_status_counts(args.project, args.user)
        state.event(event="end", counts=counts, stopped=stopped, recheck=final_moves, lights=lights)
        print(json.dumps({"counts": counts, "stopped": stopped, "recheck": final_moves,
                          "myLights": lights}))
    if stopped:
        return 3
    return 1 if counts["failed"] else 0


def remove_test(args: argparse.Namespace) -> int:
    """Take a test file off, exactly as the DELETE handler does
    (server/files.py, remove): unlink, store.remove_file, recheck if not a
    light. Only names carrying the test mark."""
    if TEST_MARK not in args.remove_test:
        print(f"refusing: only names containing {TEST_MARK} can be removed here", file=sys.stderr)
        return 2
    data = Path(auth.setting("DATA") or "/var/lib/starfront")
    store, files, _up = build(Path(args.db or data / "collab.sqlite"),
                              Path(args.files or auth.setting("FILES") or data / "files"), False)
    project = store.project(args.project)
    clean = clean_name(args.remove_test)
    kind = args.kind
    if not clean or project is None or store.file(args.project, args.user, clean, kind) is None:
        print("no such file", file=sys.stderr)
        return 2
    try:
        files.path_of(args.project, args.user, clean, kind).unlink()
    except OSError:
        pass
    store.remove_file(args.project, args.user, clean, kind)
    moved = files.recheck(project, args.user) if kind != "light" else {}
    print(json.dumps({"removed": clean, "kind": kind, "moved": moved}))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--manifest")
    p.add_argument("--project", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--state-dir", default="/var/lib/starfront/import-m31-historical")
    p.add_argument("--db", help="database (default $STARFRONT_DATA/collab.sqlite)")
    p.add_argument("--files", help="files folder (default $STARFRONT_FILES or $STARFRONT_DATA/files)")
    p.add_argument("--dry-run", action="store_true", help="validate and report; read-only")
    p.add_argument("--limit", type=int, default=0, help="import at most N files")
    p.add_argument("--batch-size", type=int, default=25, help="full recheck after every N imports")
    p.add_argument("--rate", default="30M", help="bandwidth cap (curl --limit-rate syntax)")
    p.add_argument("--timeout", type=int, default=1800, help="seconds per download")
    p.add_argument("--margin-gb", type=float, default=10.0, help="kept free above KEEP_FREE")
    p.add_argument("--no-agent-floor", dest="agent_floor", action="store_false",
                   help="do not also keep the rigs' 25 GiB auto-upload floor (+5) free")
    p.add_argument("--move-src", action="store_true",
                   help="move src_path files into place when on the same disk (staging)")
    p.add_argument("--allow-replace", action="store_true",
                   help="replace a same-name file of another size (the PUT does; off by default)")
    p.add_argument("--skip-invalid", action="store_true",
                   help="run the valid rows even when some manifest rows are invalid")
    p.add_argument("--remove-test", metavar="NAME", help=f"remove one {TEST_MARK} file and exit")
    p.add_argument("--kind", default="light", help="with --remove-test")
    args = p.parse_args(argv)
    if args.remove_test:
        return remove_test(args)
    if not args.manifest:
        p.error("--manifest is required")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
