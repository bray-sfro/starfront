"""What a filter is called, everywhere: one letter.

Every part of the program matches on a filter's name - the sequencer to move
the wheel, the FITS header, the plan's allocation, the calibration library,
the collaboration server judging whether a rig carries what a project wants.
Two spellings of one filter are therefore two filters as far as any of that
is concerned, and "Ha", "H-alpha", "ha 3nm" and "H" are all things people
type. So there is one spelling here, the letter - L R G B H O S - and every
name entering the program is turned into it at the door: typed into
Equipment, read off a wheel, brought over from N.I.N.A., written by a
coordinator on the server, or found in a settings file from before this.

A name that is none of these - a dual-band filter, something unusual - is
left as it was typed, trimmed. Only the seven have a letter, and mapping an
unknown filter to a nearest one would be quietly wrong.
"""

from __future__ import annotations

import re
from typing import Any

#: The seven, and every spelling of each that has been seen in a wheel or a
#: profile. Compared after lowering and stripping spaces, hyphens and
#: underscores, so "H-alpha", "h alpha" and "HAlpha" are one entry.
_ALIASES: dict[str, tuple[str, ...]] = {
    "L": ("l", "lum", "luminance", "luminanz", "lumi", "clear", "c", "uvir",
          "uv/ir", "uvircut", "ircut", "irc", "ir/uv", "white", "none"),
    "R": ("r", "red", "rot", "rouge"),
    "G": ("g", "green", "grn", "gruen", "grün", "vert"),
    "B": ("b", "blue", "blau", "bleu"),
    "H": ("h", "ha", "halpha", "hα", "hɑ", "ha3", "hydrogen", "hydrogenalpha",
          "hyd", "656"),
    "O": ("o", "oiii", "o3", "oxygen", "oxygeniii", "o111", "501"),
    "S": ("s", "sii", "s2", "sulfur", "sulphur", "sulfurii", "sulphurii", "s11",
          "672"),
}

_LOOKUP = {alias: letter for letter, aliases in _ALIASES.items() for alias in aliases}

#: A bandpass tacked onto the name - "Ha 3nm", "OIII-6.5nm", "SII (3 nm)" -
#: says which filter it is just as well without it.
_BANDPASS = re.compile(r"[\s(\[-]*\d+(?:\.\d+)?\s*nm[)\]]*\s*$", re.IGNORECASE)

#: How long a name may be. Kept from before, so nothing already stored moves.
MAX_LENGTH = 24


def canonical(name: Any) -> str:
    """The one spelling of a filter's name.

    "Ha" -> "H", "luminance" -> "L", "OIII 3nm" -> "O"; "Dual" stays "Dual".
    Blank stays blank.
    """
    text = " ".join(str(name if name is not None else "").split())[:MAX_LENGTH]
    if not text:
        return ""
    bare = _BANDPASS.sub("", text).strip()
    key = re.sub(r"[\s_\-]+", "", bare).lower()
    if key in _LOOKUP:
        return _LOOKUP[key]
    # "H a" or "O III" with the roman numerals spaced: the letter alone, once
    # the numerals are gone, is still the filter.
    key2 = re.sub(r"(iii|ii|i)$", "", key)
    if key2 and key2 in _LOOKUP and key2 in ("h", "o", "s"):
        return _LOOKUP[key2]
    return text


def canonical_list(names: Any) -> list[str]:
    """A list of names, each canonical, blanks dropped."""
    out = []
    for name in list(names or []):
        clean = canonical(name)
        if clean:
            out.append(clean)
    return out


def canonical_keys(table: Any) -> dict[str, Any]:
    """A dict keyed by filter name, re-keyed by the canonical name.

    Two keys that fold to one letter keep the first; a settings file with
    both "Ha" and "H" in it was already saying one thing twice.
    """
    out: dict[str, Any] = {}
    for name, value in dict(table or {}).items():
        clean = canonical(name)
        if clean and clean not in out:
            out[clean] = value
    return out


#: Which settings carry filter names, and how. Every write to these sections
#: goes through `fold_settings`, so a name is folded whether it arrived from
#: the dialog, a profile, N.I.N.A., or code writing what a wheel reported.
_LISTS = {("camera", "filterNames"), ("schedule", "filters")}
_KEYED = {("camera", "filterBandpass"), ("sequencer", "filterOffsets"),
          ("autoplan", "filterExposures")}
_SINGLE = {("camera", "fixedFilter"), ("sequencer", "autofocusFilter")}


def fold_settings(section: str, values: dict[str, Any]) -> dict[str, Any]:
    """The filter names in one settings section, in the one spelling.

    Returns the same dict, folded in place, so it can wrap a write.
    """
    for key in list(values):
        where = (section, key)
        if where in _LISTS:
            values[key] = canonical_list(values[key])
        elif where in _KEYED:
            values[key] = canonical_keys(values[key])
        elif where in _SINGLE:
            values[key] = canonical(values[key])
    return values


def same(a: Any, b: Any) -> bool:
    """Whether two names are the same filter, whatever the spelling."""
    return canonical(a).lower() == canonical(b).lower()
