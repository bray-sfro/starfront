"""A safety monitor read over the network, that fails closed (0.2.53a).

The RASA's safety monitor was the ASCOM Generic File driver reading
``Z:\\roof\\building-2\\RoofStatusFile.txt`` off a share that is no longer
mounted, and 0.2.53's Alpaca client could only speak plain http, while the
Starfront roof server (https://alpaca-api.tx.starfront.space) is https only.
So the rig had no working monitor, and "no monitor" reads *safe* in
`SafetyWatcher` by design (an observatory without one is not being watched).

This module is the source for that server, configured in the ``safety``
settings rather than as an Equipment slot, for one reason: a slot that failed
to connect is simply empty, and empty reads safe.  A source that is configured
here is asked on every poll, needs no "connected" state, and the only way it
says safe is a fresh, complete, consistent answer:

  * ``GET {root}/api/v1/safetymonitor/{n}/issafe`` answers 200 with JSON,
    ``ErrorNumber`` 0 and a boolean ``Value`` (TLS verified for https);
  * the server's clock (its HTTP ``Date`` header) is within
    ``maxClockSkewSeconds`` (120) of this PC's - time sync is off on the RASA,
    and a wrong clock makes every freshness judgement meaningless;
  * optionally (``alpacaCheckRoofState``, on by default) the Starfront roof
    report ``GET {root}/api/v1/roof/state`` has this building, its
    ``state_update`` is no older than ``staleSeconds`` on the server's own
    clock and not in this PC's future by more than the skew limit, and its
    ``is_open`` agrees with ``issafe``;
  * the last such answer is no older than ``staleSeconds`` (180).

Anything else - an exception, a timeout, a TLS failure, a non-200, malformed
JSON, a stale or future-dated report, a disagreement - is **unsafe**, with the
reason kept for the status line.  Read-only: nothing here PUTs to the server.
"""

from __future__ import annotations

import email.utils
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from .devices import alpaca

SOURCE_ALPACA = "alpaca"

DEFAULTS = {
    "alpacaScheme": "http",
    "alpacaHost": "",
    "alpacaPort": 0,                 # 0 = the scheme's default (11111 / 443)
    "alpacaBasePath": "",
    "alpacaDevice": 0,
    "alpacaCheckRoofState": True,
    "alpacaTimeoutSeconds": 5.0,
    "alpacaPollSeconds": 30.0,
    "alpacaRetrySeconds": 5.0,
    "staleSeconds": 180.0,
    "maxClockSkewSeconds": 120.0,
    # After the answer stops being a fresh "safe", ask at the retry pace for
    # this long, so the watcher's debounce sees many samples, not one or two;
    # then fall back to the poll pace (a closed roof all day is 2 GETs / 30 s).
    "alpacaFastSeconds": 300.0,
}

#: Roof states that are an explicit close whatever issafe says.
CLOSING = ("closing", "closed", "close")

_CLIENT_ID = random.randint(1, 65535)


def _num(settings: dict[str, Any], key: str) -> float:
    try:
        return float(settings.get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def _utc(text: Any) -> float | None:
    """'2026-11-04T18:06:38.792000Z' -> epoch seconds; naive means UTC."""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def configured(settings: dict[str, Any]) -> bool:
    return (str(settings.get("source") or "").lower() == SOURCE_ALPACA)


def _http_get(url: str, timeout: float) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "Starfront-safety"})
    try:
        with alpaca.urlopen(request, timeout) as response:
            return (int(response.status),
                    {k.lower(): v for k, v in response.headers.items()},
                    response.read(1_000_000))
    except urllib.error.HTTPError as exc:
        return int(exc.code), {k.lower(): v for k, v in (exc.headers or {}).items()}, b""


