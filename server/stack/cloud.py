"""Rapid Stacker Collab's cloud, kept in step with the collaboration (task T706).

The author's rule (2026-10-22): a Starfront collaboration is the only place a
project is made. A collaboration stacked on the cloud used to need a second
project, made by hand on the Collab website, and its join code pasted into the
collaboration; every later change (name, filters, region, who may watch) had to
be made twice, and was not. Now this server makes that project itself, through
the Collab API, and keeps it in step:

* **The account link.** The collaboration's owner pastes a Rapid Stacker Collab
  API token with the `projects:manage` scope once (the Collab dashboard's "API
  tokens", a signed-in session's `tokensCreate`: the device flow the plugin uses
  never grants that scope). It is checked against `GET /auth/me` and kept here,
  per person, in `stack_cloud_links`: never listed, never sent to a telescope.
  It is sealed with AES-GCM under a key derived from `STARFRONT_CREDENTIAL_KEY`,
  else the admin token; with neither (or without the `cryptography` package) it
  is stored as typed and the link says so (`sealed: false`).
* **The project.** Binding a collaboration to the cloud makes a `stack_cloud`
  row; `sync` then calls `POST /projects` (`projectsCreate`) with the fields the
  server derives from the collaboration exactly as it derives a hub project's
  (`derive`, server/app.py `_stack_fields`), opens it, and keeps the project id,
  slug and join code. The join code goes to the collaboration's telescopes as a
  pasted one did (`stackingJoinCode`). Every edit of the collaboration calls
  `PUT /projects/{id}` (`projectsUpdate`), whose `policy` REPLACES the stored
  one, so the whole derived policy is always sent. A closed collaboration is a
  `paused` project (the contract's `closed` cannot be reopened); unbinding pauses
  it too, and binding again reopens the same project.
* **Deleting.** `projectsDelete` (`POST /projects/{id}/delete`, `confirm_slug`,
  delayed by default) and `projectsRestore`. The contract (1.28.0) makes both
  session-only, so with a token they answer 401 `session_required`; then the
  project is paused (no joins, no uploads) and the collaboration says the owner
  must finish on the Collab website, with the link. Should the contract let a
  `projects:manage` token delete, the same call simply succeeds.
* **Failures** (Collab down, a token that expired or was revoked, a refusal) are
  recorded on the row (`state: failed`, `error`, `attempts`, `next_at`) and shown
  on the collaboration; the `stack-sweep` thread retries them with back-off
  (`sweep`). Nothing is dropped silently, and a create that may have succeeded
  without an answer is looked up by its slug before it is tried again, so a lost
  reply never makes a second project.

Old collaborations with a pasted join code keep that code as it is (read-only):
they have no row here and nothing is synced for them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

#: Where Rapid Stacker Collab's API is, unless the person links another.
DEFAULT_API = "https://collab.astro.ekeservices.com/api"
#: The scope a token needs to make and edit projects.
MANAGE = "projects:manage"
TIMEOUT = 10.0
#: Back-off between retries: doubling from a minute to an hour for what may
#: pass (the service is down, a rate limit), six hours for a refusal that will
#: not pass without a change (it is retried at once on the next edit anyway).
RETRY_MIN, RETRY_MAX, RETRY_REFUSED = 60.0, 3600.0, 6 * 3600.0
_URL = re.compile(r"^https?://[^\s/]+[^\s]*$", re.I)
_TOKEN = re.compile(r"^rscu_[0-9a-f]{64}$")
_SLUG_OK = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_JOIN = re.compile(r"^[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}$")
#: The contract's reserved slugs (`Slug`).
_RESERVED = {"new", "mine", "join", "resolve-join-code", "admin", "api", "docs", "download",
             "dashboard", "console", "p", "public", "status"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS stack_cloud_links (
    person TEXT PRIMARY KEY,
    api_base TEXT NOT NULL,
    token TEXT NOT NULL,
    collab_user TEXT NOT NULL DEFAULT '',
    collab_name TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '[]',
    expires_at TEXT NOT NULL DEFAULT '',
    linked REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stack_cloud (
    sf_project TEXT PRIMARY KEY,
    person TEXT NOT NULL,
    api_base TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    slug TEXT NOT NULL DEFAULT '',
    join_code TEXT NOT NULL DEFAULT '',
    doc TEXT NOT NULL DEFAULT '{}'
);
"""


