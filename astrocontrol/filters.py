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


#: The seven channels a collaboration can ask for, in the order they are
#: shown. A project wants channels; a filter on a rig *delivers* one or
#: more of them, which is the whole difference between a mono camera with
#: a wheel and a one-shot colour camera behind a dual-band filter.
CHANNELS = ("L", "R", "G", "B", "H", "O", "S")

#: Filters that pass more than one line at once, by the names people give
#: them, compared after lowering and stripping spaces, hyphens, underscores,
#: slashes, pluses and the bandpass. An L-eXtreme in front of a colour
#: camera records Ha and OIII in one sub, and the server credits both.
_MULTI: dict[tuple[str, ...], tuple[str, ...]] = {
    ("H", "O"): ("lextreme", "lxtreme", "lenhance", "lultimate", "lultimate",
                 "optolonglextreme", "optolonglenhance", "optolonglultimate",
                 "alpt", "antliaalpt", "duo", "dual", "duoband", "dualband",
                 "duonarrowband", "dualnarrowband", "dualnb", "duonb",
                 "haoiii", "oiiiha", "hao3", "o3ha", "haoiii7nm", "ho", "oh",
                 "nbz", "idasnbz", "nb1", "idasnb1", "sv220", "svbonysv220",
                 "d1", "colourmagicd1", "colormagicd1", "askard1", "askarcolourmagicd1",
                 "zwoduoband", "zwoduo", "duoband7nm", "nebulabooster",
                 "triad", "tri", "triband"),
    ("S", "O"): ("d2", "colourmagicd2", "colormagicd2", "askard2", "askarcolourmagicd2",
                 "siioiii", "oiiisii", "s2o3", "o3s2", "so", "os", "nb3", "idasnb3"),
    ("H", "S"): ("hasii", "siiha", "has2", "s2ha", "hs", "sh"),
    ("H", "O", "S"): ("triadultra", "quad", "quadband", "lquad", "quadnarrowband",
                      "quadbandultra", "hos", "haoiiisii", "antliaquadband",
                      "altairquadband", "nbx"),
}
_MULTI_LOOKUP = {alias: letters for letters, aliases in _MULTI.items() for alias in aliases}

#: Broadband filters that are a light-pollution cut rather than a colour: in
#: front of a mono camera they are luminance, in front of a colour camera they
#: are red, green and blue in one sub, like no filter at all.
_BROADBAND = ("lpro", "optolonglpro", "cls", "clsccd", "uhc", "uhcs", "lps", "lpsd1",
              "lpsd2", "lpsp2", "lpsv4", "idaslps", "lightpollution", "lp", "lpf",
              "skyglow", "cityglow", "nightsky", "uvircut", "l3", "l2")
_COLOUR_WORDS = ("rgb", "osc", "colour", "color", "oneshotcolour", "oneshotcolor",
                 "bayer", "nofilter", "open", "empty")


#: The bands that are not one of the seven: what a one-shot colour camera
#: records, and what a mono camera records through a filter that passes two
#: lines at once. Each is its own data set. An OSC frame through an
#: L-eXtreme is not Ha and it is not OIII; it is both on one Bayer matrix,
#: stacked and split by whoever processes it, so it is counted as its own
#: band and a project says whether it takes it. The spellings are fixed:
#: "OSC-RGB", "OSC-Ha+OIII", "Ha+OIII".
_BAND_LINES: dict[str, tuple[str, ...]] = {
    "RGB": ("R", "G", "B"),
    "Ha": ("H",), "OIII": ("O",), "SII": ("S",),
    "Ha+OIII": ("H", "O"), "SII+OIII": ("S", "O"), "Ha+SII": ("H", "S"),
    "Ha+OIII+SII": ("H", "O", "S"),
}
BANDS: tuple[str, ...] = tuple(["OSC-" + name for name in _BAND_LINES]
                               + [name for name in _BAND_LINES if "+" in name])
_BAND_LOOKUP = {band.lower(): band for band in BANDS}


def band(letters: Any, colour: bool = False) -> str:
    """The band a filter passing these lines records: "OSC-Ha+OIII" on a
    colour camera, "Ha+OIII" on a mono one, "H" for a mono camera's Ha."""
    want = tuple(str(c).upper() for c in letters)
    if colour:
        if set(want) <= {"R", "G", "B", "L"}:
            return "OSC-RGB"
        for name, lines in _BAND_LINES.items():
            if tuple(sorted(lines)) == tuple(sorted(want)):
                return "OSC-" + name
        return ""
    if len(want) == 1 and want[0] in CHANNELS:
        return want[0]
    for name, lines in _BAND_LINES.items():
        if "+" in name and tuple(sorted(lines)) == tuple(sorted(want)):
            return name
    return ""


def normal_channel(text: Any) -> str:
    """A channel or band in its one spelling, or "" for anything else."""
    clean = str(text if text is not None else "").strip()
    if clean.upper() in CHANNELS:
        return clean.upper()
    return _BAND_LOOKUP.get(clean.lower(), "")


def channels(name: Any, colour: bool = False) -> list[str]:
    """What a filter delivers in a camera's sub: one channel or band.

    "Ha" is ["H"] on a mono camera and ["OSC-Ha"] on a colour one;
    "L-eXtreme" is ["Ha+OIII"] mono and ["OSC-Ha+OIII"] colour; "UV/IR
    cut" is ["L"] mono and ["OSC-RGB"] colour, where a clear filter still
    records three colours on one matrix. A name nobody here knows is [],
    which is "say what it delivers", not a guess. A list, so a filter could
    one day deliver two things; today it is one.
    """
    letter = canonical(name)
    if letter in CHANNELS:
        found = band([letter], colour)
        return [found] if found else []
    text = _BANDPASS.sub("", str(name if name is not None else "")).strip()
    key = re.sub(r"[\s_\-/+&().,]+", "", text).lower()
    if not key:
        return []
    if key in _COLOUR_WORDS:
        return ["OSC-RGB"]
    lines = _MULTI_LOOKUP.get(key)
    if lines:
        found = band(lines, colour)
        return [found] if found else []
    if key in _BROADBAND:
        return ["OSC-RGB"] if colour else ["L"]
    return []


def is_osc(channel: Any) -> bool:
    """Whether a channel or band is one-shot colour data."""
    return str(channel or "").upper().startswith("OSC-")


def is_narrowband(channel: Any) -> bool:
    """Whether a channel or band carries an emission line, so it can be shot
    under a Moon: Ha, SII, or a band with either in it."""
    text = str(channel or "").upper()
    if text in ("H", "S"):
        return True
    return "HA" in text or "SII" in text


def channel_label(letter: Any) -> str:
    """"H" -> "Ha", "O" -> "OIII", "S" -> "SII"; the rest are their own name."""
    return {"H": "Ha", "O": "OIII", "S": "SII"}.get(str(letter or ""), str(letter or ""))


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
