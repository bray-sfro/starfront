"""Projects and membership: resolve a join code, read a project, join, and the
caller's own membership. Bodies and codes are the Collab contract's.

A project here is the `doc` of a `stack_projects` row (server/app.py makes it
from the Starfront collaboration); a member is a `stack_members` row keyed by
`(project_id, collab user id)`. Every write appends a `member` row to the
bound hub's change feed, key `project_id/user_id`, so the hub learns of it.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from fastapi import APIRouter, Request

from . import defaults
from .auth import Auth, Caller, json_body, now_iso
from .. import notify
from .envelope import ApiError, ok, validation_failed

#: What a member must have accepted, as the contract's `TermsVersion`.
TERMS_VERSION = os.environ.get("STARFRONT_STACK_TERMS_VERSION") or "2026-10-13-draft"
_TERMS = re.compile(r"^\d{4}-\d{2}-\d{2}(-[a-z0-9]+)?$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
#: A hub heartbeats about every 30 s; this long without one and it is called offline.
HUB_ONLINE_S = 180
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def terms_version() -> str:
    return os.environ.get("STARFRONT_STACK_TERMS_VERSION") or TERMS_VERSION


def normalise_code(text: Any) -> str:
    """A join code as stored (XXXX-XXXX-XXXX), or '' when it cannot be one."""
    if not isinstance(text, str):
        return ""
    letters = re.sub(r"[\s-]", "", text).upper()
    if len(letters) != 12 or any(c not in _CODE_ALPHABET for c in letters):
        return ""
    return f"{letters[:4]}-{letters[4:8]}-{letters[8:]}"


def mapping_of(value: Any) -> list[dict[str, str]]:
    """A contributor's filter mapping, validated: `[{raw, canonical}]`."""
    if not isinstance(value, list):
        raise ApiError("invalid_request", "filter_mapping must be a list.")
    out, bad = [], []
    for i, entry in enumerate(value):
        raw = entry.get("raw") if isinstance(entry, dict) else None
        canon = entry.get("canonical") if isinstance(entry, dict) else None
        if not isinstance(raw, str) or not isinstance(canon, str):
            raise ApiError("invalid_request", "Each filter_mapping entry needs raw and canonical text.")
        if not 1 <= len(raw) <= 80:
            bad.append((f"filter_mapping[{i}].raw", "must be 1 to 80 characters"))
        if canon != "SKIP" and canon not in defaults.CANONICAL_FILTERS:
            bad.append((f"filter_mapping[{i}].canonical", "not a canonical filter or SKIP"))
        if set(entry) - {"raw", "canonical"}:
            bad.append((f"filter_mapping[{i}]", "unknown field"))
        out.append({"raw": raw, "canonical": canon})
    if bad:
        raise validation_failed(*bad)
    return out


def member_left(store: Any, sf_project: str, sf_user_id: str, by: str = "") -> bool:
    """A person (or a telescope that has no owner) is no longer in a hub-bound
    collaboration: mark their `stack_members` row `removed` and tell the hub with
    a `member` feed row, so it re-reads the record and sees `status: removed`.
    `sf_user_id` is the Starfront id (the owner's person id, else the agent id);
    the collab id comes from `stack_identities`. True when a row changed. A
    removed member cannot rejoin by code (`membership_removed`), as the contract
    says; one the telescope removed by leaving comes back by joining the
    collaboration again (`member_returned`)."""
    found = _stack_member(store, sf_project, sf_user_id)
    if found is None:
        return False
    row, user_id, doc = found
    if doc.get("status") == "removed":
        return False
    # The status it had, for member_returned to give back: a member still
    # waiting for approval goes back to waiting, not to active.
    doc.update(status="removed", removed_at=now_iso(), removed_by=by or user_id,
               frames_disposition="keep", status_before_removal=doc.get("status", "active"))
    _save_member(store, row, user_id, doc)
    return True


