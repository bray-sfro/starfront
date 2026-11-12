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
* **The server is obeyed.** 429 waits the `Retry-After`. A 507 pauses for
  as long as it says, but only as widely as the failure: a project's storage
  quota pauses that project, and the server's disk pauses every project.
  Which pause is which is written to ``pauses.json`` beside the queue, and
  is still there after a restart. A refusal of the frame itself (wrong
  target, no task) takes that one item off the queue with the reason kept.
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


class Hold(Exception):
    """This file stays queued with a clear status. The others keep going."""


class Pause(Exception):
    """The server asked for a wait before anything else is sent."""

    def __init__(self, seconds: float, why: str, scope: str = "all") -> None:
        super().__init__(why)
        self.seconds = float(seconds)
        self.scope = scope


class Retry(Exception):
    """Try this file again later (network, server hiccup)."""


# A 507 detail that means this collaboration's quota, not the host disk.
_QUOTA_WORDS = (
    "quota",
    "project storage",
    "storage quota",
    "storage limit",
    "project is full",
    "this project is full",
)
# A 507 detail that means the uploads host itself is out of room.
_DISK_WORDS = (
    "disk",
    "no room",
    "keeping its last",
    "free space",
    "filesystem",
    "inode",
    "infrastructure",
    "no space left",
)
_PROJECT_SCOPES = {"project", "collaboration", "collab"}
_SERVER_SCOPES = {"server", "all", "global", "infrastructure"}
_QUOTA_REASONS = {"quota", "project_quota", "storage_quota"}
_DISK_REASONS = {"disk", "disk_full", "infrastructure", "filesystem"}


def _detail_fields(body: dict) -> tuple[str, dict]:
    """The human text of a response, and a dict if ``detail`` is an object."""
    raw = body.get("detail")
    if isinstance(raw, dict):
        return str(raw.get("message") or raw.get("detail") or ""), raw
    return ("" if raw is None else str(raw)), {}


def pause_scope_for_507(body: dict | None, headers: dict | None = None) -> str:
    """``project`` pauses one collaboration; ``all`` pauses every upload.

    The collab server should say which, so this client can stop guessing.
    Send the same choice in each of these (JSON wins over the header, and
    both win over the wording):

    * JSON ``scope`` ``"project"`` or ``"server"``, on the body or inside
      ``detail`` when ``detail`` is an object. ``reason`` ``"quota"`` or
      ``"disk"`` in the same place.
    * Header ``X-Starfront-Pause-Scope: project`` or ``server``.

    A project quota 507::

        {"detail": "this project's storage quota is full (40 GiB)",
         "scope": "project", "reason": "quota"}

    A server disk 507::

        {"detail": "the server is keeping its last 25 GB free; uploads are paused",
         "scope": "server", "reason": "disk"}

    Until that arrives, the detail text is read. Quota words pause that
    project only. Disk and free-space words pause every project. A 507
    that says neither stays global, so an unfamiliar disk-full message
    still holds the rig. ``scope`` is checked before ``reason``.
    """
    body = body if isinstance(body, dict) else {}
    text, extra = _detail_fields(body)
    sources = [body]
    if extra:
        sources.append(extra)
    for src in sources:
        scope = str(src.get("scope") or "").strip().lower()
        if scope in _PROJECT_SCOPES:
            return "project"
        if scope in _SERVER_SCOPES:
            return "all"
    for src in sources:
        reason = str(src.get("reason") or "").strip().lower()
        if reason in _QUOTA_REASONS:
            return "project"
        if reason in _DISK_REASONS:
            return "all"
    headers = {str(k).lower(): str(v).strip().lower() for k, v in (headers or {}).items()}
    header = headers.get("x-starfront-pause-scope", "")
    if header in _PROJECT_SCOPES:
        return "project"
    if header in _SERVER_SCOPES:
        return "all"
    low = text.lower()
    if any(word in low for word in _QUOTA_WORDS):
        return "project"
    if any(word in low for word in _DISK_WORDS):
        return "all"
    return "all"


