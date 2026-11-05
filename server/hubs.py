"""The hub link: a LAN stacking hub registers with this server and keeps in touch.

A *hub* is a `collab-hub` running on somebody's own network. It cannot be
reached from here, so it always makes the calls: it registers once (by its
owner, from the desktop), then says it is alive every minute (`heartbeat`) and
asks what has changed (`pull`). This module is that conversation and the
credentials in it; the project binding, the frame relay and the front door
are other modules.

Two secrets are made at registration and shown once:

* the **hub token** (`sfh_` + 64 hex), which the hub presents on every call.
  Only its SHA-256 is kept, so a copy of the database cannot be used to speak
  as a hub. (Telescope and person tokens are still kept in plain text; this is
  deliberately stricter.)
* the **grant secret** (32 random bytes, base64url), kept so the server can
  sign *grants*: short-lived `sfg_` bearer tokens that say "this person, on
  this telescope, may use this project on this hub". The hub holds the same
  secret and verifies a grant offline, so a hub behind NAT keeps serving its
  LAN when this server is unreachable. The secret is per hub, so a
  compromised hub can forge grants only for itself.

Grant format: `sfg_` + base64url(JSON) + `.` + base64url(HMAC-SHA256(key, the
base64url JSON text)), where the key is the 32 bytes the grant secret decodes
to. The JSON is `{"v":1,"hub","sub","name","agent","project","iat","exp"}`
with `exp = iat + 86400`. `sub` is the holder's collab user id
(server/stack/auth.py `collab_identity`), never the Starfront id: the hub keys
members by it. Stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Request

#: How long a grant lives, and how far either end of that is forgiven for
#: clocks that disagree.
GRANT_SECONDS = 86400
GRANT_LEEWAY = 300
#: The longest a pull waits for news, and how often it looks meanwhile.
PULL_SECONDS = 20.0
PULL_TICK = 0.25
#: The most changes one pull returns; the hub asks again from the last `seq`.
PULL_LIMIT = 500

_URL = re.compile(r"^https?://[^\s/]+[^\s]*$", re.I)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_token(token: str) -> str:
    """What is stored of a hub token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _key(secret: str) -> bytes:
    return _unb64(secret)


def mint_grant(hub: dict[str, Any], sub: str, name: str, agent: str,
               project: str, now: float | None = None) -> str:
    """A grant for `sub` (a user id) on `hub` (the stored hub, or any dict with
    `id` and `grant_secret`), good for a day from `now`."""
    iat = int(time.time() if now is None else now)
    body = {"v": 1, "hub": hub["id"], "sub": sub, "name": name, "agent": agent,
            "project": project, "iat": iat, "exp": iat + GRANT_SECONDS}
    payload = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    mac = hmac.new(_key(hub["grant_secret"]), payload.encode("ascii"), hashlib.sha256)
    return f"sfg_{payload}.{_b64(mac.digest())}"


