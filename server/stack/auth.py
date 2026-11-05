"""Who is calling the front door, and as whom the hub sees them.

Two credentials are accepted:

* `Authorization: Bearer sfg_...`, a **grant** (server/hubs.py): signed with the
  secret of the hub it names, saying "this person, on this telescope, may use
  this project". It is the only credential that may write. It is checked
  against the secret of the hub *named in the grant*, so a hub can mint grants
  only for itself, and for a route about one project the grant's `project` has
  to be that project.
* the `starfront_web` cookie of a signed-in browser, for read routes only (the
  dashboard of T409).

Whoever it is, the hub-side world knows them only by a **collab user id**, a
UUID v4 minted the first time they are seen and kept in `stack_identities`. The
Starfront user id (a Discord snowflake, or a telescope's id) never leaves this
module: every table the hub can read, and every response, is keyed by the
collab id.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Request

from ..hubs import _unb64, verify_grant


def collab_identity(store: Any, sf_id: str, name: str = "") -> str:
    """The collab user id of a Starfront id (a Discord snowflake, or a
    telescope's id when it has no owner), minting one on first sight. A grant's
    `sub` is this id, as is every member, rig and frame the hub can read."""
    row = store._one("SELECT collab_user_id FROM stack_identities WHERE user_id = ?", sf_id)
    if row is not None:
        return row["collab_user_id"]
    made = str(uuid.uuid4())
    store._run("INSERT OR IGNORE INTO stack_identities (user_id, collab_user_id,"
               " display_name) VALUES (?, ?, ?)", sf_id, made, name[:80])
    return store._one("SELECT collab_user_id FROM stack_identities WHERE user_id = ?",
                      sf_id)["collab_user_id"]
from ..files import COOKIE
from .envelope import ApiError, ok, validation_failed  # noqa: F401  (re-exported for routes)


@dataclass
class Caller:
    """A request's identity. `user_id` is the collab id (safe to show)."""
    user_id: str
    kind: str                      # "grant" or "cookie"
    hub: str = ""                  # the hub whose grant it is ("" for a cookie)
    project: str = ""              # the project the grant names
    name: str = ""                 # the grant's display name (not shown to others)


def now_iso() -> str:
    import time
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


class Auth:
    def __init__(self, store: Any, web_user: Callable[[str], dict[str, Any] | None] | None = None) -> None:
        self.store = store
        self.web_user = web_user or store.user_by_web_token

    # -- the credential ----------------------------------------------------
    def grant(self, token: str) -> dict[str, Any]:
        """The verified claims of a grant, or 401 `token_invalid`."""
        bad = ApiError("token_invalid", "That grant is unknown, expired or has been altered.")
        if not token.startswith("sfg_"):
            raise bad
        try:
            unverified = json.loads(_unb64(token[4:].partition(".")[0]))
            hub = self.store.hub(str(unverified.get("hub") or ""))
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise bad
        if hub is None:
            raise bad
        claims = verify_grant(token, hub["grant_secret"])
        if claims is None or claims.get("hub") != hub["id"] or not claims.get("sub"):
            raise bad
        return claims

    def caller(self, request: Request, *, write: bool = False,
               required: bool = True) -> Caller | None:
        """Who is asking. `write` routes accept a grant only; with `required`
        False an absent credential is an anonymous caller (None), though a bad
        one is still refused."""
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer ") and header[7:].strip():
            claims = self.grant(header[7:].strip())
            sub = str(claims["sub"])
            # A grant names its holder by collab user id, which is what the hub
            # reads too. A grant minted before that carried the Starfront id; it
            # is mapped as before until it expires (a day).
            known = self.store._one("SELECT 1 AS x FROM stack_identities WHERE collab_user_id = ?", sub)
            user_id = sub if known is not None else self.identity(sub, str(claims.get("name") or ""))
            return Caller(user_id, "grant",
                          hub=str(claims["hub"]), project=str(claims.get("project") or ""),
                          name=str(claims.get("name") or ""))
        cookie = request.cookies.get(COOKIE, "")
        if cookie:
            user = self.web_user(cookie)
            if user is not None:
                if write:
                    raise ApiError("not_authenticated",
                                   "Changes need a project grant; a browser session can only read.")
                return Caller(self.identity(str(user["id"]), str(user.get("name") or "")), "cookie",
                              name=str(user.get("name") or ""))
            if required:
                raise ApiError("session_expired", "That session is unknown or has expired.")
        if required:
            raise ApiError("not_authenticated", "Send a project grant as a Bearer token.")
        return None

    # -- who am I ----------------------------------------------------------
    def mount(self, router: APIRouter) -> None:
        @router.get("/auth/me")
        async def me(request: Request) -> dict[str, Any]:
            """The caller as the hub-side world knows them: the collab user id
            and the name they signed in with, never the Discord id. A browser
            without a session is 401 `not_authenticated`."""
            who = self.caller(request)
            return ok(user={"user_id": who.user_id, "display_name": who.name}, auth="session" if who.kind == "cookie" else "token")

    # -- the hub-side identity ---------------------------------------------
    def identity(self, sf_id: str, name: str = "") -> str:
        """The collab user id of a Starfront id, minting one on first sight."""
        return collab_identity(self.store, sf_id, name)

    # -- projects ----------------------------------------------------------
    def project(self, ref: str) -> dict[str, Any]:
        """The stack project named by an id or a slug, or 400 `not_found`."""
        row = self.store.stack_project(ref) if ref.startswith("prj_") else self.store._stack_row(
            self.store._one("SELECT * FROM stack_projects WHERE slug = ?", ref))
        if row is None:
            raise ApiError("not_found", "No such project.")
        return row

    def scoped(self, caller: Caller, row: dict[str, Any]) -> None:
        """A grant is for one project on one hub: 401 `token_scope` otherwise."""
        if caller.kind == "grant" and (caller.project != row["project_id"]
                                       or row["hub"] != caller.hub):
            raise ApiError("token_scope", "That grant is for a different project.")


async def json_body(request: Request) -> dict[str, Any]:
    """The request's JSON object, or 400 `invalid_request`."""
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        raise ApiError("invalid_request", "The body is not JSON.")
    if not isinstance(body, dict):
        raise ApiError("invalid_request", "The body must be a JSON object.")
    return body
