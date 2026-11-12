"""Compare the mount's clock with this PC. Read-only.

TheSkyX keeps its own time. On FSQ106 that clock was wrong while the PC's
timezone and the site coordinates were right, and the right ascension the
mount reported was off by about the same amount. This reads the UTC and
the local sidereal time the driver already exposes — ASCOM ``UTCDate`` and
``SiderealTime``, or the Alpaca names of those — and warns when either is
more than a minute from this PC. It does not set the time, sync, slew, or
change tracking.

The warnings watchdog calls `observe` on its ordinary pass, including the
first one shortly after startup. A connected mount is read once; later
passes repeat that verdict without asking the driver again. With no mount,
or a driver that does not answer, the result is ``clock check skipped``.
"""

from __future__ import annotations

import copy
import math
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import astro

#: More than about a minute. A few seconds of formula (mean sidereal time
#: here, apparent sidereal time on the driver) must not warn.
SKEW_LIMIT_SECONDS = 60.0

WARNING_KEY = "mount.clock"

_TITLE = "The mount's clock does not match this PC"
_FIX = ("Set TheSky's time to this PC, then reconnect the mount so this "
        "check runs again. Starfront will not set the clock and will not "
        "sync the mount.")

_SKIPPED = "clock check skipped"
_SKIPPED_SILENT = "clock check skipped: the mount did not report a time"

_OLE_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)

_lock = threading.Lock()
_cache: dict[int, dict[str, Any]] = {}
_logged: str | None = None
_latest: dict[str, Any] = {}

#: Replaced in tests. Production uses the PC's UTC clock.
def _pc_now() -> datetime:
    return datetime.now(timezone.utc)


clock: Callable[[], datetime] = _pc_now


def _report(*, state: str, detail: str, title: str = "", fix: str = "",
            utc: float | None = None, lst: float | None = None,
            compared: list[str] | None = None,
            mounts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "state": state,
        "detail": detail,
        "title": title,
        "fix": fix,
        "utcDeltaSeconds": None if utc is None else round(float(utc), 1),
        "lstDeltaSeconds": None if lst is None else round(float(lst), 1),
        "limitSeconds": SKEW_LIMIT_SECONDS,
        "compared": list(compared or []),
        "mounts": list(mounts or []),
    }


def _skipped(detail: str = _SKIPPED) -> dict[str, Any]:
    return _report(state="skipped", detail=detail)


_latest.update(_skipped())


def reset() -> None:
    """Forget the last read. Tests use this; a reconnect does it itself."""
    global _logged
    with _lock:
        _cache.clear()
        _logged = None
        _latest.clear()
        _latest.update(_skipped())


def latest() -> dict[str, Any]:
    """The last verdict, for the status payload. Never blocks on the mount."""
    with _lock:
        return copy.deepcopy(_latest)


def lst_delta_seconds(mount_hours: float, pc_hours: float) -> float:
    """Signed seconds, mount minus PC, the short way around 24 hours."""
    diff_hours = (float(mount_hours) - float(pc_hours) + 12.0) % 24.0 - 12.0
    return diff_hours * 3600.0


def coerce_utc(value: Any) -> datetime | None:
    """A driver clock as timezone-aware UTC, or None when it is not a time.

    ASCOM hands back a datetime (naive, meaning UTC). Alpaca hands back an
    ISO-8601 string. A bare OLE Automation date is accepted too.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        number = float(value)
        # Below ~1954 this is not a calendar date. Sidereal hours must not
        # be read as one.
        if not math.isfinite(number) or number < 20000:
            return None
        return _OLE_EPOCH + timedelta(days=number)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    year = getattr(value, "year", None)
    if not year:
        return None
    try:
        naive = datetime(int(value.year), int(value.month), int(value.day),
                         int(getattr(value, "hour", 0) or 0),
                         int(getattr(value, "minute", 0) or 0),
                         int(getattr(value, "second", 0) or 0),
                         int(getattr(value, "microsecond", 0) or 0))
    except (TypeError, ValueError):
        return None
    return naive.replace(tzinfo=timezone.utc)


def coerce_hours(value: Any) -> float | None:
    """Local sidereal time in hours, wrapped into 0..24, or None."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(hours):
        return None
    return hours % 24.0


