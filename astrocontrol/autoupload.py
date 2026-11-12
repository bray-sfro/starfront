"""Sending this rig's collaboration lights to the server, on its own.

A light shot for a collaboration task is put on a queue the moment it is on
disk, with the calibration masters the library would calibrate it with. A
worker thread of its own, at low priority, sends the queue to the server's
resumable upload API (`/api/v1/agent/uploads`) - by default as soon as each
frame is on disk, day or night, at full speed (a cap and a daytime-only
window are optional settings).

The rules, in the order they matter:

* **Nothing here can stop a night.** Every step is wrapped; a failure is a
  line in the status and a retry later. The queue is a small JSON file and
  the work is a background thread that the capture path only appends to.
* **The local files are never changed.** Frames and masters are opened
  read-only, to hash and to send; nothing is moved, renamed, compressed in
  place or deleted, whatever the server says.
* **On by default, and it can be switched off** here (`collab.autoUpload`,
  the Collab tab's "Upload my collab lights"; optionally for only some of
  this PC's rigs). The server decides too (the collaboration's `autoUpload`).
  A collaboration that is not taking uploads pauses its items for an hour
  rather than dropping them.
* **Each frame goes the moment it is saved.** The capture path's save of a
  collaboration light calls `light_saved()`, which appends it to the queue
  and wakes the worker; there is no folder scan and no batch. A frame
  younger than `SETTLE_SECONDS` is retried as soon as it has settled.
* **The server is obeyed.** 429 waits the `Retry-After`; 507 (the server's
  disk or the project's quota is full) pauses everything for as long as it
  says; a refusal of the frame itself (wrong target, no task) takes that one
  item off the queue with the reason kept.
* **Resumable.** Each file is hashed once, sent in chunks at the offsets the
  server confirms, and an upload cut off carries on where it stopped - after
  a dropped connection, a reboot, or the next day.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import astro
from .config import data_root

#: Kinds of master sent with a light, in the server's words.
MASTER_KINDS = ("flat", "dark", "bias")
#: A frame must not have changed size for this long before it is read.
SETTLE_SECONDS = 5.0
#: Retry backoff after a failure the server did not explain: 1 min to 1 h.
BACKOFF_MIN, BACKOFF_MAX = 60.0, 3600.0
#: How long a collaboration that is not taking uploads is left alone.
OFF_PAUSE = 3600.0
#: Sun altitude above which it counts as daytime for the window, degrees:
#: civil twilight, so the window closes well before the first light of a night.
DAY_SUN_ALTITUDE = -6.0
#: How often the server is asked which calibration this rig's lights need.
NEEDS_EVERY = 6 * 3600.0
#: Entries finished long ago are trimmed from the queue file after this.
KEEP_DONE = 14 * 86400.0
TIMEOUT = 60.0


class Refused(Exception):
    """The server will not take this file, for a reason it gave."""


class Pause(Exception):
    """The server asked for a wait before anything else is sent."""

    def __init__(self, seconds: float, why: str, scope: str = "all") -> None:
        super().__init__(why)
        self.seconds = float(seconds)
        self.scope = scope


class Retry(Exception):
    """Try this file again later (network, server hiccup)."""


def _below_normal_priority() -> None:
    """This thread, and its disk reads, behind everything else on the PC."""
    try:
        if sys.platform == "win32":
            import ctypes
            kernel = ctypes.windll.kernel32
            thread = kernel.GetCurrentThread()
            # Background mode lowers the thread's I/O priority as well as its
            # CPU priority: a camera download never waits behind a hash.
            THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
            if not kernel.SetThreadPriority(thread, THREAD_MODE_BACKGROUND_BEGIN):
                kernel.SetThreadPriority(thread, -1)    # BELOW_NORMAL
        elif hasattr(os, "nice"):
            # Per thread on Linux; harmless elsewhere.
            with contextlib.suppress(OSError):
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except Exception:                                  # noqa: BLE001
        pass


class Bucket:
    """A token bucket in bytes per second; 0 or less means no cap."""

    def __init__(self, mbit: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.clock, self.sleep = clock, sleep
        self.set(mbit)
        self._t = clock()
        self._tokens = 0.0

    def set(self, mbit: float) -> None:
        self.rate = max(0.0, float(mbit or 0.0)) * 1_000_000 / 8

    def take(self, n: int) -> None:
        if self.rate <= 0:
            return
        now = self.clock()
        self._tokens = min(self.rate, self._tokens + (now - self._t) * self.rate)
        self._t = now
        self._tokens -= n
        if self._tokens < 0:
            self.sleep(-self._tokens / self.rate)


def file_sha256(path: Path, block: int = 4 * 1024 ** 2) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(block)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class AutoUploader:
    """The queue and the worker. One per PC; every rig on it feeds it."""

    def __init__(self, config: Any, client: Any, library: Any = None,
                 log: Callable[[str, str], None] | None = None,
                 folder: Path | None = None, site: Callable[[], dict] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.client = client
        self.library = library
        self._log = log
        self.site = site
        self.clock = clock
        self.folder = Path(folder) if folder else data_root() / "collab-uploads"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "queue.json"
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.items: list[dict[str, Any]] = self._load()
        self.paused_until = 0.0
        self.paused_why = ""
        self.project_paused: dict[str, tuple[float, str]] = {}
        self.last_error = ""
        self.sent_bytes = 0
        self.current: dict[str, Any] | None = None
        self.needs_checked: dict[str, float] = {}
        self.bucket = Bucket(self.settings()["mbit"])
        self.http: Callable[..., tuple[int, dict, dict]] = self._http

    # -- settings ----------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        s = self.config.section("collab") if self.config is not None else {}
        return {"enabled": bool(s.get("autoUpload", True)),
                "rigs": [str(r) for r in (s.get("autoUploadRigs") or [])],
                "mbit": float(s.get("autoUploadMbit", 0.0) or 0.0),
                "daytimeOnly": bool(s.get("autoUploadDaytimeOnly", False))}

    def rig_enabled(self, rig: str) -> bool:
        s = self.settings()
        return s["enabled"] and (not s["rigs"] or str(rig) in s["rigs"])

    def in_window(self, now: float | None = None) -> tuple[bool, str]:
        if not self.settings()["daytimeOnly"]:
            return True, ""
        site = (self.site() if self.site else None) or {}
        lat, lon = site.get("latitude"), site.get("longitude")
        if lat is None or lon is None:
            return False, "waiting for daytime, but this PC's site is not set (Site panel)"
        alt = astro.sun_altitude(now if now is not None else self.clock(), float(lat), float(lon))
        if alt >= DAY_SUN_ALTITUDE:
            return True, ""
        return False, "waiting for daytime (uploads run between dawn and dusk)"

    # -- the queue file ------------------------------------------------------
    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return [d for d in data if isinstance(d, dict) and d.get("path")]
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        with self._lock:
            now = self.clock()
            self.items = [i for i in self.items
                          if i.get("state") not in ("sent", "already", "refused")
                          or now - float(i.get("doneAt") or now) < KEEP_DONE]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.items, indent=0), "utf-8")
            os.replace(tmp, self.path)

    def _has(self, project: str, path: str, kind: str) -> bool:
        return any(i["path"] == path and i["project"] == project and i["kind"] == kind
                   for i in self.items)

    def add(self, path: str | Path, project: str, kind: str = "light", task: str = "",
            rig: str = "", night: str = "", filter_name: str = "") -> bool:
        path = str(path)
        with self._lock:
            if self._has(project, path, kind):
                return False
            self.items.append({"path": path, "project": project, "kind": kind, "task": task,
                               "rig": rig, "night": night, "filter": filter_name,
                               "added": self.clock(), "state": "queued", "attempts": 0,
                               "nextTry": 0.0, "error": "", "id": "", "sha256": "", "bytes": 0})
            self._save()
        self._wake.set()
        return True

    # -- what the capture path calls -----------------------------------------
    def light_saved(self, path: str | None, context: dict[str, Any], rig: str,
                    want: dict[str, Any] | None, night: str = "",
                    filter_name: str = "") -> bool:
        """A light is on disk. Queue it, and its masters, if it is for a
        collaboration task and this rig sends its lights. Never raises."""
        try:
            project = str((context or {}).get("collabProject") or "")
            task = str((context or {}).get("collabTask") or "")
            if not path or not project or not task or not self.rig_enabled(rig):
                return False
            added = self.add(path, project, "light", task, rig, night, filter_name)
            if want:
                self.add_masters(want, project, task, rig)
            return added
        except Exception as exc:                       # noqa: BLE001 - never stops a night
            self._say(f"Auto-upload: could not queue a light ({exc})", "warn")
            return False

    def add_masters(self, want: dict[str, Any], project: str, task: str = "",
                    rig: str = "", kinds: tuple[str, ...] = MASTER_KINDS) -> int:
        """The masters the library would calibrate a frame like `want` with."""
        if self.library is None:
            return 0
        n = 0
        for kind in kinds:
            try:
                master, _why = self.library.match(kind, want)
            except Exception:                          # noqa: BLE001
                master = None
            if master and master.get("path"):
                n += int(self.add(master["path"], project, kind, task, rig))
        return n

    # -- the worker ------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="collab-upload")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        _below_normal_priority()
        while not self._stop.is_set():
            wait = 60.0
            try:
                wait = self.step()
            except Exception as exc:                   # noqa: BLE001 - never fatal
                self.last_error = str(exc)
            self._wake.wait(max(0.2, wait))
            self._wake.clear()

    def due(self) -> dict[str, Any] | None:
        now = self.clock()
        with self._lock:
            for item in self.items:
                if item["state"] not in ("queued", "sending", "retry"):
                    continue
                if float(item.get("nextTry") or 0) > now:
                    continue
                until, _ = self.project_paused.get(item["project"], (0.0, ""))
                if until > now:
                    continue
                return item
        return None

    def step(self) -> float:
        """One unit of work; returns how long to wait before the next."""
        s = self.settings()
        if not s["enabled"]:
            return 300.0
        now = self.clock()
        if self.paused_until > now:
            return min(600.0, self.paused_until - now)
        ok, why = self.in_window(now)
        if not ok:
            self.paused_why = why
            return 600.0
        self.paused_why = ""
        self.bucket.set(s["mbit"])
        self._check_needs()
        item = self.due()
        if item is None:
            return self.next_wait(now)
        self.send(item)
        return 0.0

    def next_wait(self, now: float) -> float:
        """Until the soonest queued item is due (a just-saved frame settling,
        a retry), at most 5 min. A newly saved frame wakes the worker at once."""
        with self._lock:
            times = [float(i.get("nextTry") or 0) for i in self.items
                     if i["state"] in ("queued", "sending", "retry")]
        times = [t for t in times if t > now]
        return max(0.2, min([300.0] + [t - now for t in times]))

    def _mark(self, item: dict[str, Any], **values: Any) -> None:
        with self._lock:
            item.update(values)
            self._save()

    def send(self, item: dict[str, Any]) -> None:
        path = Path(item["path"])
        self.current = item
        try:
            if not path.is_file():
                raise Refused("the file is no longer on this PC")
            size = path.stat().st_size
            age = self.clock() - path.stat().st_mtime
            if age < SETTLE_SECONDS:
                # Just saved: try again the moment it has settled. Not a
                # failure, so no back-off and no attempt counted.
                self._mark(item, state="queued", nextTry=self.clock() + (SETTLE_SECONDS - age) + 0.2)
                return
            if not item.get("sha256") or int(item.get("bytes") or 0) != size:
                self._mark(item, sha256=file_sha256(path), bytes=size, id="")
            self._mark(item, state="sending")
            self._upload(item, path)
        except Refused as exc:
            self._mark(item, state="refused", error=str(exc), doneAt=self.clock())
            self._say(f"Auto-upload: {path.name} not sent - {exc}", "warn")
        except Pause as exc:
            until = self.clock() + exc.seconds
            if exc.scope == "project":
                self.project_paused[item["project"]] = (until, str(exc))
            else:
                self.paused_until, self.paused_why = until, str(exc)
            self._mark(item, state="queued", error=str(exc))
        except (Retry, OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            attempts = int(item.get("attempts") or 0) + 1
            delay = min(BACKOFF_MAX, BACKOFF_MIN * 2 ** (attempts - 1)) * (0.8 + 0.4 * random.random())
            self._mark(item, state="retry", attempts=attempts, error=str(exc),
                       nextTry=self.clock() + delay)
            self.last_error = str(exc)
        finally:
            self.current = None

    def _upload(self, item: dict[str, Any], path: Path) -> None:
        code, body, headers = self.http("POST", "/api/v1/agent/uploads", {
            "project": item["project"], "name": path.name, "kind": item["kind"],
            "bytes": item["bytes"], "sha256": item["sha256"], "task": item.get("task") or ""})
        self._obey(code, body, headers)
        if body.get("already"):
            self._mark(item, state="already", status=body.get("status") or "",
                       name=body.get("name") or "", doneAt=self.clock(), error="")
            return
        upload_id, offset = body["id"], int(body.get("offset") or 0)
        chunk = int(body.get("chunk") or 8 * 1024 ** 2)
        self._mark(item, id=upload_id)
        with open(path, "rb") as handle:           # read-only, always
            while offset < item["bytes"]:
                if self._stop.is_set() or not self.settings()["enabled"]:
                    raise Retry("stopped")
                ok, why = self.in_window()
                if not ok:
                    raise Pause(600.0, why)
                handle.seek(offset)
                data = handle.read(min(chunk, item["bytes"] - offset))
                self.bucket.take(len(data))
                code, answer, headers = self.http(
                    "PUT", f"/api/v1/agent/uploads/{upload_id}?offset={offset}", data)
                if code == 409 and "offset" in answer:
                    offset = int(answer["offset"])
                    continue
                if code == 404:
                    # The session expired on the server (idle for days): start again.
                    self._mark(item, id="")
                    raise Retry("the upload session expired; starting again")
                self._obey(code, answer, headers)
                offset = int(answer["offset"])
                self.sent_bytes += len(data)
        code, answer, headers = self.http("POST", f"/api/v1/agent/uploads/{upload_id}/finish", None)
        if code == 409 and "offset" in answer:
            raise Retry(str(answer.get("detail") or "the server is missing part of the file"))
        if code == 422:
            # The hash did not match: the session is gone; hash again next time.
            self._mark(item, sha256="", id="")
            raise Retry(str(answer.get("detail") or "sha256 mismatch"))
        self._obey(code, answer, headers)
        self._mark(item, state="sent", status=answer.get("status") or "",
                   name=answer.get("name") or "", reasons=answer.get("reasons") or [],
                   doneAt=self.clock(), error="")

    def _obey(self, code: int, body: dict[str, Any], headers: dict[str, str]) -> None:
        if 200 <= code < 300:
            return
        detail = str(body.get("detail") or f"HTTP {code}")
        retry_after = float(headers.get("retry-after") or 0) or None
        if code == 429:
            raise Pause(retry_after or 60.0, detail)
        if code == 507:
            raise Pause(retry_after or 6 * 3600.0, detail)
        if code == 409:
            # The collaboration is not taking uploads (off, closed, not this rig).
            raise Pause(OFF_PAUSE, detail, scope="project")
        if code == 401:
            raise Pause(OFF_PAUSE, "this PC is not signed in to the collaboration server")
        if code in (400, 403, 404, 413):
            raise Refused(detail)
        raise Retry(detail)

    # -- calibration the server says it is waiting for ---------------------------
    def _check_needs(self) -> None:
        if self.library is None:
            return
        now = self.clock()
        projects = {i["project"]: i for i in self.items if i["kind"] == "light"}
        for project, sample in projects.items():
            if now - self.needs_checked.get(project, 0.0) < NEEDS_EVERY:
                continue
            self.needs_checked[project] = now
            try:
                code, body, _ = self.http("GET", f"/api/v1/agent/uploads?project={project}", None)
            except Exception:                          # noqa: BLE001
                continue
            if code != 200:
                continue
            for need in body.get("calibrationNeeds") or []:
                want = {"filter": need.get("filterRaw") or need.get("filter") or "",
                        "exposure": need.get("exposure"), "gain": need.get("gain"),
                        "offset": need.get("offset"), "temperature": need.get("temp"),
                        "binning": need.get("xbin") or 1, "camera": need.get("camera") or ""}
                kinds = tuple(k for k in need.get("kinds") or [] if k in MASTER_KINDS)
                self.add_masters(want, project, sample.get("task") or "", sample.get("rig") or "", kinds)

    # -- the wire ----------------------------------------------------------------
    def _http(self, method: str, path: str, body: Any) -> tuple[int, dict, dict]:
        ready = self.client._ready() if self.client is not None else None
        if ready is None:
            raise Pause(OFF_PAUSE, "this PC is not joined to the collaboration server")
        url, token = ready
        if isinstance(body, (bytes, bytearray)):
            data, kind = bytes(body), "application/octet-stream"
        elif body is None:
            data, kind = (b"" if method in ("POST", "PUT") else None), "application/json"
        else:
            data, kind = json.dumps(body).encode("utf-8"), "application/json"
        request = urllib.request.Request(f"{url}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Content-Type": kind, "User-Agent": "Starfront"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                text = response.read().decode("utf-8")
                return response.status, (json.loads(text) if text else {}), \
                    {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            try:
                answer = json.loads(text)
            except ValueError:
                answer = {"detail": text[:300]}
            return exc.code, answer if isinstance(answer, dict) else {"detail": str(answer)}, \
                {k.lower(): v for k, v in (exc.headers or {}).items()}

    # -- what the Collab tab shows ------------------------------------------------
    def status(self) -> dict[str, Any]:
        s = self.settings()
        now = self.clock()
        with self._lock:
            counts: dict[str, int] = {}
            pending = 0
            for i in self.items:
                state = "waiting" if i["state"] == "retry" else i["state"]
                counts[state] = counts.get(state, 0) + 1
                if i.get("status") == "pending":
                    pending += 1
            queued = counts.get("queued", 0) + counts.get("waiting", 0) + counts.get("sending", 0)
            refused = [{"file": Path(i["path"]).name, "why": i.get("error") or ""}
                       for i in self.items if i["state"] == "refused"][-5:]
        if not s["enabled"]:
            line = "Auto-upload is off"
        elif self.paused_until > now:
            line = f"Auto-upload paused: {self.paused_why} (until {time.strftime('%H:%M', time.localtime(self.paused_until))})"
        elif self.current is not None:
            line = f"Auto-upload: sending {Path(self.current['path']).name}, {queued} in the queue"
        elif self.paused_why:
            line = f"Auto-upload: {queued} queued, {self.paused_why}"
        else:
            line = f"Auto-upload: {queued} queued" if queued else "Auto-upload: up to date"
        sent = counts.get("sent", 0) + counts.get("already", 0)
        line += f" · {sent} sent"
        if pending:
            line += f" · {pending} waiting for calibration on the server"
        if counts.get("refused"):
            line += f" · {counts['refused']} refused"
        return {"enabled": s["enabled"], "rigs": s["rigs"], "mbit": s["mbit"],
                "daytimeOnly": s["daytimeOnly"], "running": self.running,
                "counts": counts, "pendingCalibration": pending, "line": line,
                "pausedUntil": self.paused_until or None, "pausedWhy": self.paused_why,
                "projectsPaused": {p: {"until": u, "why": w} for p, (u, w) in self.project_paused.items() if u > now},
                "sentBytes": self.sent_bytes, "lastError": self.last_error, "refused": refused}

    def _say(self, message: str, level: str = "info") -> None:
        if self._log is not None:
            with contextlib.suppress(Exception):
                self._log(message, level)
