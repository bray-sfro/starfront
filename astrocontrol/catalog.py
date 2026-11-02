"""A small deep-sky catalog, plus name resolution for everything else.

The bundled table is deliberately short: the whole Messier catalog and the
showpieces most people actually point a telescope at.  It is here so the object
search keeps working at a dark site with no internet, which is where a capture
PC usually lives.  Anything not in the table falls through to CDS Sesame, which
resolves essentially any published designation but needs a network.

Coordinates are J2000, written the way a catalog prints them and parsed once at
import: an hour/minute string is far easier to check by eye than a decimal.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

# id, common name, type, RA (h m), Dec (d m), magnitude, size in arcminutes, constellation
_ROWS: tuple[tuple, ...] = (
    ("M1", "Crab Nebula", "supernova remnant", "05 34.5", "+22 01", 8.4, 6, "Tau"),
    ("M2", "", "globular cluster", "21 33.5", "-00 49", 6.5, 16, "Aqr"),
    ("M3", "", "globular cluster", "13 42.2", "+28 23", 6.2, 18, "CVn"),
    ("M4", "", "globular cluster", "16 23.6", "-26 32", 5.6, 26, "Sco"),
    ("M5", "", "globular cluster", "15 18.6", "+02 05", 5.6, 23, "Ser"),
    ("M6", "Butterfly Cluster", "open cluster", "17 40.1", "-32 13", 4.2, 25, "Sco"),
    ("M7", "Ptolemy Cluster", "open cluster", "17 53.9", "-34 49", 3.3, 80, "Sco"),
    ("M8", "Lagoon Nebula", "emission nebula", "18 03.8", "-24 23", 6.0, 90, "Sgr"),
    ("M9", "", "globular cluster", "17 19.2", "-18 31", 7.7, 9, "Oph"),
    ("M10", "", "globular cluster", "16 57.1", "-04 06", 6.6, 15, "Oph"),
    ("M11", "Wild Duck Cluster", "open cluster", "18 51.1", "-06 16", 5.8, 14, "Sct"),
    ("M12", "", "globular cluster", "16 47.2", "-01 57", 6.7, 15, "Oph"),
    ("M13", "Hercules Cluster", "globular cluster", "16 41.7", "+36 28", 5.8, 20, "Her"),
    ("M14", "", "globular cluster", "17 37.6", "-03 15", 7.6, 11, "Oph"),
    ("M15", "", "globular cluster", "21 30.0", "+12 10", 6.2, 18, "Peg"),
    ("M16", "Eagle Nebula", "emission nebula", "18 18.8", "-13 47", 6.0, 35, "Ser"),
    ("M17", "Omega Nebula", "emission nebula", "18 20.8", "-16 11", 7.0, 46, "Sgr"),
    ("M18", "", "open cluster", "18 19.9", "-17 08", 7.5, 9, "Sgr"),
    ("M19", "", "globular cluster", "17 02.6", "-26 16", 6.8, 14, "Oph"),
    ("M20", "Trifid Nebula", "emission nebula", "18 02.6", "-23 02", 6.3, 28, "Sgr"),
    ("M21", "", "open cluster", "18 04.6", "-22 30", 6.5, 13, "Sgr"),
    ("M22", "", "globular cluster", "18 36.4", "-23 54", 5.1, 24, "Sgr"),
    ("M23", "", "open cluster", "17 56.8", "-19 01", 6.9, 27, "Sgr"),
    ("M24", "Sagittarius Star Cloud", "star cloud", "18 16.9", "-18 29", 4.6, 90, "Sgr"),
    ("M25", "", "open cluster", "18 31.6", "-19 15", 4.6, 32, "Sgr"),
    ("M26", "", "open cluster", "18 45.2", "-09 24", 8.0, 15, "Sct"),
    ("M27", "Dumbbell Nebula", "planetary nebula", "19 59.6", "+22 43", 7.4, 8, "Vul"),
    ("M28", "", "globular cluster", "18 24.5", "-24 52", 6.8, 11, "Sgr"),
    ("M29", "", "open cluster", "20 23.9", "+38 32", 7.1, 7, "Cyg"),
    ("M30", "", "globular cluster", "21 40.4", "-23 11", 7.2, 12, "Cap"),
    ("M31", "Andromeda Galaxy", "galaxy", "00 42.7", "+41 16", 3.4, 178, "And"),
    ("M32", "", "galaxy", "00 42.7", "+40 52", 8.1, 8, "And"),
    ("M33", "Triangulum Galaxy", "galaxy", "01 33.9", "+30 39", 5.7, 73, "Tri"),
    ("M34", "", "open cluster", "02 42.0", "+42 47", 5.5, 35, "Per"),
    ("M35", "", "open cluster", "06 08.9", "+24 20", 5.3, 28, "Gem"),
    ("M36", "", "open cluster", "05 36.1", "+34 08", 6.3, 12, "Aur"),
    ("M37", "", "open cluster", "05 52.4", "+32 33", 6.2, 24, "Aur"),
    ("M38", "", "open cluster", "05 28.4", "+35 50", 7.4, 21, "Aur"),
    ("M39", "", "open cluster", "21 32.2", "+48 26", 4.6, 32, "Cyg"),
    ("M40", "Winnecke 4", "double star", "12 22.4", "+58 05", 8.4, 1, "UMa"),
    ("M41", "", "open cluster", "06 46.0", "-20 44", 4.5, 38, "CMa"),
    ("M42", "Orion Nebula", "emission nebula", "05 35.3", "-05 23", 4.0, 85, "Ori"),
    ("M43", "De Mairan's Nebula", "emission nebula", "05 35.5", "-05 16", 9.0, 20, "Ori"),
    ("M44", "Beehive Cluster", "open cluster", "08 40.4", "+19 59", 3.1, 95, "Cnc"),
    ("M45", "Pleiades", "open cluster", "03 47.4", "+24 07", 1.6, 110, "Tau"),
    ("M46", "", "open cluster", "07 41.8", "-14 49", 6.1, 27, "Pup"),
    ("M47", "", "open cluster", "07 36.6", "-14 30", 4.4, 30, "Pup"),
    ("M48", "", "open cluster", "08 13.8", "-05 48", 5.8, 54, "Hya"),
    ("M49", "", "galaxy", "12 29.8", "+08 00", 8.4, 9, "Vir"),
    ("M50", "", "open cluster", "07 03.2", "-08 20", 5.9, 16, "Mon"),
    ("M51", "Whirlpool Galaxy", "galaxy", "13 29.9", "+47 12", 8.4, 11, "CVn"),
    ("M52", "", "open cluster", "23 24.2", "+61 35", 6.9, 13, "Cas"),
    ("M53", "", "globular cluster", "13 12.9", "+18 10", 7.6, 13, "Com"),
    ("M54", "", "globular cluster", "18 55.1", "-30 29", 7.6, 12, "Sgr"),
    ("M55", "", "globular cluster", "19 40.0", "-30 58", 6.3, 19, "Sgr"),
    ("M56", "", "globular cluster", "19 16.6", "+30 11", 8.3, 7, "Lyr"),
    ("M57", "Ring Nebula", "planetary nebula", "18 53.6", "+33 02", 8.8, 1.4, "Lyr"),
    ("M58", "", "galaxy", "12 37.7", "+11 49", 9.7, 6, "Vir"),
    ("M59", "", "galaxy", "12 42.0", "+11 39", 9.6, 5, "Vir"),
    ("M60", "", "galaxy", "12 43.7", "+11 33", 8.8, 7, "Vir"),
    ("M61", "", "galaxy", "12 21.9", "+04 28", 9.7, 6, "Vir"),
    ("M62", "", "globular cluster", "17 01.2", "-30 07", 6.5, 15, "Oph"),
    ("M63", "Sunflower Galaxy", "galaxy", "13 15.8", "+42 02", 8.6, 12, "CVn"),
    ("M64", "Black Eye Galaxy", "galaxy", "12 56.7", "+21 41", 8.5, 9, "Com"),
    ("M65", "", "galaxy", "11 18.9", "+13 05", 9.3, 10, "Leo"),
    ("M66", "", "galaxy", "11 20.2", "+12 59", 8.9, 9, "Leo"),
    ("M67", "", "open cluster", "08 51.4", "+11 49", 6.1, 30, "Cnc"),
    ("M68", "", "globular cluster", "12 39.5", "-26 45", 7.8, 12, "Hya"),
    ("M69", "", "globular cluster", "18 31.4", "-32 21", 7.6, 10, "Sgr"),
    ("M70", "", "globular cluster", "18 43.2", "-32 18", 7.9, 8, "Sgr"),
    ("M71", "", "globular cluster", "19 53.8", "+18 47", 8.2, 7, "Sge"),
    ("M72", "", "globular cluster", "20 53.5", "-12 32", 9.3, 6, "Aqr"),
    ("M73", "", "asterism", "20 59.0", "-12 38", 9.0, 3, "Aqr"),
    ("M74", "Phantom Galaxy", "galaxy", "01 36.7", "+15 47", 9.4, 10, "Psc"),
    ("M75", "", "globular cluster", "20 06.1", "-21 55", 8.6, 6, "Sgr"),
    ("M76", "Little Dumbbell", "planetary nebula", "01 42.4", "+51 34", 10.1, 2.7, "Per"),
    ("M77", "Cetus A", "galaxy", "02 42.7", "-00 01", 8.9, 7, "Cet"),
    ("M78", "", "reflection nebula", "05 46.7", "+00 03", 8.3, 8, "Ori"),
    ("M79", "", "globular cluster", "05 24.5", "-24 33", 7.7, 9, "Lep"),
    ("M80", "", "globular cluster", "16 17.0", "-22 59", 7.3, 9, "Sco"),
    ("M81", "Bode's Galaxy", "galaxy", "09 55.6", "+69 04", 6.9, 27, "UMa"),
    ("M82", "Cigar Galaxy", "galaxy", "09 55.8", "+69 41", 8.4, 11, "UMa"),
    ("M83", "Southern Pinwheel", "galaxy", "13 37.0", "-29 52", 7.5, 13, "Hya"),
    ("M84", "", "galaxy", "12 25.1", "+12 53", 9.1, 6, "Vir"),
    ("M85", "", "galaxy", "12 25.4", "+18 11", 9.1, 7, "Com"),
    ("M86", "", "galaxy", "12 26.2", "+12 57", 8.9, 9, "Vir"),
    ("M87", "Virgo A", "galaxy", "12 30.8", "+12 24", 8.6, 8, "Vir"),
    ("M88", "", "galaxy", "12 32.0", "+14 25", 9.6, 7, "Com"),
    ("M89", "", "galaxy", "12 35.7", "+12 33", 9.8, 5, "Vir"),
    ("M90", "", "galaxy", "12 36.8", "+13 10", 9.5, 9, "Vir"),
    ("M91", "", "galaxy", "12 35.4", "+14 30", 10.2, 5, "Com"),
    ("M92", "", "globular cluster", "17 17.1", "+43 08", 6.4, 14, "Her"),
    ("M93", "", "open cluster", "07 44.6", "-23 52", 6.2, 22, "Pup"),
    ("M94", "", "galaxy", "12 50.9", "+41 07", 8.2, 11, "CVn"),
    ("M95", "", "galaxy", "10 44.0", "+11 42", 9.7, 7, "Leo"),
    ("M96", "", "galaxy", "10 46.8", "+11 49", 9.2, 7, "Leo"),
    ("M97", "Owl Nebula", "planetary nebula", "11 14.8", "+55 01", 9.9, 3.4, "UMa"),
    ("M98", "", "galaxy", "12 13.8", "+14 54", 10.1, 10, "Com"),
    ("M99", "", "galaxy", "12 18.8", "+14 25", 9.9, 5, "Com"),
    ("M100", "", "galaxy", "12 22.9", "+15 49", 9.3, 7, "Com"),
    ("M101", "Pinwheel Galaxy", "galaxy", "14 03.2", "+54 21", 7.9, 29, "UMa"),
    ("M102", "Spindle Galaxy", "galaxy", "15 06.5", "+55 46", 9.9, 5, "Dra"),
    ("M103", "", "open cluster", "01 33.2", "+60 42", 7.4, 6, "Cas"),
    ("M104", "Sombrero Galaxy", "galaxy", "12 40.0", "-11 37", 8.0, 9, "Vir"),
    ("M105", "", "galaxy", "10 47.8", "+12 35", 9.3, 5, "Leo"),
    ("M106", "", "galaxy", "12 19.0", "+47 18", 8.4, 19, "CVn"),
    ("M107", "", "globular cluster", "16 32.5", "-13 03", 8.1, 10, "Oph"),
    ("M108", "Surfboard Galaxy", "galaxy", "11 11.5", "+55 40", 10.0, 8, "UMa"),
    ("M109", "", "galaxy", "11 57.6", "+53 22", 9.8, 8, "UMa"),
    ("M110", "", "galaxy", "00 40.4", "+41 41", 8.5, 17, "And"),

    # ---- showpieces outside the Messier list --------------------------------
    ("NGC 7000", "North America Nebula", "emission nebula", "20 59.0", "+44 20", 4.0, 120, "Cyg"),
    ("IC 5070", "Pelican Nebula", "emission nebula", "20 50.8", "+44 21", 8.0, 60, "Cyg"),
    ("NGC 6960", "Western Veil", "supernova remnant", "20 45.7", "+30 43", 7.0, 70, "Cyg"),
    ("NGC 6992", "Eastern Veil", "supernova remnant", "20 56.4", "+31 43", 7.0, 60, "Cyg"),
    ("NGC 6888", "Crescent Nebula", "emission nebula", "20 12.0", "+38 21", 7.4, 18, "Cyg"),
    ("IC 5146", "Cocoon Nebula", "emission nebula", "21 53.5", "+47 16", 7.2, 12, "Cyg"),
    ("NGC 6826", "Blinking Planetary", "planetary nebula", "19 44.8", "+50 31", 8.8, 0.5, "Cyg"),
    ("NGC 6819", "Foxhead Cluster", "open cluster", "19 41.3", "+40 11", 7.3, 5, "Cyg"),
    ("IC 1396", "Elephant's Trunk", "emission nebula", "21 39.1", "+57 30", 3.5, 170, "Cep"),
    ("NGC 7380", "Wizard Nebula", "emission nebula", "22 47.0", "+58 06", 7.2, 25, "Cep"),
    ("NGC 6946", "Fireworks Galaxy", "galaxy", "20 34.9", "+60 09", 8.8, 11, "Cep"),
    ("NGC 281", "Pacman Nebula", "emission nebula", "00 52.8", "+56 37", 7.4, 35, "Cas"),
    ("IC 1805", "Heart Nebula", "emission nebula", "02 32.7", "+61 27", 6.5, 60, "Cas"),
    ("IC 1848", "Soul Nebula", "emission nebula", "02 51.3", "+60 25", 6.5, 60, "Cas"),
    ("NGC 7635", "Bubble Nebula", "emission nebula", "23 20.7", "+61 12", 10.0, 15, "Cas"),
    ("NGC 7789", "Caroline's Rose", "open cluster", "23 57.0", "+56 44", 6.7, 16, "Cas"),
    ("NGC 457", "Owl Cluster", "open cluster", "01 19.6", "+58 17", 6.4, 13, "Cas"),
    ("NGC 663", "", "open cluster", "01 46.0", "+61 15", 7.1, 16, "Cas"),
    ("NGC 869", "Double Cluster (h Per)", "open cluster", "02 19.0", "+57 09", 5.3, 30, "Per"),
    ("NGC 884", "Double Cluster (chi Per)", "open cluster", "02 22.4", "+57 07", 6.1, 30, "Per"),
    ("NGC 1499", "California Nebula", "emission nebula", "04 03.3", "+36 25", 5.0, 145, "Per"),
    ("NGC 1275", "Perseus A", "galaxy", "03 19.8", "+41 31", 11.9, 2, "Per"),
    ("NGC 891", "", "galaxy", "02 22.6", "+42 21", 9.9, 14, "And"),
    ("NGC 752", "", "open cluster", "01 57.8", "+37 41", 5.7, 75, "And"),
    ("NGC 253", "Sculptor Galaxy", "galaxy", "00 47.6", "-25 17", 7.1, 28, "Scl"),
    ("NGC 7331", "", "galaxy", "22 37.1", "+34 25", 9.5, 11, "Peg"),
    ("NGC 7293", "Helix Nebula", "planetary nebula", "22 29.6", "-20 48", 7.3, 25, "Aqr"),
    ("NGC 7009", "Saturn Nebula", "planetary nebula", "21 04.2", "-11 22", 8.0, 0.5, "Aqr"),
    ("IC 342", "Hidden Galaxy", "galaxy", "03 46.8", "+68 06", 8.4, 21, "Cam"),
    ("IC 405", "Flaming Star Nebula", "emission nebula", "05 16.2", "+34 16", 6.0, 30, "Aur"),
    ("IC 434", "Horsehead Nebula", "dark nebula", "05 41.0", "-02 27", 6.8, 60, "Ori"),
    ("NGC 2024", "Flame Nebula", "emission nebula", "05 41.7", "-01 51", 2.0, 30, "Ori"),
    ("IC 2118", "Witch Head Nebula", "reflection nebula", "05 06.9", "-07 13", 13.0, 180, "Eri"),
    ("NGC 2237", "Rosette Nebula", "emission nebula", "06 31.7", "+05 03", 5.5, 80, "Mon"),
    ("NGC 2244", "Rosette Cluster", "open cluster", "06 32.4", "+04 52", 4.8, 24, "Mon"),
    ("NGC 2264", "Christmas Tree Cluster", "open cluster", "06 41.1", "+09 53", 3.9, 20, "Mon"),
    ("NGC 2359", "Thor's Helmet", "emission nebula", "07 18.6", "-13 12", 11.5, 8, "CMa"),
    ("NGC 2392", "Eskimo Nebula", "planetary nebula", "07 29.2", "+20 55", 9.2, 0.8, "Gem"),
    ("NGC 2903", "", "galaxy", "09 32.2", "+21 30", 8.9, 13, "Leo"),
    ("NGC 3628", "Hamburger Galaxy", "galaxy", "11 20.3", "+13 35", 9.5, 15, "Leo"),
    ("NGC 4565", "Needle Galaxy", "galaxy", "12 36.3", "+25 59", 9.6, 16, "Com"),
    ("NGC 4631", "Whale Galaxy", "galaxy", "12 42.1", "+32 32", 9.2, 15, "CVn"),
    ("NGC 4038", "Antennae Galaxies", "galaxy", "12 01.9", "-18 52", 10.3, 5, "Crv"),
    ("NGC 5907", "Splinter Galaxy", "galaxy", "15 15.9", "+56 19", 10.4, 13, "Dra"),
    ("NGC 6543", "Cat's Eye Nebula", "planetary nebula", "17 58.6", "+66 38", 8.1, 0.3, "Dra"),
    ("NGC 6302", "Bug Nebula", "planetary nebula", "17 13.7", "-37 06", 9.6, 3, "Sco"),
    ("NGC 6822", "Barnard's Galaxy", "galaxy", "19 44.9", "-14 48", 8.7, 15, "Sgr"),
    ("NGC 5128", "Centaurus A", "galaxy", "13 25.5", "-43 01", 6.8, 26, "Cen"),
    ("NGC 5139", "Omega Centauri", "globular cluster", "13 26.8", "-47 29", 3.9, 36, "Cen"),
    ("NGC 3372", "Eta Carinae Nebula", "emission nebula", "10 45.1", "-59 52", 1.0, 120, "Car"),
    ("NGC 3532", "Wishing Well Cluster", "open cluster", "11 06.4", "-58 40", 3.0, 55, "Car"),
    ("NGC 4755", "Jewel Box", "open cluster", "12 53.6", "-60 20", 4.2, 10, "Cru"),
    ("NGC 104", "47 Tucanae", "globular cluster", "00 24.1", "-72 05", 4.0, 31, "Tuc"),
    ("NGC 292", "Small Magellanic Cloud", "galaxy", "00 52.7", "-72 50", 2.7, 320, "Tuc"),
    ("NGC 2070", "Tarantula Nebula", "emission nebula", "05 38.6", "-69 06", 8.0, 40, "Dor"),
)


def _hours(text: str) -> float:
    hour, minute = text.split()
    return int(hour) + float(minute) / 60.0


def _degrees(text: str) -> float:
    sign = -1.0 if text.lstrip().startswith("-") else 1.0
    degree, minute = text.replace("+", "").replace("-", "").split()
    return sign * (int(degree) + float(minute) / 60.0)


def _build() -> list[dict[str, Any]]:
    objects = []
    for identifier, name, kind, ra, dec, magnitude, size, constellation in _ROWS:
        objects.append({
            "id": identifier,
            "name": name or identifier,
            "type": kind,
            "ra": round(_hours(ra), 6),
            "dec": round(_degrees(dec), 5),
            "magnitude": magnitude,
            "size": size,
            "constellation": constellation,
            # What the search box matches against, lower-cased once up front.
            "search": " ".join(filter(None, (identifier, identifier.replace(" ", ""),
                                             name, constellation))).lower(),
        })
    return objects


OBJECTS: list[dict[str, Any]] = _build()


def listing() -> list[dict[str, Any]]:
    """The whole bundled catalog, without the search index."""
    return [{k: v for k, v in entry.items() if k != "search"} for entry in OBJECTS]


def search(query: str, limit: int = 30) -> list[dict[str, Any]]:
    """Bundled objects matching `query`, best (shortest, earliest) first."""
    needle = (query or "").strip().lower()
    if not needle:
        return []
    compact = needle.replace(" ", "")
    hits = []
    for entry in OBJECTS:
        position = entry["search"].find(needle)
        if position < 0:
            position = entry["search"].find(compact)
        if position >= 0:
            hits.append((position, len(entry["name"]), entry))
    hits.sort(key=lambda item: (item[0], item[1]))
    return [{k: v for k, v in entry.items() if k != "search"}
            for _, _, entry in hits[:limit]]


# ---------------------------------------------------------------------------
# Sesame, for everything the bundled table does not know
# ---------------------------------------------------------------------------

_SESAME = "https://cds.unistra.fr/cgi-bin/nph-sesame/-oI/SNV?"
_COORDS = re.compile(r"^%J\s+([-+0-9.]+)\s+([-+0-9.]+)", re.MULTILINE)
_OTYPE = re.compile(r"^%C\.0\s+(.+)$", re.MULTILINE)

_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_cache_lock = threading.Lock()
_CACHE_SECONDS = 3600.0


def resolve(name: str, timeout: float = 8.0) -> dict[str, Any] | None:
    """Look a name up at CDS Sesame.  Returns None when it is not known.

    Results are cached for an hour: object coordinates do not move, and the
    search box would otherwise hit the service on every keystroke.
    """
    query = (name or "").strip()
    if not query:
        return None

    key = query.lower()
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_SECONDS:
            return hit[1]

    result: dict[str, Any] | None = None
    try:
        url = _SESAME + urllib.parse.quote(query)
        request = urllib.request.Request(url, headers={"User-Agent": "Starfront"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
        match = _COORDS.search(body)
        if match:
            kind = _OTYPE.search(body)
            result = {
                "id": query,
                "name": query,
                "type": (kind.group(1).strip() if kind else "resolved"),
                "ra": round(float(match.group(1)) / 15.0, 6),
                "dec": round(float(match.group(2)), 5),
                "magnitude": None,
                "size": None,
                "constellation": "",
                "source": "sesame",
            }
    except Exception:                      # noqa: BLE001 - offline is a normal state here
        result = None

    with _cache_lock:
        _cache[key] = (now, result)
    return result


def lookup(query: str, limit: int = 30) -> dict[str, Any]:
    """Search the bundled catalog, then Sesame if nothing local matched."""
    local = search(query, limit)
    if local:
        return {"results": local, "source": "catalog"}
    remote = resolve(query)
    return {"results": [remote] if remote else [], "source": "sesame"}