def member_returned(store: Any, sf_project: str, sf_user_id: str) -> bool:
    """A telescope that left a hub-bound collaboration (`member_left`, by
    "telescope") has joined it again: its `stack_members` row gets back the
    status it had, and the hub is told with a `member` feed row. True when a row
    changed.

    Why: leaving is the dock's Leave button (declining the last share), and it
    used the owner's removal, which is final. On 2026-10-28 one press took the
    backyard rig off the Bubble Nebula for good: its plugin could not rejoin
    (`membership_removed`), its frames stopped going, and a hand upload was
    refused (`not_member`). A person who left can now come back by joining the
    collaboration, as they could before stacking existed. Any other removal
    (`removed_by` not "telescope", as an owner's would be) is still final."""
    found = _stack_member(store, sf_project, sf_user_id)
    if found is None:
        return False
    row, user_id, doc = found
    if doc.get("status") != "removed" or doc.get("removed_by") != "telescope":
        return False
    before = doc.pop("status_before_removal", None)
    if before not in ("active", "pending"):
        # A row from before the status was kept: what a new join would get.
        before = "active" if (row["doc"] or {}).get("approval", "open") == "open" else "pending"
    for key in ("removed_at", "removed_by", "frames_disposition"):
        doc.pop(key, None)
    doc.update(status=before, rejoined_at=now_iso())
    _save_member(store, row, user_id, doc)
    return True


def _stack_member(store: Any, sf_project: str,
                  sf_user_id: str) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
    """The stacking project of a Starfront collaboration, the collab user id of a
    Starfront id, and that member's doc; None when any is missing."""
    row = store.stack_project_of(sf_project)
    if row is None:
        return None
    ident = store._one("SELECT collab_user_id FROM stack_identities WHERE user_id = ?", sf_user_id)
    if ident is None:
        return None
    user_id = ident["collab_user_id"]
    member = store._one("SELECT doc FROM stack_members WHERE project_id = ? AND user_id = ?",
                        row["project_id"], user_id)
    if member is None:
        return None
    return row, user_id, json.loads(member["doc"] or "{}")


def _save_member(store: Any, row: dict[str, Any], user_id: str, doc: dict[str, Any]) -> None:
    store._run("UPDATE stack_members SET doc = ?, rev = rev + 1 WHERE project_id = ? AND user_id = ?",
               json.dumps(doc), row["project_id"], user_id)
    if row["hub"]:
        store.append_change(row["hub"], "member", f"{row['project_id']}/{user_id}")


