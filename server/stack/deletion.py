"""Deleting a hub-backed collaboration (decision D004).

Deleting files a seven-day, restorable, delayed deletion. The `stack_projects`
document becomes `status: "deleted"` with `deleted_at`, `delete_after` and
`deleted_by` (and `deleted_by_role`, the only one the contract shows), and a
`project` row goes in the hub's change feed so the hub stops taking frames and,
once `delete_after` has passed, runs its own purge. The front door answers
409 `project_deleted` to joins and uploads meanwhile (projects.py, frames.py).

On this server, once the window has passed the `stack-sweep` thread deletes the
relay bytes that belong to the project (`purge_relay`): its frames, and the
library frames of people who are in no other live hub project. Rows stay.
What the hub pushed (`STARFRONT_DATA/stack/public/<id>/`: `status.json`,
`masters/`, previews) is KEPT, so the project page still works read-only; the
owner removes it explicitly with `remove_results`.

Restoring (`restore`) works until `delete_after` and puts the project back to
the status it had.
"""

from __future__ import annotations

import shutil
import time
from calendar import timegm
from pathlib import Path
from typing import Any

WINDOW_DAYS = 7


class Refused(Exception):
    """A lifecycle step that cannot happen; `status` is the HTTP code, `message` for a person."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def stamp(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


def epoch(text: Any) -> float | None:
    try:
        return float(timegm(time.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


def _pretty(text: Any) -> str:
    return str(text or "")[:10]


def window_open(doc: dict[str, Any], now: float) -> bool:
    due = epoch(doc.get("delete_after"))
    return due is not None and now < due


def _tell(store: Any, row: dict[str, Any]) -> None:
    if row["hub"]:
        store.append_change(row["hub"], "project", row["project_id"])


def delete(store: Any, sf_project: str, by: str, role: str, now: float | None = None) -> dict[str, Any]:
    """File the delayed deletion. `by` is a Starfront person id, `role` owner or admin."""
    now = time.time() if now is None else now
    row = store.stack_project_of(sf_project)
    if row is None or not row["hub"]:
        raise Refused(400, "Only a collaboration stacked on a hub can be deleted this way; "
                           "close it instead.")
    doc = dict(row["doc"])
    if doc.get("status") == "deleted":
        raise Refused(409, "This collaboration is already deleted.")
    project = store.project(sf_project)
    doc.update(status_before_delete=doc.get("status", "open"),
               collab_status_before_delete=(project or {}).get("status", "open"),
               status="deleted", deleted_at=stamp(now),
               delete_after=stamp(now + WINDOW_DAYS * 86400),
               deleted_by=by, deleted_by_role=role, updated_at=stamp(now))
    for k in ("relay_purged_at", "results_removed_at"):
        doc.pop(k, None)
    store.set_stack_project(row["project_id"], row["hub"], doc)
    if project is not None and project.get("status") != "closed":
        store.set_project(sf_project, project["payload"], "closed")    # off everybody's list
    _tell(store, row)
    return doc


def restore(store: Any, sf_project: str, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    row = store.stack_project_of(sf_project)
    if row is None or row["doc"].get("status") != "deleted":
        raise Refused(409, "This collaboration is not deleted.")
    doc = dict(row["doc"])
    if not window_open(doc, now) or doc.get("relay_purged_at"):
        raise Refused(409, f"The restore window ended on {_pretty(doc.get('delete_after'))}: the "
                           "raw frames have been purged. The final results are kept until you "
                           "remove them.")
    doc["status"] = doc.pop("status_before_delete", "open")
    before = doc.pop("collab_status_before_delete", None)
    for k in ("deleted_at", "delete_after", "deleted_by", "deleted_by_role"):
        doc.pop(k, None)
    doc["updated_at"] = stamp(now)
    store.set_stack_project(row["project_id"], row["hub"], doc)
    project = store.project(sf_project)
    if project is not None and before:
        store.set_project(sf_project, project["payload"], before)
    _tell(store, row)
    return doc


def remove_results(store: Any, sf_project: str, public: Path, now: float | None = None) -> dict[str, Any]:
    """The owner removes the kept `status.json`, `masters/` and previews."""
    now = time.time() if now is None else now
    row = store.stack_project_of(sf_project)
    if row is None or row["doc"].get("status") != "deleted":
        raise Refused(409, "Only the kept results of a deleted collaboration can be removed.")
    doc = dict(row["doc"])
    if window_open(doc, now):
        raise Refused(409, f"The collaboration can still be restored until "
                           f"{_pretty(doc.get('delete_after'))}; remove its results after that.")
    folder = (public / row["project_id"]).resolve()
    if folder.is_relative_to(public.resolve()) and folder != public.resolve():
        shutil.rmtree(folder, ignore_errors=True)
    doc.update(results_removed_at=stamp(now), updated_at=stamp(now))
    store.set_stack_project(row["project_id"], row["hub"], doc)
    _tell(store, row)
    return doc


def due_projects(store: Any, now: float) -> list[dict[str, Any]]:
    out = []
    for r in store._all("SELECT project_id FROM stack_projects WHERE"
                        " json_extract(doc, '$.status') = 'deleted'"):
        row = store.stack_project(r["project_id"])
        d = row["doc"]
        if not d.get("relay_purged_at") and epoch(d.get("delete_after")) is not None \
                and now >= epoch(d["delete_after"]):
            out.append(row)
    return out


def library_only_for(store: Any, row: dict[str, Any]) -> list[str]:
    """Frame ids in the library of people who belong to this project and to no
    other live hub project: their library frames are attributable only to it."""
    ids = []
    for m in store._all("SELECT user_id FROM stack_members WHERE project_id = ?", row["project_id"]):
        other = store._one(
            "SELECT 1 AS x FROM stack_members m JOIN stack_projects p ON p.project_id = m.project_id"
            " WHERE m.user_id = ? AND m.project_id <> ? AND p.hub <> ''"
            " AND COALESCE(json_extract(p.doc, '$.status'), '') <> 'deleted'"
            " AND COALESCE(json_extract(m.doc, '$.status'), '') <> 'removed'",
            m["user_id"], row["project_id"])
        if other is None:
            ids += [r["frame_id"] for r in store._all(
                "SELECT frame_id FROM stack_library WHERE user_id = ? AND path <> ''"
                " AND json_extract(doc, '$.relay_deleted') IS NULL", m["user_id"])]
    return ids