def verify_grant(token: str, secret: str, now: float | None = None,
                 leeway: int = GRANT_LEEWAY) -> dict[str, Any] | None:
    """The grant's claims when `token` was signed under `secret` and is within
    its lifetime (give or take `leeway` seconds), else None. Never raises on
    a malformed token."""
    try:
        if not token.startswith("sfg_"):
            return None
        payload, _, signature = token[4:].partition(".")
        if not payload or not signature:
            return None
        want = hmac.new(_key(secret), payload.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(want, _unb64(signature)):
            return None
        claims = json.loads(_unb64(payload))
        if not isinstance(claims, dict) or claims.get("v") != 1:
            return None
        at = time.time() if now is None else now
        iat, exp = float(claims["iat"]), float(claims["exp"])
        if at > exp + leeway or at < iat - leeway:
            return None
        return claims
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None


def _public(hub: dict[str, Any]) -> dict[str, Any]:
    """A hub as its owner sees it: never the token or the secret."""
    return {"id": hub["id"], "name": hub["name"], "owner": hub["owner"],
            "lanUrl": hub["lan_url"], "created": hub["created"],
            "seen": hub["seen"], "status": hub["status"],
            "cursor": hub["cursor"], "allowedCoordinators": hub["coordinators"]}


class Hubs:
    """The routes, bound to the server's store and its idea of who is asking."""

    def __init__(self, app: FastAPI, store: Any,
                 person: Callable[[str], dict[str, Any] | None],
                 may_start: Callable[[dict[str, Any]], bool]) -> None:
        self.app = app
        self.store = store
        self.person = person
        self.may_start = may_start
        self._routes()

    # -- who is asking ---------------------------------------------------
    @staticmethod
    def _token(request: Request) -> str:
        header = request.headers.get("authorization", "")
        return header[7:].strip() if header.lower().startswith("bearer ") else ""

    def owner(self, request: Request) -> dict[str, Any]:
        """A person (not a telescope), or 401."""
        who = self.person(self._token(request))
        if who is None:
            raise HTTPException(status_code=401, detail="not signed in")
        return who

    def hub_as_owner(self, request: Request, hub_id: str) -> dict[str, Any]:
        who = self.owner(request)
        hub = self.store.hub(hub_id)
        if hub is None or hub["owner"] != who["id"]:
            # One answer for "no such hub" and "not yours": ids are not a secret
            # worth confirming to a stranger.
            raise HTTPException(status_code=404, detail="no such hub of yours")
        return hub

    def hub_as_itself(self, request: Request, hub_id: str) -> dict[str, Any]:
        """The hub, when the request carries its token, else 401."""
        token = self._token(request)
        hub = self.store.hub_by_token(hash_token(token)) if token.startswith("sfh_") else None
        if hub is None or not secrets.compare_digest(hub["id"], hub_id):
            raise HTTPException(status_code=401, detail="unknown hub token")
        return hub

    # -- the routes --------------------------------------------------------
    def _routes(self) -> None:
        app = self.app

        @app.post("/api/v1/hubs")
        def register(request: Request, body: dict[str, Any]) -> dict[str, Any]:
            who = self.owner(request)
            if not self.may_start(who):
                raise HTTPException(status_code=403,
                                    detail="you may not register a hub")
            name = str(body.get("name") or "").strip()
            lan = str(body.get("lanUrl") or "").strip()
            if not name or len(name) > 80:
                raise HTTPException(status_code=400,
                                    detail="a hub needs a name of up to 80 characters")
            if lan and (len(lan) > 200 or not _URL.match(lan)):
                raise HTTPException(status_code=400,
                                    detail="lanUrl must be an http:// or https:// address")
            hub_id = "hub_" + secrets.token_hex(8)
            token = "sfh_" + secrets.token_hex(32)
            secret = _b64(secrets.token_bytes(32))
            self.store.add_hub(hub_id, who["id"], name, hash_token(token), secret, lan)
            return {"hubId": hub_id, "hubToken": token, "grantSecret": secret}

        @app.get("/api/v1/hubs")
        def mine(request: Request) -> dict[str, Any]:
            who = self.owner(request)
            return {"hubs": [_public(h) for h in self.store.hubs_of(who["id"])]}

        @app.delete("/api/v1/hubs/{hub_id}")
        def revoke(request: Request, hub_id: str) -> dict[str, Any]:
            hub = self.hub_as_owner(request, hub_id)
            self.store.delete_hub(hub["id"])
            return {"ok": True}

        @app.put("/api/v1/hubs/{hub_id}/coordinators")
        def coordinators(request: Request, hub_id: str, body: dict[str, Any]) -> dict[str, Any]:
            hub = self.hub_as_owner(request, hub_id)
            listed = body.get("coordinators")
            if not isinstance(listed, list) or len(listed) > 100 or not all(
                    isinstance(c, str) and 0 < len(c) <= 64 for c in listed):
                raise HTTPException(status_code=400,
                                    detail="coordinators must be a list of up to 100 user ids")
            clean = [c for c in dict.fromkeys(listed) if c != hub["owner"]]
            self.store.set_hub_coordinators(hub["id"], clean)
            return {"allowedCoordinators": clean}

        @app.post("/api/v1/hubs/{hub_id}/heartbeat")
        def heartbeat(request: Request, hub_id: str, body: dict[str, Any]) -> dict[str, Any]:
            hub = self.hub_as_itself(request, hub_id)
            self.store.hub_heard(hub["id"], status=body)
            return {"ok": True, "time": time.time()}

        # A plain `def`, so FastAPI runs it in its threadpool and the sleeping
        # below parks one worker thread, never the event loop. A hub holds one
        # such thread for up to 20 s at a time; an asyncio wait would need the
        # store (blocking sqlite) called off-loop anyway, for no gain here.
        @app.get("/api/v1/hubs/{hub_id}/pull")
        def pull(request: Request, hub_id: str, since: int = Query(0, ge=0),
                 wait: float = Query(PULL_SECONDS, ge=0, le=PULL_SECONDS)) -> dict[str, Any]:
            hub = self.hub_as_itself(request, hub_id)
            self.store.hub_heard(hub["id"], cursor=since)
            deadline = time.monotonic() + wait
            while True:
                rows = self.store.changes_since(hub["id"], since, PULL_LIMIT)
                if rows or time.monotonic() >= deadline:
                    break
                time.sleep(PULL_TICK)
            return {"changes": rows, "seq": rows[-1]["seq"] if rows else since}