class AlpacaSafetySource:
    """The configured network monitor. Thread-safe; `read()` never raises."""

    def __init__(self, settings: Callable[[], dict[str, Any]],
                 fetch: Callable[[str, float], tuple[int, dict[str, str], bytes]] | None = None,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 log: Callable[[str, str], None] | None = None) -> None:
        self._settings = settings
        self._fetch = fetch or _http_get
        self._clock = clock
        self._mono = monotonic
        self._log = log
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._transaction = 0
        self._key: tuple | None = None
        self._attempt_at: float | None = None     # monotonic
        self._ok_at: float | None = None          # monotonic, last good answer
        self._safe: bool | None = None            # the last good answer
        self._error: str | None = "not asked yet"
        self._skew: float | None = None
        self._data_age: float | None = None
        self._name: str | None = None
        self._last_logged: str | None = None
        self._confirmed = 0                         # consecutive confirmed-unsafe reads
        self._explicit: str | None = None
        self._unsafe_since: float | None = None     # monotonic, not-safe began

    # -- configuration -----------------------------------------------------
    def _config(self) -> dict[str, Any]:
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in (self._settings() or {}).items() if v is not None})
        return merged

    def _root(self, s: dict[str, Any]) -> str:
        return alpaca.server_root(str(s.get("alpacaScheme") or "http"),
                                  str(s.get("alpacaHost") or ""),
                                  int(s.get("alpacaPort") or 0) or None,
                                  str(s.get("alpacaBasePath") or ""))

    def describe(self) -> str:
        s = self._config()
        try:
            root = self._root(s)
        except Exception:                          # noqa: BLE001
            root = f"{s.get('alpacaScheme')}://{s.get('alpacaHost') or '?'}"
        label = f" ({self._name})" if self._name else ""
        return f"Alpaca {root} safetymonitor {int(s.get('alpacaDevice') or 0)}{label}"

    # -- asking ------------------------------------------------------------
    def _query(self) -> str:
        self._transaction += 1
        return urllib.parse.urlencode({"ClientID": _CLIENT_ID,
                                       "ClientTransactionID": self._transaction})

    def _ask(self, s: dict[str, Any]) -> tuple[bool, float, float | None, bool]:
        """One complete read: (safe, skew, data_age, confirmed) or ValueError.

        `confirmed` (0.2.53a glitch handling) is True only for an unsafe that
        two things on the server agree on: issafe=false *and* the building's
        roof/state says closed (is_open false), or an explicit closing/closed
        roof state (is_open false, or a ``state``/``status`` of closing or
        closed) even while issafe still says true. issafe=false with the roof
        report saying open is a disagreement - an error, unsafe, unconfirmed.
        """
        root = self._root(s)
        device = int(s.get("alpacaDevice") or 0)
        timeout = max(1.0, _num(s, "alpacaTimeoutSeconds"))
        max_skew = _num(s, "maxClockSkewSeconds")
        stale = _num(s, "staleSeconds")

        status, headers, body = self._fetch(
            f"{root}/api/v1/safetymonitor/{device}/issafe?{self._query()}", timeout)
        if status != 200:
            raise ValueError(f"issafe answered HTTP {status}")
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as exc:                   # noqa: BLE001
            raise ValueError(f"issafe answer is not JSON ({exc})") from exc
        if not isinstance(data, dict):
            raise ValueError("issafe answer is not an Alpaca object")
        error = data.get("ErrorNumber", 0)
        if error not in (0, None):
            raise ValueError(f"Alpaca error {error}: {data.get('ErrorMessage') or ''}".strip())
        value = data.get("Value")
        if not isinstance(value, bool):
            raise ValueError(f"issafe Value is {value!r}, not true/false")

        now = self._clock()
        server_now = None
        date = headers.get("date")
        if date:
            try:
                server_now = email.utils.parsedate_to_datetime(date).timestamp()
            except Exception:                      # noqa: BLE001
                server_now = None
        if server_now is None:
            raise ValueError("the server sent no usable Date, so this PC's clock "
                             "cannot be checked")
        skew = server_now - now
        self._skew = skew
        if abs(skew) > max_skew:
            raise ValueError(f"this PC's clock is {-skew:+.0f} s off the server's "
                             f"(limit {max_skew:.0f} s): fix the time sync")

        data_age = None
        if s.get("alpacaCheckRoofState", True):
            status, _, body = self._fetch(f"{root}/api/v1/roof/state", timeout)
            if status != 200:
                raise ValueError(f"roof/state answered HTTP {status}")
            try:
                report = json.loads(body.decode("utf-8"))
            except Exception as exc:               # noqa: BLE001
                raise ValueError(f"roof/state is not JSON ({exc})") from exc
            if not isinstance(report, list):
                raise ValueError("roof/state is not a list")
            mine = [b for b in report if isinstance(b, dict)
                    and str(b.get("device_number")) == str(device)]
            if len(mine) != 1:
                raise ValueError(f"roof/state has no single entry for device {device}")
            building = mine[0]
            if building.get("name"):
                self._name = str(building.get("name"))[:40]
            stamp = _utc(building.get("state_update"))
            if stamp is None:
                raise ValueError("roof/state has no state_update for this building")
            if stamp - now > max_skew:
                raise ValueError(f"the roof report is {stamp - now:.0f} s in this PC's "
                                 "future: a clock is wrong")
            data_age = server_now - stamp
            self._data_age = data_age
            if data_age > stale:
                raise ValueError(f"the building's roof report is {data_age:.0f} s old "
                                 f"(limit {stale:.0f} s)")
            is_open = building.get("is_open")
            if not isinstance(is_open, bool):
                raise ValueError(f"roof/state is_open is {is_open!r}")
            words = " ".join(str(building.get(k) or "") for k in ("state", "status"))
            closing = (not is_open) or any(w in words.lower().split() for w in CLOSING)
            if closing:
                # An explicit close from the roof report: unsafe, confirmed,
                # whatever issafe says.
                self._explicit = (f"roof/state says {words.strip() or 'closed'}"
                                  + ("" if not value else " (issafe still true)"))
                return False, skew, data_age, True
            if not value:
                raise ValueError(f"issafe={value} but roof/state is_open={is_open}: "
                                 "the server disagrees with itself")
        return value, skew, data_age, False

    def poll(self, force: bool = False) -> None:
        """Ask the server if it is time to (never raises, never blocks status)."""
        s = self._config()
        key = tuple(s.get(k) for k in ("alpacaScheme", "alpacaHost", "alpacaPort",
                                       "alpacaBasePath", "alpacaDevice"))
        if not self._poll_lock.acquire(blocking=False):
            return                                 # another thread is asking now
        try:
            with self._lock:
                if key != self._key:
                    # A different server or device: nothing known about it yet.
                    self._key = key
                    self._attempt_at = self._ok_at = self._safe = None
                    self._skew = self._data_age = self._name = None
                    self._error = "not asked yet"
                    self._confirmed, self._unsafe_since = 0, None
                mono = self._mono()
                settled = self._error is None and self._safe is True
                fast = (not settled and (self._unsafe_since is None
                        or mono - self._unsafe_since < _num(s, "alpacaFastSeconds")))
                wait = (_num(s, "alpacaRetrySeconds") if fast
                        else _num(s, "alpacaPollSeconds"))
                if (not force and self._attempt_at is not None
                        and mono - self._attempt_at < wait):
                    return
                self._attempt_at = mono
            self._explicit = None
            try:
                safe, _, _, confirmed = self._ask(s)
                failure = None
            except Exception as exc:               # noqa: BLE001 - every failure is unsafe
                safe, failure, confirmed = None, (str(exc) or exc.__class__.__name__), False
            with self._lock:
                self._confirmed = self._confirmed + 1 if confirmed else 0
                if failure is None and safe is True:
                    self._unsafe_since = None
                elif self._unsafe_since is None:
                    self._unsafe_since = self._mono()
                if failure is not None:
                    self._error = failure
                    self._safe = None
                    message = (f"Safety source {self.describe()}: {failure} "
                               "- reading UNSAFE")
                    if message != self._last_logged:
                        self._last_logged = message
                        if self._log is not None:
                            self._log(message, "warn")
                    return
                recovered = self._last_logged is not None
                self._safe = safe
                self._ok_at = self._mono()
                self._error = None
                self._last_logged = None
            if recovered and self._log is not None:
                self._log(f"Safety source {self.describe()} is answering again", "info")
        finally:
            self._poll_lock.release()

    def read(self) -> tuple[bool, str | None]:
        """(safe, why-not). Safe only on a fresh, complete, consistent answer."""
        self.poll()
        s = self._config()
        with self._lock:
            if self._error is not None:
                return False, self._error
            age = None if self._ok_at is None else self._mono() - self._ok_at
            if age is None or age > _num(s, "staleSeconds"):
                return False, (f"no good answer for {age:.0f} s" if age is not None
                               else "no good answer yet")
            if self._safe is not True:
                return False, self._explicit       # the server says unsafe
            return True, None

    def confirmed_reads(self) -> int:
        """How many reads in a row were a confirmed unsafe (see `_ask`).
        Zero unless the latest answer is itself fresh and confirmed."""
        s = self._config()
        with self._lock:
            age = None if self._ok_at is None else self._mono() - self._ok_at
            if self._error is not None or age is None or age > _num(s, "staleSeconds"):
                return 0
            return self._confirmed

    def status(self) -> dict[str, Any]:
        with self._lock:
            age = None if self._ok_at is None else round(self._mono() - self._ok_at, 1)
            return {
                "source": SOURCE_ALPACA,
                "name": self.describe(),
                "serverSafe": self._safe,
                "lastUpdateAge": age,
                "skewSeconds": None if self._skew is None else round(self._skew, 1),
                "dataAge": None if self._data_age is None else round(self._data_age, 1),
                "error": self._error,
                "confirmedReads": self._confirmed,
                "explicitClose": self._explicit,
            }
