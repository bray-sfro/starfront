"""The `InitHeader` allow-list, and the rule that a location is never kept.

`frames/init` takes the few FITS cards the service needs to judge a frame
before its bytes arrive. The contract makes that object an allow-list: any
other key, and in particular `SITELAT`, `SITELONG`, `SITEELEV` and `OBSERVER`,
is refused with 400 `header_rejected`, so the privacy rule can be tested from
outside. T405's `init` calls `check()`; nothing here touches the database.

`ALLOWED` is a copy of `components/schemas/InitHeader`; `tools/check_server.py`
compares the two whenever the contract is at hand.
"""

from __future__ import annotations

from typing import Any

from .envelope import ApiError

#: The cards that say where the telescope is or who is under it. Never stored,
#: never echoed, whatever a file carries.
LOCATION_CARDS = frozenset({"SITELAT", "SITELONG", "SITEELEV", "OBSERVER"})

# kind: "str" (max length), "num", "int" (minimum), "numstr" (number or string)
#: card -> (kind, limit)
ALLOWED: dict[str, tuple[str, int | None]] = {
    "FILTER": ("str", 80), "IMAGETYP": ("str", 40), "EXPTIME": ("num", None),
    "GAIN": ("num", None), "OFFSET": ("num", None),
    "XBINNING": ("int", 1), "YBINNING": ("int", 1),
    "INSTRUME": ("str", 80), "TELESCOP": ("str", 80), "FOCALLEN": ("num", None),
    "XPIXSZ": ("num", None), "YPIXSZ": ("num", None), "BAYERPAT": ("str", 8),
    "DATE-OBS": ("str", 40), "OBJECT": ("str", 80), "RA": ("numstr", None),
    "DEC": ("numstr", None), "OBJCTRA": ("str", None), "OBJCTDEC": ("str", None),
    "PIXSCALE": ("num", None), "SET-TEMP": ("num", None), "CALSTAT": ("str", 16),
    "NAXIS1": ("int", 1), "NAXIS2": ("int", 1), "BITPIX": ("int", None),
}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fits(kind: str, limit: int | None, value: Any) -> str:
    """Why `value` is not acceptable for a card of this kind, or ''."""
    if kind == "str":
        if not isinstance(value, str):
            return "must be text"
        return f"must be at most {limit} characters" if limit and len(value) > limit else ""
    if kind == "num":
        return "" if _number(value) else "must be a number"
    if kind == "numstr":
        return "" if _number(value) or isinstance(value, str) else "must be a number or text"
    if isinstance(value, bool) or not isinstance(value, int):
        return "must be an integer"
    return f"must be at least {limit}" if limit is not None and value < limit else ""


def check(header: Any) -> dict[str, Any]:
    """The header, when every key is on the allow-list and fits its card.

    Raises 400 `header_rejected` (with `errors`, one per key) for a key off the
    list, and 400 `validation_failed` for a value of the wrong type. Never
    returns a location card: they are always off the list.
    """
    if not isinstance(header, dict):
        raise ApiError("invalid_request", "header must be an object of FITS cards.")
    refused = [{"field": f"header.{k}",
                "message": ("a location or observer card is never accepted"
                            if str(k).upper() in LOCATION_CARDS
                            else "not an allowed card")}
               for k in header if k not in ALLOWED]
    if refused:
        raise ApiError("header_rejected",
                       "The header carries cards that are not on the allow-list.",
                       errors=refused)
    bad = []
    for key, value in header.items():
        kind, limit = ALLOWED[key]
        why = _fits(kind, limit, value)
        if why:
            bad.append({"field": f"header.{key}", "message": why})
    if bad:
        raise ApiError("validation_failed", "One or more fields are not acceptable.",
                       errors=bad)
    return header


def without_location(header: dict[str, Any]) -> dict[str, Any]:
    """A copy of a header read from a file, minus the location cards, for
    anything that would store or show it."""
    return {k: v for k, v in header.items() if str(k).upper() not in LOCATION_CARDS}
