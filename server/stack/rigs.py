"""Rigs: a contributor's (camera, telescope, focal length, pixel size, binning,
mono or OSC), identified by a fingerprint every implementation computes the
same way.

`fingerprint()` is byte-identical to Rapid Stacker Collab's
`backend/lambdas/shared/rigs.js`: SHA-256 hex of the lower-cased, trimmed
camera and telescope, the rounded focal length, the pixel size rounded to one
decimal, the binning and `mono`/`osc`, joined with U+001F; the `rig_id` is
`rig_` and its first 12 hex characters. `JS round` is "half up", which is not
Python's `round`, so it is spelled out. A rig belongs to a collab user id and
is written to the bound hub's feed as `rig`, key `user_id/rig_id`.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from fastapi import APIRouter, Request

from .auth import Auth, Caller, json_body, now_iso
from .envelope import ApiError, ok, validation_failed

SEP = "\u001f"
BAYER = ("RGGB", "BGGR", "GRBG", "GBRG")


def js_round(x: float) -> int:
    """JavaScript's `Math.round`: halves go up."""
    return math.floor(x + 0.5)


def fingerprint(camera: str, telescope: str, focal_length_mm: float, pixel_size_um: float,
                binning: int, osc: bool) -> str:
    tenths = js_round(pixel_size_um * 10)
    pixel = f"{tenths // 10}.{tenths % 10}" if tenths >= 0 else f"-{abs(tenths) // 10}.{abs(tenths) % 10}"
    return hashlib.sha256(SEP.join([
        camera.strip().lower(), telescope.strip().lower(), str(js_round(focal_length_mm)),
        pixel, str(binning), "osc" if osc else "mono"]).encode("utf-8")).hexdigest()


def rig_id_of(fp: str) -> str:
    return "rig_" + fp[:12]


