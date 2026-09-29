#!/usr/bin/env python3
"""Turn the Yale Bright Star Catalogue into the star table the sky chart draws.

The planetarium renders its own chart rather than streaming survey imagery, so
it needs the stars locally.  BSC5 is the right size for this: 9110 entries down
to about magnitude 6.5, which is roughly what a dark sky shows the naked eye and
far more than a chart ever needs to label.

Run this to regenerate `astrocontrol/web/vendor/stars.json`:

    python tools/build_star_catalog.py

It needs an internet connection once; the result is committed and the
application never downloads anything.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import sys
import urllib.request
from pathlib import Path

SOURCE = "http://tdc-www.harvard.edu/catalogs/bsc5.dat.gz"
OUTPUT = Path(__file__).resolve().parent.parent / "astrocontrol" / "web" / "vendor" / "stars.json"

# Column positions from the BSC5 byte-by-byte description (1-based in the docs,
# so each is one less here).
COLUMNS = {
    "hr": (0, 4),
    "name": (4, 14),        # Flamsteed[0:3] Bayer[3:6] superscript[6] constellation[7:10]
    "ra": (75, 83),         # HHMMSS.S, J2000
    "dec": (83, 90),        # sDDMMSS, J2000
    "vmag": (102, 107),
    "bv": (109, 114),
    "spectral": (127, 147),
}

# Proper names, keyed by Bayer designation as BSC5 writes it.  Deliberately kept
# to names that are actually used at a telescope: a chart with every catalogued
# name on it is unreadable, and a wrong label is worse than none.
PROPER_NAMES = {
    "Alp And": "Alpheratz", "Bet And": "Mirach", "Gam And": "Almach",
    "Alp Aql": "Altair", "Gam Aql": "Tarazed", "Zet Aql": "Okab",
    "Alp Aqr": "Sadalmelik", "Bet Aqr": "Sadalsuud", "Del Aqr": "Skat",
    "Alp Ari": "Hamal", "Bet Ari": "Sheratan", "Gam Ari": "Mesarthim",
    "Alp Aur": "Capella", "Bet Aur": "Menkalinan", "The Aur": "Mahasim",
    "Alp Boo": "Arcturus", "Bet Boo": "Nekkar", "Gam Boo": "Seginus",
    "Eps Boo": "Izar", "Eta Boo": "Muphrid",
    "Alp CMa": "Sirius", "Bet CMa": "Mirzam", "Del CMa": "Wezen",
    "Eps CMa": "Adhara", "Eta CMa": "Aludra",
    "Alp CMi": "Procyon", "Bet CMi": "Gomeisa",
    "Alp CVn": "Cor Caroli", "Bet CVn": "Chara",
    "Alp Cae": "", "Alp Cap": "Algedi", "Bet Cap": "Dabih",
    "Del Cap": "Deneb Algedi", "Gam Cap": "Nashira",
    "Alp Car": "Canopus", "Bet Car": "Miaplacidus", "Eps Car": "Avior",
    "Iot Car": "Aspidiske",
    "Alp Cas": "Schedar", "Bet Cas": "Caph", "Gam Cas": "Navi",
    "Del Cas": "Ruchbah", "Eps Cas": "Segin",
    "Alp Cen": "Rigil Kentaurus", "Bet Cen": "Hadar", "Gam Cen": "Muhlifain",
    "The Cen": "Menkent",
    "Alp Cep": "Alderamin", "Bet Cep": "Alfirk", "Gam Cep": "Errai",
    "Alp Cet": "Menkar", "Bet Cet": "Diphda", "Gam Cet": "Kaffaljidhma",
    "Omi Cet": "Mira", "Zet Cet": "Baten Kaitos",
    "Alp CrB": "Alphecca", "Bet CrB": "Nusakan",
    "Alp Crv": "Alchiba", "Bet Crv": "Kraz", "Gam Crv": "Gienah",
    "Del Crv": "Algorab",
    "Alp Cru": "Acrux", "Bet Cru": "Mimosa", "Gam Cru": "Gacrux",
    "Del Cru": "Imai",
    "Alp Cnc": "Acubens", "Bet Cnc": "Altarf", "Gam Cnc": "Asellus Borealis",
    "Del Cnc": "Asellus Australis",
    "Alp Cyg": "Deneb", "Bet Cyg": "Albireo", "Gam Cyg": "Sadr",
    "Del Cyg": "Fawaris", "Eps Cyg": "Aljanah",
    "Alp Dra": "Thuban", "Bet Dra": "Rastaban", "Gam Dra": "Eltanin",
    "Alp Eri": "Achernar", "Bet Eri": "Cursa", "The Eri": "Acamar",
    "Gam Eri": "Zaurak",
    "Alp Gem": "Castor", "Bet Gem": "Pollux", "Gam Gem": "Alhena",
    "Del Gem": "Wasat", "Eps Gem": "Mebsuta", "Eta Gem": "Propus",
    "Mu  Gem": "Tejat", "Xi  Gem": "Alzirr",
    "Alp Gru": "Alnair", "Bet Gru": "Tiaki",
    "Alp Her": "Rasalgethi", "Bet Her": "Kornephoros", "Del Her": "Sarin",
    "Alp Hya": "Alphard",
    "Alp Leo": "Regulus", "Bet Leo": "Denebola", "Gam Leo": "Algieba",
    "Del Leo": "Zosma", "The Leo": "Chertan", "Zet Leo": "Adhafera",
    "Mu  Leo": "Rasalas",
    "Alp Lep": "Arneb", "Bet Lep": "Nihal",
    "Alp Lib": "Zubenelgenubi", "Bet Lib": "Zubeneschamali",
    "Alp Lyr": "Vega", "Bet Lyr": "Sheliak", "Gam Lyr": "Sulafat",
    "Alp Oph": "Rasalhague", "Bet Oph": "Cebalrai", "Eta Oph": "Sabik",
    "Alp Ori": "Betelgeuse", "Bet Ori": "Rigel", "Gam Ori": "Bellatrix",
    "Del Ori": "Mintaka", "Eps Ori": "Alnilam", "Zet Ori": "Alnitak",
    "Kap Ori": "Saiph", "Iot Ori": "Hatysa",
    "Alp Pav": "Peacock",
    "Alp Peg": "Markab", "Bet Peg": "Scheat", "Gam Peg": "Algenib",
    "Eps Peg": "Enif",
    "Alp Per": "Mirfak", "Bet Per": "Algol",
    "Alp Phe": "Ankaa",
    "Alp Psc": "Alrescha", "Eta Psc": "Kullat Nunu",
    "Alp PsA": "Fomalhaut",
    "Alp Pup": "", "Zet Pup": "Naos", "Rho Pup": "Tureis",
    "Alp Sco": "Antares", "Bet Sco": "Acrab", "Del Sco": "Dschubba",
    "Eps Sco": "Larawag", "The Sco": "Sargas", "Lam Sco": "Shaula",
    "Kap Sco": "Girtab", "Ups Sco": "Lesath",
    "Alp Ser": "Unukalhai",
    "Alp Sgr": "Rukbat", "Bet Sgr": "Arkab", "Gam Sgr": "Alnasl",
    "Del Sgr": "Kaus Media", "Eps Sgr": "Kaus Australis",
    "Lam Sgr": "Kaus Borealis", "Sig Sgr": "Nunki", "Zet Sgr": "Ascella",
    "Pi  Sgr": "Albaldah",
    "Alp Tau": "Aldebaran", "Bet Tau": "Elnath", "Eta Tau": "Alcyone",
    "Eps Tau": "Ain",
    "Alp TrA": "Atria",
    "Alp UMa": "Dubhe", "Bet UMa": "Merak", "Gam UMa": "Phecda",
    "Del UMa": "Megrez", "Eps UMa": "Alioth", "Zet UMa": "Mizar",
    "Eta UMa": "Alkaid", "Iot UMa": "Talitha",
    "Alp UMi": "Polaris", "Bet UMi": "Kochab", "Gam UMi": "Pherkad",
    "Alp Vir": "Spica", "Gam Vir": "Porrima", "Eps Vir": "Vindemiatrix",
    "Zet Vir": "Heze", "Del Vir": "Auva", "Bet Vir": "Zavijava",
    "Del Vel": "Alsephina", "Gam Vel": "Regor", "Lam Vel": "Suhail",
    "Kap Vel": "Markeb",
}


def parse_line(line: str) -> dict | None:
    """One BSC5 record, or None for the handful with no position."""
    def field(key: str) -> str:
        start, end = COLUMNS[key]
        return line[start:end]

    ra_text, dec_text = field("ra").strip(), field("dec").strip()
    if not ra_text or not dec_text:
        return None                     # novae and lost objects carry no position
    try:
        ra_hours = (int(ra_text[0:2]) + int(ra_text[2:4]) / 60.0
                    + float(ra_text[4:]) / 3600.0)
        sign = -1.0 if dec_text[0] == "-" else 1.0
        dec = sign * (int(dec_text[1:3]) + int(dec_text[3:5]) / 60.0
                      + int(dec_text[5:7]) / 3600.0)
        magnitude = float(field("vmag"))
    except ValueError:
        return None

    try:
        colour = float(field("bv"))
    except ValueError:
        colour = 0.0                    # unmeasured: draw it white

    name = field("name")
    flamsteed = name[0:3].strip()
    bayer = name[3:6].strip()
    superscript = name[6:7].strip()
    constellation = name[7:10].strip()

    designation = ""
    if bayer:
        designation = f"{bayer}{superscript} {constellation}".strip()
    elif flamsteed and constellation:
        designation = f"{flamsteed} {constellation}"

    # PROPER_NAMES is keyed on the three-character Bayer code as BSC5 pads it,
    # so "Mu Gem" is stored and looked up as "Mu  Gem".
    proper = PROPER_NAMES.get(f"{bayer:<3} {constellation}", "") if bayer else ""

    return {
        "ra": round(ra_hours * 15.0, 4),
        "dec": round(dec, 4),
        "mag": round(magnitude, 2),
        "bv": round(colour, 2),
        "proper": proper,
        "designation": designation,
        "constellation": constellation,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=SOURCE, help="BSC5 URL or local .gz path")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--limit", type=float, default=6.5,
                        help="faintest magnitude to keep")
    args = parser.parse_args()

    if args.source.startswith(("http://", "https://")):
        print(f"downloading {args.source}")
        raw = urllib.request.urlopen(args.source, timeout=120).read()
    else:
        raw = Path(args.source).read_bytes()
    text = gzip.decompress(raw).decode("latin-1")

    stars = []
    named = 0
    for line in text.splitlines():
        entry = parse_line(line)
        if entry is None or entry["mag"] > args.limit:
            continue
        if entry["proper"]:
            named += 1
        # Positional rows keep the file small: 9000 objects as JSON objects with
        # seven keys each would be several megabytes of repeated key names.
        stars.append([entry["ra"], entry["dec"], entry["mag"], entry["bv"],
                      entry["proper"], entry["designation"]])

    stars.sort(key=lambda row: row[2])          # brightest first, so labels win ties
    payload = {
        "source": args.source,
        "generated": dt.date.today().isoformat(),
        "fields": ["ra", "dec", "mag", "bv", "proper", "designation"],
        "note": "RA and Dec are J2000 degrees. Yale Bright Star Catalogue, 5th ed.",
        "stars": stars,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, separators=(",", ":")), "utf-8")
    size = args.output.stat().st_size
    print(f"wrote {len(stars)} stars ({named} with proper names) "
          f"to {args.output} — {size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
