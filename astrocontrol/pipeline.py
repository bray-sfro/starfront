"""The live pipeline: a frame lands on disk, and a shared picture moves.

Everything else in this folder is a step. This is the thing that runs them,
in order, on every sub, all night, on the machine that is also driving the
mount — which is the constraint that shapes all of it.

    raw sub  ->  calibrate  ->  place it on the sky  ->  resample onto the
    shared canvas  ->  fold into the stack  ->  send the tile  ->  show it

Calibration is already somebody else's job: `calibration.Library` has matched
and applied masters to every light this program takes since long before this
existed, and it writes the result into a `calibrated` folder beside the raw
frame. So this begins where that ends, and the first thing it does with a
frame is find out where on the sky it is.

**Nothing here can stop a night.** The same rule the collaboration client
follows, for the same reason: a rig at a dark site behind a domestic
connection has to work when the connection does not. Every stage is wrapped,
every failure is a log line and a frame left out of the stack, and the queue
is bounded so that a pipeline falling behind drops contributions rather than
memory. The raw frame is safe on disk before any of this is queued, and
nothing here ever writes to it.

**Frames that cannot be sent are spooled, not dropped.** A tile that the
server would not take — because the server is down, or the roof is between
the observatory and the router — is written to a folder and sent on the next
poll. A night behind a dead connection arrives late rather than never, which
is the difference between a collaboration people trust and one they stop
using.

**The stack is kept locally as well as centrally.** Partly because it is
free — the tile has already been made, folding it into a local canvas is one
more addition — and mostly because it is the answer to "is tonight working?"
at two in the morning with the network down. A rig that can see its own stack
growing needs nobody's server to know that its focus is holding.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import livestack
from .config import data_root
from .imaging import align, fits, stars, wcs
from .livestack import LiveStack, Plan, StackError, Tile

#: Frames waiting to be stacked. A sub takes minutes and a stack takes
#: seconds, so this is never deep in normal running; when it is, the rig is
#: doing something else it should be doing more (a mosaic sweep at ten-second
#: subs, say) and the honest answer is to say so and leave the frames out.
BACKLOG = 8

#: How often the accumulators are written out, in seconds. Every contribution
#: would be four canvas-sized arrays per sub for no benefit; never would mean
#: losing the night to a power cut. Two minutes costs at most two minutes.
SAVE_EVERY = 120.0

#: Stars taken from each frame for the alignment. Enough to pin a
#: six-parameter fit to a fraction of a pixel; few enough that finding them
#: costs a second rather than ten.
ALIGN_STARS = 400


class Job:
    """One frame on its way into a stack, and everything needed to place it."""

    def __init__(self, path: Path, project: str, filter_name: str,
                 plan: Plan, night: str = "", agent: str = "",
                 seconds: float = 0.0, task: str = "",
                 telescope: str = "") -> None:
        self.path = Path(path)
        self.project = project
        self.filter = filter_name
        self.plan = plan
        self.night = night
        self.agent = agent
        self.seconds = seconds
        self.task = task
        self.telescope = telescope
        self.queued = time.time()

    @property
    def tile_id(self) -> str:
        """Stable across retries, unique across contributors.

        Built from the rig and the file rather than from a counter or a
        random number, so the same frame offered twice — by a retry, by a
        re-scan of a folder, by two copies of the program — is recognised as
        the same frame and counted once.
        """
        return f"{self.agent or self.telescope or 'rig'}:{self.night}:{self.path.name}"


class StackPipeline:
    """Calibrated frames in, a live stack and outgoing tiles out."""

    def __init__(self, config: Any, root: Path | str | None = None,
                 send: Callable[[Tile, Job], bool] | None = None,
                 log: Callable[[str, str], None] | None = None) -> None:
        self.config = config
        self.root = Path(root) if root else (data_root() / "livestacks")
        #: Called with a finished tile; returns whether the server took it.
        #: Left unset on a rig that is not in a collaboration, which then
        #: keeps a local stack and sends nothing.
        self.send = send
        self._log = log

        self._queue: queue.Queue[Job] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._stacks: dict[str, LiveStack] = {}
        self._saved: dict[str, float] = {}
        self._last: dict[str, Any] | None = None
        self._counts = {"stacked": 0, "refused": 0, "sent": 0, "spooled": 0}

    # -- settings ----------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        return self.config.section("livestack")

    @property
    def enabled(self) -> bool:
        return bool(self.settings().get("enabled", True))

    @property
    def spool(self) -> Path:
        return self.root / "outbox"

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, daemon=True,
                                        name="livestack")
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    # -- putting work in ---------------------------------------------------
    def submit(self, job: Job) -> bool:
        """Offer a calibrated frame to the pipeline. Never raises, never blocks.

        Returns whether it was taken. A refusal is a normal outcome worth
        reporting — the pipeline is off, or it is behind — and never a reason
        for the caller to do anything but carry on capturing.
        """
        if not self.enabled:
            return False
        if self._queue.qsize() >= BACKLOG:
            self._say(f"The live stack is {BACKLOG} frames behind; "
                      f"{job.path.name} was left out of it", "warn")
            self._counts["refused"] += 1
            return False
        self.start()
        self._queue.put(job)
        return True

    # -- the stacks --------------------------------------------------------
    def stack_for(self, project: str, filter_name: str, plan: Plan) -> LiveStack:
        """The stack for one project and filter, opened or created.

        Held open between frames: reopening means reading five canvas-sized
        arrays off disk, and a rig shooting five-minute subs would spend a
        measurable part of the night doing it.
        """
        key = f"{project}/{filter_name}"
        with self._lock:
            found = self._stacks.get(key)
            if found is not None and found.plan.payload() == plan.payload():
                return found
            root = livestack.stack_root(self.root, project, filter_name)
            stack = LiveStack.open(root, plan)
            self._stacks[key] = stack
            return stack

    def existing_plan(self, project: str, filter_name: str) -> Plan | None:
        """The canvas this project and filter are already being stacked on.

        Asked before a canvas is worked out from anything else, because a
        canvas that changes mid-night throws the night away. See
        `livestack.stored_plan`.
        """
        key = f"{project}/{filter_name}"
        with self._lock:
            found = self._stacks.get(key)
            if found is not None:
                return found.plan
        return livestack.stored_plan(
            livestack.stack_root(self.root, project, filter_name))

    def stacks(self) -> dict[str, LiveStack]:
        with self._lock:
            return dict(self._stacks)

    def save_all(self) -> None:
        for stack in self.stacks().values():
            with contextlib.suppress(OSError):
                stack.save()

    # -- the work ----------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                self._flush_spool()
                continue
            try:
                self._process(job)
            except Exception as exc:              # noqa: BLE001 - never fatal
                self._counts["refused"] += 1
                self._last = {"path": job.path.name, "error": str(exc),
                              "at": time.time()}
                self._say(f"{job.path.name} was left out of the live stack — "
                          f"{exc}", "warn")

    def _process(self, job: Job) -> None:
        started = time.monotonic()
        stack = self.stack_for(job.project, job.filter, job.plan)
        canvas = stack.canvas

        frame, header = fits.read(job.path)
        guess = wcs.from_header(header, allow_derived=True)

        solution, placing = self._place(guess, frame, stack)

        tile = livestack.make_tile(
            frame, solution, canvas, job.tile_id, seconds=job.seconds,
            agent=job.agent or job.telescope, filter_name=job.filter,
            night=job.night,
            degree=1 if self.settings().get("removeGradient", True) else 0)
        report = stack.add(tile)

        # Taken out either way: a few hundred star positions are not something
        # to carry into a status payload that the UI polls every second.
        found = placing.pop("stars", [])
        if report.get("added"):
            # Only a frame that was actually taken contributes to what later
            # frames are aligned against. One that was refused is, by
            # definition, not trusted to say where anything is.
            stack.remember_stars(align.star_sky(found, solution))
            self._counts["stacked"] += 1

        self._maybe_save(job, stack)
        sent = self._deliver(tile, job)

        self._last = {
            "path": job.path.name,
            "project": job.project,
            "filter": job.filter,
            "placing": placing,
            "stack": report,
            "sent": sent,
            "seconds": round(time.monotonic() - started, 2),
            "at": time.time(),
        }
        self._say(f"Live stack: {job.path.name} — {placing['detail']}; "
                  f"{report.get('detail', 'not added')}"
                  + ("" if sent else "; held to send later"))

    def _place(self, guess: wcs.Wcs, frame: np.ndarray,
               stack: LiveStack) -> tuple[wcs.Wcs, dict[str, Any]]:
        """Work out where the frame really is, as well as it can be known.

        A frame carrying a genuine WCS — a CD matrix from a plate solve — is
        taken at its word when the stack has nothing to check it against, and
        is still refined when it has: two solvers on two machines agree to
        well under a pixel in *scale and rotation* and can still disagree on
        the absolute frame by more than one, and a stack cares only about the
        relative answer.

        A frame carrying only a program's own rotation keyword is never taken
        at its word. Those were measured, on real frames, to be several
        degrees out — see `imaging.align` — and several degrees is hundreds of
        pixels at the corner of a wide field.
        """
        found = stars.detect(frame)[:ALIGN_STARS]
        reference = stack.reference_near(guess, ALIGN_STARS)

        if len(reference) < align.MIN_MATCHES:
            if guess.source == "derived":
                # Nothing to check it against and nothing worth trusting. It
                # still goes in — it is the frame that will anchor the stack
                # and give everything after it a reference — but the log says
                # plainly what the stack's astrometry is resting on.
                detail = ("nothing in the stack to align against yet, so this "
                          "frame anchors it on its header's own guess"
                          + (f" ({guess.assumptions[0]})" if guess.assumptions
                             else ""))
            else:
                detail = "placed by the plate solution in its header"
            return guess, {"aligned": False, "source": guess.source,
                           "detail": detail, "stars": found}

        try:
            fixed, note = align.refine(guess, align.star_pixels(found),
                                       reference, reference=stack.canvas)
        except align.AlignError as exc:
            if guess.source != "derived":
                # A real WCS that would not match is still a real WCS. It is
                # far more likely that this panel is on sky the stack has
                # barely touched than that a plate solver was wrong.
                return guess, {"aligned": False, "source": guess.source,
                               "detail": f"kept the header's plate solution "
                                         f"({exc})", "stars": found}
            raise StackError(
                f"could not work out where this frame is: {exc}") from exc
        return fixed, {**note, "source": "aligned", "stars": found}

    def _maybe_save(self, job: Job, stack: LiveStack) -> None:
        key = f"{job.project}/{job.filter}"
        now = time.monotonic()
        if now - self._saved.get(key, 0.0) < SAVE_EVERY:
            return
        with contextlib.suppress(OSError):
            stack.save()
            self._saved[key] = now

    # -- sending -----------------------------------------------------------
    def _deliver(self, tile: Tile, job: Job) -> bool:
        """Hand a tile to the server, or spool it for the next time there is one."""
        if self.send is None:
            return False
        try:
            if self.send(tile, job):
                self._counts["sent"] += 1
                return True
        except Exception as exc:                  # noqa: BLE001 - offline is normal
            self._say(f"Could not send {job.path.name} to the collaboration "
                      f"({exc}); it will go with the next one", "warn")
        self._spool(tile, job)
        return False

    def _spool(self, tile: Tile, job: Job) -> None:
        try:
            self.spool.mkdir(parents=True, exist_ok=True)
            safe = tile.id.replace(":", "_").replace("/", "_")
            path = self.spool / f"{job.project}__{job.filter}__{safe}.tile"
            path.write_bytes(tile.encode())
            self._counts["spooled"] += 1
        except OSError as exc:
            self._say(f"Could not hold {job.path.name} to send later: {exc}",
                      "warn")

    def _flush_spool(self) -> int:
        """Try the held tiles again. Called whenever the queue goes quiet.

        Oldest first, and one failure stops the run: if the server is still
        down there is no point walking the whole folder to find that out
        forty times.
        """
        if self.send is None or not self.spool.is_dir():
            return 0
        sent = 0
        for path in sorted(self.spool.glob("*.tile"))[:50]:
            try:
                tile = Tile.decode(path.read_bytes())
            except (OSError, StackError):
                # A tile that cannot be read will never be sendable, and
                # leaving it there means retrying it for the rest of time.
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            project, _, rest = path.stem.partition("__")
            filter_name = rest.partition("__")[0]
            # A placeholder canvas. The tile was resampled onto the real one
            # when it was made and carries its own box, so nothing downstream
            # of here reads the job's plan — only its project, filter and
            # agent, which is what a sender needs to address it.
            job = Job(path, project, filter_name or tile.filterName,
                      Plan(0.0, 0.0, 1.0, 1.0, 1.0), night=tile.night,
                      agent=tile.agent, seconds=tile.seconds)
            try:
                if not self.send(tile, job):
                    break
            except Exception:                     # noqa: BLE001 - still offline
                break
            with contextlib.suppress(OSError):
                path.unlink()
            sent += 1
            self._counts["sent"] += 1
        return sent

    # -- reporting ---------------------------------------------------------
    def _say(self, message: str, level: str = "info") -> None:
        if self._log is None:
            return
        with contextlib.suppress(Exception):
            self._log(message, level)

    def status(self) -> dict[str, Any]:
        held = 0
        if self.spool.is_dir():
            with contextlib.suppress(OSError):
                held = sum(1 for _ in self.spool.glob("*.tile"))
        return {
            "enabled": self.enabled,
            "running": self.running,
            "root": str(self.root),
            "pending": self._queue.qsize(),
            "held": held,
            "counts": dict(self._counts),
            "last": self._last,
            "stacks": {key: stack.summary()
                       for key, stack in self.stacks().items()},
        }


# ---------------------------------------------------------------------------
# Deciding what canvas a target deserves
# ---------------------------------------------------------------------------

def plan_for_target(ra_hours: float, dec: float, width: float, height: float,
                    scale: float, max_pixels: int = 4096) -> Plan:
    """A canvas for one rig's own target, from what the program already knows.

    A solo livestack needs no coordinator and no server: the target's
    coordinates and the rig's own field are enough, and the canvas is made a
    little larger than the field so that dithering, a drifting mount and a
    meridian flip all still land inside it rather than being cropped away one
    sub at a time.
    """
    # A fifth again on each side. Dither is arcseconds and a flip is
    # arcminutes; this covers both and costs a few per cent more memory.
    margin = 1.2
    return Plan(ra=(float(ra_hours) * 15.0) % 360.0, dec=float(dec),
                width=abs(width) * margin, height=abs(height) * margin,
                scale=float(scale), maxPixels=max_pixels)


def plan_for_project(region: dict[str, Any], scales: list[float] | None = None,
                     max_pixels: int = 4096) -> Plan:
    """A canvas for a collaboration, from its region and its contributors.

    The region is a `collab.Region` payload — degrees of sky throughout, which
    is why this can take it as it stands. The scale is decided by
    `Plan.for_region`, which takes the median of what the contributing
    telescopes can do.
    """
    return Plan.for_region(
        ra=float(region.get("ra") or 0.0), dec=float(region.get("dec") or 0.0),
        width=float(region.get("width") or 1.0),
        height=float(region.get("height") or 1.0),
        scales=scales, max_pixels=max_pixels)
