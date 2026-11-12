"""Registered scopes on the collaboration server.

The server files a light under the scope an agent is bound to
(``deviceKey = scope:<id>``). A name — the equipment profile, the N.I.N.A.
profile, ``presence.telescope`` — is still accepted and stored, and it may
still be sent so the others' charts can show it, but it is not how a file is
filed. The id is. A rename changes ``displayName`` only.

One agent can hold several binds (rasa1 is bound to RASA-L, RASA-A and
RASA-R). The upload then has to name the scope. The server otherwise tries
the camera serial and then RASA-L/A/R in INSTRUME, and answers 409 when none
of those match. This module decides which id to send, how the dropdown is
ordered, and which 400/403/409 answers hold one file instead of dropping it
or pausing every upload.

Registering and binding need a person's token. Nothing here mints an id.
"""

from __future__ import annotations

import re
from typing import Any

#: Where a person registers a scope. The app only links here.
REGISTER_URL = "https://collab.starfront.space"

#: The three RASA drawers on a multiscope mount. Matched as names, never as
#: a guess about which filter is in the drawer.
RASA_DRAWERS = ("RASA-L", "RASA-A", "RASA-R")

_DRAWER_TOKEN = {
    "rasal": "RASA-L",
    "rasaa": "RASA-A",
    "rasar": "RASA-R",
}

#: Structured error codes the server may send beside ``detail``.
_HOLD_CODES = {
    "not_bound",
    "unknown_scope",
    "foreign_scope",
    "missing_scope",
    "camera_miss",
    "scope",
}

_HOLD_WORDS = (
    "scope",
    "not bound",
    "unbound",
    "instrume",
    "camera miss",
    "rasa-l",
    "rasa-a",
    "rasa-r",
)

#: A 409 that means the collaboration is not taking files. That pauses the
#: project; it is not a per-file scope hold.
_PROJECT_PAUSE = (
    "auto-upload",
    "uploads are off",
    "upload is off",
    "not taking",
)


def registered_scopes_enabled(config: Any) -> bool:
    """The feature flag. Missing means on, so a new client files by scope.

    Off is the previous path: no ``scopeId`` on an upload, and the server
    keeps using the telescope name it already stored until this client is
    switched on.
    """
    if config is None:
        return True
    getter = getattr(config, "get", None)
    if not callable(getter):
        return True
    value = getter("collab", "registeredScopes", True)
    if value is None:
        return True
    return bool(value)


def parse_scope_list(payload: Any) -> list[dict[str, Any]]:
    """Scopes from a hello body or from ``GET /api/v1/scopes``.

    A missing ``scopes`` key is "this answer did not say", which is an empty
    list here; the caller decides whether that replaces a list it already
    has. Entries with no id are dropped. Nothing is invented.
    """
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, dict):
        if "scopes" not in payload and "id" in payload:
            raw = [payload]
        else:
            raw = payload.get("scopes") or []
    else:
        return []
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        scope_id = str(item.get("id") or "").strip()
        if not scope_id or scope_id in seen:
            continue
        seen.add(scope_id)
        name = str(item.get("name") or "").strip()
        display = str(item.get("displayName") or name or scope_id).strip()
        rows.append({
            "id": scope_id,
            "name": name,
            "displayName": display,
            "boundToThisAgent": bool(item.get("boundToThisAgent")),
            "cameraSerial": str(item.get("cameraSerial") or "").strip(),
            "cameraModel": str(item.get("cameraModel") or "").strip(),
        })
    return rows


