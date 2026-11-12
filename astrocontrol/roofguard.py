"""The roof stays shut for darks, bias and panel flats.

Lights and sky flats may open the roof, and only through the sequencer's
existing path (technician lock, safety monitor). While a dark, a bias, a
dark-flat or a panel-flat set is being taken — a recipe from the Calibrate
tab or the same recipe run as a plan task — an open is refused and no frame
is taken unless the roof reads closed.

Nothing here sends OpenShutter. `request_open` is the one call the sequencer
and the roof button use; it raises before the dome is asked when a
closed-roof run is in progress.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .devices.base import DeviceError

_lock = threading.Lock()
_holders: dict[str, str] = {}


def begin(token: str, why: str) -> None:
    with _lock:
        _holders[str(token)] = str(why or "")


def end(token: str) -> None:
    with _lock:
        _holders.pop(str(token), None)


def blocked() -> str | None:
    """Why the roof must stay shut, or None when an open is allowed."""
    with _lock:
        if not _holders:
            return None
        why = next(iter(_holders.values()))
    text = "the roof stays shut during darks, bias and panel flats"
    if why:
        text = f"{text} ({why})"
    return text


def request_open(dome: Any) -> None:
    """Open the roof, or refuse without calling the dome.

    The sequencer and the roof button both come through here. A closed-roof
    calibration run raises before `open_shutter`, so the dome's command log
    stays empty.
    """
    why = blocked()
    if why:
        raise DeviceError(f"{why}; the roof was not opened")
    dome.open_shutter()


@contextmanager
def closed_for(why: str) -> Iterator[None]:
    token = uuid.uuid4().hex
    begin(token, why)
    try:
        yield
    finally:
        end(token)


def needs_closed_roof(spec: dict[str, Any]) -> bool:
    """Darks, bias, dark-flats and panel flats. Sky flats do not."""
    kind = str((spec or {}).get("frameType") or "").lower()
    if kind in ("bias", "dark", "darkflat"):
        return True
    if kind == "flat" and str((spec or {}).get("source") or "panel").lower() != "sky":
        return True
    return False
