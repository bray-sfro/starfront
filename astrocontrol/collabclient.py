"""Picking imaging tasks up from the collaboration server.

The observatory's half of the protocol: say hello, ask what to shoot, and hand
back what was captured.  Three calls.

**Nothing here can stop a night.**  The server is a source of suggestions, not
a dependency: if it is unreachable, the last task it gave is still on the plan
and the run carries on exactly as it would have.  A rig at a dark site behind a
domestic connection has to work when the connection does not, and a collab that
could take a night away by going down would be worse than no collab.

**Nothing here starts imaging on its own either.**  A task arrives *offered*
and stays that way until somebody accepts it.  An assignment that silently
rewrote what a mount did tonight would be the software going rogue, however
well meant — so this fetches, reports and waits to be told.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from . import collab
from .capture import CaptureService

#: Short: this is a poll of a few hundred bytes, and a request that hangs is a
#: thread that hangs.
TIMEOUT = 15.0

#: Never ask more often than this however the settings are written.
MIN_POLL = 60.0


def has_rotator(rig: Any) -> bool:
    """Whether this telescope can turn its camera at all.

    Asked of the equipment record as well as of what is connected, because the
    question comes up in the afternoon with everything switched off — a rotator
    that is configured but not yet powered is still a rotator. Without one the
    camera sits at whatever angle it is in the focuser, every panel of a mosaic
    is shot there, and nothing can correct the frames between panels; a target
    built on the assumption that it could would have overlaps that quietly
    decay with declination.
    """
    device = rig.manager.get("rotator")
    if device is not None and device.connected:
        return True
    try:
        return "rotator" in (rig.store.rig(rig.id).get("devices") or {})
    except Exception:                             # noqa: BLE001 - not knowing is "no"
        return False


def server_url(config: Any) -> str:
    """The collaboration server: what was typed in Advanced, else the built-in."""
    url = str(config.get("collab", "serverUrl", "") or "").strip().rstrip("/")
    return url or collab.DEFAULT_SERVER


class CollabClient:
    """Talks to the collaboration server on a timer, and never blocks a night."""

    def __init__(self, rigs: Any, config: Any, targets: Any = None,
                 log: Any = None) -> None:
        self.rigs = rigs
        self.config = config
        # Where the per-night logs live, for saying what this rig actually
        # achieves rather than what it claims. Optional, so a client can be
        # built without one.
        self.targets = targets
        self._log = log
        #: How many hours a night this rig gives collaborations, worked out by
        #: the program from its plan (the span pinned on a collaboration's
        #: entry). Set by whoever builds both; None means nobody has said.
        self.hours_hint: Any = None
        #: Called after every successful poll, on the polling thread, with the
        #: list of tasks. The program hangs its "bring the plan into step"
        #: on this: the server deals each night's panels afresh, and a deal
        #: that sat in the client until somebody pressed a button would
        #: leave the plan shooting last night's list.
        self.on_poll: Any = None
        #: Where this telescope points and what it is doing, for the others
        #: to see: a callable returning {ra, dec, state, target, project}, or
        #: None. Set by the program; sent with every check-in when the
        #: `sharePosition` setting is on.
        self.presence_hint: Any = None

        self._thread: threading.Thread | None = None
        # Everybody else, as the server last told us.
        self._presence: dict[str, Any] = {}
        self._agent_id: str = ""
        self._clock_skew: float | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

        self._task: dict[str, Any] | None = None
        #: Every chunk this rig holds. A rig that has joined three
        #: collaborations has three of them to plan a night around.
        self._tasks: list[dict[str, Any]] = []
        self._requirements: dict[str, Any] = {}
        self._by_project: dict[str, Any] = {}
        self._open: list[dict[str, Any]] = []
        self._error: str | None = None
        self._checked: float = 0.0
        self._reached: float = 0.0
        self._server: dict[str, Any] = {}
        self._compatibility: dict[str, Any] | None = None

    # -- settings ----------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        return self.config.section("collab")

    def server_url(self) -> str:
        """The server this rig talks to: the built-in one unless overridden."""
        return server_url(self.config)

    def _ready(self) -> tuple[str, str] | None:
        settings = self.settings()
        if not settings.get("enabled", False):
            return None
        url = self.server_url()
        token = str(settings.get("token") or "").strip()
        if not url or not token:
            return None
        return url, token

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="collab")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            wait = MIN_POLL
            try:
                if self._ready():
                    self.poll()
                    wait = max(MIN_POLL,
                               float(self.settings().get("pollMinutes") or 10) * 60.0)
            except Exception as exc:              # noqa: BLE001 - never fatal
                with self._lock:
                    self._error = str(exc)
            self._stop.wait(wait)

    # -- the wire ----------------------------------------------------------
    def _call(self, method: str, path: str,
              body: dict[str, Any] | None = None) -> dict[str, Any]:
        ready = self._ready()
        if ready is None:
            raise RuntimeError("the collaboration server is not configured")
        url, token = ready
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{url}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "User-Agent": "Starfront"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            try:
                detail = json.loads(detail).get("detail", detail)
            except ValueError:
                pass
            raise RuntimeError(f"{exc.code}: {detail}") from exc
        except Exception as exc:                  # noqa: BLE001 - offline is normal
            raise RuntimeError(str(exc)) from exc
        return json.loads(text) if text else {}

    # -- what this rig is --------------------------------------------------
    def profile(self) -> dict[str, Any]:
        """What the server needs to know to decide whether this rig can help.

        Assembled from what the program already knows: the optics, the camera,
        the filters, and what this rig actually achieves — measured rather than
        claimed, because the point of the pre-flight check is to be right.
        """
        rig = self.rigs.master
        settings = self.settings()
        optics = rig.config.section("optics")
        camera = rig.manager.get("camera")

        pixel = optics.get("pixelSize")
        width = optics.get("sensorWidth")
        height = optics.get("sensorHeight")
        # One-shot colour: known for certain from a connected camera's Bayer
        # matrix, and remembered so that it is still known in the afternoon
        # with everything off. Until then, whatever was ticked in Equipment.
        colour = bool(rig.config.get("camera", "colour", False))
        if camera is not None and camera.connected and camera.sensor_width:
            pixel = camera.pixel_size_um or pixel
            width = camera.sensor_width or width
            height = camera.sensor_height or height
            seen = bool(getattr(camera, "bayer_pattern", None))
            if seen != colour:
                colour = seen
                with _quiet():
                    rig.config.update("camera", {"colour": colour})

        # Filter names come from the wheel where there is one, overlaid with
        # what was typed in — the same list everything else in the program uses.
        names: list[str] = []
        wheel = rig.manager.get("filterwheel")
        if wheel is not None and wheel.connected:
            names = [str(name).strip() for name in (wheel.names or [])
                     if str(name).strip()]
        if not names:
            names = [str(name).strip()
                     for name in (rig.config.get("camera", "filterNames", []) or [])
                     if str(name).strip()]
        bandpasses = rig.config.get("camera", "filterBandpass", {}) or {}

        return collab.RigProfile(
            name=rig.name,
            focalLength=optics.get("focalLength"),
            pixelSize=pixel,
            sensorWidth=width,
            sensorHeight=height,
            binning=int(rig.config.get("camera", "binning", 1) or 1),
            filters={name: bandpasses.get(name) for name in names},
            colour=colour,
            typicalHfr=self._typical_hfr(),
            typicalGuideRms=self._typical_guiding(),
            # The sub length each filter is shot at, which is what the darks
            # are built for and what any share of a project is dealt at.
            exposures=self._default_exposures(),
            # Fixed at the camera's own angle when there is nothing to turn
            # it with; None when a rotator can put it wherever a project wants.
            rotation=(None if has_rotator(rig)
                      else float(optics.get("rotation") or 0.0)),
            # What this rig is prepared to give, which is a different question
            # from what it is capable of, and the one that decides how much work
            # it is fair to hand it.
            # Nobody types this any more: it is read off the plan, from the
            # time pinned on the collaboration's entry there. A setting is
            # still honoured for anybody who set one before.
            hoursPerNight=(float(settings.get("hoursPerNight") or 0.0)
                           or self._hours_from_plan() or None),
            windowFrom=str(settings.get("fromClock") or ""),
            windowTo=str(settings.get("toClock") or ""),
        ).payload()

    def _default_exposures(self) -> dict[str, float]:
        from . import calibrating
        try:
            return calibrating.default_exposures(self.rigs.master, self.config)
        except Exception:                          # noqa: BLE001 - never fatal
            return {}

    def _hours_from_plan(self) -> float | None:
        if self.hours_hint is None:
            return None
        with _quiet():
            value = float(self.hours_hint() or 0.0)
            return value if value > 0 else None
        return None

    def _typical_hfr(self) -> float | None:
        """Median star size this rig actually achieves, in arcseconds.

        From the per-night logs the sequencer already keeps — every night it
        has ever run, rather than whatever is in memory now, so a rig that has
        been switched off since last week still knows what it is capable of.

        **Converted to arcseconds here.** The logs hold HFR in pixels, and a
        project's limit is in arcseconds because that is the only unit meaning
        the same thing on two telescopes. Comparing the stored number directly
        against a project's limit would call a 3"/px rig superb.
        """
        scale = self._scale()
        if scale is None or self.targets is None:
            return None
        nightly: list[float] = []
        try:
            for target in self.targets.listing():
                for night in ((target.get("integration") or {}).get("log") or []):
                    count = night.get("hfrCount") or 0
                    if count:
                        nightly.append((night["hfrSum"] / count) * scale)
        except Exception:                         # noqa: BLE001 - best effort
            return None
        if not nightly:
            return None
        nightly.sort()
        # A real median, both middle values averaged on an even count. It was
        # `nightly[len // 2]`, which for two nights returns the worse of them —
        # defensible as a conservative claim, but it is not what "median" means
        # and a number that claims to be one should be one.
        middle = len(nightly) // 2
        value = (nightly[middle] if len(nightly) % 2
                 else (nightly[middle - 1] + nightly[middle]) / 2.0)
        return round(value, 2)

    def _typical_guiding(self) -> float | None:
        guider = self.rigs.master.manager.get("guider")
        if guider is None or not guider.connected:
            return None
        try:
            status = guider.status()
        except Exception:                         # noqa: BLE001
            return None
        value = status.get("rmsTotal")
        return round(float(value), 2) if value else None

    def _scale(self) -> float | None:
        optics = self.rigs.master.config.section("optics")
        focal = optics.get("focalLength")
        pixel = optics.get("pixelSize")
        if not focal or not pixel:
            return None
        binning = int(self.rigs.master.config.get("camera", "binning", 1) or 1)
        return 206.265 * float(pixel) * max(1, binning) / float(focal)

    # -- the three calls ---------------------------------------------------
    def hello(self) -> dict[str, Any]:
        body: dict[str, Any] = {"protocol": collab.PROTOCOL,
                                "profile": self.profile()}
        if self.presence_hint is not None and self.settings().get("sharePosition", True):
            with _quiet():
                said = self.presence_hint()
                if said:
                    body["presence"] = said
        answer = self._call("POST", "/api/v1/agent/hello", body)
        with self._lock:
            self._server = answer
            self._agent_id = str(answer.get("agent") or "")
            self._reached = time.time()
            self._error = None
            # How far this PC's clock is from the server's. A wrong clock
            # puts every timestamp and every plan time out by the same
            # amount, and nothing else in the program can tell.
            try:
                self._clock_skew = time.time() - float(answer.get("serverTime"))
            except (TypeError, ValueError):
                self._clock_skew = None
        return answer

    def presence(self) -> dict[str, Any]:
        """Who else is on the sky, as the server last said. Never fails."""
        try:
            answer = self._call("GET", "/api/v1/presence")
        except RuntimeError:
            # An older server has no such endpoint; nothing to show is fine.
            return {}
        with self._lock:
            self._presence = answer
        return answer

    def poll(self) -> dict[str, Any] | None:
        """Ask what to shoot. The task is held; nothing is acted on."""
        self.hello()
        # Which night this rig is in, by its own reckoning. The server deals
        # a rig's panels once per night and holds them, and this is how it
        # knows when the night has turned.
        night = CaptureService.night_name()
        answer = self._call("GET", f"/api/v1/agent/task?night={night}")
        task = answer.get("task")
        # Older servers answer with one task and no list; one task is still a
        # list of one, so the rest of the program need not know the difference.
        tasks = answer.get("tasks")
        if tasks is None:
            tasks = [task] if task else []
        try:
            listing = self._call("GET", "/api/v1/agent/projects")
            open_projects = listing.get("projects") or []
        except RuntimeError:
            # An older server has no such endpoint. Not being able to browse is
            # not a reason to fail a poll that otherwise worked.
            open_projects = []
        self.presence()
        with self._lock:
            previous = (self._task or {}).get("id"), (self._task or {}).get("version")
            self._task = task
            self._tasks = tasks
            self._requirements = answer.get("requirements") or {}
            self._by_project = answer.get("requirementsByProject") or {}
            self._open = open_projects
            self._checked = time.time()
            self._error = None
        if task and ((task.get("id"), task.get("version")) != previous):
            self._say(f"Collaboration: a task for {task.get('projectName') or 'a project'}"
                      f" — {task.get('state')}")
        self._recheck()
        # Every poll also sends what has been shot and not yet reported. Best
        # effort, after the task fetch so a server that is down costs nothing
        # more than the poll already did.
        with _quiet():
            self.report_pending()
        if self.on_poll is not None:
            with _quiet():
                self.on_poll(list(tasks))
        return task

    def report_pending(self) -> int:
        """Send every panel that has been shot on a collaboration and not yet
        reported, and hear the verdicts.

        Per panel, because depth is per point on the sky: a mosaic's panels are
        different sky, and the server builds its map of who has covered what
        from each panel's own footprint. Marked reported only for rows the
        server says it recorded — a night the server did not take is sent again
        next poll, and nothing is lost by it having been down.
        """
        if self.targets is None:
            return 0
        scale = self._scale()
        bandpasses = self.rigs.master.config.get("camera", "filterBandpass", {}) or {}
        focal = self.rigs.master.config.get("optics", "focalLength", None)
        colour = bool(self.rigs.master.config.get("camera", "colour", False))
        sent = 0
        for target in self.targets.listing():
            stamp = target.get("collab") or {}
            if not stamp.get("task"):
                continue
            pending = self.targets.unreported_panels(target["id"])
            if not pending:
                continue
            by_index = {int(p.get("index") or 0): p for p in (target.get("panels") or [])}
            batch = []
            for row in pending:
                panel = by_index.get(row["panel"])
                footprint = None
                if panel is not None:
                    footprint = collab.Region(
                        ra=float(panel["ra"]) * 15.0, dec=float(panel["dec"]),
                        width=float(target.get("panelWidth") or 0.0),
                        height=float(target.get("panelHeight") or 0.0),
                        rotation=float(panel.get("rotation") or 0.0)).payload()
                elif not target.get("panels"):
                    footprint = collab.Region(
                        ra=float(target["ra"]) * 15.0, dec=float(target["dec"]),
                        width=float(target.get("panelWidth") or 0.0),
                        height=float(target.get("panelHeight") or 0.0),
                        rotation=float(target.get("rotation") or 0.0)).payload()
                frames = int(row["frames"])
                seconds = float(row["seconds"])
                hfr_px = (row["hfrSum"] / row["hfrCount"]) if row.get("hfrCount") else None
                batch.append({
                    "project": stamp.get("project"), "task": stamp["task"],
                    "night": row["night"], "panel": str(row["panel"]),
                    "filterName": row["filter"], "frames": frames,
                    "seconds": seconds,
                    "exposure": (seconds / frames) if frames else 0.0,
                    "footprint": footprint, "scale": scale,
                    "focalLength": float(focal) if focal else None,
                    "colour": colour,
                    # Arcseconds, never pixels: the only unit that means the
                    # same thing on two telescopes.
                    "hfr": (hfr_px * scale) if (hfr_px is not None and scale) else None,
                    "guideRms": (row["rmsSum"] / row["rmsCount"]) if row.get("rmsCount") else None,
                    "bandpass": bandpasses.get(row["filter"]),
                })
            answer = self.report(batch)
            for row, recorded in zip(pending, answer.get("recorded") or []):
                if recorded.get("id"):
                    self.targets.mark_reported(target["id"], row["night"],
                                               row["panel"], row["filter"])
                    sent += 1
        return sent

    def open_projects(self) -> list[dict[str, Any]]:
        """Collaborations that are open, with whether this rig qualifies."""
        answer = self._call("GET", "/api/v1/agent/projects")
        with self._lock:
            self._open = answer.get("projects") or []
            self._reached = time.time()
            self._error = None
        return self._open

    def join(self, project_id: str, hours: float = 0.0,
             exposure: float = 0.0) -> dict[str, Any]:
        """Sign up for a share of a project.

        Nobody has to deal it out: a rig that meets the requirements asks, and
        the server hands back the part of the sky least spoken for. The server
        checks the requirements again on its side, so a refusal here is a real
        refusal rather than a client being polite.

        The share is dealt at this rig's own default exposure for each
        filter - the length its darks are built for - so every light it
        shoots for the project can be calibrated. An exposure named here
        only stands in for a filter with no default of its own.
        """
        answer = self._call(
            "POST", f"/api/v1/agent/projects/{project_id}/join",
            {"hours": hours, "exposure": exposure,
             "exposures": self._default_exposures()})
        task = answer.get("task")
        if task:
            with self._lock:
                self._tasks = [entry for entry in self._tasks
                               if entry.get("id") != task.get("id")] + [task]
                self._task = self._task or task
            self._say(f"Collaboration: joined {task.get('projectName')}",
                      "success")
        return answer

    def tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._tasks)

    def respond(self, state: str) -> dict[str, Any]:
        """Accept, decline or finish the task in hand."""
        with self._lock:
            task = dict(self._task) if self._task else None
        if task is None:
            raise RuntimeError("there is no task to respond to")
        answer = self._call("POST", f"/api/v1/agent/task/{task['id']}",
                            {"state": state})
        with self._lock:
            self._task = answer.get("task")
        self._say(f"Collaboration: task {state}")
        return answer

    def report(self, contributions: list[dict[str, Any]]) -> dict[str, Any]:
        """Hand back a night. Best effort: a failure here loses no data.

        The contribution is built from the night log, which stays on disk — so
        a report that cannot be sent tonight can be sent tomorrow, and nothing
        is lost by the server being down.
        """
        if not contributions:
            return {"recorded": []}
        answer = self._call("POST", "/api/v1/agent/report",
                            {"contributions": contributions})
        for row in answer.get("recorded", []):
            verdict = row.get("verdict") or {}
            self._say("Collaboration: "
                      + ("accepted" if row.get("accepted") else "rejected")
                      + f" — {verdict.get('summary', '')}",
                      "success" if row.get("accepted") else "warn")
        return answer

    # -- reporting ---------------------------------------------------------
    def _recheck(self) -> None:
        """Can this rig actually do what the task asks?"""
        try:
            wants = collab.Requirements.read(self._requirements)
            profile = collab.RigProfile.read(self.profile())
            with self._lock:
                self._compatibility = collab.compatibility(profile, wants)
        except Exception:                         # noqa: BLE001
            with self._lock:
                self._compatibility = None

    def _say(self, message: str, level: str = "info") -> None:
        if self._log is not None:
            with _quiet():
                self._log(message, level)

    def status(self) -> dict[str, Any]:
        settings = self.settings()
        with self._lock:
            return {
                "enabled": bool(settings.get("enabled", False)),
                "configured": bool(self._ready()),
                "running": self.running,
                "server": self.server_url(),
                "task": self._task,
                "tasks": list(self._tasks),
                "open": list(self._open),
                "requirements": self._requirements,
                "requirementsByProject": dict(self._by_project),
                "compatibility": self._compatibility,
                "checkedAt": self._checked or None,
                "reachedAt": self._reached or None,
                # Offline is a normal state for a remote rig, not a fault — so
                # it is reported next to when it was last reached rather than
                # as an error on its own.
                "online": bool(self._reached
                               and time.time() - self._reached < 900),
                "error": self._error,
                # Everybody else: who is about, and where they point.
                "agentId": self._agent_id,
                "presence": dict(self._presence),
                "clockSkew": self._clock_skew,
            }


class _quiet:
    """Logging must never be the thing that breaks a night."""

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return kind is not None
