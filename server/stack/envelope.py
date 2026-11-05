"""The Collab envelope: how every answer under `/stack/api/` is shaped.

The contract (Rapid Stacker Collab `docs/api/README.md`) fixes it: success is
`{"ok": true, ...fields}`, failure is `{"ok": false, "code", "message", ...}`,
each `code` has one HTTP status, and **nothing under `/api` is ever a 403 or a
404** (a CDN in front of the original service turns both into an HTML page, and
clients branch on `code`). The plugin is written against that, so this front
door keeps it for every answer it can give, including paths it does not have,
methods it does not allow and bodies FastAPI itself refuses.

`ERROR_STATUS` is a copy of the `ErrorCode` table in `openapi.yaml`;
`tools/check_server.py` compares it with that file whenever it is at hand.
Stdlib and FastAPI only.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

#: `components/schemas/ErrorCode` of the contract: code -> HTTP status.
ERROR_STATUS: dict[str, int] = {
    "admin_only": 401, "already_member": 409, "bad_etags": 400, "bad_origin": 400,
    "contract_mismatch": 409, "device_denied": 200, "device_expired": 200,
    "dimensions_too_large": 400, "display_name_taken": 409, "duplicate": 409,
    "email_in_use": 409, "email_not_verified": 409, "frame_state": 409, "frame_too_large": 400,
    "frame_withdrawn": 409, "gold_in_progress": 409, "grid_locked": 409, "grid_required": 409,
    "header_rejected": 400, "ingest_paused": 503, "internal_error": 500, "invalid_code": 400,
    "invalid_credentials": 401, "invalid_link": 400, "invalid_request": 400,
    "invalid_transition": 409, "job_state": 409, "last_owner": 409, "lease_held": 409,
    "lease_lost": 409, "membership_pending": 409, "membership_removed": 409,
    "mosaic_grid": 409, "mosaic_required": 409, "no_new_frames": 409, "not_authenticated": 401,
    "not_found": 400, "not_maintainer": 401, "not_member": 401, "not_owner": 401,
    "panel_assigned": 409, "policy_violation": 409, "project_deleted": 409,
    "project_not_open": 409, "purge_started": 409, "quota_exceeded": 429, "rate_limited": 429,
    "raw_download_not_allowed": 409, "raw_unavailable": 409, "session_expired": 401,
    "session_required": 401, "size_mismatch": 409, "slow_down": 429, "slug_reserved": 409,
    "slug_taken": 409, "stale_checkpoint": 409, "strip_required": 400, "terms_outdated": 409,
    "token_invalid": 401, "token_scope": 401, "unsupported_frame_type": 400,
    "upload_incomplete": 409, "validation_failed": 400, "worker_only": 401,}


class ApiError(Exception):
    """An answer that is an error: raise it anywhere in a route."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        if code not in ERROR_STATUS:
            raise ValueError(f"{code!r} is not a code of the contract")
        self.code = code
        self.message = message
        self.extra = extra

    @property
    def status(self) -> int:
        return ERROR_STATUS[self.code]


def validation_failed(*problems: tuple[str, str]) -> ApiError:
    """400 `validation_failed` with the contract's `errors` list."""
    return ApiError("validation_failed", "One or more fields are not acceptable.",
                    errors=[{"field": f, "message": m} for f, m in problems])


def reply(error: ApiError) -> JSONResponse:
    body = {"ok": False, "code": error.code, "message": error.message}
    body.update(error.extra)
    return JSONResponse(status_code=error.status, content=body)


def ok(**fields: Any) -> dict[str, Any]:
    """A success body: `ok: true` and the fields at the top level."""
    return {"ok": True, **fields}


def install(app: FastAPI) -> None:
    """Make every failure of `app` (a sub-application mounted at `/stack/api`)
    an envelope with a status other than 403 or 404."""

    @app.exception_handler(ApiError)
    async def _api(_request: Request, exc: ApiError) -> JSONResponse:
        return reply(exc)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return reply(ApiError("invalid_request",
                              "The request is not valid: " + "; ".join(
                                  str(e.get("msg", "invalid")) for e in exc.errors()[:3])))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code in (403, 404):
            return reply(ApiError("not_found", "There is nothing at that address."))
        if exc.status_code == 405:
            return reply(ApiError("invalid_request", "That method is not allowed here."))
        if exc.status_code == 401:
            return reply(ApiError("not_authenticated", str(exc.detail)))
        if exc.status_code >= 500:
            return reply(ApiError("internal_error", "The server could not do that."))
        return reply(ApiError("invalid_request", str(exc.detail)))

    @app.exception_handler(Exception)
    async def _crash(_request: Request, exc: Exception) -> JSONResponse:
        return reply(ApiError("internal_error", "The server could not do that."))
