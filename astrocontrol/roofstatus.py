"""One line for the roof, for the main window. Display only.

The dome is not commanded from here. The shutter state is the one the status
snapshot already read, and a technician lock is the text the sequencer already
stored. Elapsed time counts from when this process first saw the shutter
change, so "Roof opening…" ticks while the open is in progress.
"""

from __future__ import annotations

from typing import Any

_track = {"state": None, "since": None}


def reset() -> None:
    _track["state"] = None
    _track["since"] = None


def observe(dome: dict[str, Any] | None, locked: str = "", now: float = 0.0) -> dict[str, Any]:
    """The badge: opening/closing with elapsed seconds, or open, closed, error, lock."""
    dome = dome or {}
    if not dome.get("connected"):
        return {"connected": False, "text": "", "state": "", "tone": "",
                "since": None, "elapsed": 0, "locked": ""}
    state = str(dome.get("shutterState") or "unknown").lower()
    if state != _track["state"]:
        _track["state"] = state
        _track["since"] = float(now)
    since = float(_track["since"] if _track["since"] is not None else now)
    elapsed = max(0, int(float(now) - since))
    lock = " ".join(str(locked or "").split())
    if lock and state != "open":
        text, tone = f"Roof locked: {lock}", "locked"
    elif state == "opening":
        text, tone = f"Roof opening… {elapsed}s", "moving"
    elif state == "open":
        text, tone = "Roof open", "open"
    elif state == "closing":
        text, tone = f"Roof closing… {elapsed}s", "moving"
    elif state == "closed":
        text, tone = "Roof closed", "closed"
    elif state == "error":
        text, tone = "Roof error" + (f": {lock}" if lock else ""), "error"
    else:
        text, tone = f"Roof {state}", "error"
    return {"connected": True, "text": text, "state": state, "tone": tone,
            "since": since, "elapsed": elapsed, "locked": lock}