def pixel_scale(pixel_size_um: float, binning: int, focal_length_mm: float) -> float:
    return js_round(206.265 * pixel_size_um * binning / focal_length_mm * 10000) / 10000


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Rigs:
    def __init__(self, store: Any, auth: Auth, router: APIRouter) -> None:
        self.store = store
        self.auth = auth
        self._routes(router)

    @staticmethod
    def wire(doc: dict[str, Any]) -> dict[str, Any]:
        return {"rig_id": doc["rig_id"], "name": doc["name"], "camera": doc["camera"],
                "telescope": doc["telescope"], "focal_length_mm": doc["focal_length_mm"],
                "pixel_size_um": doc["pixel_size_um"], "binning": doc["binning"],
                "osc": bool(doc.get("osc")), "bayer_pattern": doc.get("bayer_pattern"),
                "pixel_scale_arcsec": doc.get("pixel_scale_arcsec"),
                "width_px": doc.get("width_px"), "height_px": doc.get("height_px"),
                "confirmed": bool(doc.get("confirmed")), "frame_count": int(doc.get("frame_count", 0)),
                "last_frame_at": doc.get("last_frame_at"), "orientation": doc.get("orientation"),
                "created_at": doc["created_at"]}

    def get(self, user_id: str, rig_id: str) -> dict[str, Any] | None:
        row = self.store._one("SELECT doc FROM stack_rigs WHERE user_id = ? AND rig_id = ?",
                              user_id, rig_id)
        return json.loads(row["doc"]) if row else None

    def _save(self, who: Caller, doc: dict[str, Any], new: bool) -> None:
        if new:
            self.store._run("INSERT INTO stack_rigs (user_id, rig_id, doc) VALUES (?, ?, ?)",
                            who.user_id, doc["rig_id"], json.dumps(doc))
        else:
            self.store._run("UPDATE stack_rigs SET doc = ?, rev = rev + 1 WHERE user_id = ?"
                            " AND rig_id = ?", json.dumps(doc), who.user_id, doc["rig_id"])
        if who.hub:
            self.store.append_change(who.hub, "rig", f"{who.user_id}/{doc['rig_id']}")

    def _routes(self, router: APIRouter) -> None:
        auth = self.auth

        @router.get("/rigs")
        async def listing(request: Request) -> dict[str, Any]:
            who = auth.caller(request)
            rows = [json.loads(r["doc"]) for r in self.store._all(
                "SELECT doc FROM stack_rigs WHERE user_id = ?", who.user_id)]
            rows.sort(key=lambda d: d["created_at"])
            return ok(rigs=[self.wire(d) for d in rows])

        @router.post("/rigs")
        async def upsert(request: Request) -> dict[str, Any]:
            who = auth.caller(request, write=True)
            body = await json_body(request)
            for key in ("camera", "telescope"):
                if not isinstance(body.get(key), str):
                    raise ApiError("invalid_request", f"{key} is required, as text.")
            for key in ("focal_length_mm", "pixel_size_um"):
                if not _number(body.get(key)):
                    raise ApiError("invalid_request", f"{key} is required, as a number.")
            if isinstance(body.get("binning"), bool) or not isinstance(body.get("binning"), int):
                raise ApiError("invalid_request", "binning is required, as an integer.")
            problems = [(k, "unknown field") for k in sorted(set(body) - {
                "camera", "telescope", "focal_length_mm", "pixel_size_um", "binning",
                "bayer_pattern", "width_px", "height_px", "name"})]
            camera, telescope = body["camera"].strip(), body["telescope"].strip()
            if not camera:
                problems.append(("camera", "must not be blank"))
            if not telescope:
                problems.append(("telescope", "must not be blank"))
            if body["focal_length_mm"] <= 0:
                problems.append(("focal_length_mm", "must be above zero"))
            if body["pixel_size_um"] <= 0:
                problems.append(("pixel_size_um", "must be above zero"))
            if not 1 <= body["binning"] <= 8:
                problems.append(("binning", "must be 1 to 8"))
            if body.get("bayer_pattern") is not None and body["bayer_pattern"] not in BAYER:
                problems.append(("bayer_pattern", "must be RGGB, BGGR, GRBG or GBRG"))
            for key in ("width_px", "height_px"):
                if key in body and (isinstance(body[key], bool) or not isinstance(body[key], int)
                                    or body[key] < 1):
                    problems.append((key, "must be a positive integer"))
            name = body.get("name")
            if name is not None and (not isinstance(name, str) or len(name.strip()) > 80):
                problems.append(("name", "must be text of at most 80 characters"))
            if problems:
                raise validation_failed(*problems)
            osc = body.get("bayer_pattern") is not None
            rid = rig_id_of(fingerprint(camera, telescope, body["focal_length_mm"],
                                        body["pixel_size_um"], body["binning"], osc))
            doc = self.get(who.user_id, rid)
            new = doc is None
            if new:
                doc = {"rig_id": rid, "camera": camera, "telescope": telescope,
                       "focal_length_mm": body["focal_length_mm"],
                       "pixel_size_um": body["pixel_size_um"], "binning": body["binning"],
                       "osc": osc, "pixel_scale_arcsec": pixel_scale(
                           body["pixel_size_um"], body["binning"], body["focal_length_mm"]),
                       "frame_count": 0, "created_at": now_iso(),
                       "name": f"{camera} on {telescope}"[:80]}
            if name and name.strip():
                doc["name"] = name.strip()
            if osc:
                doc["bayer_pattern"] = body["bayer_pattern"]
            for key in ("width_px", "height_px"):
                if body.get(key):
                    doc[key] = body[key]
            doc["confirmed"] = True
            self._save(who, doc, new)
            return ok(rig=self.wire(doc))

        @router.patch("/rigs/{rig_id}")
        async def patch(rig_id: str, request: Request) -> dict[str, Any]:
            who = auth.caller(request, write=True)
            body = await json_body(request)
            doc = self.get(who.user_id, rig_id)
            problems = [(k, "unknown field") for k in sorted(set(body) - {"name", "confirmed"})]
            if "name" in body:
                if not isinstance(body["name"], str):
                    raise ApiError("invalid_request", "name must be text.")
                if not 1 <= len(body["name"].strip()) <= 80:
                    problems.append(("name", "must be 1 to 80 characters"))
            if "confirmed" in body and not isinstance(body["confirmed"], bool):
                raise ApiError("invalid_request", "confirmed must be true or false.")
            if problems:
                raise validation_failed(*problems)
            if doc is None:
                raise ApiError("not_found", "No such rig.")
            if "name" in body:
                doc["name"] = body["name"].strip()
            if "confirmed" in body:
                doc["confirmed"] = body["confirmed"]
            self._save(who, doc, False)
            return ok(rig=self.wire(doc))