def _as_utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _amount(seconds: float) -> str:
    """``2h 03m`` / ``45s``. The sign is carried by the caller."""
    whole = int(round(abs(seconds)))
    if whole >= 3600:
        hours, rest = divmod(whole, 3600)
        minutes = int(round(rest / 60.0))
        if minutes == 60:
            hours += 1
            minutes = 0
        return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    if whole >= 60:
        minutes, secs = divmod(whole, 60)
        return f"{minutes}m {secs:02d}s" if secs else f"{minutes}m"
    return f"{whole}s"


def _way(seconds: float) -> str:
    return "ahead" if seconds >= 0 else "behind"


def _placement(seconds: float) -> str:
    """``2h ahead of this PC`` / ``45s behind this PC``."""
    if seconds >= 0:
        return f"{_amount(seconds)} ahead of this PC"
    return f"{_amount(seconds)} behind this PC"


def _degrees(seconds: float) -> str:
    degrees = abs(seconds) / 3600.0 * 15.0
    if degrees >= 10:
        return f"{degrees:.0f}°"
    return f"{degrees:.1f}°"


def assess(raw: dict[str, Any] | None, *, now: datetime,
           longitude: float | None) -> dict[str, Any]:
    """One driver's clock against `now`. Pure: it does not touch a device.

    `raw` is ``{"utc": ..., "lst": ...}`` from `Mount.reported_clock`.
    None, or a reading with neither value, is a skip.
    """
    if not raw:
        return _skipped(_SKIPPED_SILENT)
    when = _as_utc(now)
    utc_value = coerce_utc(raw.get("utc"))
    lst_value = coerce_hours(raw.get("lst"))
    utc_delta = ((utc_value - when).total_seconds() if utc_value is not None else None)
    lst_delta = None
    if lst_value is not None and longitude is not None:
        pc_lst = astro.local_sidereal_hours(float(longitude), when)
        lst_delta = lst_delta_seconds(lst_value, pc_lst)
    compared = []
    if utc_delta is not None:
        compared.append("utc")
    if lst_delta is not None:
        compared.append("lst")
    if not compared:
        return _skipped(_SKIPPED_SILENT)

    offending = []
    if utc_delta is not None and abs(utc_delta) > SKEW_LIMIT_SECONDS:
        offending.append(abs(utc_delta))
    if lst_delta is not None and abs(lst_delta) > SKEW_LIMIT_SECONDS:
        offending.append(abs(lst_delta))
    if not offending:
        bits = []
        if utc_delta is not None:
            bits.append("UTC matches" if abs(utc_delta) < 0.5
                        else f"UTC {_amount(utc_delta)} {_way(utc_delta)}")
        if lst_delta is not None:
            bits.append("LST matches" if abs(lst_delta) < 0.5
                        else f"LST {_amount(lst_delta)} {_way(lst_delta)}")
        return _report(state="ok",
                       detail="Mount clock matches this PC (" + ", ".join(bits) + ").",
                       utc=utc_delta, lst=lst_delta, compared=compared)

    problems = []
    notes = []
    if utc_delta is not None and abs(utc_delta) > SKEW_LIMIT_SECONDS:
        problems.append(f"UTC {_placement(utc_delta)}")
    elif utc_delta is not None:
        notes.append(f"UTC agrees ({_amount(utc_delta)} {_way(utc_delta)})")
    else:
        notes.append("the driver did not report UTC")
    if lst_delta is not None and abs(lst_delta) > SKEW_LIMIT_SECONDS:
        problems.append(f"local sidereal time {_placement(lst_delta)}")
    elif lst_delta is not None:
        notes.append(f"LST agrees ({_amount(lst_delta)} {_way(lst_delta)})")
    detail = "The mount reports " + " and ".join(problems) + "."
    if notes:
        detail += " " + " ".join(note[0].upper() + note[1:] + "." for note in notes)
    detail += (f" That shifts right ascension by about {_degrees(max(offending))}."
               " Nothing was changed.")
    return _report(state="warn", detail=detail, title=_TITLE, fix=_FIX,
                   utc=utc_delta, lst=lst_delta, compared=compared)


def _connected(ctx: Any) -> list[tuple[Any, Any]]:
    rigs = getattr(ctx, "rigs", None)
    if rigs is None:
        return []
    found = []
    for rig in list(getattr(rigs, "all", None) or []):
        manager = getattr(rig, "manager", None)
        get = getattr(manager, "get", None)
        if not callable(get):
            continue
        try:
            device = get("mount")
        except Exception:
            continue
        if device is not None and getattr(device, "connected", False):
            found.append((rig, device))
    return found