#: A missing, zero or nonsense cap is 10 Mbit/s, not "no cap".
DEFAULT_UPLOAD_MBIT = 10.0

#: Colour one-shot cameras this tandem must not upload. Matched on the
#: camera name, not on the folder the file was saved in.
_COLOUR_CAMERA = ("6200mc", "asi6200mc")
_MONO_CAMERA = ("6200mm", "asi6200mm")


def effective_mbit(value: Any) -> float:
    """Megabits a second. 0, missing, NaN or negative becomes 10."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_UPLOAD_MBIT
    if number != number or number <= 0 or number == float("inf"):
        return DEFAULT_UPLOAD_MBIT
    return number


def refuses_upload(identity: dict[str, Any] | None) -> bool:
    """True when this frame is positively a follower that must not be sent.

    An unknown camera is not refused here: the backfill uses `allows_upload`
    strict, and a live capture passes the rig role. A header this client
    cannot read must not block a master light that was queued on purpose.
    """
    if not identity:
        return False
    if identity.get("upload") is True:
        return False
    if identity.get("upload") is False:
        return True
    if str(identity.get("role") or "") == "follower":
        return True
    if identity.get("colour") and str(identity.get("role") or "") != "master":
        return True
    return False


def follower_frame(identity: dict[str, Any] | None) -> bool:
    """True when this is the follower camera (Rokinon / ASI6200MC).

    Used for uploads and for the collaboration session report. An empty
    identity is not a follower: older tallies that never recorded a camera
    stay reportable.
    """
    ident = dict(identity or {})
    if not any(ident.get(key) for key in ("role", "camera", "telescope", "name", "bayer", "colour")):
        return False
    guessed = identity_from_header({
        "INSTRUME": ident.get("camera") or "",
        "TELESCOP": ident.get("telescope") or ident.get("name") or "",
        "BAYERPAT": ident.get("bayer") or "",
    })
    if ident.get("role"):
        guessed["role"] = ident["role"]
    if ident.get("colour") and guessed.get("role") != "master":
        guessed["colour"] = True
        if not guessed.get("role"):
            guessed["role"] = "follower"
    if ident.get("upload") is not None:
        guessed["upload"] = ident.get("upload")
    return refuses_upload(guessed)


def allows_upload(identity: dict[str, Any] | None, *, strict: bool = False) -> bool:
    """Whether this telescope's lights may be queued or sent.

    A follower defaults to off. ``upload`` True or False is that rig's own
    ``camera.uploadCollab`` and wins over the default. Colour (Bayer, or an
    ASI6200MC) is a follower when the role is not master. An empty identity
    is the old callers, which the rig list already filtered; strict mode
    (the backfill) does not upload a frame it cannot identify.
    """
    if not identity:
        return not strict
    if identity.get("upload") is True:
        return True
    if identity.get("upload") is False:
        return False
    role = str(identity.get("role") or "")
    if role == "follower":
        return False
    if identity.get("colour") and role != "master":
        return False
    if role == "master" or identity.get("mono"):
        return True
    return not strict


def identity_from_header(header: dict[str, Any] | None) -> dict[str, Any]:
    """Rig and camera from the FITS cards. The directory is not consulted."""
    header = header or {}
    instrument = str(header.get("INSTRUME") or "")
    telescop = str(header.get("TELESCOP") or "")
    bayer = str(header.get("BAYERPAT") or "").strip()
    compact = instrument.lower().replace(" ", "")
    tel = telescop.lower()
    colour = bool(bayer) or any(mark in compact for mark in _COLOUR_CAMERA)
    mono = any(mark in compact for mark in _MONO_CAMERA)
    # "6200mc" is also contained in nothing about the mono camera.
    if mono and "6200mc" not in compact:
        colour = False
    role = ""
    if colour or "rokinon" in tel:
        role = "follower"
    elif mono or "fsq" in tel:
        role = "master"
    return {"role": role, "colour": colour and role != "master", "mono": mono,
            "camera": instrument, "telescope": telescop, "bayer": bayer,
            "upload": None, "name": telescop}


def _same_rig(identity: dict[str, Any], rig: dict[str, Any]) -> bool:
    """Header camera/telescope against a known rig. Not the folder."""
    cam = str(identity.get("camera") or "").lower().replace(" ", "")
    tel = str(identity.get("telescope") or "").lower()
    rig_cam = str(rig.get("camera") or "").lower().replace(" ", "")
    rig_name = str(rig.get("name") or "").lower()
    if rig_cam and cam and (rig_cam in cam or cam in rig_cam):
        return True
    if rig_name and tel and (rig_name in tel or tel in rig_name):
        return True
    return False


def _short_why(why: str) -> str:
    """A few words for the status line; the full reason stays on the item."""
    text = (why or "").lower()
    if "quota" in text:
        return "quota full"
    if any(word in text for word in ("disk", "no room", "keeping its last", "free space")):
        return "disk full"
    compact = " ".join((why or "").split())
    return (compact[:80] if compact else "paused")


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
                 clock: Callable[[], float] = time.time,
                 targets: Any = None, rigs: Any = None) -> None:
        self.config = config
        self.client = client
        self.library = library
        self._log = log
        self.site = site
        self.clock = clock
        self.targets = targets
        self.rigs = rigs
        self._logged_queue: int | None = None
        self._logged_wait = ""
        self.folder = Path(folder) if folder else data_root() / "collab-uploads"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "queue.json"
        self.pause_path = self.folder / "pauses.json"
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.items: list[dict[str, Any]] = self._load()
        self.paused_until = 0.0
        self.paused_why = ""
        self.project_paused: dict[str, tuple[float, str]] = {}
        self._load_pauses()
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
                "mbit": effective_mbit(s.get("autoUploadMbit", 0.0)),
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

    def _load_pauses(self) -> None:
        """Global and per-project pauses, written beside the queue.

        The queue file stays a list of frames. Pauses live in ``pauses.json``
        so a restart still knows which collaboration is full and which pause
        holds every project.
        """
        try:
            data = json.loads(self.pause_path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        now = self.clock()
        until = float(data.get("paused_until") or 0)
        if until > now:
            self.paused_until = until
            self.paused_why = str(data.get("paused_why") or "")
        projects = data.get("projects") or {}
        if not isinstance(projects, dict):
            return
        for project, row in projects.items():
            if not isinstance(row, dict):
                continue
            end = float(row.get("until") or 0)
            if end > now:
                self.project_paused[str(project)] = (end, str(row.get("why") or ""))

    def _save_pauses(self) -> None:
        now = self.clock()
        with self._lock:
            projects = {p: {"until": u, "why": w}
                        for p, (u, w) in self.project_paused.items() if u > now}
            payload = {"paused_until": self.paused_until if self.paused_until > now else 0.0,
                       "paused_why": self.paused_why if self.paused_until > now else "",
                       "projects": projects}
            tmp = self.pause_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=0), "utf-8")
            os.replace(tmp, self.pause_path)

    def _has(self, project: str, path: str, kind: str) -> bool:
        return any(i["path"] == path and i["project"] == project and i["kind"] == kind
                   for i in self.items)

    def release_held(self, rig: str = "") -> int:
        """Queue held files again after the scope for them has changed.

        A blank ``rig`` releases every held file. The file was never removed
        from the queue; this only lets the worker try it with the new id.
        """
        n = 0
        with self._lock:
            for item in self.items:
                if item.get("state") != "held":
                    continue
                if rig and str(item.get("rig") or "") not in ("", rig):
                    continue
                item.update(state="queued", error="", status="", nextTry=0.0)
                n += 1
            if n:
                self._save()
        if n:
            self._wake.set()
        return n

    def add(self, path: str | Path, project: str, kind: str = "light", task: str = "",
            rig: str = "", night: str = "", filter_name: str = "",
            identity: dict[str, Any] | None = None) -> bool:
        path = str(path)
        with self._lock:
            if self._has(project, path, kind):
                return False
            row = {"path": path, "project": project, "kind": kind, "task": task,
                   "rig": rig, "night": night, "filter": filter_name,
                   "added": self.clock(), "state": "queued", "attempts": 0,
                   "nextTry": 0.0, "error": "", "id": "", "sha256": "", "bytes": 0}
            if identity:
                row["identity"] = {k: v for k, v in identity.items()
                                   if isinstance(v, (str, int, float, bool)) or v is None}
            self.items.append(row)
            self._save()
        self._wake.set()
        return True

    # -- what the capture path calls -----------------------------------------
    def light_saved(self, path: str | None, context: dict[str, Any], rig: str,
                    want: dict[str, Any] | None, night: str = "",
                    filter_name: str = "", identity: dict[str, Any] | None = None) -> bool:
        """A light is on disk. Queue it, and its masters, if it is for a
        collaboration task and this rig sends its lights. Never raises.

        A follower (the Rokinon on this tandem) is not queued. The decision
        is the rig role and the camera, not the folder the file landed in.
        """
        try:
            project = str((context or {}).get("collabProject") or "")
            task = str((context or {}).get("collabTask") or "")
            if not path or not project or not task or not self.rig_enabled(rig):
                return False
            if identity and not allows_upload(identity):
                self._say(f"Auto-upload: not queuing {Path(path).name} "
                          f"({identity.get('name') or identity.get('camera') or 'follower'} "
                          "does not upload)")
                return False
            added = self.add(path, project, "light", task, rig, night, filter_name,
                             identity=identity)
            if want and (not identity or allows_upload(identity)):
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
        try:
            self.backfill()
        except Exception as exc:                   # noqa: BLE001 - never fatal
            self._say(f"Auto-upload: backfill skipped ({exc})", "warn")
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
        if self.paused_until > now:
            return None
        with self._lock:
            for item in self.items:
                if item["state"] not in ("queued", "sending", "retry"):
                    continue
                if float(item.get("nextTry") or 0) > now:
                    continue
                until, _ = self.project_paused.get(str(item.get("project") or ""), (0.0, ""))
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
        self._log_queue()
        if self.paused_until > now:
            return min(600.0, self.paused_until - now)
        ok, why = self.in_window(now)
        if not ok:
            if self._logged_wait != why:
                self._logged_wait = why
                self._say(f"Auto-upload: {why} ({self._queued_count()} queued)")
            self.paused_why = why
            return 600.0
        self._logged_wait = ""
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
            pauses = [u for u, _why in self.project_paused.values() if u > now]
        times = [t - now for t in times if t > now]
        if self.paused_until > now:
            pauses.append(self.paused_until)
        pauses = [u - now for u in pauses]
        return max(0.2, min([300.0] + times + pauses))

    def _mark(self, item: dict[str, Any], **values: Any) -> None:
        with self._lock:
            item.update(values)
            self._save()

    def _queued_count(self) -> int:
        with self._lock:
            return sum(1 for i in self.items if i.get("state") in ("queued", "sending", "retry"))

    def _log_queue(self) -> None:
        n = self._queued_count()
        if n != self._logged_queue:
            self._logged_queue = n
            self._say(f"Auto-upload: {n} queued")

    def _identity_for_send(self, item: dict[str, Any], path: Path) -> dict[str, Any] | None:
        ident = item.get("identity")
        if isinstance(ident, dict) and ident:
            return ident
        if path.suffix.lower() not in (".fits", ".fit", ".fts"):
            return None
        try:
            from .imaging.fits import read_header
            return identity_from_header(read_header(path))
        except Exception:                          # noqa: BLE001 - a header we cannot read is not a refusal
            return None

    def send(self, item: dict[str, Any]) -> None:
        path = Path(item["path"])
        self.current = item
        try:
            ident = self._identity_for_send(item, path)
            if refuses_upload(ident):
                raise Refused("this telescope does not upload "
                              f"({(ident or {}).get('name') or (ident or {}).get('camera') or 'follower'})")
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
            self._say(f"Auto-upload: {path.name} {item.get('state') or 'sent'}"
                      + (f" ({item.get('project')})" if item.get("project") else ""))
            self._log_queue()
        except Refused as exc:
            self._mark(item, state="refused", error=str(exc), doneAt=self.clock())
            self._say(f"Auto-upload: {path.name} not sent - {exc}", "warn")
            self._log_queue()
        except Hold as exc:
            # A scope the server will not file this frame under. The file
            # stays in the queue with the reason. The other cameras' files
            # are not paused and this one is not thrown away.
            self._mark(item, state="held", error=str(exc), status=str(exc))
            self._say(f"Auto-upload: {path.name} held — {exc}", "warn")
            self._log_queue()
        except Pause as exc:
            until = self.clock() + exc.seconds
            with self._lock:
                if exc.scope == "project":
                    self.project_paused[str(item.get("project") or "")] = (until, str(exc))
                    scope = f"project {item.get('project') or ''}".strip()
                else:
                    self.paused_until, self.paused_why = until, str(exc)
                    scope = "every project"
            self._mark(item, state="queued", error=str(exc))
            self._save_pauses()
            self._say(f"Auto-upload paused ({scope}): {exc}", "warn")
        except (Retry, OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            attempts = int(item.get("attempts") or 0) + 1
            delay = min(BACKOFF_MAX, BACKOFF_MIN * 2 ** (attempts - 1)) * (0.8 + 0.4 * random.random())
            self._mark(item, state="retry", attempts=attempts, error=str(exc),
                       nextTry=self.clock() + delay)
            self.last_error = str(exc)
            self._say(f"Auto-upload: {path.name} will retry — {exc}", "warn")
        finally:
            self.current = None

    def _scope_for_item(self, item: dict[str, Any], path: Path) -> str:
        """The scope id for this file, or blank when the server should resolve the bind."""
        from . import scopes
        if not scopes.registered_scopes_enabled(self.config):
            return ""
        assignments = []
        for rig in self._rig_rows():
            assignments.append({
                "id": str(rig.get("id") or ""),
                "name": str(rig.get("name") or ""),
                "scopeId": str(rig.get("scopeId") or ""),
            })
        known: list[dict[str, Any]] = []
        client = self.client
        if client is not None:
            getter = getattr(client, "known_scopes", None)
            if callable(getter):
                try:
                    known = list(getter() or [])
                except Exception:                  # noqa: BLE001 - no list means "send the stored id"
                    known = []
        identity = dict(self._identity_for_send(item, path) or {})
        if item.get("rig") and not identity.get("rig"):
            identity["rig"] = item.get("rig")
        return scopes.scope_id_for_frame(
            assignments, identity, known or None,
            enabled=True)

    def _upload(self, item: dict[str, Any], path: Path) -> None:
        from . import scopes
        body_json = {
            "project": item["project"], "name": path.name, "kind": item["kind"],
            "bytes": item["bytes"], "sha256": item["sha256"], "task": item.get("task") or ""}
        # scopeId selects among this agent's binds. The equipment-profile
        # name is not sent: the server stores it and does not file by it.
        body_json.update(scopes.upload_extra(
            self._scope_for_item(item, path),
            enabled=scopes.registered_scopes_enabled(self.config)))
        code, body, headers = self.http("POST", "/api/v1/agent/uploads", body_json)
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
        from . import scopes
        hold = scopes.scope_hold_message(code, body)
        if hold:
            raise Hold(hold)
        detail = str(body.get("detail") or f"HTTP {code}")
        retry_after = float(headers.get("retry-after") or 0) or None
        if code == 429:
            raise Pause(retry_after or 60.0, detail)
        if code == 507:
            # Quota is one project (the 409 path). Disk / infrastructure holds
            # every project. See pause_scope_for_507 for the body the collab
            # server should send, and the text rule used until it does.
            text, _extra = _detail_fields(body)
            shown = text or detail
            scope = pause_scope_for_507(body, headers)
            if scope == "project" and "quota" not in shown.lower():
                shown = f"project quota full: {shown}" if shown else "project quota full"
            raise Pause(retry_after or 6 * 3600.0, shown, scope=scope)
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

    # -- lights that never got a PROJID -----------------------------------------
    def _capture_root(self) -> Path:
        """The same folder CaptureService writes into, blank setting included."""
        from .capture import capture_root
        return capture_root(self.config)

    def _current_task_id(self) -> str:
        """The task this PC is on, when the collab client knows it."""
        client = self.client
        if client is None:
            return ""
        status = getattr(client, "status", None)
        try:
            if callable(status):
                task = (status() or {}).get("task") or {}
                return str(task.get("id") or "")
            task = getattr(client, "_task", None) or {}
            return str(task.get("id") or "")
        except Exception:                              # noqa: BLE001 - no task is not a guess
            return ""

    def _target_rows(self) -> list[dict[str, Any]]:
        src = self.targets
        if src is None:
            return []
        if callable(src):
            src = src()
        if hasattr(src, "listing"):
            return list(src.listing())
        return [row for row in src if isinstance(row, dict)]

    def _rig_rows(self) -> list[dict[str, Any]]:
        src = self.rigs
        if src is None:
            return []
        rigs = list(getattr(src, "all", src) or [])
        rows = []
        for rig in rigs:
            if isinstance(rig, dict):
                rows.append(rig)
                continue
            camera = None
            with contextlib.suppress(Exception):
                camera = rig.manager.get("camera")
            rows.append({
                "id": getattr(rig, "id", ""),
                "name": getattr(rig, "name", ""),
                "role": getattr(rig, "role", ""),
                "camera": str(getattr(camera, "name", "") or "") if camera is not None else "",
                "colour": bool(getattr(camera, "bayer_pattern", None)) if camera is not None else False,
                "upload": getattr(getattr(rig, "capture", None), "upload_collab", None),
                "scopeId": str(getattr(rig, "scope_id", "") or ""),
            })
        return rows

    @staticmethod
    def _target_folder(path: Path, root: Path) -> str:
        """The directory directly under the capture root: the target's folder."""
        try:
            relative = path.resolve().relative_to(root.resolve())
        except ValueError:
            return ""
        return relative.parts[0] if relative.parts else ""

    @staticmethod
    def _stamp_of(target: dict[str, Any]) -> tuple[str, str]:
        stamp = target.get("collab") or {}
        return str(stamp.get("project") or ""), str(stamp.get("task") or "")

    def _folder_hits(self, folder: str,
                     targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Targets whose collab stamp is exactly this capture folder.

        Exact means the folder equals the target name, the stamp's project
        name, the project id, or the task id — compared as written and as
        `clean_target` writes a folder. A parent directory that merely
        contains the name is not a match, and the first of two names is
        not either.
        """
        from .capture import clean_target
        wanted = folder.strip().lower()
        if not wanted:
            return []
        hits = []
        for target in targets:
            project, task = self._stamp_of(target)
            if not project or not task:
                continue
            stamp = target.get("collab") or {}
            tokens: set[str] = set()
            for text in (target.get("name"), stamp.get("projectName"), project, task):
                raw = str(text or "").strip()
                if not raw:
                    continue
                tokens.add(raw.lower())
                tokens.add(clean_target(raw).lower())
            if wanted in tokens:
                hits.append(target)
        return hits

    def _project_for(self, path: Path, header: dict[str, Any],
                     targets: list[dict[str, Any]], root: Path
                     ) -> tuple[tuple[str, str] | None, str]:
        """The one stamp for this file, or a reason it was not matched.

        A header `COLTASK` or `PROJID` that names exactly one stamp wins.
        Otherwise the capture folder must name exactly one stamp. Two
        targets with the same name stay unresolved unless the task this PC
        is on is exactly one of them. Anything still shared is skipped.
        """
        header_task = str(header.get("COLTASK") or "").strip()
        header_project = str(header.get("PROJID") or "").strip()
        if header_task or header_project:
            exact = []
            for target in targets:
                project, task = self._stamp_of(target)
                if not project or not task:
                    continue
                if header_task and task == header_task:
                    exact.append(target)
                elif not header_task and project == header_project:
                    exact.append(target)
            if len(exact) == 1:
                return self._stamp_of(exact[0]), ""
            if len(exact) > 1:
                return None, "ambiguous"

        hits = self._folder_hits(self._target_folder(path, root), targets)
        if len(hits) == 1:
            return self._stamp_of(hits[0]), ""
        if len(hits) > 1:
            current = self._current_task_id()
            narrowed = [target for target in hits
                        if self._stamp_of(target)[1] == current] if current else []
            if len(narrowed) == 1:
                return self._stamp_of(narrowed[0]), ""
            ids = ", ".join(sorted({self._stamp_of(target)[1] for target in hits}))
            folder = self._target_folder(path, root)
            self._ambiguous = getattr(self, "_ambiguous", {})
            self._ambiguous.setdefault(folder, ids)
            return None, "ambiguous"
        return None, "no target"

    def backfill(self, root: Path | str | None = None,
                 targets: list[dict[str, Any]] | None = None,
                 rigs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Queue master lights that have no PROJID. Do not rewrite the files.

        The project and task are attached on the queue item, which is what
        the upload request sends. A follower frame is not queued, including
        one filed under the master's folder. The server drops a duplicate
        when the file is sent (``already``).
        """
        from .imaging.fits import read_header
        base = Path(root) if root else self._capture_root()
        rows = list(targets) if targets is not None else self._target_rows()
        known = list(rigs) if rigs is not None else self._rig_rows()
        queued: list[str] = []
        skipped_names: dict[str, list[str]] = {}
        self._ambiguous = {}
        scanned = matched = 0

        def skip(reason: str, name: str) -> None:
            skipped_names.setdefault(reason, []).append(name)

        if not base.is_dir():
            self._say(f"Auto-upload: backfill root {base}: not a folder, "
                      "scanned 0, matched 0, queued 0, skipped 0")
            return {"root": str(base), "scanned": 0, "matched": 0, "queued": 0,
                    "skipped": 0, "skippedBy": {}, "queuedNames": [],
                    "skippedNames": []}
        paths = sorted(path for path in base.rglob("*.fit*") if path.is_file())
        scanned = len(paths)
        for path in paths:
            try:
                header = read_header(path)
            except Exception:                          # noqa: BLE001 - skip a bad file
                skip("bad header", path.name)
                continue
            kind = str(header.get("IMAGETYP") or "").strip().lower()
            if not kind.startswith("light"):
                skip("not a light", path.name)
                continue
            if str(header.get("PROJID") or "").strip():
                skip("has PROJID", path.name)
                continue
            found, why = self._project_for(path, header, rows, base)
            if found is None:
                skip(why or "no target", path.name)
                continue
            matched += 1
            project, task = found
            ident = identity_from_header(header)
            matched_id = ""
            for rig in known:
                if _same_rig(ident, rig):
                    matched_id = str(rig.get("id") or "")
                    if rig.get("role"):
                        ident["role"] = rig["role"]
                    if rig.get("upload") is not None:
                        ident["upload"] = rig.get("upload")
                    if rig.get("colour"):
                        ident["colour"] = True
                        if ident.get("role") != "master":
                            ident["role"] = ident.get("role") or "follower"
                    if matched_id:
                        ident["rig"] = matched_id
                    break
            if not allows_upload(ident, strict=True):
                skip("follower", path.name)
                continue
            if self.add(path, project, "light", task,
                        rig=matched_id or str(ident.get("name") or ""),
                        filter_name=str(header.get("FILTER") or ""),
                        identity=ident):
                queued.append(path.name)
            else:
                skip("already queued", path.name)
        skipped = sum(len(names) for names in skipped_names.values())
        reason_bits = [f"{reason} {len(names)}"
                       for reason, names in sorted(skipped_names.items())]
        ambiguous = getattr(self, "_ambiguous", {})
        if ambiguous:
            detail = "; ".join(f"{folder} ({ids})" for folder, ids in sorted(ambiguous.items()))
            reason_bits.append(f"ambiguous folders: {detail}")
        self._say(
            f"Auto-upload: backfill root {base}: scanned {scanned}, "
            f"matched {matched}, queued {len(queued)}, skipped {skipped}"
            + (f" ({', '.join(reason_bits)})" if reason_bits else ""))
        if queued:
            self._log_queue()
        return {"root": str(base), "scanned": scanned, "matched": matched,
                "queued": len(queued), "skipped": skipped,
                "skippedBy": {reason: len(names) for reason, names in skipped_names.items()},
                "queuedNames": queued,
                "skippedNames": [name for names in skipped_names.values() for name in names]}

    # -- what the Collab tab shows ------------------------------------------------
    def status(self) -> dict[str, Any]:
        s = self.settings()
        now = self.clock()
        with self._lock:
            counts: dict[str, int] = {}
            pending = 0
            proj_counts: dict[str, int] = {}
            for i in self.items:
                state = "waiting" if i["state"] == "retry" else i["state"]
                counts[state] = counts.get(state, 0) + 1
                if i.get("status") == "pending":
                    pending += 1
                if i["state"] in ("queued", "sending", "retry"):
                    project = str(i.get("project") or "")
                    proj_counts[project] = proj_counts.get(project, 0) + 1
            queued = counts.get("queued", 0) + counts.get("waiting", 0) + counts.get("sending", 0)
            refused = [{"file": Path(i["path"]).name, "why": i.get("error") or ""}
                       for i in self.items if i["state"] == "refused"][-5:]
            held = [{"file": Path(i["path"]).name, "why": i.get("error") or ""}
                    for i in self.items if i.get("state") == "held"][-8:]
            active = {p: (u, w) for p, (u, w) in self.project_paused.items() if u > now}
        if not s["enabled"]:
            line = "Auto-upload is off"
        elif self.paused_until > now:
            line = f"Auto-upload paused: {self.paused_why} (until {time.strftime('%H:%M', time.localtime(self.paused_until))})"
        elif active:
            # One full project is not the whole rig. Name it, and count the
            # queue still moving for the others. The Collab tab shows this line.
            bits = []
            if self.current is not None:
                bits.append(f"sending {Path(self.current['path']).name}")
            for project, (until, why) in sorted(active.items()):
                n = proj_counts.get(project, 0)
                clock = time.strftime("%H:%M", time.localtime(until))
                bits.append(f"{project} paused ({n} queued, {_short_why(why)}, until {clock})")
            for project, n in sorted(proj_counts.items()):
                if project not in active:
                    bits.append(f"{project} {n} queued")
            line = "Auto-upload: " + " · ".join(bits)
        elif self.current is not None:
            line = f"Auto-upload: sending {Path(self.current['path']).name}, {queued} in the queue"
        elif self.paused_why:
            line = f"Auto-upload: {queued} queued, {self.paused_why}"
        else:
            line = f"Auto-upload: {queued} queued" if queued else "Auto-upload: up to date"
        project_counts = {}
        for project in set(proj_counts) | set(active):
            until, why = active.get(project, (0.0, ""))
            project_counts[project] = {"queued": proj_counts.get(project, 0), "paused": project in active,
                                       "until": until or None, "why": why}
        sent = counts.get("sent", 0) + counts.get("already", 0)
        line += f" · {sent} sent"
        if pending:
            line += f" · {pending} waiting for calibration on the server"
        if counts.get("held"):
            line += f" · {counts['held']} held"
        if counts.get("refused"):
            line += f" · {counts['refused']} refused"
        return {"enabled": s["enabled"], "rigs": s["rigs"], "mbit": s["mbit"],
                "daytimeOnly": s["daytimeOnly"], "running": self.running,
                "counts": counts, "pendingCalibration": pending, "line": line,
                "held": held,
                "pausedUntil": self.paused_until or None, "pausedWhy": self.paused_why,
                "projectsPaused": {p: {"until": u, "why": w} for p, (u, w) in self.project_paused.items() if u > now},
                "projectCounts": project_counts,
                "sentBytes": self.sent_bytes, "lastError": self.last_error, "refused": refused}

    def _say(self, message: str, level: str = "info") -> None:
        if self._log is not None:
            with contextlib.suppress(Exception):
                self._log(message, level)
