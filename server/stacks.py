"""The community live stack: the one part of this server that holds pixels.

Everything else the server does is a ledger of who shot what. This is the
picture itself, and it exists because a collaboration whose members can watch
their frames landing in a shared image *on the same night* is a different
thing from one that finds out in March whether it worked.

The arrangement is the same pull-shaped one as the rest of the protocol.
Agents push tiles when they have them; the server folds each into a running
accumulator and can hand anybody a PNG of where it has got to. No agent ever
holds the canonical stack and no agent can corrupt one — a tile is a
contribution, and the accumulator decides what to do with it, including
declining it and saying why.

**One stack per project per filter.** Ha and OIII of the same nebula are two
pictures that happen to share a canvas, and averaging them together produces
neither. Sharing the canvas is exactly what makes them line up to the pixel
when somebody combines the channels afterwards, which is the real reason the
grid is defined centrally rather than negotiated.

**The canvas is decided once and then never moves.** A grid recomputed when a
seventh telescope joins would be a different grid, and every accumulator
built on the old one would be meaningless on the new one — silently, because
the arrays are the same shape and full of plausible numbers. So a project
that has a canvas keeps it.

Built as a router over an injected store rather than importing the
application, because the application imports this. That also means the whole
thing can be exercised without standing a server up.
"""

from __future__ import annotations

import contextlib
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import Response

from astrocontrol import collab, filters, livestack

#: How long a rendered preview is served before it is made again. A stack a
#: dozen people are watching would otherwise re-render its canvas once per
#: viewer per refresh, which is the one thing in this server capable of using
#: real processor time.
PREVIEW_SECONDS = 20.0

#: The most a single tile may be. A tile is one sub reprojected onto a canvas
#: of at most a few thousand pixels and compressed, so a few tens of megabytes
#: is already generous; past it something is wrong, and saying so beats
#: swallowing it.
MAX_TILE_BYTES = 64 * 1024 * 1024