class Projects:
    def __init__(self, store: Any, auth: Auth, router: APIRouter) -> None:
        self.store = store
        self.auth = auth
        self._routes(router)

    # -- shapes ---------------------------------------------------------------
    def member(self, project_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.store._one("SELECT * FROM stack_members WHERE project_id = ? AND user_id = ?",
                              project_id, user_id)
        if row is not None:
            row["doc"] = json.loads(row["doc"] or "{}")
        return row

    def _members(self, project_id: str) -> list[dict[str, Any]]:
        return [dict(r, doc=json.loads(r["doc"] or "{}")) for r in self.store._all(
            "SELECT * FROM stack_members WHERE project_id = ?", project_id)]

    def counts(self, row: dict[str, Any]) -> dict[str, Any]:
        base = dict(row["doc"].get("counts") or {})
        active = sum(1 for m in self._members(row["project_id"]) if m["doc"].get("status") == "active")
        out = {"member_count": active, "frames_accepted": int(base.get("frames_accepted", 0)),
               "integration_s": base.get("integration_s", 0)}
        if base.get("last_update"):
            out["last_update"] = base["last_update"]
        return out

    def membership(self, row: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
        d = member["doc"]
        quotas = row["doc"].get("quotas") or defaults.QUOTAS
        used = self.store._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM stack_frames WHERE"
            " project_id = ? AND user_id = ? AND status NOT IN ('withdrawn', 'rejected')",
            row["project_id"], member["user_id"])
        said = {s: self.store._one(
            "SELECT COUNT(*) AS n FROM stack_frames WHERE project_id = ? AND user_id = ?"
            " AND status = ?", row["project_id"], member["user_id"], s)["n"]
            for s in ("accepted", "rejected")}
        out = {
            "project_id": row["project_id"], "user_id": member["user_id"],
            "role": d.get("role", "contributor"), "status": d.get("status", "active"),
            "display_name": d.get("display_name", ""),
            "country": d.get("country"),
            "accepted_terms_version": d.get("accepted_terms_version", ""),
            "accepted_terms_at": d.get("accepted_terms_at", ""),
            "allow_owner_raw_download": bool(d.get("allow_owner_raw_download", False)),
            "filter_mapping": list(d.get("filter_mapping") or []),
            "usage": {"bytes_used": int(used["b"]), "frames_used": int(used["n"]),
                      "bytes_cap": int(quotas["contributor_max_bytes"]),
                      "frames_cap": int(quotas["contributor_max_frames"])},
            "stats": {"frames_accepted": said["accepted"], "frames_rejected": said["rejected"],
                      "integration_s": d.get("integration_s", 0), "last_frame_at": d.get("last_frame_at")},
            "joined_at": d.get("joined_at", ""),
        }
        return out

    def summary(self, row: dict[str, Any]) -> dict[str, Any]:
        d = row["doc"]
        return {"project_id": row["project_id"], "slug": row["slug"], "name": d.get("name", ""),
                "target_name": d.get("target_name", ""), "status": d.get("status", "open"),
                "visibility": d.get("visibility", "unlisted"), "owner_name": d.get("owner_name", ""),
                "palette": d.get("palette", "none"), "filters": list(d.get("filters") or []),
                "counts": self.counts(row), "combined_preview_url": None,
                "created_at": d.get("created_at", "")}

    def project(self, row: dict[str, Any], mine: dict[str, Any] | None) -> dict[str, Any]:
        d = row["doc"]
        out = {**self.summary(row), "description": d.get("description", ""),
               "licence": d.get("licence", "members_only"), "grid": d.get("grid"),
               "mosaic": None, "policy": d.get("policy") or dict(defaults.POLICY),
               "terms_version": terms_version(), "my_membership": None,
               "updated_at": d.get("updated_at", d.get("created_at", ""))}
        out.pop("combined_preview_url")      # a summary field, not a Project one
        if d.get("status") == "deleted":
            # Only while deleted (the contract); `results_kept` is Starfront's own:
            # the raw frames are gone after delete_after, the final results stay
            # until the owner removes them (D004).
            for k in ("deleted_at", "delete_after", "deleted_by_role"):
                if d.get(k):
                    out[k] = d[k]
            out["results_kept"] = not d.get("results_removed_at")
        if d.get("rotation_from_first_light"):
            out["rotation_from_first_light"] = True
        if mine is not None:
            out["my_membership"] = self.membership(row, mine)
            out["quotas"] = d.get("quotas") or dict(defaults.QUOTAS)
            if mine["doc"].get("role") in ("owner", "maintainer") and row.get("join_code"):
                out["join_code"] = row["join_code"]
        return out

    def stacked_by(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Which hub stacks a project and how it last reported: the name, the
        workers it said it had, and when it was last heard. None when unbound."""
        hub = self.store.hub(row["hub"]) if row["hub"] else None
        if hub is None:
            return None
        said = hub.get("status") or {}
        workers = said.get("workers") if isinstance(said, dict) else None
        if isinstance(workers, (list, dict)):
            workers = len(workers)
        seen = float(hub.get("seen") or 0)
        return {"hub_name": hub["name"],
                "workers": int(workers) if isinstance(workers, (int, float)) and not isinstance(workers, bool) else None,
                "last_seen": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seen)) if seen else None),
                "online": bool(seen) and time.time() - seen < HUB_ONLINE_S}

    def changed(self, row: dict[str, Any], user_id: str) -> None:
        if row["hub"]:
            self.store.append_change(row["hub"], "member", f"{row['project_id']}/{user_id}")

    # -- the routes -------------------------------------------------------------
    def _routes(self, router: APIRouter) -> None:
        auth = self.auth

        @router.post("/projects/resolve-join-code")
        async def resolve(request: Request) -> dict[str, Any]:
            who = auth.caller(request, write=True)
            body = await json_body(request)
            if not isinstance(body.get("code"), str):
                raise ApiError("invalid_request", "code is required, as text.")
            if set(body) - {"code"} or len(body["code"]) > 20:
                raise validation_failed(("code", "at most 20 characters; no other field"))
            code = normalise_code(body["code"])
            found = self.store._one("SELECT project_id FROM stack_projects WHERE join_code = ?",
                                    code) if code else None
            if found is None:
                raise ApiError("invalid_code", "That join code is not known.")
            row = auth.project(found["project_id"])
            if row["hub"] != who.hub:
                raise ApiError("invalid_code", "That join code is not known.")
            d = row["doc"]
            if d.get("status") == "deleted":
                raise ApiError("project_deleted", "That project is scheduled for deletion.")
            mine = self.member(row["project_id"], who.user_id)
            base = f"{request.url.scheme}://{request.url.netloc}"
            return ok(project=self.summary(row), grid=d.get("grid"),
                      policy=d.get("policy") or dict(defaults.POLICY),
                      terms_version=terms_version(), terms_url=f"{base}/stack/terms",
                      requires_approval=d.get("approval", "open") != "open",
                      already_member=bool(mine and mine["doc"].get("status") != "removed"))

        @router.get("/projects")
        async def list_projects(request: Request, scope: str = "public") -> dict[str, Any]:
            if scope not in ("public", "mine"):
                raise validation_failed(("scope", "must be public or mine"))
            if scope == "public":
                # The public directory is not built here: a project is found by its link.
                return ok(projects=[], directory_enabled=False, next_cursor=None)
            who = auth.caller(request)
            out = []
            for member in self.store._all("SELECT * FROM stack_members WHERE user_id = ?",
                                          who.user_id):
                member["doc"] = json.loads(member["doc"] or "{}")
                row = self.store.stack_project(member["project_id"])
                if row is None or member["doc"].get("status") == "removed":
                    continue
                item = self.project(row, member)
                item.pop("join_code", None)
                item["stacked_by"] = self.stacked_by(row)
                out.append(item)
            out.sort(key=lambda p: p.get("created_at") or "", reverse=True)
            return ok(projects=out, directory_enabled=False, next_cursor=None)

        @router.get("/projects/{project}")
        async def get_project(project: str, request: Request) -> dict[str, Any]:
            who = auth.caller(request, required=False)
            row = auth.project(project)
            if who is not None:
                auth.scoped(who, row)
            mine = self.member(row["project_id"], who.user_id) if who else None
            if mine is not None and mine["doc"].get("status") == "removed":
                mine = None
            entitled = who is not None and who.kind == "grant"
            if row["doc"].get("visibility") == "private" and mine is None and not entitled:
                raise ApiError("not_found", "No such project.")
            return ok(project=self.project(row, mine))

        @router.post("/projects/{project}/join")
        async def join(project: str, request: Request) -> dict[str, Any]:
            who = auth.caller(request, write=True)
            row = auth.project(project)
            auth.scoped(who, row)
            body = await json_body(request)
            for key in ("display_name", "accepted_terms_version"):
                if not isinstance(body.get(key), str):
                    raise ApiError("invalid_request", f"{key} is required, as text.")
            if body.get("code") is not None and not isinstance(body["code"], str):
                raise ApiError("invalid_request", "code must be text.")
            if "allow_owner_raw_download" in body and not isinstance(
                    body["allow_owner_raw_download"], bool):
                raise ApiError("invalid_request", "allow_owner_raw_download must be true or false.")
            unknown = set(body) - {"code", "display_name", "country", "accepted_terms_version",
                                   "allow_owner_raw_download", "filter_mapping"}
            problems = [(k, "unknown field") for k in sorted(unknown)]
            name = body["display_name"].strip()
            if not 2 <= len(name) <= 40:
                problems.append(("display_name", "must be 2 to 40 characters"))
            if not _TERMS.match(body["accepted_terms_version"]):
                problems.append(("accepted_terms_version", "not a terms version"))
            country = body.get("country")
            if country is not None and not (isinstance(country, str) and _COUNTRY.match(country)):
                problems.append(("country", "must be two capital letters"))
            if problems:
                raise validation_failed(*problems)
            mapping = mapping_of(body.get("filter_mapping") or [])
            # The grant names the project, so it is the credential; a code that
            # is sent has to be the project's own.
            sent = str(body.get("code") or "").strip()
            if sent and (not normalise_code(sent) or normalise_code(sent) != row.get("join_code")):
                raise ApiError("invalid_code", "That join code does not belong to this project.")
            d = row["doc"]
            if d.get("status") == "deleted":
                raise ApiError("project_deleted", "That project is scheduled for deletion.")
            if d.get("status") != "open":
                raise ApiError("project_not_open", "That project is not accepting joins.")
            have = self.member(row["project_id"], who.user_id)
            if have is not None:
                raise ApiError("membership_removed" if have["doc"].get("status") == "removed"
                               else "already_member",
                               "You were removed from this project." if have["doc"].get("status") == "removed"
                               else "You are already a member of this project.")
            if body["accepted_terms_version"] != terms_version():
                raise ApiError("terms_outdated", "Those are not the current terms.",
                               terms_version=terms_version())
            self._unique_name(row["project_id"], name, who.user_id)
            stamp = now_iso()
            doc = {"role": "contributor",
                   "status": "active" if d.get("approval", "open") == "open" else "pending",
                   "display_name": name, "country": country,
                   "accepted_terms_version": body["accepted_terms_version"],
                   "accepted_terms_at": stamp,
                   "allow_owner_raw_download": bool(body.get("allow_owner_raw_download", False)),
                   "filter_mapping": mapping, "joined_at": stamp}
            self.store._run("INSERT INTO stack_members (project_id, user_id, doc) VALUES (?, ?, ?)",
                            row["project_id"], who.user_id, json.dumps(doc))
            self.changed(row, who.user_id)
            if doc["status"] == "active":
                # Only a new active membership is announced: a re-join is refused
                # above, and a pending one is not in yet.
                notify.notify_member_joined(self.store, self.store.project(row["sf_project"]),
                                            name, member_id=who.user_id)
            return ok(membership=self.membership(row, self.member(row["project_id"], who.user_id)))

        @router.get("/projects/{project}/membership")
        async def get_membership(project: str, request: Request) -> dict[str, Any]:
            who = auth.caller(request)
            row = auth.project(project)
            auth.scoped(who, row)
            return ok(membership=self.membership(row, self._own(row, who)))

        @router.put("/projects/{project}/membership")
        async def put_membership(project: str, request: Request) -> dict[str, Any]:
            who = auth.caller(request, write=True)
            row = auth.project(project)
            auth.scoped(who, row)
            mine = self._own(row, who)
            body = await json_body(request)
            problems = [(k, "unknown field") for k in sorted(
                set(body) - {"display_name", "country", "allow_owner_raw_download",
                             "filter_mapping", "accepted_terms_version"})]
            doc = mine["doc"]
            if "display_name" in body:
                if not isinstance(body["display_name"], str):
                    raise ApiError("invalid_request", "display_name must be text.")
                if not 2 <= len(body["display_name"].strip()) <= 40:
                    problems.append(("display_name", "must be 2 to 40 characters"))
            if "country" in body and body["country"] is not None and not (
                    isinstance(body["country"], str) and _COUNTRY.match(body["country"])):
                problems.append(("country", "must be two capital letters or null"))
            if "allow_owner_raw_download" in body and not isinstance(
                    body["allow_owner_raw_download"], bool):
                raise ApiError("invalid_request", "allow_owner_raw_download must be true or false.")
            if "accepted_terms_version" in body and not (
                    isinstance(body["accepted_terms_version"], str)
                    and _TERMS.match(body["accepted_terms_version"])):
                problems.append(("accepted_terms_version", "not a terms version"))
            if problems:
                raise validation_failed(*problems)
            mapping = mapping_of(body["filter_mapping"]) if "filter_mapping" in body else None
            if "accepted_terms_version" in body:
                if body["accepted_terms_version"] != terms_version():
                    raise ApiError("terms_outdated", "Those are not the current terms.",
                                   terms_version=terms_version())
                doc["accepted_terms_version"] = body["accepted_terms_version"]
                doc["accepted_terms_at"] = now_iso()
            if "display_name" in body:
                name = body["display_name"].strip()
                self._unique_name(row["project_id"], name, who.user_id)
                doc["display_name"] = name
            if "country" in body:
                doc["country"] = body["country"]
            if "allow_owner_raw_download" in body:
                doc["allow_owner_raw_download"] = body["allow_owner_raw_download"]
            if mapping is not None:
                doc["filter_mapping"] = mapping
            self.store._run("UPDATE stack_members SET doc = ?, rev = rev + 1 WHERE project_id = ?"
                            " AND user_id = ?", json.dumps(doc), row["project_id"], who.user_id)
            self.changed(row, who.user_id)
            return ok(membership=self.membership(row, self.member(row["project_id"], who.user_id)))

    def _own(self, row: dict[str, Any], who: Caller) -> dict[str, Any]:
        mine = self.member(row["project_id"], who.user_id)
        if mine is None or mine["doc"].get("status") == "removed":
            raise ApiError("not_member", "You are not a member of this project.")
        return mine

    def _unique_name(self, project_id: str, name: str, user_id: str) -> None:
        """409 `display_name_taken` when another member has it (any case)."""
        for other in self._members(project_id):
            if other["user_id"] != user_id and other["doc"].get("status") != "removed" and \
                    str(other["doc"].get("display_name", "")).casefold() == name.casefold():
                raise ApiError("display_name_taken", "Another member already uses that display name.")