class CloudError(Exception):
    """A Collab call that did not do what was asked.

    `transient` is True for what may pass by itself (no answer, 5xx, 429);
    `code` is the contract's error code where the service gave one."""

    def __init__(self, message: str, status: int = 0, code: str = "",
                 transient: bool = False) -> None:
        super().__init__(message)
        self.message, self.status, self.code, self.transient = message, status, code, transient


def person_key(person_id: str) -> str:
    """The admin token's person has no id; its link is kept under 'admin'."""
    return person_id or "admin"


def site_of(api_base: str) -> str:
    """The website for an API base: `https://x/api` -> `https://x`."""
    base = api_base.rstrip("/")
    return base[:-4] if base.endswith("/api") else base


def normal_base(text: str) -> str:
    base = (text or "").strip().rstrip("/") or DEFAULT_API
    if len(base) > 200 or not _URL.match(base):
        raise CloudError("the Rapid Stacker Collab address must be an http:// or https:// URL")
    return base


# --------------------------------------------------------------- sealing
def _cipher(secret: str):
    if not secret:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return None
    return AESGCM(hashlib.sha256(b"starfront-cloud-link\x00" + secret.encode("utf-8")).digest())


def seal(text: str, secret: str) -> str:
    """`v1:` + base64(nonce + AES-GCM), or `plain:` + the text when no key."""
    cipher = _cipher(secret)
    if cipher is None:
        return "plain:" + text
    nonce = os.urandom(12)
    return "v1:" + base64.b64encode(nonce + cipher.encrypt(nonce, text.encode("utf-8"), b"rscu")).decode("ascii")


def unseal(blob: str, secret: str) -> str | None:
    """The token, or None when it cannot be read (the key changed)."""
    if blob.startswith("plain:"):
        return blob[6:]
    cipher = _cipher(secret)
    if cipher is None or not blob.startswith("v1:"):
        return None
    try:
        raw = base64.b64decode(blob[3:])
        return cipher.decrypt(raw[:12], raw[12:], b"rscu").decode("utf-8")
    except Exception:            # noqa: BLE001 - a wrong key is an InvalidTag; any failure is "unreadable"
        return None


