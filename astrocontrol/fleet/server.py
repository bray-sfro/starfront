"""The dashboard's own little web server."""
from __future__ import annotations

import collections
import hmac
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from .core import ACTIONS, CommandError, Fleet

WEB = Path(__file__).resolve().parent.parent / "web"
LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}


class CommandRequest(BaseModel):
    action: str
    rig: str | None = None
    confirm: bool = False
    ra: float | None = None
    dec: float | None = None


def create_app(fleet: Fleet) -> FastAPI:
    app = FastAPI(title="Starfront fleet", docs_url=None, redoc_url=None)
    app.state.fleet = fleet
    token = str(fleet.settings.get("dashboardToken") or "")
    thumbs: collections.OrderedDict[tuple[str, str], bytes] = collections.OrderedDict()
    thumbs_lock = threading.Lock()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.client.host if request.client else ""
        local = host in LOCAL or host.startswith("127.") or host.startswith("::ffff:127.")
        if not local:
            given = request.headers.get("x-dashboard-token") or \
                request.query_params.get("token") or ""
            if not token or not hmac.compare_digest(given, token):
                return JSONResponse(status_code=403, content={
                    "detail": "The fleet dashboard is open from other PCs only with "
                              "dashboardToken (fleet.json)"})
        return await call_next(request)

    @app.get("/")
    def index():
        return FileResponse(WEB / "fleet" / "index.html",
                            headers={"Cache-Control": "no-store"})

    @app.get("/fleet.js")
    def script():
        return FileResponse(WEB / "fleet" / "fleet.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-store"})

    @app.get("/style.css")
    def style():
        return FileResponse(WEB / "style.css", media_type="text/css")

    @app.get("/favicon.ico")
    def favicon():
        icon = WEB / "favicon.ico"
        if icon.exists():
            return FileResponse(icon)
        return Response(status_code=204)

    @app.get("/api/fleet")
    def snapshot() -> dict[str, Any]:
        out = fleet.snapshot()
        out["actions"] = {name: {"confirm": spec[3], "rig": spec[2]}
                          for name, spec in ACTIONS.items()}
        out["settings"] = {k: fleet.settings.get(k) for k in (
            "pollSeconds", "staleSeconds", "offlineSeconds", "offlineAlertMinutes")}
        return out

    @app.post("/api/nodes/{node}/command")
    def command(node: str, body: CommandRequest) -> dict[str, Any]:
        try:
            return fleet.command(node, body.action, rig=body.rig, confirm=body.confirm,
                                 ra=body.ra, dec=body.dec)
        except CommandError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @app.get("/api/commands/{command_id}")
    def command_state(command_id: int) -> dict[str, Any]:
        with fleet.lock:
            record = fleet.commands.get(command_id)
            if record is None:
                raise HTTPException(status_code=404, detail="no such command")
            return dict(record)

    @app.get("/api/thumb/{node}/{image_id}")
    def thumb(node: str, image_id: str):
        target = fleet.nodes.get(node)
        if target is None:
            raise HTTPException(status_code=404, detail="no such node")
        key = (node, image_id)
        with thumbs_lock:
            data = thumbs.get(key)
        if data is None:
            try:
                reply = target.send("GET", f"/api/images/{image_id}/render.png",
                                    params={"maxDim": 360}, timeout=12.0)
            except Exception as exc:              # noqa: BLE001
                raise HTTPException(status_code=504, detail=f"{node} did not answer") from exc
            if reply.status_code != 200:
                raise HTTPException(status_code=reply.status_code, detail="no thumbnail")
            data = reply.content
            with thumbs_lock:
                thumbs[key] = data
                while len(thumbs) > 64:
                    thumbs.popitem(last=False)
        return Response(content=data, media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})

    @app.post("/api/quit")
    def quit_dashboard(request: Request) -> dict[str, Any]:
        """The Stop button (this PC only): Starfront.exe --fleet has no window."""
        host = request.client.host if request.client else ""
        if not (host in LOCAL or host.startswith("127.")):
            raise HTTPException(status_code=403, detail="only from the PC running it")
        stop = getattr(app.state, "shutdown", None)
        if stop is None:
            return {"stopping": False}
        threading.Timer(0.3, stop).start()
        return {"stopping": True}

    @app.get("/healthz")
    def health():
        return RedirectResponse("/api/fleet")

    return app
