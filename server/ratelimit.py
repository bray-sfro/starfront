"""A small in-process rate limiter for the endpoints anybody can call.

One process serves everything, so a dictionary of recent hits per caller is
enough; nothing has to be shared. The caller is the address Caddy saw (the
service only listens on loopback, behind it), falling back to the socket's
peer for a test server run without Caddy.

Only unauthenticated endpoints that write rows or start a sign-in are limited
here: four telescopes at one observatory share an address, so the agent
endpoints are left alone.
"""

from __future__ import annotations

import threading
import time
from collections import deque

#: (method, path prefix) -> (requests, per seconds). The first match wins.
RULES: list[tuple[str, str, int, float]] = [
    ("POST", "/api/v1/auth/login", 10, 60.0),
    ("GET", "/files/signin", 10, 60.0),
    ("GET", "/auth/discord/start", 20, 60.0),
    ("GET", "/auth/discord/callback", 20, 60.0),
    ("GET", "/api/v1/auth/poll", 120, 60.0),
    ("GET", "/api/v1/lookup", 60, 60.0),
    # Claiming a purchase by order number and email: a handful of tries.
    ("POST", "/api/v1/me/billing/link", 10, 600.0),
    # Published coadds are public and each master is tens of MB: enough for
    # anybody fetching a release, not enough to empty the droplet's transfer.
    ("GET", "/releases", 60, 600.0),
]


#: What a page loads to draw itself - its script, the preview pictures, a
#: release's manifest - is not limited: the page fetches several per view
#: (one per filter and stars/starless), and they are small and cached. The
#: masters under /releases still are.
EXEMPT_SUFFIXES = (".js", ".css", ".jpg", ".jpeg", ".png", ".webp", ".json")


def client_address(request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        # Caddy sets this to the address it was connected from; the last
        # entry is the one it added.
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else ""


class Limiter:
    def __init__(self, rules=None) -> None:
        self.rules = list(RULES if rules is None else rules)
        self._hits: dict[tuple[str, str], deque] = {}
        self._lock = threading.Lock()
        self._swept = time.monotonic()

    def rule(self, method: str, path: str):
        if method == "GET" and path.startswith("/releases") and path.lower().endswith(EXEMPT_SUFFIXES):
            return None
        for m, prefix, count, window in self.rules:
            if method == m and (path == prefix or path.startswith(prefix + "/")):
                return prefix, count, window
        return None

    def allow(self, method: str, path: str, address: str,
              now: float | None = None) -> tuple[bool, float]:
        """Whether this call may go ahead, and if not how long to wait."""
        found = self.rule(method, path)
        if found is None:
            return True, 0.0
        prefix, count, window = found
        now = time.monotonic() if now is None else now
        with self._lock:
            if now - self._swept > 300:
                self._sweep(now)
            hits = self._hits.setdefault((prefix, address), deque())
            while hits and now - hits[0] > window:
                hits.popleft()
            if len(hits) >= count:
                return False, max(1.0, window - (now - hits[0]))
            hits.append(now)
            return True, 0.0

    def _sweep(self, now: float) -> None:
        self._swept = now
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] > 600]:
            del self._hits[key]