# --------------------------------------------------------------- the wire
def call(base: str, token: str, method: str, path: str, body: Any = None,
         timeout: float = TIMEOUT) -> dict[str, Any]:
    """One Collab API call; the answer's JSON, or CloudError."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json", "User-Agent": "Starfront"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        text, status = exc.read().decode("utf-8", "replace"), exc.code
    except Exception as exc:                  # noqa: BLE001 - offline, DNS, timeout
        raise CloudError(f"Rapid Stacker Collab could not be reached ({exc})", transient=True) from exc
    try:
        answer = json.loads(text) if text else {}
    except ValueError:
        answer = {}
    if 200 <= status < 300 and isinstance(answer, dict):
        return answer
    code = str(answer.get("code") or "") if isinstance(answer, dict) else ""
    message = str(answer.get("message") or "") if isinstance(answer, dict) else ""
    raise CloudError(f"Rapid Stacker Collab answered {status}"
                     + (f" {code}" if code else "") + (f": {message}" if message else ""),
                     status, code, transient=status >= 500 or status == 429 or status == 0)


def slug_for(name: str, n: int = 1) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40].strip("-")
    if len(base) < 3:
        base = (base + "-project").strip("-")
    if base in _RESERVED:
        base += "-project"
    if n > 1:
        suffix = f"-{n}"
        base = base[:40 - len(suffix)].strip("-") + suffix
    return base[:40].strip("-")


class Cloud:
    """The links and the projects; `derive(sf_project)` is the server's word on
    what the project should be (see `install` in server/app.py)."""

    def __init__(self, store: Any, secret: str,
                 derive: Callable[[str], dict[str, Any] | None]) -> None:
        self.store = store
        self.secret = secret
        self.derive = derive
        self.lock = threading.RLock()
        for statement in SCHEMA.strip().split(";"):
            if statement.strip():
                store._run(statement)

    @property
    def sealed(self) -> bool:
        return _cipher(self.secret) is not None

    # -- the account link -------------------------------------------------
    def link(self, person: str, api_base: str, token: str) -> dict[str, Any]:
        """Check a token against `GET /auth/me` and keep it. CloudError when
        it is not a working token with `projects:manage`."""
        token = (token or "").strip()
        if not _TOKEN.match(token):
            raise CloudError("a Rapid Stacker Collab API token is rscu_ followed by 64 letters "
                             "and digits; create one on your Collab dashboard under API tokens")
        base = normal_base(api_base)
        me = call(base, token, "GET", "/auth/me")
        scopes = [s for s in me.get("scopes") or [] if isinstance(s, str)]
        if me.get("auth") != "token" or MANAGE not in scopes:
            raise CloudError("that token does not have the projects:manage scope: create a new "
                             "one on your Collab dashboard and tick Manage projects")
        user = me.get("user") or {}
        self.store._run(
            "INSERT OR REPLACE INTO stack_cloud_links (person, api_base, token, collab_user,"
            " collab_name, scopes, expires_at, linked) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            person_key(person), base, seal(token, self.secret), str(user.get("user_id") or ""),
            str(user.get("name") or ""), json.dumps(scopes), str(me.get("expires_at") or ""), time.time())
        # Collaborations that waited for a working link try again now.
        for row in self.store._all("SELECT sf_project FROM stack_cloud WHERE person = ?", person_key(person)):
            self.sync(row["sf_project"])
        return self.view(person)

    def unlink(self, person: str) -> None:
        self.store._run("DELETE FROM stack_cloud_links WHERE person = ?", person_key(person))

    def _link(self, person: str) -> dict[str, Any] | None:
        return self.store._one("SELECT * FROM stack_cloud_links WHERE person = ?", person_key(person))

    def linked(self, person: str) -> dict[str, Any] | None:
        """The link when there is one whose token can be read, else None."""
        row = self._link(person)
        if row is None or unseal(row["token"], self.secret) is None:
            return None
        return row

    def view(self, person: str) -> dict[str, Any]:
        """The link as its owner sees it: never the token."""
        row = self._link(person)
        out = {"linked": False, "apiBase": DEFAULT_API, "site": site_of(DEFAULT_API),
               "user": "", "expiresAt": "", "sealed": self.sealed, "unreadable": False}
        if row is None:
            return out
        readable = unseal(row["token"], self.secret) is not None
        out.update(linked=readable, unreadable=not readable, apiBase=row["api_base"],
                   site=site_of(row["api_base"]), user=row["collab_name"], expiresAt=row["expires_at"],
                   sealed=row["token"].startswith("v1:"))
        return out

    def _client(self, row: dict[str, Any]) -> tuple[str, str]:
        link = self._link(row["person"])
        token = unseal(link["token"], self.secret) if link else None
        if link is None or token is None:
            raise CloudError("the collaboration's owner has no working Rapid Stacker Collab link: "
                             "link the account again in the Collab tab", code="not_linked")
        return link["api_base"], token

    # -- the rows ---------------------------------------------------------
    def row(self, sf_project: str) -> dict[str, Any] | None:
        row = self.store._one("SELECT * FROM stack_cloud WHERE sf_project = ?", sf_project)
        if row is not None:
            row["doc"] = json.loads(row["doc"] or "{}")
        return row

    def _save(self, row: dict[str, Any]) -> None:
        self.store._run("UPDATE stack_cloud SET person = ?, api_base = ?, project_id = ?, slug = ?,"
                        " join_code = ?, doc = ? WHERE sf_project = ?",
                        row["person"], row["api_base"], row["project_id"], row["slug"],
                        row["join_code"], json.dumps(row["doc"]), row["sf_project"])

    def bind(self, sf_project: str, person: str) -> dict[str, Any]:
        """The row of a collaboration bound to the cloud, made when it is new."""
        row = self.row(sf_project)
        link = self._link(person)
        base = link["api_base"] if link else DEFAULT_API
        if row is None:
            self.store._run("INSERT OR IGNORE INTO stack_cloud (sf_project, person, api_base, doc)"
                            " VALUES (?, ?, ?, ?)", sf_project, person_key(person), base,
                            json.dumps({"state": "pending"}))
            return self.row(sf_project)
        if not row["project_id"] and row["person"] != person_key(person):
            row["person"], row["api_base"] = person_key(person), base
            self._save(row)
        return row

    def join_code(self, sf_project: str) -> str:
        row = self.row(sf_project)
        if row is None or not row["project_id"] or row["doc"].get("deleted_at"):
            return ""
        return row["join_code"]

    def owner_view(self, sf_project: str) -> dict[str, Any] | None:
        """What the collaboration's owner is shown about the cloud project."""
        row = self.row(sf_project)
        if row is None:
            return None
        d = row["doc"]
        site = site_of(row["api_base"])
        return {"state": d.get("state", "pending"), "error": d.get("error", ""),
                "code": d.get("code", ""), "attempts": int(d.get("attempts") or 0),
                "nextAt": d.get("next_at") or 0, "syncedAt": d.get("synced_at") or 0,
                "projectId": row["project_id"], "slug": row["slug"], "joinCode": row["join_code"],
                "remoteStatus": d.get("remote_status", ""), "site": site,
                "pageUrl": f"{site}/p/{row['slug']}" if row["slug"] else "",
                "deletedAt": d.get("deleted_at", ""), "deleteAfter": d.get("delete_after", ""),
                "remoteDeleted": bool(d.get("remote_deleted"))}

    # -- deleting -----------------------------------------------------------
    def delete(self, sf_project: str, deleted_at: str, delete_after: str) -> dict[str, Any] | None:
        row = self.row(sf_project)
        if row is None:
            return None
        row["doc"].update(deleted_at=deleted_at, delete_after=delete_after)
        self._save(row)
        return self.sync(sf_project)

    def restore(self, sf_project: str) -> dict[str, Any] | None:
        row = self.row(sf_project)
        if row is None:
            return None
        for k in ("deleted_at", "delete_after"):
            row["doc"].pop(k, None)
        row["doc"]["restore_wanted"] = True
        self._save(row)
        return self.sync(sf_project)

    # -- keeping it in step -------------------------------------------------
    def sync(self, sf_project: str, now: float | None = None) -> dict[str, Any] | None:
        """Bring the Collab project in line with the collaboration. Never
        raises: a failure is recorded on the row for the sweep to retry."""
        now = time.time() if now is None else now
        with self.lock:
            row = self.row(sf_project)
            if row is None:
                return None
            d = row["doc"]
            want = self.derive(sf_project)
            bound = want is not None and bool(want.get("bound"))
            if not bound and not d.get("deleted_at") and (
                    not row["project_id"] or d.get("remote_status") != "open"):
                # Unbound and nothing open there: no call to make, nothing to retry.
                d.update(state="ok", error="", code="", attempts=0, next_at=0)
                self._save(row)
                return row
            try:
                base, token = self._client(row)
                row["api_base"] = base
                if d.get("deleted_at"):
                    state = self._delete(base, token, row)
                elif not bound:
                    state = self._pause(base, token, row)
                else:
                    if not row["project_id"]:
                        self._create(base, token, row, want)
                    if d.get("restore_wanted") and d.get("remote_deleted"):
                        self._restore(base, token, row)
                    self._update(base, token, row, want)
                    state = "ok"
                if state == "ok":
                    d.pop("restore_wanted", None)
                    d.update(error="", code="")
                d.update(state=state, attempts=0, next_at=0, synced_at=now)
            except CloudError as e:
                attempts = int(d.get("attempts") or 0) + 1
                wait = (min(RETRY_MAX, RETRY_MIN * 2 ** (attempts - 1)) if e.transient
                        else RETRY_REFUSED)
                d.update(state="failed", error=e.message, code=e.code, attempts=attempts,
                         next_at=now + wait, failed_at=now)
            except Exception as e:      # noqa: BLE001 - an answer of the wrong shape: record, retry later
                attempts = int(d.get("attempts") or 0) + 1
                d.update(state="failed", error=f"unexpected answer from Rapid Stacker Collab ({e!r})"[:300],
                         code="internal", attempts=attempts, failed_at=now,
                         next_at=now + min(RETRY_MAX, RETRY_MIN * 2 ** (attempts - 1)))
            self._save(row)
            return row

    def sweep(self, now: float | None = None) -> int:
        """Retry every row whose next attempt is due; the number retried."""
        now = time.time() if now is None else now
        due = [r["sf_project"] for r in self.store._all(
            "SELECT sf_project FROM stack_cloud WHERE json_extract(doc, '$.state') IN ('failed', 'pending')"
            " AND COALESCE(json_extract(doc, '$.next_at'), 0) <= ?", now)]
        for sf_project in due:
            self.sync(sf_project, now)
        return len(due)

    # -- the steps ------------------------------------------------------------
    def _create(self, base: str, token: str, row: dict[str, Any], want: dict[str, Any]) -> None:
        fields = want["fields"]
        if not fields.get("filters"):
            raise CloudError("none of the collaboration's filters is one Rapid Stacker Collab "
                             "stacks (L, R, G, B, Ha, OIII, SII, ... or a colour camera band): "
                             "add one, and the project is made", code="no_filters")
        if not fields.get("grid"):
            raise CloudError("the collaboration has no region to frame the stack", code="no_grid")
        d = row["doc"]
        # A create whose answer was lost may have made the project: look first.
        pending = d.get("pending_slug")
        if pending:
            try:
                found = call(base, token, "GET", f"/projects/{pending}").get("project") or {}
                mine = (found.get("my_membership") or {}).get("role") == "owner"
                if mine and found.get("project_id"):
                    self._adopt(base, token, row, found)
                    return
            except CloudError as e:
                if e.transient:
                    raise
        owner = str(want.get("owner_name") or "").strip()[:40].strip()
        if len(owner) < 2:
            owner = "Starfront owner"
        body = {k: fields[k] for k in ("name", "target_name", "description", "visibility", "licence",
                                        "palette", "filters", "grid", "policy") if k in fields}
        # A mosaic framed by its panels says so (false); the placeholder asks
        # the first light to frame it.
        body.update(rotation_from_first_light=fields.get("rotation_from_first_light", True) is not False,
                    owner_display_name=owner)
        for n in range(1, 21):
            slug = slug_for(fields["name"], n)
            if not _SLUG_OK.match(slug):
                slug = slug_for("starfront-collaboration", n)
            d["pending_slug"] = slug
            self._save(row)
            try:
                made = call(base, token, "POST", "/projects", {**body, "slug": slug})
            except CloudError as e:
                if e.code in ("slug_taken", "slug_reserved"):
                    continue
                raise
            self._adopt(base, token, row, made.get("project") or {})
            d["grid_sent"] = fields["grid"]
            return
        raise CloudError("no free slug for this collaboration's name on Rapid Stacker Collab",
                         code="slug_taken")

    def _adopt(self, base: str, token: str, row: dict[str, Any], project: dict[str, Any]) -> None:
        if not project.get("join_code") and project.get("project_id"):
            project = call(base, token, "GET", f"/projects/{project['project_id']}").get("project") or project
        row["project_id"] = str(project.get("project_id") or "")
        row["slug"] = str(project.get("slug") or "")
        code = str(project.get("join_code") or "")
        row["join_code"] = code if _JOIN.match(code) else row["join_code"]
        row["doc"]["remote_status"] = str(project.get("status") or "draft")
        row["doc"].pop("pending_slug", None)
        self._save(row)

    def _put(self, base: str, token: str, row: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        got = call(base, token, "PUT", f"/projects/{row['project_id']}", body).get("project") or {}
        if got.get("status"):
            row["doc"]["remote_status"] = got["status"]
        code = str(got.get("join_code") or "")
        if _JOIN.match(code):
            row["join_code"] = code          # rotated on the website: the telescopes follow
        return got

    def _status_path(self, remote: str, wanted: str) -> list[str]:
        """The `status` values to send, in order, to go from `remote` to `wanted`
        by the contract's machine: draft->open, open<->paused."""
        if remote == wanted:
            return []
        if remote == "draft":
            return ["open"] if wanted == "open" else ["open", "paused"]
        if remote in ("open", "paused") and wanted in ("open", "paused"):
            return [wanted]
        raise CloudError(f"the Rapid Stacker Collab project is {remote} and cannot be made "
                         f"{wanted} (closed and archived projects cannot reopen); unbind it here "
                         "and bind again to make a new one", code="invalid_transition")

    def _update(self, base: str, token: str, row: dict[str, Any], want: dict[str, Any]) -> None:
        d, fields = row["doc"], want["fields"]
        if not fields.get("filters"):
            raise CloudError("none of the collaboration's filters is one Rapid Stacker Collab "
                             "stacks: the project keeps its last filters until one is added",
                             code="no_filters")
        body = {k: fields[k] for k in ("name", "target_name", "description", "visibility", "licence",
                                        "palette", "filters", "policy") if k in fields}
        grid = fields.get("grid")
        if grid and not d.get("grid_locked") and grid != d.get("grid_sent"):
            body.update(grid=grid, rotation_from_first_light=fields.get(
                "rotation_from_first_light", True) is not False)
        # Refused before anything is sent when the project can no longer get there.
        self._status_path(d.get("remote_status", "draft"), want.get("status", "open"))
        try:
            self._put(base, token, row, body)
        except CloudError as e:
            if e.code != "grid_locked" or "grid" not in body:
                raise
            d["grid_locked"] = True          # the first light framed it; the region no longer moves it
            body.pop("grid", None)
            body.pop("rotation_from_first_light", None)
            self._put(base, token, row, body)
        if "grid" in body:
            d["grid_sent"] = grid
        for status in self._status_path(d.get("remote_status", "draft"), want.get("status", "open")):
            self._put(base, token, row, {"status": status})

    def _pause(self, base: str, token: str, row: dict[str, Any]) -> str:
        """Unbound (or the collaboration is gone): stop its intake, keep it."""
        if row["project_id"] and row["doc"].get("remote_status") == "open":
            self._put(base, token, row, {"status": "paused"})
        return "ok"

    def _delete(self, base: str, token: str, row: dict[str, Any]) -> str:
        d = row["doc"]
        if not row["project_id"] or d.get("remote_deleted"):
            return "ok"
        try:
            got = call(base, token, "POST", f"/projects/{row['project_id']}/delete",
                       {"confirm_slug": row["slug"]}).get("project") or {}
            d.update(remote_deleted=True, remote_status=got.get("status") or "deleted",
                     remote_delete_after=got.get("delete_after") or "")
            return "ok"
        except CloudError as e:
            if e.code not in ("session_required", "token_scope"):
                raise
        # The contract keeps deletion for a signed-in browser: stop intake, and
        # say what is left to do and where.
        if d.get("remote_status") == "open":
            self._put(base, token, row, {"status": "paused"})
        site = site_of(row["api_base"])
        d.update(code="session_required",
                 error=("Rapid Stacker Collab lets only a signed-in browser delete a project. The "
                        f"server paused it (no joins, no uploads); delete it at {site}/p/{row['slug']} "
                        "(Console, Delete project) if you want it gone there too."))
        return "needs_owner"

    def _restore(self, base: str, token: str, row: dict[str, Any]) -> None:
        d = row["doc"]
        try:
            got = call(base, token, "POST", f"/projects/{row['project_id']}/restore", {}).get("project") or {}
        except CloudError as e:
            if e.code in ("session_required", "token_scope"):
                raise CloudError("Rapid Stacker Collab lets only a signed-in browser restore a "
                                 f"project: restore it at {site_of(row['api_base'])}/p/{row['slug']}, "
                                 "and the server takes it from there", e.status, e.code) from e
            raise
        d.update(remote_deleted=False, remote_status=got.get("status") or "open")
        d.pop("remote_delete_after", None)
