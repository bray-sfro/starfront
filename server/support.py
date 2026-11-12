"""Get help: a signed-in person's question becomes a help desk ticket.

The pages have a *Get help* form. What the person types goes to the help
desk (Zendesk) as a ticket from them, and the server adds, as a private
note only the support team sees, everything an agent would otherwise have
to ask for: who they are on Discord, their plan and how much room they
use, the project they were looking at and its frames, the last stack and
its error, and each of their telescopes with its last check-in.

Nothing is sent by itself. A ticket is made only when a person presses
Send; a failed stack or a refused upload shows on the page with the form
one click away, and does not open a ticket on its own (see
server/deploy/BILLING-SUPPORT.md for why).

Off unless `STARFRONT_SUPPORT=1` *and* the three Zendesk settings are
present. They are read from the environment or from `zendesk.env` beside
the database, never from the repository:

| | |
|---|---|
| `STARFRONT_SUPPORT` | `1` to offer the form (default off) |
| `STARFRONT_ZENDESK_SUBDOMAIN` | `example` for example.zendesk.com |
| `STARFRONT_ZENDESK_EMAIL` | the Zendesk user the API token belongs to |
| `STARFRONT_ZENDESK_TOKEN` | that user's API token |
| `STARFRONT_ZENDESK_OAUTH_TOKEN` | instead of the two above: an OAuth access token limited to writing tickets |
| `STARFRONT_ZENDESK_TAGS` | tags on every ticket (default `auto-processing,collab-server`) |
| `STARFRONT_ZENDESK_FIELDS` | optional JSON, ticket field ids: `{"discord_id": 123, "plan": 456, "project": 789}` |
| `STARFRONT_HELP_URL` | the help centre, linked beside the form (and shown alone while the form is off) |
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from server import auth

ENV_FILE = "zendesk.env"
#: Tickets one person may open: this many a day, and not two inside a minute.
PER_DAY = 5
MIN_GAP_SECONDS = 60
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s.]{2,24}$")
_SUBDOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_ID = re.compile(r"^[0-9a-f]{6,32}$")
_log = logging.getLogger("starfront.support")

#: (method, url, headers, body) -> (status, parsed JSON). Replaced in tests.
Send = Callable[[str, str, dict[str, str], bytes], tuple[int, dict[str, Any]]]


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def load_env_file(folder: Path, name: str = ENV_FILE) -> None:
    """Put a `KEY=value` file beside the database into the environment,
    without overriding what is already set (as auth.load_env_file does for
    discord.env)."""
    path = Path(folder) / name
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def http_send(method: str, url: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, data=body or None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:     # noqa: S310 - https only, see below
            raw, status = response.read(), response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise HTTPException(status_code=502, detail="the help desk could not be reached") from error
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        parsed = {}
    if isinstance(parsed, list):
        parsed = {"items": parsed}
    return status, parsed if isinstance(parsed, dict) else {}


class HelpRequest(BaseModel):
    message: str = Field(min_length=10, max_length=4000)
    subject: str = Field(default="", max_length=120)
    email: str = Field(min_length=5, max_length=254)
    #: The project the person was looking at, if any (theirs, or a collaboration).
    project: str = Field(default="", max_length=32)


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{round(seconds / 60)} min ago"
    if seconds < 2 * 86400:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def _gb(size: float) -> str:
    return f"{float(size) / 1024 ** 3:.1f} GB"


class Support:
    def __init__(self, app: FastAPI, store: Any, who: Callable[[Request], dict[str, Any] | None],
                 personal: Any = None, site: Any = None, jobs: Any = None, *,
                 enabled: bool | None = None, subdomain: str | None = None,
                 email: str | None = None, token: str | None = None,
                 help_url: str | None = None, send: Send = http_send,
                 clock: Callable[[], float] = time.time) -> None:
        self.store, self.who = store, who
        self.personal, self.site, self.jobs = personal, site, jobs
        self.send, self.clock = send, clock
        self.subdomain = (auth.setting("ZENDESK_SUBDOMAIN") if subdomain is None else subdomain).strip().lower()
        self.email = (auth.setting("ZENDESK_EMAIL") if email is None else email).strip()
        self._token = (auth.setting("ZENDESK_TOKEN") if token is None else token).strip()
        self._oauth = auth.setting("ZENDESK_OAUTH_TOKEN") if token is None else ""
        self.help_url = (auth.setting("HELP_URL") if help_url is None else help_url).strip()
        if not self.help_url.startswith("https://"):
            self.help_url = ""
        self.tags = [t.strip() for t in (auth.setting("ZENDESK_TAGS") or "auto-processing,collab-server").split(",")
                     if t.strip()][:10]
        try:
            fields = json.loads(auth.setting("ZENDESK_FIELDS") or "{}")
        except ValueError:
            fields = {}
        self.fields = {str(k): int(v) for k, v in fields.items()
                       if isinstance(v, int) and not isinstance(v, bool)} if isinstance(fields, dict) else {}
        wanted = _flag("STARFRONT_SUPPORT") if enabled is None else enabled
        self.configured = bool(_SUBDOMAIN.match(self.subdomain)
                               and (self._oauth or (self.email and self._token)))
        if wanted and not self.configured:
            _log.warning("STARFRONT_SUPPORT is on but the Zendesk settings are incomplete; the form stays off")
        self.enabled = bool(wanted and self.configured)
        self._routes(app)

    # -- what the agent needs to know ------------------------------------------
    def context(self, who: dict[str, Any], project_id: str = "") -> dict[str, Any]:
        """Everything about this person the support team would ask for."""
        now = self.clock()
        uid = str(who["id"])
        out: dict[str, Any] = {"discord": {"name": who.get("name") or "", "id": uid}}
        if self.personal is not None:
            allowed = self.personal.entitlement(who)
            out["plan"] = {"name": allowed.get("plan") or "none", "state": allowed.get("state") or "",
                           "allowed": bool(allowed.get("personal")), "why": allowed.get("why") or "",
                           "storageBytes": int(allowed.get("storageBytes") or 0),
                           "usedBytes": self.personal.used(uid)}
        project = self.store.project(project_id) if _ID.match(project_id or "") else None
        # Only what this person may see: their own personal project, or a collaboration.
        if project is not None and project.get("scope") == "personal" \
                and str(project.get("owner_id") or "") != uid:
            project = None
        if project is not None:
            out["project"] = self._project(project, uid)
        if self.site is not None:
            cards = [self.site.telescope_card(agent, now) for agent in self.store.agents_of(uid)]
            cards.sort(key=lambda c: -(c["seen"] or 0))
            out["telescopes"] = [{"name": c["name"], "id": c["id"], "online": c["online"],
                                  "lastSeen": _ago(c["ageSeconds"]), "state": c["state"],
                                  "target": c["target"]} for c in cards[:12]]
        return out

    def _project(self, project: dict[str, Any], uid: str) -> dict[str, Any]:
        pid = project["id"]
        mine = self.store.files_in(pid, uid)
        problems = []
        for row in sorted(mine, key=lambda r: -float(r.get("received") or 0)):
            if row.get("status") in ("pending", "quarantine") and len(problems) < 5:
                try:
                    reasons = json.loads(row.get("reasons") or "[]")
                except (TypeError, ValueError):
                    reasons = []
                problems.append({"name": row["name"], "kind": row.get("kind") or "",
                                 "status": row["status"],
                                 "reasons": [str(r)[:200] for r in reasons][:3]})
        out: dict[str, Any] = {
            "id": pid, "name": project["name"], "kind": project.get("scope") or "collab",
            "status": project.get("status") or "",
            "myFiles": len(mine), "myBytes": sum(int(r.get("bytes") or 0) for r in mine),
            "lights": self.store.file_status_counts(pid, uid),
            "problemFiles": problems,
            "uploadsInFlight": sum(1 for u in self.store.uploads(pid) if str(u.get("user")) == uid),
        }
        if self.jobs is not None:
            job, asked = self.jobs.read(pid), self.jobs.read_request(pid)
            out["stack"] = {"job": job.get("status"), "stale": bool(job.get("stale")),
                            "message": str(job.get("message") or "")[:300],
                            "version": job.get("version") or "",
                            "request": str(asked.get("state") or ""),
                            "requestMessage": str(asked.get("message") or "")[:300]}
        return out

    @staticmethod
    def note(ctx: dict[str, Any]) -> str:
        """The context as the private note on the ticket: plain lines an
        agent reads top to bottom."""
        d = ctx["discord"]
        lines = ["Sent from the Get help form on collab.starfront.space.", "",
                 f"Discord: {d['name']} (id {d['id']})"]
        plan = ctx.get("plan")
        if plan:
            state = f", {plan['state']}" if plan["state"] else ""
            lines.append(f"Plan: {plan['name']}{state}; storage {_gb(plan['usedBytes'])} of {_gb(plan['storageBytes'])}")
            if not plan["allowed"]:
                lines.append(f"Uploads refused: {plan['why']}")
            elif plan["storageBytes"] and plan["usedBytes"] >= plan["storageBytes"]:
                lines.append("Uploads refused: storage is full")
        project = ctx.get("project")
        if project:
            lights = ", ".join(f"{n} {status or 'unchecked'}" for status, n in sorted(project["lights"].items()))
            lines += ["", f"Project: {project['name']} (id {project['id']}, {project['kind']}, {project['status']})",
                      f"Their files in it: {project['myFiles']} ({_gb(project['myBytes'])}); lights: {lights or 'none'}"]
            if project["uploadsInFlight"]:
                lines.append(f"Uploads in flight: {project['uploadsInFlight']}")
            stack = project.get("stack")
            if stack:
                lines.append(f"Stack: job {stack['job']}{' (stale)' if stack['stale'] else ''}"
                             f"{' ' + stack['version'] if stack['version'] else ''}"
                             f"{'; request ' + stack['request'] if stack['request'] else ''}")
                for message in dict.fromkeys((stack["message"], stack["requestMessage"])):
                    if message:
                        lines.append(f"  says: {message}")
            for problem in project["problemFiles"]:
                lines.append(f"  {problem['status']}: {problem['name']} ({problem['kind']})"
                             + (": " + "; ".join(problem["reasons"]) if problem["reasons"] else ""))
        scopes = ctx.get("telescopes")
        if scopes is not None:
            lines += ["", f"Telescopes ({len(scopes)}):" if scopes else "Telescopes: none connected"]
            for scope in scopes:
                doing = " · ".join(x for x in (scope["state"], scope["target"]) if x)
                lines.append(f"  {scope['name']} (id {scope['id']}): {'online' if scope['online'] else 'offline'},"
                             f" last check-in {scope['lastSeen']}{' · ' + doing if doing else ''}")
        return "\n".join(lines)

    # -- the help desk --------------------------------------------------------------
    def _call(self, method: str, path: str, body: dict[str, Any],
              extra: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        basic = base64.b64encode(f"{self.email}/token:{self._token}".encode()).decode()
        headers = {"Authorization": f"Bearer {self._oauth}" if self._oauth else f"Basic {basic}", "Content-Type": "application/json",
                   "Accept": "application/json", **(extra or {})}
        return self.send(method, f"https://{self.subdomain}.zendesk.com{path}", headers,
                         json.dumps(body).encode("utf-8"))

    def open_ticket(self, request_id: str, who: dict[str, Any], body: HelpRequest,
                    ctx: dict[str, Any]) -> str:
        """Make the ticket and add the private note. The ticket's id."""
        subject = " ".join(body.subject.split()) or " ".join(body.message.split())[:70]
        plan = ctx.get("plan") or {}
        custom = []
        for key, value in (("discord_id", ctx["discord"]["id"]), ("discord_name", ctx["discord"]["name"]),
                           ("plan", plan.get("name") or ""),
                           ("project", (ctx.get("project") or {}).get("id") or "")):
            if key in self.fields and value:
                custom.append({"id": self.fields[key], "value": value})
        ticket: dict[str, Any] = {
            "subject": f"[Auto-processing] {subject}"[:150],
            "comment": {"body": body.message.strip(), "public": True},
            "requester": {"name": str(who.get("name") or "Starfront member")[:80], "email": body.email.strip()},
            "external_id": f"starfront-help-{request_id}",
            "tags": self.tags,
        }
        if custom:
            ticket["custom_fields"] = custom
        status, made = self._call("POST", "/api/v2/tickets.json", {"ticket": ticket},
                                  {"Idempotency-Key": f"starfront-help-{request_id}"})
        ticket_id = str((made.get("ticket") or {}).get("id") or "")
        if status not in (200, 201) or not ticket_id:
            _log.error("help desk refused a ticket: HTTP %s %s", status, str(made.get("error") or "")[:120])
            raise HTTPException(status_code=502, detail="the help desk did not take the request; try again shortly")
        # The context goes on as a note the customer is not sent. If it does
        # not stick the ticket still stands; the journal says so.
        status, _ = self._call("PUT", f"/api/v2/tickets/{ticket_id}.json",
                               {"ticket": {"comment": {"body": self.note(ctx), "public": False}}})
        if status != 200:
            _log.error("ticket %s was made but its context note was refused: HTTP %s", ticket_id, status)
        return ticket_id

    def _signed_in(self, request: Request) -> dict[str, Any]:
        try:
            who = self.who(request)
        except HTTPException:
            who = None
        if who is None or not who.get("id"):
            raise HTTPException(status_code=401, detail="sign in with Discord first")
        return who

    def _routes(self, app: FastAPI) -> None:

        @app.get("/api/v1/support")
        def about(request: Request) -> dict[str, Any]:
            """Whether the form is offered, and where the help centre is. Public."""
            return {"available": self.enabled, "helpUrl": self.help_url}

        @app.post("/api/v1/support")
        def ask(body: HelpRequest, request: Request) -> dict[str, Any]:
            """Send a question to the support team, with the context attached."""
            if not self.enabled:
                raise HTTPException(status_code=404, detail="the help form is not open; use the help centre")
            who = self._signed_in(request)
            if not _EMAIL.match(body.email.strip()):
                raise HTTPException(status_code=400, detail="an email address to answer you at is expected")
            now, uid = self.clock(), str(who["id"])
            # A request the help desk never took does not count against them.
            recent = [r for r in self.store.support_requests_of(uid, now - 86400)
                      if r["state"] != "failed"]
            if len(recent) >= PER_DAY or (recent and now - float(recent[0]["created"]) < MIN_GAP_SECONDS):
                raise HTTPException(status_code=429, headers={"Retry-After": "60"},
                                    detail="that is a lot of requests; reply on the ticket you already have, "
                                           "or try again later")
            ctx = self.context(who, body.project)
            request_id = secrets.token_hex(8)
            self.store.add_support_request(request_id, uid, (ctx.get("project") or {}).get("id", ""),
                                           body.subject or body.message[:70], now)
            try:
                ticket = self.open_ticket(request_id, who, body, ctx)
            except HTTPException:
                self.store.set_support_request(request_id, "failed")
                raise
            self.store.set_support_request(request_id, "sent", ticket)
            return {"sent": True, "ticket": ticket}