def dropdown_choices(scopes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """What the collab dropdown lists. Bound scopes first, then by display name.

    The label is the server's ``displayName``. There is no free-text name and
    no id that was not in ``scopes``.
    """
    rows = []
    for item in parse_scope_list({"scopes": list(scopes or [])}):
        rows.append({
            "id": item["id"],
            "displayName": item["displayName"],
            "bound": item["boundToThisAgent"],
        })
    rows.sort(key=lambda row: (0 if row["bound"] else 1,
                               row["displayName"].casefold(), row["id"]))
    return rows


def label_for(scopes: list[dict[str, Any]] | None, scope_id: str) -> str:
    """The current display name of a stored id. Empty when the server has no such id.

    The stored value stays the id either way, so a rename only changes this
    string.
    """
    wanted = str(scope_id or "").strip()
    if not wanted:
        return ""
    for row in parse_scope_list({"scopes": list(scopes or [])}):
        if row["id"] == wanted:
            return row["displayName"]
    return ""


def accept_scope_id(scopes: list[dict[str, Any]] | None, wanted: str) -> str:
    """An id the server listed, or blank to clear the selection.

    Anything else is refused. The UI cannot register a scope, and it must
    not store an id it made up.
    """
    clean = str(wanted or "").strip()
    if not clean:
        return ""
    if any(c.isspace() for c in clean) or len(clean) > 80:
        raise ValueError("a scope is chosen from the server's list, not typed")
    known = {row["id"] for row in parse_scope_list({"scopes": list(scopes or [])})}
    if clean not in known:
        raise ValueError("unknown scope")
    return clean


def bound_count(scopes: list[dict[str, Any]] | None) -> int:
    return sum(1 for row in parse_scope_list({"scopes": list(scopes or [])})
               if row["boundToThisAgent"])


def rasa_drawer(text: Any) -> str:
    """``RASA-L``, ``RASA-A`` or ``RASA-R`` when ``text`` names that drawer.

    ``rasa1`` is the agent, not a drawer, and is not matched. A filter letter
    on its own is not a drawer either.
    """
    raw = str(text or "")
    if not raw.strip():
        return ""
    tokens = [tok.lower() for tok in re.findall(r"[A-Za-z0-9]+", raw)]
    candidates = list(tokens)
    candidates.extend(tokens[i] + tokens[i + 1] for i in range(len(tokens) - 1))
    compact = re.sub(r"[^a-z0-9]+", "", raw.lower())
    candidates.append(compact)
    for token in candidates:
        found = _DRAWER_TOKEN.get(token)
        if found:
            return found
    return ""


def _identity_drawer(identity: dict[str, Any]) -> str:
    for key in ("instrument", "camera", "telescope", "name", "instrume"):
        found = rasa_drawer(identity.get(key))
        if found:
            return found
    return ""


def _match_telescope(assignments: list[dict[str, Any]],
                     identity: dict[str, Any]) -> dict[str, Any] | None:
    """The equipment telescope this frame belongs to, when that is knowable."""
    rig = str(identity.get("rig") or "").strip()
    if rig:
        for row in assignments:
            if rig in (str(row.get("id") or ""), str(row.get("name") or "")):
                return row
    drawer = _identity_drawer(identity)
    if drawer:
        for row in assignments:
            if rasa_drawer(row.get("name")) == drawer:
                return row
            if rasa_drawer(row.get("scopeName")) == drawer:
                return row
    telescope = str(identity.get("telescope") or identity.get("name") or "").strip()
    if telescope:
        for row in assignments:
            name = str(row.get("name") or "")
            if name and name.casefold() == telescope.casefold():
                return row
    if len(assignments) == 1:
        return assignments[0]
    return None


def scope_id_for_frame(assignments: list[dict[str, Any]] | None,
                       identity: dict[str, Any] | None,
                       scopes: list[dict[str, Any]] | None = None, *,
                       enabled: bool = True) -> str:
    """The ``scopeId`` to put on this upload, or ``""`` to leave it off.

    Off the flag, always ``""``: the old path. On it, the id stored for the
    equipment telescope that took the frame — each RASA drawer has its own.
    An id the server does not currently list is not sent. With one telescope
    and no stored id, the server resolves the bind and nothing is sent.
    """
    if not enabled:
        return ""
    rows = [row for row in (assignments or []) if isinstance(row, dict)]
    ident = dict(identity or {})
    match = _match_telescope(rows, ident)
    if match is None:
        return ""
    scope_id = str(match.get("scopeId") or "").strip()
    if not scope_id:
        return ""
    if scopes:
        known = {row["id"] for row in parse_scope_list({"scopes": list(scopes)})}
        if scope_id not in known:
            return ""
    return scope_id


def upload_extra(scope_id: str, *, enabled: bool) -> dict[str, str]:
    """Fields added to ``POST /api/v1/agent/uploads``.

    The equipment-profile name is not one of them. Presence may still carry
    it for the chart; filing does not.
    """
    if enabled and str(scope_id or "").strip():
        return {"scopeId": str(scope_id).strip()}
    return {}


def scope_hold_message(code: int, body: Any) -> str | None:
    """A per-file scope answer, as the status to show while the file is held.

    ``None`` means this answer is something else (a project that is not taking
    uploads, a bad frame, a resume). A held file stays in the queue. It is
    not refused and it does not pause the other files.
    """
    if code not in (400, 403, 409) or not isinstance(body, dict):
        return None
    if "offset" in body:
        return None
    detail = str(body.get("detail") or "").strip()
    low = detail.casefold()
    if code == 409 and any(phrase in low for phrase in _PROJECT_PAUSE):
        return None
    kind = str(body.get("error") or body.get("code") or "").strip().casefold()
    textual = any(word in low for word in _HOLD_WORDS)
    if kind not in _HOLD_CODES and not textual:
        return None
    if detail:
        return detail
    return {
        400: "the server does not know that scope",
        403: "that scope is not this telescope's",
        409: "this file is not bound to a scope",
    }[code]