def build(store: Any, agent_from: Callable[..., dict[str, Any]],
          data: Path) -> APIRouter:
    """The stack endpoints, over a given store and a given agent check."""
    router = APIRouter()
    root = Path(data) / "stacks"
    open_stacks: dict[str, livestack.LiveStack] = {}
    previews: dict[str, tuple[float, int, bytes]] = {}
    lock = threading.RLock()

    def key(project: str, filter_name: str) -> str:
        return f"{project}/{filters.canonical(filter_name) or 'none'}"

    def open_stack(project: str, filter_name: str,
                   plan: livestack.Plan | None = None) -> livestack.LiveStack:
        """A stack, held open between contributions.

        Reopening means reading five canvas-sized arrays off disk. With six
        observatories contributing a sub every few minutes that would be most
        of what this server does, so they stay open and are saved on the way
        out of each contribution.
        """
        name = key(project, filter_name)
        with lock:
            found = open_stacks.get(name)
            if found is not None and (plan is None
                                      or found.plan.payload() == plan.payload()):
                return found
            folder = livestack.stack_root(
                root, project, filters.canonical(filter_name) or "none")
            stack = livestack.LiveStack.open(folder, plan)
            open_stacks[name] = stack
            return stack

    def plan_for(project: dict[str, Any]) -> livestack.Plan:
        """The canvas a project stacks onto, decided once and then kept.

        Worked out from the project's own region and the scales of the
        telescopes that have joined it, then written onto the project. See the
        module docstring for why it must not be recomputed.
        """
        payload = project.get("payload") or {}
        stored = payload.get("canvas")
        if stored:
            return livestack.Plan.read(stored)

        region = payload.get("region") or {}
        scales: list[float] = []
        for task in store.tasks_in(project["id"]):
            profile = collab.RigProfile.read(
                (store.agent(task["agent"]) or {}).get("profile") or {})
            scale = profile.scale()
            if scale:
                scales.append(scale)
        plan = livestack.Plan.for_region(
            ra=float(region.get("ra") or 0.0),
            dec=float(region.get("dec") or 0.0),
            width=float(region.get("width") or 1.0),
            height=float(region.get("height") or 1.0), scales=scales)
        store.set_project(project["id"], {**payload, "canvas": plan.payload()})
        return plan

    def require_project(project_id: str, must_be_open: bool = False
                        ) -> dict[str, Any]:
        found = store.project(project_id)
        if found is None:
            raise HTTPException(status_code=404, detail="no such project")
        if must_be_open and found.get("status") != "open":
            raise HTTPException(status_code=409,
                                detail="this collaboration is closed")
        return found

    # -- the agent's side --------------------------------------------------
    @router.get("/api/v1/agent/stack/plan")
    def stack_plan(project: str = Query(..., max_length=64),
                   agent: dict[str, Any] = Depends(agent_from)) -> dict[str, Any]:
        """The canvas this project stacks onto.

        An agent asks once and reprojects everything it shoots onto exactly
        this grid. The handful of numbers *are* the definition; both sides
        build the identical WCS from them, which is why a tile resampled in a
        shed in Texas lands on the pixels the server expects without a plate
        solution ever crossing the wire.
        """
        found = require_project(project)
        store.seen(agent["id"])
        return {"plan": plan_for(found).payload(), "protocol": collab.PROTOCOL}

    @router.post("/api/v1/agent/stack/tile")
    def stack_tile(project: str = Query(..., max_length=64),
                   body: bytes = Body(..., media_type="application/octet-stream"),
                   agent: dict[str, Any] = Depends(agent_from)) -> dict[str, Any]:
        """Fold one contributed tile into the community stack.

        The body is a Starfront tile — a small binary of values, weights and a
        box on the canvas — rather than JSON, because this is the one endpoint
        that carries pixels, and base64 inside a JSON envelope would add a
        third to every sub of every night for nothing.

        The verdict comes straight back, and it is meant to be read: a
        contributor is told that their frame was scaled by 0.31 to match, or
        that it did not correlate with the stack at all, on the night, while
        there is still time to discover that their flat is for the wrong
        filter.
        """
        found = require_project(project, must_be_open=True)
        if len(body) > MAX_TILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"that tile is {len(body) // 1_000_000} MB and the "
                       f"limit is {MAX_TILE_BYTES // 1_000_000} MB")
        store.seen(agent["id"])

        try:
            tile = livestack.Tile.decode(body)
        except livestack.StackError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # A contributor does not get to say who it is. An agent's identity
        # comes from its token, exactly as it does for a night's report, so a
        # tile can never be credited to somebody else's telescope — and the
        # tile id is rewritten to match, so that two rigs that happen to name
        # a file the same way do not collide in the ledger.
        tile.agent = agent["id"]
        tile.id = f"{agent['id']}:{tile.id.split(':', 1)[-1]}"

        stack = open_stack(project, tile.filterName, plan_for(found))
        try:
            report = stack.add(tile)
        except livestack.StackError as exc:
            # Not a 500 and not a 400: the tile was well formed and the stack
            # declined it, which is a judgement about the data rather than
            # about the request, and belongs in the body where the agent can
            # log it for its operator.
            return {"added": False, "refused": True, "detail": str(exc)}

        summary = stack.summary()
        name = filters.canonical(tile.filterName) or "none"
        store.upsert_stack(project, name, stack.plan.payload(), summary)
        with lock:
            previews.pop(key(project, tile.filterName), None)
            with contextlib.suppress(OSError):
                stack.save()
        return {"added": bool(report.get("added")), "report": report,
                "stack": summary}

    # -- anybody's side ----------------------------------------------------
    @router.get("/api/v1/stacks")
    def list_stacks(project: str = Query(default="", max_length=64)
                    ) -> dict[str, Any]:
        """Every live stack, or one project's. Open to anybody, like projects.

        Watching is the point. A collaboration whose picture can only be seen
        by somebody holding a telescope token is one nobody outside it can be
        excited by — and being excited by it is how the next contributor turns
        up.
        """
        return {"stacks": store.stacks(project or None)}

    @router.get("/api/v1/stacks/{project}/{filter_name}")
    def read_stack(project: str, filter_name: str) -> dict[str, Any]:
        """What a stack holds: how many frames, from whom, how deep, how far."""
        require_project(project)
        return {"stack": open_stack(project, filter_name).summary()}

    @router.get("/api/v1/stacks/{project}/{filter_name}/preview.png")
    def stack_preview(project: str, filter_name: str,
                      width: int = Query(default=1400, ge=64, le=4096)):
        """The stack as it stands, as a PNG.

        Cached for a few seconds. The expensive part is rendering a canvas,
        and a dozen people watching the same picture refresh must not each
        cost their own render of it.
        """
        require_project(project)
        name = key(project, filter_name)
        now = time.time()
        with lock:
            cached = previews.get(name)
            if (cached is not None and now - cached[0] < PREVIEW_SECONDS
                    and cached[1] == width):
                return Response(content=cached[2], media_type="image/png")
        image, _ = open_stack(project, filter_name).preview(max_dim=width)
        with lock:
            previews[name] = (now, width, image)
        return Response(content=image, media_type="image/png",
                        headers={"Cache-Control": f"max-age={int(PREVIEW_SECONDS)}"})

    return router