def _longitude(ctx: Any, mounts: list[tuple[Any, Any]]) -> float | None:
    """The PC's site first. The mount's own longitude is only a fallback.

    The point of the sidereal comparison is the sky this PC believes, which
    is what a plan is aimed at. A mount longitude is used when the site has
    not been typed in yet.
    """
    section: Any = {}
    try:
        if hasattr(ctx, "section"):
            section = ctx.section("site")
    except Exception:
        section = {}
    if isinstance(section, dict) and section.get("longitude") is not None:
        try:
            return float(section["longitude"])
        except (TypeError, ValueError):
            pass
    for _rig, mount in mounts:
        site = getattr(mount, "site", None)
        if isinstance(site, dict) and site.get("longitude") is not None:
            try:
                return float(site["longitude"])
            except (TypeError, ValueError):
                continue
    return None


def _sample(mount: Any) -> dict[str, Any] | None:
    """Call the driver's read-only clock, or None if it cannot answer."""
    reader = getattr(mount, "reported_clock", None)
    if not callable(reader):
        return None
    try:
        raw = reader()
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def _rig_log(ctx: Any) -> Callable[..., Any] | None:
    rigs = getattr(ctx, "rigs", None)
    log = getattr(rigs, "log", None) if rigs is not None else None
    return log if callable(log) else None


def _emit(log: Callable[..., Any], text: str, level: str) -> None:
    try:
        log(text, level)
    except TypeError:
        log(text)


def observe(ctx: Any, log: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Look once per connected mount, then repeat that verdict.

    Safe to call on every watchdog pass. The driver is asked only the first
    time that mount object is seen connected. Disconnecting drops the
    memory, so the next connection is read again.
    """
    global _logged
    if log is None:
        log = _rig_log(ctx)
    mounts = _connected(ctx)
    now = _as_utc(clock())
    longitude = _longitude(ctx, mounts)
    fresh: list[tuple[Any, dict[str, Any]]] = []
    ready: list[dict[str, Any]] = []
    with _lock:
        live = {id(mount) for _rig, mount in mounts}
        for stale in [key for key in _cache if key not in live]:
            del _cache[stale]
        pending = []
        for rig, mount in mounts:
            hit = _cache.get(id(mount))
            if hit is None:
                pending.append((rig, mount))
            else:
                ready.append(hit)
    for rig, mount in pending:
        name = str(getattr(rig, "name", "") or "").strip()
        raw = _sample(mount)
        part = assess(raw, now=now, longitude=longitude)
        part = {**part, "name": name}
        if name and len(mounts) > 1 and part["detail"]:
            part = {**part, "detail": f"{name}: {part['detail']}"}
        fresh.append((mount, part))
    with _lock:
        for mount, part in fresh:
            _cache[id(mount)] = part
        report = _combine(ready + [part for _mount, part in fresh])
        _latest.clear()
        _latest.update(report)
        announce = None
        if report["detail"] != _logged:
            _logged = report["detail"]
            if report["state"] != "warn":
                announce = report["detail"]
    if announce and log is not None:
        _emit(log, announce, "info")
    return copy.deepcopy(report)


def _combine(parts: list[dict[str, Any]]) -> dict[str, Any]:
    if not parts:
        return _skipped()
    warns = [part for part in parts if part["state"] == "warn"]
    oks = [part for part in parts if part["state"] == "ok"]
    if warns:
        chosen = max(warns, key=lambda part: max(
            abs(part["utcDeltaSeconds"] or 0.0), abs(part["lstDeltaSeconds"] or 0.0)))
        compared: list[str] = []
        for part in warns:
            for name in part["compared"]:
                if name not in compared:
                    compared.append(name)
        return _report(state="warn", detail=" ".join(part["detail"] for part in warns),
                       title=_TITLE, fix=_FIX,
                       utc=chosen["utcDeltaSeconds"], lst=chosen["lstDeltaSeconds"],
                       compared=compared, mounts=[_face(part) for part in parts])
    if oks:
        chosen = oks[0]
        return _report(state="ok", detail=" ".join(part["detail"] for part in oks),
                       utc=chosen["utcDeltaSeconds"], lst=chosen["lstDeltaSeconds"],
                       compared=list(chosen["compared"]), mounts=[_face(part) for part in parts])
    return _report(state="skipped", detail=parts[0]["detail"],
                   mounts=[_face(part) for part in parts])


def _face(part: dict[str, Any]) -> dict[str, Any]:
    """One mount, as published. No nested copy of the same shape."""
    shown = dict(part)
    shown.pop("mounts", None)
    shown.pop("fix", None)
    shown.pop("title", None)
    return shown
