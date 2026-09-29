"""The coordinator's half of the collaboration protocol.

Where `collabclient` is a telescope asking what to shoot, this is a *person*
administering the project it was asked about: enrolling rigs, drawing regions,
handing chunks of sky out, and overruling a verdict the rules got wrong.

**The admin token never reaches the browser.**  It lives in the settings file
and is used from here, which is why the Collab tab talks to this program and
this program talks to the server, rather than the page calling the server
directly.  A credential in a page is a credential in the browser's memory, in
its devtools, and in any extension that asks — and this one can rewrite every
project on the server.

Two tokens, one server.  The agent token in the same settings section belongs to
*this machine* and can only fetch tasks and report frames; this one belongs to
*you*.  They are deliberately not interchangeable: a token that sits in a
settings file next to a telescope must not be able to administer anything, and
the day one leaks is the day that distinction is the only thing that matters.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

#: Longer than the agent's poll timeout: a coordinator is sitting in front of
#: the window waiting for the answer, and a project listing with a season of
#: contributions behind it is a bigger reply than "here is your task".
TIMEOUT = 30.0


class CollabError(RuntimeError):
    """The server said no, or could not be reached.

    Carries the HTTP status where there was one, so the caller can tell "your
    token is wrong" (401) from "that server is not running" (no status) — which
    are the two failures that actually happen and want very different advice.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CollabAdmin:
    """Talks to the collaboration server as the coordinator.

    Stateless on purpose: every call reads the current settings rather than
    caching a URL or a token. Changing the server in the settings takes effect
    on the next click, with nothing to restart and no stale credential to
    explain.
    """

    def __init__(self, config: Any) -> None:
        self.config = config

    # -- settings ----------------------------------------------------------
    def _server(self) -> str:
        from .collabclient import server_url
        return server_url(self.config)

    def _token(self) -> str:
        """The credential that makes this program a *person* on the server.

        The owner's admin token where there is one; otherwise the user token
        Discord sign-in left behind. Either one is a person, and the server
        decides what that person may do; this side only has to send it.
        """
        admin = str(self.config.get("collab", "adminToken", "") or "").strip()
        if admin:
            return admin
        return str(self.config.get("collab", "userToken", "") or "").strip()

    def configured(self) -> bool:
        return bool(self._server() and self._token())

    # -- signing in --------------------------------------------------------
    def auth_status(self) -> dict[str, Any]:
        """Whether this server offers Discord sign-in."""
        return self._call("GET", "/api/v1/auth", authenticated=False)

    def begin_login(self) -> dict[str, Any]:
        """Start a sign-in: a code to poll with and a page to open."""
        return self._call("POST", "/api/v1/auth/login", authenticated=False)

    def poll_login(self, code: str) -> dict[str, Any]:
        return self._call("GET", f"/api/v1/auth/poll?code={urllib.parse.quote(code)}",
                          authenticated=False)

    def me(self) -> dict[str, Any]:
        """Who the server takes this program for, on the credential it holds."""
        return self._call("GET", "/api/v1/auth/me")

    def logout(self) -> dict[str, Any]:
        return self._call("POST", "/api/v1/auth/logout")

    # -- the wire ----------------------------------------------------------
    def _call(self, method: str, path: str,
              body: dict[str, Any] | None = None,
              authenticated: bool = True) -> dict[str, Any]:
        url = self._server()
        if not url:
            raise CollabError("no collaboration server is set")
        token = self._token()
        if authenticated and not token:
            raise CollabError("no coordinator token is set for this server")

        headers = {"Content-Type": "application/json",
                   "User-Agent": "Starfront"}
        if authenticated:
            headers["Authorization"] = f"Bearer {token}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(f"{url}{path}", data=data,
                                         method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            try:
                detail = json.loads(detail).get("detail", detail)
            except ValueError:
                pass
            raise CollabError(str(detail), exc.code) from exc
        except Exception as exc:                  # noqa: BLE001 - offline is normal
            raise CollabError(f"{url} could not be reached: {exc}") from exc
        return json.loads(text) if text else {}

    # -- is anything there -------------------------------------------------
    def health(self) -> dict[str, Any]:
        """Whether the server is up, and whether it will take orders.

        Unauthenticated deliberately: "the server is running but your token is
        wrong" and "there is no server" are different problems, and asking with
        the token could not tell them apart.
        """
        answer = self._call("GET", "/api/v1/health", authenticated=False)
        return {**answer, "server": self._server(),
                "coordinator": bool(self._token())}

    def status(self) -> dict[str, Any]:
        """Everything the Collab tab needs to draw the coordinator's half.

        Never raises. The tab has to render when the server is down — that is
        precisely the moment somebody needs to be told what is wrong, and a
        blank panel does not tell them anything.
        """
        result: dict[str, Any] = {
            "server": self._server(),
            "configured": self.configured(),
            "online": False,
            "adminConfigured": False,
            "discord": False,
            "roleRequired": False,
            "error": None,
            "agents": [],
            "projects": [],
        }
        if not self._server():
            return result
        try:
            health = self.health()
            result["online"] = bool(health.get("ok"))
            result["adminConfigured"] = bool(health.get("adminConfigured"))
            result["discord"] = bool(health.get("discord"))
            result["roleRequired"] = bool(health.get("roleRequired"))
        except CollabError as exc:
            result["error"] = str(exc)
            return result
        if not self._token():
            return result
        try:
            result["agents"] = self.agents()
            result["projects"] = self.projects()
        except CollabError as exc:
            result["error"] = str(exc)
        return result

    # -- telescopes --------------------------------------------------------
    def agents(self) -> list[dict[str, Any]]:
        return self._call("GET", "/api/v1/agents").get("agents", [])

    def add_agent(self, name: str, owner: str = "") -> dict[str, Any]:
        """Enrol a telescope. The token comes back once and is never stored.

        Shown once and not kept, because there is nowhere honest to keep it:
        this program is the coordinator's window, not the observatory the token
        is for. It gets pasted into that machine's settings and forgotten here.
        """
        return self._call("POST", "/api/v1/agents",
                          {"name": name, "owner": owner})

    # -- projects ----------------------------------------------------------
    def projects(self) -> list[dict[str, Any]]:
        return self._call("GET", "/api/v1/projects",
                          authenticated=False).get("projects", [])

    def project(self, project_id: str) -> dict[str, Any]:
        """One project with its tasks and everything contributed to it."""
        return self._call("GET", f"/api/v1/projects/{urllib.parse.quote(project_id)}",
                          authenticated=False)

    def add_project(self, name: str, region: dict[str, Any],
                    requirements: dict[str, Any] | None = None,
                    goals: dict[str, float] | None = None,
                    coordinator: str = "", notes: str = "",
                    kind: str = "mosaic") -> dict[str, Any]:
        return self._call("POST", "/api/v1/projects", {
            "name": name, "region": region, "kind": kind,
            "requirements": requirements or {},
            "goals": goals or {},
            "coordinator": coordinator, "notes": notes,
        })

    def update_project(self, project_id: str,
                       changes: dict[str, Any]) -> dict[str, Any]:
        """Change a project after it was started. Only the keys given move."""
        return self._call("PUT", f"/api/v1/projects/{urllib.parse.quote(project_id)}",
                          changes)

    # -- delegation --------------------------------------------------------
    def add_task(self, project_id: str, agent: str, region: dict[str, Any],
                 filters: list[dict[str, Any]], note: str = "") -> dict[str, Any]:
        """Hand one telescope a chunk of the project's sky."""
        return self._call(
            "POST", f"/api/v1/projects/{urllib.parse.quote(project_id)}/tasks",
            {"agent": agent, "region": region, "filters": filters, "note": note})

    # -- the ledger --------------------------------------------------------
    def set_verdict(self, row_id: str, accepted: bool,
                    reason: str = "") -> dict[str, Any]:
        """Overrule the automatic judgement on one night's data."""
        query = urllib.parse.urlencode({"accepted": str(bool(accepted)).lower(),
                                        "reason": reason})
        return self._call(
            "POST",
            f"/api/v1/contributions/{urllib.parse.quote(row_id)}/verdict?{query}")
