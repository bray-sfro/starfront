"""Cover and calibrator-light status for the main window. Display only.

The badge is built from the last reading a background probe stored. A status
request never calls the driver: a CoverState read that hangs (the 0.2.52b
`/api/status` that returned no body) stays on the probe thread, and the
request returns the cached line, or "unknown" when there is no reading yet.

Nothing here commands the cover or the roof.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from . import coverprep

_lock = threading.Lock()
_store: dict[int, dict[str, Any]] = {}
_inflight: dict[int, threading.Thread] = {}

_BADGE = {
    "open": ("Cover open", "open"),
    "closed": ("Cover closed", "closed"),
    "moving": ("Cover moving", "moving"),
    "unknown": ("Cover unknown", "unknown"),
    "unread": ("Cover unknown", "unknown"),
    "error": ("Cover error", "error"),
    "notpresent": ("Cover not present", "notpresent"),
}


def reset() -> None:
    with _lock:
        _store.clear()


def drop(panel: Any) -> None:
    """Forget a reading. Used when the panel or the camera reconnects."""
    if panel is None:
        return
    with _lock:
        _store.pop(id(panel), None)


def note(panel: Any, reading: dict[str, Any]) -> None:
    if panel is None:
        return
    stored = dict(reading)
    stored.setdefault("readAt", time.time())
    with _lock:
        _store[id(panel)] = stored


def cached(panel: Any) -> dict[str, Any]:
    """The last reading. Instant. Never touches the driver."""
    if panel is None:
        return {"connected": False, "coverState": "", "text": "", "tone": ""}
    with _lock:
        found = _store.get(id(panel))
    connected = bool(getattr(panel, "connected", False))
    if not found:
        return {
            "connected": connected,
            "coverState": "unknown" if connected else "",
            "hasCover": None,
            "lightOn": None,
            "calibratorState": "unknown" if connected else "",
            "brightness": None,
            "maxBrightness": getattr(panel, "max_brightness", None),
            "readAt": None,
            "ageSeconds": None,
            "stale": True,
            "pending": True,
            "error": "",
        }
    read_at = found.get("readAt")
    age = None if read_at is None else round(max(0.0, time.time() - float(read_at)), 1)
    return {
        **found,
        "connected": connected and found.get("connected", True),
        "ageSeconds": age,
        "stale": bool(found.get("stale")) or (age is not None and age > 15.0),
        "pending": False,
    }


def _probe_generic(panel: Any) -> dict[str, Any]:
    """One reading from whatever attributes the panel offers.

    Runs on the probe thread. A hang here does not reach the status request.
    """
    state = coverprep.read_state(panel)
    brightness = None
    maximum = getattr(panel, "max_brightness", None)
    light = None
    calibrator = "unknown"
    error = ""
    try:
        brightness = int(panel.brightness)
    except Exception as exc:                          # noqa: BLE001 - a status miss
        error = str(exc)
    try:
        maximum = int(getattr(panel, "max_brightness", maximum) or 0) or maximum
    except Exception:
        pass
    try:
        raw = getattr(panel, "calibrator_state", None)
        if raw is None and hasattr(panel, "light_on"):
            light = bool(panel.light_on)
            calibrator = "ready" if light else "off"
        else:
            calibrator = coverprep.map_calibrator(raw)
            light = calibrator == "ready"
    except Exception as exc:                          # noqa: BLE001
        error = error or str(exc)
        light = None
    has = None
    if state == "notpresent":
        has = False
    elif state in ("closed", "moving", "open", "error"):
        has = True
    return {
        "connected": True,
        "coverState": state,
        "hasCover": has,
        "lightOn": light,
        "calibratorState": calibrator,
        "brightness": brightness,
        "maxBrightness": maximum,
        "error": error,
        "stale": False,
    }


def _probe(panel: Any) -> None:
    started = time.time()
    try:
        fast = getattr(panel, "probe_cover", None)
        if callable(fast):
            reading = dict(fast(timeout=1.5) or {})
            reading.setdefault("connected", True)
        else:
            reading = _probe_generic(panel)
    except Exception as exc:                          # noqa: BLE001 - cached as unknown
        reading = {
            "connected": bool(getattr(panel, "connected", False)),
            "coverState": "unread",
            "hasCover": None,
            "lightOn": None,
            "calibratorState": "unknown",
            "brightness": None,
            "maxBrightness": getattr(panel, "max_brightness", None),
            "error": str(exc),
            "stale": True,
        }
    reading["readAt"] = time.time()
    reading["readSeconds"] = round(time.time() - started, 3)
    note(panel, reading)


def kick(panel: Any) -> None:
    """Start a probe if one is not already running. Does not wait for it."""
    if panel is None or not getattr(panel, "connected", False):
        return
    key = id(panel)
    with _lock:
        thread = _inflight.get(key)
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(
            target=_probe, args=(panel,), daemon=True, name="cover-status")
        _inflight[key] = thread
        thread.start()


def observe(panel_status: dict[str, Any] | None) -> dict[str, Any]:
    """One badge line from a status dict that was already read. No driver call."""
    panel = panel_status or {}
    if not panel.get("connected"):
        return {"connected": False, "text": "", "state": "", "tone": "",
                "light": "", "lightOn": None, "brightness": None,
                "maxBrightness": None, "calibratorState": "",
                "readAt": panel.get("readAt"), "stale": False}
    state = str(panel.get("coverState") or "unknown").lower()
    label, tone = _BADGE.get(state, (f"Cover {state}", "unknown"))
    light_on = panel.get("lightOn")
    brightness = panel.get("brightness")
    maximum = panel.get("maxBrightness")
    calibrator = str(panel.get("calibratorState") or "")
    if light_on is True:
        if brightness is not None and maximum:
            light = f"light on {brightness}/{maximum}"
        elif brightness is not None:
            light = f"light on {brightness}"
        else:
            light = "light on"
    elif calibrator == "notready":
        light = "light not ready"
    elif light_on is False or calibrator == "off":
        light = "light off"
    else:
        light = "light unknown"
    text = f"{label} · {light}"
    return {
        "connected": True,
        "text": text,
        "state": state if state != "unread" else "unknown",
        "tone": tone,
        "light": light,
        "lightOn": light_on,
        "brightness": brightness,
        "maxBrightness": maximum,
        "calibratorState": calibrator,
        "readAt": panel.get("readAt"),
        "ageSeconds": panel.get("ageSeconds"),
        "stale": bool(panel.get("stale")),
        "error": panel.get("error") or "",
    }
