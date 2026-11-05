"""Records for the hub: the full member and rig rows the change feed names.

The feed (server/hubs.py `pull`) carries only keys: `member` rows are
`project_id/user_id`, `rig` rows `user_id/rig_id`. A hub that sees one asks
here for the record, with its hub token like every call in server/hubs.py
(plain `{"detail"}` errors, not the Collab envelope), and gets it in the shape
of Rapid Stacker Collab's `docs/api/records.md` (`collab-members`,
`collab-rigs`).

* `GET  /api/v1/hubs/{id}/records/member/{project_id}/{user_id}`
* `GET  /api/v1/hubs/{id}/records/rig/{user_id}/{rig_id}`
* `POST /api/v1/hubs/{id}/records/batch {"member": [[p, u], ...], "rig": [[u, r], ...]}`
  answers `{member: [...], rig: [...], missing: [{kind, key}, ...]}`, at most
  `BATCH_MAX` keys in all.

Ids are collab user ids throughout; a Discord or Starfront person id never
appears in a body. A project that is not bound to THIS hub, an unknown member
and a rig whose owner is in none of the hub's projects are all the same 404 (or
a `missing` entry): ids are not worth confirming. A member who left or was
removed is still readable, with `status: removed`; that is how the hub learns
of the removal its feed row announced. A rig is visible while its owner is a
member (not removed) of a project bound to the hub.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request

BATCH_MAX = 200


class Records:
    def __init__(self, store: Any, hubs: Any) -> None:
        self.store = store
        self.hubs = hubs

    # -- the records -------------------------------------------------------------
    def member(self, hub: dict[str, Any], project_id: str, user_id: str) -> dict[str, Any] | None:
        project = self.store.stack_project(project_id)
        if project is None or project["hub"] != hub["id"]:
            return None
        row = self.store._one("SELECT * FROM stack_members WHERE project_id = ? AND user_id = ?",
                              project_id, user_id)
        if row is None:
            return None
        d = json.loads(row["doc"] or "{}")
        out = {"project_id": project_id, "user_id": user_id,
               "role": d.get("role", "contributor"), "status": d.get("status", "active"),
               "display_name": d.get("display_name", ""), "country": d.get("country"),
               "accepted_terms_version": d.get("accepted_terms_version", ""),
               "accepted_terms_at": d.get("accepted_terms_at", ""),
               "allow_owner_raw_download": bool(d.get("allow_owner_raw_download", False)),
               "filter_mapping": list(d.get("filter_mapping") or []),
               "joined_at": d.get("joined_at", "")}
        for key in ("removed_at", "removed_by", "frames_disposition"):
            if d.get(key):
                out[key] = d[key]
        return out

    def rig(self, hub: dict[str, Any], user_id: str, rig_id: str) -> dict[str, Any] | None:
        seen = self.store._one(
            "SELECT 1 AS x FROM stack_members m JOIN stack_projects p ON p.project_id = m.project_id"
            " WHERE p.hub = ? AND m.user_id = ? AND COALESCE(json_extract(m.doc, '$.status'), '')"
            " <> 'removed' LIMIT 1", hub["id"], user_id)
        if seen is None:
            return None
        row = self.store._one("SELECT doc FROM stack_rigs WHERE user_id = ? AND rig_id = ?",
                              user_id, rig_id)
        if row is None:
            return None
        d = json.loads(row["doc"])
        keep = ("rig_id", "name", "camera", "telescope", "focal_length_mm", "pixel_size_um",
                "binning", "bayer_pattern", "pixel_scale_arcsec", "width_px", "height_px",
                "last_frame_at", "orientation", "created_at", "updated_at")
        out = {"user_id": user_id, **{k: d.get(k) for k in keep},
               "osc": bool(d.get("osc")), "confirmed": bool(d.get("confirmed")),
               "frame_count": int(d.get("frame_count", 0))}
        return out

    # -- routes ------------------------------------------------------------------
    def mount(self, app: FastAPI) -> None:
        @app.get("/api/v1/hubs/{hub_id}/records/member/{project_id}/{user_id}")
        def get_member(request: Request, hub_id: str, project_id: str, user_id: str) -> dict[str, Any]:
            hub = self.hubs.hub_as_itself(request, hub_id)
            record = self.member(hub, project_id, user_id)
            if record is None:
                raise HTTPException(status_code=404, detail="no such member")
            return record

        @app.get("/api/v1/hubs/{hub_id}/records/rig/{user_id}/{rig_id}")
        def get_rig(request: Request, hub_id: str, user_id: str, rig_id: str) -> dict[str, Any]:
            hub = self.hubs.hub_as_itself(request, hub_id)
            record = self.rig(hub, user_id, rig_id)
            if record is None:
                raise HTTPException(status_code=404, detail="no such rig")
            return record

        @app.post("/api/v1/hubs/{hub_id}/records/batch")
        def batch(request: Request, hub_id: str, body: dict[str, Any]) -> dict[str, Any]:
            hub = self.hubs.hub_as_itself(request, hub_id)
            pairs: dict[str, list[tuple[str, str]]] = {}
            for kind in ("member", "rig"):
                listed = body.get(kind, [])
                if not isinstance(listed, list) or not all(
                        isinstance(p, list) and len(p) == 2 and all(
                            isinstance(s, str) and 0 < len(s) <= 128 for s in p) for p in listed):
                    raise HTTPException(status_code=400,
                                        detail=f"{kind} must be a list of [id, id] pairs")
                pairs[kind] = [(p[0], p[1]) for p in listed]
            if set(body) - {"member", "rig"}:
                raise HTTPException(status_code=400, detail="only member and rig keys are read")
            if len(pairs["member"]) + len(pairs["rig"]) > BATCH_MAX:
                raise HTTPException(status_code=400,
                                    detail=f"at most {BATCH_MAX} keys in one batch")
            out: dict[str, Any] = {"member": [], "rig": [], "missing": []}
            for kind, read in (("member", self.member), ("rig", self.rig)):
                for a, b in pairs[kind]:
                    record = read(hub, a, b)
                    if record is None:
                        out["missing"].append({"kind": kind, "key": f"{a}/{b}"})
                    else:
                        out[kind].append(record)
            return out


def install(app: FastAPI, store: Any, hubs: Any) -> Records:
    """Mount the record routes (before the `/stack` static mount)."""
    records = Records(store, hubs)
    records.mount(app)
    return records
