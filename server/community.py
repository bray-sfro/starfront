"""The collaboration's live stack: uploaded lights in, one picture out.

Telescopes already send their frames to this server as files. This is what
happens next. A light landing in a collaboration is queued, and a single
background worker registers it onto the project's canvas and folds it into
a running stack. The upload returns as soon as the file is on disk. The
picture is a different, slower thing, and it must not be allowed to hold
the door.

Registration reuses the same pieces a rig uses on its own machine.
`imaging.wcs` reads whatever plate solution the header actually carries.
`imaging.align` then measures that solution against the stars the stack has
already accepted, because two telescopes' headers do not agree with each
other closely enough to stack. `livestack.make_tile` resamples onto one
grid, takes a tilted sky off, and weighs the frame by its noise and its
exposure. The accumulator rejects outliers once it has seen enough frames
to have an opinion. A Bayer mosaic is debayered to luminance first, or the
stack would be a chequerboard.

What this deliberately does not do is in the module that serves the page:
no blind catalogue solve (the first frame's header is the absolute sky, and
everything after is matched to the stack), and no calibration (flats and
darks are stored and left for a proper stack). Both are named in the check
rather than papered over.

The page at `/watch` is public and read-only. It is given a stretched
preview, who contributed, and how many frames. It is not given a token, a
file list, or anywhere to send one.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from astrocontrol import collab, filters
from astrocontrol.imaging import align, debayer, fits, stars, wcs
from astrocontrol.imaging.debayer import DebayerError
from astrocontrol.imaging.wcs import Wcs, WcsError
from astrocontrol.livestack import (
    LiveStack, Plan, StackError, make_tile, stack_root, stored_plan,
)

#: The long side of the shared canvas, in pixels. Past this the grid is
#: coarsened rather than the process allowed to grow without a limit. Five
#: accumulators of a 2048 square is about a hundred megabytes, which is the
#: budget for one open stack.
CANVAS_LIMIT = 2048

#: Stacks held open. Each one is the canvas. The rest stay on disk and are
#: reopened when a frame or a viewer asks for them.
MAX_OPEN = 2

#: Identifiers waiting in memory. The durable queue is the pending file;
#: this is only how the worker is woken. Paths, never pixels.
QUEUE_LIMIT = 64

#: Viewers held open on the event stream. Past this a new one is told to
#: refresh the picture itself, so a public page cannot be used to pin an
#: unbounded number of queues.
MAX_VIEWERS = 48

#: How many frames the public recent list keeps. Enough to see what just
#: happened, not a second copy of the ledger.
RECENT_LIMIT = 40

#: Stars measured per frame. The same budget the local pipeline uses.
ALIGN_STARS = 400

#: How far a frame's star size may move its weight. Seeing is already in
#: the noise, somewhat; this stops a long, badly trailed sub from outvoting
#: a short sharp one purely because it is long. Clamped so one odd
#: measurement cannot silence a telescope or let it take over.
QUALITY_MIN = 0.25
QUALITY_MAX = 4.0

#: Half-flux radius to a Gaussian-equivalent FWHM. For a round Gaussian the
#: half-light radius is about half the FWHM, and that is the conversion used
#: when the page says "star size".
FWHM_PER_HFR = 2.0

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NOT_LIGHT = {"DARK", "FLAT", "BIAS", "DARKFLAT", "DARK FLAT"}
_BROADBAND = {"", "L", "OSC", "RGB", "COLOUR", "COLOR", "BAYER"}


def _safe_id(value: str) -> bool:
    return bool(_SAFE_ID.fullmatch(value or ""))


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _number(header: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = wcs.card(header, key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(number):
            return float(number)
    return None


def _filter_name(header: dict[str, Any], filename: str, debayered: bool) -> str:
    """Which stack a frame belongs on.

    The header wins. A filename is only consulted when the header says
    nothing, and only a token that is actually a filter name is kept — the
    whole filename is not a filter, and treating it as one would open a new
    stack per file.
    """
    raw = str(wcs.card(header, "FILTER") or "").strip()
    if raw and raw.lower() not in ("none", "null"):
        name = filters.canonical(raw)
    else:
        name = ""
        for token in re.split(r"[^A-Za-z0-9+]+", Path(filename).stem):
            folded = filters.canonical(token)
            if folded and (folded != token or token.upper() in ("L", "R", "G", "B", "H", "O", "S")):
                name = folded
                break
    if debayered and name.upper() in _BROADBAND:
        return "L"
    return name or "L"


def _retarget(solution: Wcs, width: int, height: int, dx: int, dy: int) -> Wcs:
    """The same solution on a frame that lost `dx` columns and `dy` rows."""
    return Wcs(solution.crval1, solution.crval2,
               solution.crpix1 - dx, solution.crpix2 - dy, solution.cd,
               width, height, source=solution.source,
               assumptions=solution.assumptions)


def place_frame(guess: Wcs, frame: np.ndarray, stack: LiveStack
                ) -> tuple[Wcs, dict[str, Any]]:
    """Where this frame sits on the sky, measured against the stack.

    The header is the guess. Once the stack has stars in the same patch of
    sky, the guess is replaced by a star match. A guess that cannot be
    matched when there *are* stars to match it against is refused: a frame
    stacked a few pixels off spoils every pixel it touches, and a missing
    frame does not.

    The first frame has nothing to match and anchors the stack on its own
    header. That is the absolute orientation of everything that follows.
    """
    found = stars.detect(np.asarray(frame))[:ALIGN_STARS]
    reference = stack.reference_near(guess, ALIGN_STARS)
    if len(reference) < align.MIN_MATCHES or len(found) < align.MIN_MATCHES:
        if (stack.frames and len(reference) >= align.MIN_MATCHES
                and len(found) < align.MIN_MATCHES):
            raise StackError(
                f"only {len(found)} stars in this frame, which is not enough "
                "to register it against the stack")
        if guess.source == "derived":
            detail = ("nothing in the stack to align against yet, so this "
                      "frame anchors it on its header's own guess")
        else:
            detail = "placed by the plate solution in its header"
        return guess, {"aligned": False, "source": guess.source,
                       "detail": detail, "stars": found}
    try:
        fixed, note = align.refine(
            guess, align.star_pixels(found), reference, reference=stack.canvas)
    except align.AlignError as exc:
        raise StackError(str(exc)) from exc
    return fixed, {**note, "source": "aligned", "stars": found}


def _star_size(found: list[tuple[float, float, float]], scale: float) -> float | None:
    """Gaussian-equivalent FWHM in arcseconds, from the frame's own stars."""
    if not found or not scale:
        return None
    radii = sorted(float(star[2]) for star in found[:80] if star[2] > 0)
    if not radii:
        return None
    return FWHM_PER_HFR * float(np.median(radii)) * float(scale)


