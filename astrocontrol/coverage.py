"""What the survey has already looked at, and when.

Kept in the Sun's frame rather than in RA and Dec, which is the only way the
question makes sense.  "Have I already shot this?" is, for a twilight survey, a
question about a patch of the Sun's neighbourhood - forty degrees east of the
Sun, ten degrees north of the ecliptic - and that patch is a different piece of
celestial sphere every single night.  Record RA and Dec and every night looks
like fresh sky, because it is; the sweep would then re-cover the same *relative*
region for ever and never widen.

So the key is the (dlambda, beta) cell, quantised, exactly as the panels were
generated.  Each cell remembers when it was last observed and how many times.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .config import data_root

# Cells older than this are dropped on save: the survey region moves with the
# seasons and a cell nobody has visited in half a year tells you nothing.
MAX_AGE_DAYS = 400


class CoverageStore:
    """The survey's coverage map, written through on every change."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_root() / "survey-coverage.json")
        self._lock = threading.RLock()
        self._cells: dict[str, dict[str, Any]] = {}
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(stored, dict):
            return
        cells = stored.get("cells")
        if isinstance(cells, dict):
            with self._lock:
                self._cells = {
                    key: value for key, value in cells.items()
                    if isinstance(value, dict) and value.get("last")
                }

    def save(self) -> None:
        cutoff = time.time() - MAX_AGE_DAYS * 86400
        with self._lock:
            self._cells = {key: value for key, value in self._cells.items()
                           if float(value.get("last", 0)) >= cutoff}
            payload = json.dumps({"cells": self._cells, "updated": time.time()},
                                 indent=2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(payload, "utf-8")
        except OSError:
            pass

    # -- access ------------------------------------------------------------
    def last_observed(self) -> dict[str, float]:
        """Cell key to the timestamp it was last shot, for the planner's filter."""
        with self._lock:
            return {key: float(value["last"]) for key, value in self._cells.items()}

    def listing(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"cell": key, **value} for key, value in
                    sorted(self._cells.items(), key=lambda kv: -kv[1]["last"])]

    def record(self, cell: str, when: float | None = None,
               detail: dict[str, Any] | None = None) -> None:
        """Note that a cell has been observed."""
        when = when or time.time()
        with self._lock:
            entry = self._cells.setdefault(cell, {"visits": 0, "first": when})
            entry["last"] = when
            entry["visits"] = int(entry.get("visits", 0)) + 1
            if detail:
                entry.update({k: v for k, v in detail.items()
                              if k in ("elongation", "beta", "side")})
        self.save()

    def record_many(self, cells: list[dict[str, Any]],
                    when: float | None = None) -> int:
        """Note a whole sweep at once, which is how it actually happens."""
        when = when or time.time()
        counted = 0
        with self._lock:
            for panel in cells:
                key = panel.get("cell")
                if not key:
                    continue
                entry = self._cells.setdefault(key, {"visits": 0, "first": when})
                entry["last"] = when
                entry["visits"] = int(entry.get("visits", 0)) + 1
                for field in ("elongation", "beta", "side"):
                    if field in panel:
                        entry[field] = panel[field]
                counted += 1
        if counted:
            self.save()
        return counted

    def forget(self, cell: str) -> None:
        with self._lock:
            self._cells.pop(cell, None)
        self.save()

    def clear(self) -> int:
        with self._lock:
            removed = len(self._cells)
            self._cells = {}
        self.save()
        return removed
