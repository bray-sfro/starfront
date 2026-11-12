"""fleet.json: which Starfront PCs make up the fleet."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    # Where the dashboard listens. 127.0.0.1 = this PC only; 0.0.0.0 to open
    # it from the laptop over the VPN (then set dashboardToken).
    "listen": {"host": "127.0.0.1", "port": 8790},
    # The fleet token every node was given (Settings > server.fleetToken on
    # each Starfront); a node may override it with its own "token".
    "token": "",
    # Needed from anywhere but this PC when set.
    "dashboardToken": "",
    "pollSeconds": 2.0,          # how often each node is asked
    "timeoutSeconds": 3.0,       # how long one answer may take
    "staleSeconds": 10.0,        # no answer for this long: "stale Ns"
    "offlineSeconds": 30.0,      # no answer for this long: "offline"
    "offlineAlertMinutes": 5.0,  # an offline PC becomes an alert after this
    "recoveringAlertMinutes": 10.0,
    "nodes": [],
}

EXAMPLE = {
    **DEFAULTS,
    "token": "paste the fleet token from each Starfront here",
    "nodes": [
        {"name": "DESKTOP-4PILSFA", "host": "DESKTOP-4PILSFA", "port": 8765,
         "note": "triple RASA (RASA1-3)"},
        {"name": "WIN-Q9DV8QD504V", "host": "WIN-Q9DV8QD504V", "port": 8765,
         "note": "FSQ106"},
        {"name": "DESKTOP-F7DOP99", "host": "DESKTOP-F7DOP99", "port": 8765},
        {"name": "EAGLE4PRO0258", "host": "EAGLE4PRO0258", "port": 8765},
    ],
}


def default_path() -> Path:
    from ..config import data_root
    return data_root() / "fleet.json"


def load(path: str | Path | None = None) -> dict[str, Any]:
    """Read fleet.json; write an example one first if there is none."""
    target = Path(path) if path else default_path()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(EXAMPLE, indent=2), encoding="utf-8")
    data = json.loads(target.read_text(encoding="utf-8"))
    return normalise(data, source=str(target))


def normalise(data: dict[str, Any], source: str = "") -> dict[str, Any]:
    out = {**DEFAULTS, **(data or {})}
    out["listen"] = {**DEFAULTS["listen"], **(data.get("listen") or {})}
    nodes = []
    seen: set[str] = set()
    for raw in data.get("nodes") or []:
        name = str(raw.get("name") or raw.get("host") or "").strip()
        host = str(raw.get("host") or name).strip()
        if not name or not host or name in seen:
            continue
        seen.add(name)
        nodes.append({"name": name, "host": host, "port": int(raw.get("port") or 8765),
                      "token": raw.get("token"), "note": raw.get("note", ""),
                      "scheme": raw.get("scheme", "http")})
    out["nodes"] = nodes
    out["source"] = source
    return out