def _quality(fwhm: float | None, previous: list[float]) -> float:
    if not fwhm or not previous:
        return 1.0
    reference = float(np.median(previous))
    if reference <= 0 or fwhm <= 0:
        return 1.0
    return float(min(QUALITY_MAX, max(QUALITY_MIN, (reference / fwhm) ** 2)))


class CommunityStacker:
    """One worker, a folder of stacks, and the pages that watch them."""

    def __init__(self, store: Any, root: Path | str,
                 files_root: Path | str | None = None) -> None:
        self.store = store
        self.root = Path(root)
        self.files_root = Path(files_root).resolve() if files_root else None
        self.root.mkdir(parents=True, exist_ok=True)
        self.gate = threading.Event()
        self.gate.set()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._queue: queue.Queue[str] = queue.Queue(maxsize=QUEUE_LIMIT)
        self._queued: set[str] = set()
        self._busy = False
        self._lock = threading.RLock()
        self._stacks: dict[str, LiveStack] = {}
        self._used: dict[str, float] = {}
        self._previews: dict[tuple, tuple[float, int, bytes]] = {}
        self._viewers: list[dict[str, Any]] = []
        self._pending_path = self.root / "pending.json"

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._pump()
        self._worker = threading.Thread(target=self._run, name="community-stack",
                                        daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self.gate.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=10)
        self._worker = None

    def reload(self) -> None:
        """What a process restart does: drop memory, reopen from disk.

        The accumulators and the pending list are files. Closing the open
        stacks flushes them; the next frame and the next viewer read them
        back. Used by the check, and the same sequence `start` runs after a
        real restart.
        """
        self.wait_idle(60)
        self.stop()
        with self._lock:
            for stack in self._stacks.values():
                with _quiet():
                    stack.save()
                with _quiet():
                    stack.close()
            self._stacks.clear()
            self._used.clear()
            self._previews.clear()
        self.start()

    def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._busy and self._queue.empty() and not self._jobs():
                return True
            time.sleep(0.05)
        return False

    # -- the queue ---------------------------------------------------------
    def offer(self, job: dict[str, Any]) -> None:
        """Remember a light to stack. Never raises, never reads the pixels.

        Called from the upload itself. The file is already on disk; this
        writes one small record and wakes the worker.
        """
        try:
            project = str(job.get("project") or "")
            name = str(job.get("name") or "")
            path = str(job.get("path") or "")
            if not _safe_id(project) or not name or not path:
                return
            record = {
                "id": f"{job.get('user') or ''}:{name}:{int(job.get('bytes') or 0)}",
                "project": project,
                "user": str(job.get("user") or ""),
                "name": name,
                "path": path,
                "bytes": int(job.get("bytes") or 0),
                "telescope": str(job.get("telescope") or "")[:60],
                "uploader": str(job.get("uploader") or "")[:60],
            }
            with self._lock:
                jobs = self._jobs()
                if any(existing.get("id") == record["id"] for existing in jobs):
                    return
                jobs.append(record)
                _atomic_json(self._pending_path, {"jobs": jobs})
            self._enqueue(record["id"])
        except Exception:
            # The file is already kept. Failing the upload because the stack
            # could not be told about it would punish the thing that worked.
            return

    def _jobs(self) -> list[dict[str, Any]]:
        payload = _read_json(self._pending_path, {"jobs": []})
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        return list(jobs) if isinstance(jobs, list) else []

    def _enqueue(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._queued:
                return
            try:
                self._queue.put_nowait(job_id)
            except queue.Full:
                return
            self._queued.add(job_id)

    def _pump(self) -> None:
        for job in self._jobs():
            job_id = str(job.get("id") or "")
            if job_id:
                self._enqueue(job_id)

    def _find(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            for job in self._jobs():
                if job.get("id") == job_id:
                    return job
        return None

    def _drop(self, job_id: str) -> None:
        with self._lock:
            jobs = [job for job in self._jobs() if job.get("id") != job_id]
            _atomic_json(self._pending_path, {"jobs": jobs})

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.gate.is_set():
                self.gate.wait(timeout=0.5)
                continue
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                self._pump()
                continue
            with self._lock:
                self._queued.discard(job_id)
            if self._stop.is_set() or not self.gate.is_set():
                self._enqueue(job_id)
                continue
            job = self._find(job_id)
            if job is None:
                continue
            self._busy = True
            try:
                self._process(job)
            except StackError as exc:
                self._record(job, False, str(exc))
            except Exception as exc:  # noqa: BLE001 - one bad frame ends there
                self._record(job, False, f"{type(exc).__name__}: {exc}")
            finally:
                self._busy = False
                self._drop(job_id)
                self._notify(str(job.get("project") or ""))

    # -- one frame ---------------------------------------------------------
    def _process(self, job: dict[str, Any]) -> None:
        path = Path(str(job.get("path") or ""))
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise StackError(f"the frame could not be read ({exc})") from exc
        if self.files_root is not None and not _under(resolved, self.files_root):
            raise StackError("that frame is not in the files folder")
        name = str(job.get("name") or path.name)
        if name.lower().endswith(".xisf"):
            raise StackError("XISF is kept on disk but not live-stacked")
        if not resolved.is_file():
            raise StackError("the uploaded frame is not on disk")

        header = fits.read_header(resolved)
        if int(header.get("NAXIS") or 0) != 2:
            raise StackError("only 2-D FITS frames are stacked")
        kind = str(header.get("IMAGETYP") or "").strip().upper().replace(" ", "")
        if kind in _NOT_LIGHT or kind.replace(" ", "") in _NOT_LIGHT:
            raise StackError(f"this is a {kind or 'calibration frame'}, not a light")

        values, header = fits.read_values(resolved)
        frame = np.asarray(values, dtype=np.float32)
        del values
        bitpix = int(header.get("BITPIX") or 0)
        colour = False
        try:
            prepared = debayer.apply(frame, header)
        except DebayerError as exc:
            raise StackError(str(exc)) from exc
        if prepared is not None:
            frame, dx, dy = prepared
            colour = True
        else:
            dx = dy = 0

        try:
            solution = wcs.from_header(header, allow_derived=True)
        except WcsError as exc:
            raise StackError(str(exc)) from exc
        if (dx or dy or solution.width != frame.shape[1]
                or solution.height != frame.shape[0]):
            solution = _retarget(solution, int(frame.shape[1]), int(frame.shape[0]), dx, dy)

        project = str(job["project"])
        filter_name = _filter_name(header, name, colour)
        seconds = _number(header, "EXPTIME", "EXPOSURE", "ONTIME") or 0.0
        who = _contributor(job)
        stack = self._open(project, filter_name, solution.scale)
        solution, placing = place_frame(solution, frame, stack)
        found = list(placing.get("stars") or [])
        fwhm = _star_size(found, solution.scale)
        previous = [float(row["fwhm"]) for row in stack.taken.values()
                    if row.get("fwhm")]
        factor = _quality(fwhm, previous)

        tile = make_tile(
            frame, solution, stack.canvas, str(job["id"]),
            seconds=float(seconds), agent=who, filter_name=filter_name,
            night=str(header.get("DATE-OBS") or "")[:10])
        del frame
        tile.note["calibrated"] = False
        tile.note["debayered"] = colour
        if factor != 1.0 and tile.weight is not None:
            tile.weight = np.asarray(tile.weight, dtype=np.float32) * np.float32(factor)
        report = stack.add(tile)
        if report.get("added"):
            stack.remember_stars(align.star_sky(found, solution))
        report["contributor"] = who
        report["frame"] = name
        report["fwhm"] = None if fwhm is None else round(fwhm, 3)
        report["quality"] = round(factor, 3)
        report["debayered"] = colour
        report["bitpix"] = bitpix
        report["aligned"] = bool(placing.get("aligned"))
        report["residual"] = placing.get("residual")
        report["placing"] = str(placing.get("detail") or "")[:240]
        report["noise"] = (tile.note or {}).get("noise")
        self._publish(project, filter_name, stack)
        self._record(job, bool(report.get("added")),
                     str(report.get("detail") or report.get("placing") or ""),
                     extra={
                         "filter": filter_name,
                         "contributor": who,
                         "frame": name,
                         "seconds": float(seconds),
                         "aligned": bool(placing.get("aligned")),
                         "residual": placing.get("residual"),
                         "fwhm": report["fwhm"],
                         "quality": report["quality"],
                         "debayered": colour,
                         "bitpix": bitpix,
                         "gain": report.get("gain"),
                         "rejected": report.get("rejected"),
                         "duplicate": bool(report.get("duplicate")),
                     })

    def _record(self, job: dict[str, Any], added: bool, detail: str,
                extra: dict[str, Any] | None = None) -> None:
        project = str(job.get("project") or "")
        if not _safe_id(project):
            return
        text = detail.replace(str(job.get("path") or ""), str(job.get("name") or ""))
        entry = {
            "at": round(time.time(), 3),
            "added": bool(added),
            "contributor": _contributor(job),
            "frame": str(job.get("name") or ""),
            "detail": text[:240],
        }
        if extra:
            for key, value in extra.items():
                if key in ("path", "user", "token"):
                    continue
                entry[key] = value
        path = self.root / project / "journal.json"
        with self._lock:
            journal = _read_json(path, {"recent": []})
            recent = list(journal.get("recent") or [])
            recent.append(entry)
            _atomic_json(path, {"recent": recent[-RECENT_LIMIT:]})

    # -- stacks ------------------------------------------------------------
    def _scales(self, project_id: str) -> list[float]:
        found: list[float] = []
        for task in self.store.tasks_in(project_id):
            agent = self.store.agent(str(task.get("agent") or "")) or {}
            profile = collab.RigProfile.read(agent.get("profile") or {})
            scale = profile.scale()
            if scale and scale > 0:
                found.append(float(scale))
        return found

    def _plan(self, project_id: str, frame_scale: float) -> Plan:
        path = self.root / project_id / "canvas.json"
        if path.is_file():
            return Plan.read(_read_json(path, {}))
        project = self.store.project(project_id) or {}
        region = (project.get("payload") or {}).get("region") or {}
        # The telescopes that have joined, when any have. The frame being
        # stacked is one of them, and counting it again would pull the
        # median toward whoever happened to upload first. A frame that
        # arrives before anyone has joined is the only scale there is.
        scales = self._scales(project_id)
        if not scales and frame_scale and frame_scale > 0:
            scales.append(float(frame_scale))
        plan = Plan.for_region(
            ra=float(region.get("ra") or 0.0),
            dec=float(region.get("dec") or 0.0),
            width=float(region.get("width") or 1.0),
            height=float(region.get("height") or 1.0),
            scales=scales, max_pixels=CANVAS_LIMIT)
        _atomic_json(path, plan.payload())
        return Plan.read(plan.payload())

    def _open(self, project_id: str, filter_name: str, frame_scale: float) -> LiveStack:
        key = f"{project_id}/{filter_name}"
        with self._lock:
            found = self._stacks.get(key)
            if found is not None:
                self._used[key] = time.monotonic()
                return found
            folder = stack_root(self.root, project_id, filter_name)
            existing = stored_plan(folder)
            plan = existing or self._plan(project_id, frame_scale)
            stack = LiveStack.open(folder, None if existing is not None else plan)
            self._stacks[key] = stack
            self._used[key] = time.monotonic()
            while len(self._stacks) > MAX_OPEN:
                oldest = min((name for name in self._stacks if name != key),
                             key=lambda name: self._used.get(name, 0.0))
                self._release(oldest)
            return stack

    def _open_existing(self, project_id: str, filter_name: str) -> LiveStack | None:
        key = f"{project_id}/{filter_name}"
        with self._lock:
            found = self._stacks.get(key)
            if found is not None:
                self._used[key] = time.monotonic()
                return found
        folder = stack_root(self.root, project_id, filter_name)
        if stored_plan(folder) is None:
            return None
        return self._open(project_id, filter_name, 0.0)

    def _release(self, key: str) -> None:
        found = self._stacks.pop(key, None)
        self._used.pop(key, None)
        if found is None:
            return
        with _quiet():
            found.save()
        with _quiet():
            found.close()

    def _publish(self, project_id: str, filter_name: str, stack: LiveStack) -> None:
        summary = stack.summary()
        with _quiet():
            stack.save()
        contributors: dict[str, dict[str, Any]] = {}
        for row in stack.taken.values():
            name = str(row.get("contributor") or row.get("agent") or "observer")[:60]
            bucket = contributors.setdefault(name, {
                "name": name, "frames": 0, "seconds": 0.0, "fwhm": [],
            })
            bucket["frames"] += 1
            bucket["seconds"] += float(row.get("seconds") or 0.0)
            if row.get("fwhm"):
                bucket["fwhm"].append(float(row["fwhm"]))
        listed = []
        for bucket in contributors.values():
            sizes = bucket.pop("fwhm")
            bucket["seconds"] = round(bucket["seconds"], 1)
            bucket["medianFwhm"] = round(float(np.median(sizes)), 2) if sizes else None
            listed.append(bucket)
        listed.sort(key=lambda row: (-int(row["frames"]), str(row["name"])))
        safe_filter = _filter_dir(filter_name)
        payload = {
            "filter": safe_filter,
            "frames": summary["frames"],
            "seconds": summary["seconds"],
            "covered": summary["covered"],
            "rejectedPixels": summary["rejectedPixels"],
            "referenceStars": summary["referenceStars"],
            "width": summary["plan"]["pixelWidth"],
            "height": summary["plan"]["pixelHeight"],
            "scale": summary["plan"]["actualScale"],
            "maxFramesDeep": summary["maxFramesDeep"],
            "onDisk": summary["onDisk"],
            "updated": summary["updated"],
            "contributors": listed,
            "preview": f"/api/v1/watch/{project_id}/{safe_filter}/preview.png",
        }
        _atomic_json(stack.root / "public.json", payload)
        with self._lock:
            self._previews = {key: value for key, value in self._previews.items()
                              if key[0] != project_id or key[1] != safe_filter}

    def _credits_snapshot(self, project_id: str) -> dict[str, Any] | None:
        project = self.store.project(project_id)
        if project is None:
            return None
        stacks = []
        base = self.root / project_id
        if base.is_dir():
            for child in sorted(path for path in base.iterdir() if path.is_dir()):
                payload = _read_json(child / "public.json", None)
                if isinstance(payload, dict) and payload.get("filter"):
                    stacks.append(payload)
        journal = _read_json(base / "journal.json", {"recent": []})
        recent = list(journal.get("recent") or [])[-20:]
        updated = [float(row.get("updated") or 0.0) for row in stacks]
        pending = 0
        with self._lock:
            pending = sum(1 for job in self._jobs() if job.get("project") == project_id)
        return {
            "project": project_id,
            "name": project.get("name") or "",
            "frames": sum(int(row.get("frames") or 0) for row in stacks),
            "pending": pending,
            "updated": max(updated) if updated else None,
            "stacks": stacks,
            "recent": recent,
        }

    def catalog(self) -> list[dict[str, Any]]:
        listed = []
        for project in self.store.projects():
            if project.get("status") != "open":
                continue
            payload = project.get("payload") or {}
            summary = self._credits_snapshot(project["id"]) or {}
            frames = int(summary.get("frames") or 0)
            if not payload.get("collectFiles") and frames <= 0:
                continue
            listed.append({
                "id": project["id"],
                "name": project.get("name") or "",
                "frames": frames,
                "updated": summary.get("updated"),
                "watch": f"/watch/{project['id']}",
            })
        listed.sort(key=lambda row: (-int(row["frames"]), str(row["name"])))
        return listed

    def preview_png(self, project_id: str, filter_name: str, width: int) -> bytes | None:
        stack = self._open_existing(project_id, filter_name)
        if stack is None or stack.frames <= 0:
            return None
        updated = float(stack.updated or 0.0)
        key = (project_id, filter_name, int(width))
        with self._lock:
            cached = self._previews.get(key)
            if cached is not None and cached[0] == updated and cached[1] == width:
                return cached[2]
        image, _info = stack.preview(max_dim=width)
        with self._lock:
            self._previews[key] = (updated, width, image)
            while len(self._previews) > 16:
                self._previews.pop(next(iter(self._previews)))
        return image

    # -- viewers -----------------------------------------------------------
    def _notify(self, project_id: str) -> None:
        if not project_id:
            return
        summary = self._credits_snapshot(project_id)
        if summary is None:
            return
        payload = json.dumps(summary)
        with self._lock:
            viewers = list(self._viewers)
        for viewer in viewers:
            if viewer["project"] != project_id:
                continue
            slot: queue.Queue[str] = viewer["queue"]
            try:
                slot.put_nowait(payload)
            except queue.Full:
                with _quiet():
                    slot.get_nowait()
                with _quiet():
                    slot.put_nowait(payload)

    def subscribe(self, project_id: str) -> queue.Queue[str] | None:
        with self._lock:
            if len(self._viewers) >= MAX_VIEWERS:
                return None
            slot: queue.Queue[str] = queue.Queue(maxsize=4)
            self._viewers.append({"project": project_id, "queue": slot})
            return slot

    def unsubscribe(self, slot: queue.Queue[str]) -> None:
        with self._lock:
            self._viewers = [viewer for viewer in self._viewers
                             if viewer["queue"] is not slot]

    def attach(self, app: FastAPI) -> None:
        stacker = self

        @app.get("/watch")
        def watch_index() -> HTMLResponse:
            return HTMLResponse(_PAGE.replace("{{PROJECT}}", ""),
                                headers={"Cache-Control": "no-store"})

        @app.get("/watch/{project_id}")
        def watch_project(project_id: str) -> HTMLResponse:
            if not _safe_id(project_id) or stacker.store.project(project_id) is None:
                raise HTTPException(status_code=404, detail="no such collaboration")
            return HTMLResponse(_PAGE.replace("{{PROJECT}}", project_id),
                                headers={"Cache-Control": "no-store"})

        @app.get("/api/v1/watch")
        def watch_list() -> dict[str, Any]:
            return {"projects": stacker.catalog()}

        @app.get("/api/v1/watch/{project_id}")
        def watch_one(project_id: str) -> dict[str, Any]:
            if not _safe_id(project_id):
                raise HTTPException(status_code=404, detail="no such collaboration")
            summary = stacker._credits_snapshot(project_id)
            if summary is None:
                raise HTTPException(status_code=404, detail="no such collaboration")
            return summary

        @app.get("/api/v1/watch/{project_id}/events")
        def watch_events(project_id: str):
            if not _safe_id(project_id) or stacker.store.project(project_id) is None:
                raise HTTPException(status_code=404, detail="no such collaboration")
            slot = stacker.subscribe(project_id)
            if slot is None:
                raise HTTPException(status_code=503,
                                    detail="too many viewers; reload the picture")

            async def stream():
                # Polled rather than waited on. A blocking read here would
                # sit on the event loop, and the event loop is also what
                # accepts the next frame.
                quiet = 0.0
                try:
                    summary = stacker._credits_snapshot(project_id) or {}
                    yield _event(summary)
                    while True:
                        try:
                            payload = slot.get_nowait()
                        except queue.Empty:
                            await asyncio.sleep(0.4)
                            quiet += 0.4
                            if quiet >= 15:
                                quiet = 0.0
                                yield ": keep-alive\n\n"
                            continue
                        quiet = 0.0
                        yield f"event: stack\ndata: {payload}\n\n"
                finally:
                    stacker.unsubscribe(slot)

            return StreamingResponse(stream(), media_type="text/event-stream",
                                    headers={"Cache-Control": "no-cache",
                                             "X-Accel-Buffering": "no"})

        @app.get("/api/v1/watch/{project_id}/{filter_name}/preview.png")
        def watch_preview(project_id: str, filter_name: str,
                          width: int = Query(default=1200, ge=64, le=2048)):
            if not _safe_id(project_id) or stacker.store.project(project_id) is None:
                raise HTTPException(status_code=404, detail="no such collaboration")
            safe = _filter_dir(filter_name)
            image = stacker.preview_png(project_id, safe, width)
            if image is None:
                raise HTTPException(status_code=404, detail="no stack yet")
            return Response(content=image, media_type="image/png",
                            headers={"Cache-Control": "no-cache"})


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _filter_dir(name: str) -> str:
    """The same spelling `livestack.stack_root` uses for the folder."""
    safe = "".join(character if character.isalnum() or character in "-_" else "_"
                   for character in (name or "none")) or "none"
    return safe


def _contributor(job: dict[str, Any]) -> str:
    telescope = str(job.get("telescope") or "").strip()
    uploader = str(job.get("uploader") or "").strip()
    if uploader.lower() in ("coordinator", "admin", ""):
        uploader = ""
    if telescope and uploader:
        return f"{telescope} ({uploader})"[:60]
    return (telescope or uploader or "observer")[:60]


def _event(payload: dict[str, Any]) -> str:
    return f"event: stack\ndata: {json.dumps(payload)}\n\n"


class _quiet:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: Any) -> bool:
        return True


_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Starfront live stack</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background: #0c1016; color: #e6edf5;
         font: 15px/1.5 "Segoe UI", system-ui, sans-serif; }
  main { max-width: 1100px; margin: 0 auto; padding: 28px 20px 64px; }
  h1 { font-size: 26px; font-weight: 600; margin: 0 0 4px; }
  h2 { font-size: 18px; margin: 22px 0 8px; }
  a { color: #9fe4bd; text-decoration: none; }
  a:hover { text-decoration: underline; }
  p { margin: 6px 0; }
  .muted { color: #8f9bb0; }
  .status { font-size: 13px; }
  .live { color: #7ee7a5; } .wait { color: #e5c07b; } .bad { color: #ff8a7a; }
  .card { background: #121821; border: 1px solid #243044; border-radius: 12px;
          padding: 14px 16px; margin: 12px 0; }
  .card h2 { margin-top: 0; }
  img.stack { width: 100%; height: auto; background: #000; border-radius: 8px; display: block; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid #1e2836; }
  th { color: #8f9bb0; font-weight: 500; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .stats { display: flex; flex-wrap: wrap; gap: 14px 22px; margin: 8px 0 12px; }
  .stats b { display: block; font-size: 18px; font-weight: 600; }
  ul { margin: 8px 0; padding-left: 18px; }
  li { margin: 2px 0; }
</style></head>
<body data-project="{{PROJECT}}"><main>
<h1>Starfront live stack</h1>
<p class="muted">A public, read-only picture of a collaboration. It updates as new frames are stacked. Nothing on this page can change the collaboration.</p>
<p class="status" id="status">Connecting…</p>
<div id="index"></div>
<div id="view"></div>
</main>
<script>
(function () {
  const root = document.body.getAttribute("data-project") || "";
  const status = document.getElementById("status");
  const index = document.getElementById("index");
  const view = document.getElementById("view");
  const text = (value) => document.createTextNode(value == null ? "" : String(value));
  const el = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content != null) node.appendChild(text(content));
    return node;
  };
  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };

  function setStatus(kind, message) {
    status.className = "status " + kind;
    status.textContent = message;
  }

  function drawList(projects) {
    clear(index);
    const heading = el("h2", "", "Collaborations");
    index.appendChild(heading);
    if (!projects.length) {
      index.appendChild(el("p", "muted", "No collaboration is collecting frames right now."));
      return;
    }
    projects.forEach((project) => {
      const card = el("div", "card");
      const link = document.createElement("a");
      link.href = project.watch;
      link.appendChild(text(project.name || project.id));
      const title = el("h2");
      title.appendChild(link);
      card.appendChild(title);
      const line = el("p", "muted");
      line.appendChild(text((project.frames || 0) + " frames in the live stack"));
      card.appendChild(line);
      index.appendChild(card);
    });
  }

  function drawProject(summary) {
    clear(view);
    const title = el("h2", "", summary.name || summary.project);
    view.appendChild(title);
    const line = el("p", "muted");
    const pending = summary.pending ? (", " + summary.pending + " waiting to be stacked") : "";
    line.appendChild(text((summary.frames || 0) + " frames stacked" + pending));
    view.appendChild(line);
    const stacks = summary.stacks || [];
    if (!stacks.length) {
      view.appendChild(el("p", "", "Waiting for the first frame."));
      return;
    }
    stacks.forEach((stack) => {
      const card = el("div", "card");
      card.appendChild(el("h2", "", "Filter " + stack.filter));
      const stats = el("div", "stats");
      const bits = [
        [String(stack.frames || 0), "frames"],
        [stack.seconds != null ? (stack.seconds + " s") : "—", "exposure"],
        [stack.covered != null ? (Math.round(stack.covered * 100) + "%") : "—", "of the canvas"],
        [stack.scale != null ? (stack.scale + " \"/px") : "—", "scale"],
        [String(stack.rejectedPixels || 0), "outlier pixels"],
      ];
      bits.forEach((pair) => {
        const cell = el("div");
        cell.appendChild(el("b", "", pair[0]));
        cell.appendChild(text(pair[1]));
        stats.appendChild(cell);
      });
      card.appendChild(stats);
      if (stack.preview) {
        const image = document.createElement("img");
        image.className = "stack";
        image.alt = "Stretched preview of the " + stack.filter + " stack";
        image.src = stack.preview + "?width=1200&v=" + (stack.updated || 0);
        card.appendChild(image);
        card.appendChild(el("p", "muted", "Stretched for display. The stack itself stays linear."));
      }
      const people = stack.contributors || [];
      if (people.length) {
        const table = document.createElement("table");
        const head = document.createElement("thead");
        const row = document.createElement("tr");
        ["Contributor", "Frames", "Exposure (s)", "Star size (\")"].forEach((label, i) => {
          row.appendChild(el("th", i ? "num" : "", label));
        });
        head.appendChild(row);
        table.appendChild(head);
        const body = document.createElement("tbody");
        people.forEach((person) => {
          const line = document.createElement("tr");
          line.appendChild(el("td", "", person.name || "observer"));
          line.appendChild(el("td", "num", person.frames));
          line.appendChild(el("td", "num", person.seconds));
          line.appendChild(el("td", "num", person.medianFwhm == null ? "—" : person.medianFwhm));
          body.appendChild(line);
        });
        table.appendChild(body);
        card.appendChild(table);
      }
      view.appendChild(card);
    });
    const recent = summary.recent || [];
    if (recent.length) {
      const card = el("div", "card");
      card.appendChild(el("h2", "", "Latest frames"));
      const list = document.createElement("ul");
      recent.slice().reverse().slice(0, 8).forEach((entry) => {
        const item = document.createElement("li");
        const mark = entry.added ? "stacked" : "not stacked";
        item.appendChild(text(
          (entry.contributor || "observer") + " · " + (entry.frame || "frame")
          + " · " + mark + (entry.detail ? " — " + entry.detail : "")));
        list.appendChild(item);
      });
      card.appendChild(list);
      view.appendChild(card);
    }
  }

  async function loadIndex() {
    const response = await fetch("/api/v1/watch");
    if (!response.ok) throw new Error("the list could not be read");
    const body = await response.json();
    drawList(body.projects || []);
  }

  let source = null;
  async function loadProject() {
    const response = await fetch("/api/v1/watch/" + root);
    if (!response.ok) throw new Error("this collaboration could not be read");
    drawProject(await response.json());
  }

  function connect() {
    if (!window.EventSource) {
      setStatus("wait", "This browser has no live connection; the picture refreshes every few seconds.");
      return;
    }
    source = new EventSource("/api/v1/watch/" + root + "/events");
    source.addEventListener("stack", (event) => {
      setStatus("live", "Live — the picture updates as frames are stacked.");
      try { drawProject(JSON.parse(event.data)); } catch (err) { setStatus("bad", "A live update could not be read."); }
    });
    source.onerror = () => setStatus("wait", "Reconnecting…");
  }

  (root ? loadProject() : loadIndex()).then(() => {
    if (root) connect();
    else setStatus("live", "Pick a collaboration to watch.");
  }).catch((err) => setStatus("bad", err.message || "Could not load."));

  if (root) {
    setInterval(() => { loadProject().catch(() => {}); }, 15000);
  }
})();
</script>
</body></html>
"""
