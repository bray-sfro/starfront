"""The upload rules: header reading, matching, and the real M31 cases."""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import uploadrules as u  # noqa: E402
from fitsfake import light_cards, write_fits, write_fz, write_xisf  # noqa: E402

STACK = Path("/workspace/collab/stack")
HDRS = STACK / "hdrs.jsonl"


def F(path, name=""):
    return u.facts_of_file(path, name)


# -- reading headers -------------------------------------------------------

def test_reads_plain_fits(tmp_path):
    f = F(write_fits(tmp_path / "a.fits", light_cards()))
    assert f["exposure"] == 300.0 and f["filter"] == "H" and f["gain"] == 100
    assert f["width"] == 64 and f["height"] == 48 and not f["truncated"]
    assert f["headerKind"] == "light" and f["cameraKey"] == "zwoasi2600mmpro"


def test_reads_fpack_extension_header(tmp_path):
    f = F(write_fz(tmp_path / "a.fits.fz", light_cards(FILTER="OIII")))
    assert f["format"] == "fits.fz" and f["filter"] == "O" and f["width"] == 64


def test_reads_xisf(tmp_path):
    f = F(write_xisf(tmp_path / "a.xisf", light_cards(FILTER="Blue", READOUTM="PhotoGraphic DSO")))
    assert f["format"] == "xisf" and f["filter"] == "B" and f["readout"] == "PhotoGraphic DSO"
    assert f["width"] == 64 and f["exposure"] == 300.0


def test_truncated_and_garbage(tmp_path):
    f = F(write_fits(tmp_path / "t.fits", light_cards(), truncate=3000))
    assert f["truncated"]
    st, why, _ = u.judge_light(dict(f, kind="light"), [], u.DEFAULTS)
    assert st == u.QUARANTINE and "shorter" in why[0]
    bad = tmp_path / "b.fits"
    bad.write_bytes(b"SIMPLE  =                    T" + b" " * 50)
    assert "error" in F(bad)
    xisf = tmp_path / "b.xisf"
    xisf.write_bytes(b"XISF0100" + (10 ** 9).to_bytes(4, "little") + b"\0" * 4 + b"<xisf/>")
    assert "error" in F(xisf)


def test_xisf_doctype_refused(tmp_path):
    xml = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><xisf>&a;</xisf>'
    p = tmp_path / "e.xisf"
    p.write_bytes(b"XISF0100" + len(xml).to_bytes(4, "little") + b"\0" * 4 + xml)
    assert "DOCTYPE" in F(p)["error"]


def test_exposure_from_name_when_header_has_none(tmp_path):
    f = F(write_fits(tmp_path / "MasterDark-300sec.fits", light_cards(IMAGETYP="DARK", EXPTIME=None)),
          "ASI2600MM-MasterDark-300sec.fits")
    assert f["exposure"] == 300.0 and f["exposureFrom"] == "name"


# -- judging ---------------------------------------------------------------

def mk(kind, fid, **cards):
    hdr = {"cards": {k: str(v) for k, v in light_cards(**cards).items()}, "width": 64, "height": 48}
    return {"id": fid, "kind": kind, "label": "", "meta": u.facts(hdr, fid)}


def judge(rows, **rules):
    return u.judge_all(rows, {**u.DEFAULTS, **rules})


@pytest.mark.parametrize("missing,word", [("EXPTIME", "exposure"), ("FILTER", "filter"),
                                          ("INSTRUME", "camera"), ("GAIN", "gain")])
def test_light_missing_required_header_is_quarantined(missing, word):
    over = {missing: None}
    if missing == "EXPTIME":
        over["EXPOSURE"] = None
    r = judge([mk("light", "l1", **over)])
    assert r["l1"][0] == u.QUARANTINE and word in r["l1"][1][0]


def test_osc_without_filter_is_fine():
    rows = [mk("light", "l", FILTER=None, BAYERPAT="RGGB"), mk("flat", "f", IMAGETYP="FLAT", FILTER=None, BAYERPAT="RGGB"),
            mk("bias", "b", IMAGETYP="BIAS")]
    assert judge(rows)["l"][0] == u.READY


def test_pending_until_calibration_arrives():
    light = mk("light", "l")
    assert judge([light])["l"][0] == u.PENDING
    rows = [light, mk("flat", "f", IMAGETYP="FLAT", EXPTIME=2.0)]
    st, why = judge(rows)["l"]
    assert st == u.PENDING and len(why) == 1 and "darks at 300 s" in why[0]
    rows.append(mk("dark", "d", IMAGETYP="DARK"))
    assert judge(rows)["l"] == (u.READY, [])


def test_dark_tolerances():
    base = [mk("light", "l"), mk("flat", "f", IMAGETYP="FLAT")]
    assert judge(base + [mk("dark", "d", IMAGETYP="DARK", EXPTIME=301.0)])["l"][0] == u.READY
    assert judge(base + [mk("dark", "d", IMAGETYP="DARK", EXPTIME=320.0)])["l"][0] == u.PENDING
    assert judge(base + [mk("dark", "d", IMAGETYP="DARK", EXPTIME=320.0)],
                 darkExposureTolerancePct=10)["l"][0] == u.READY
    assert judge(base + [mk("dark", "d", IMAGETYP="DARK", **{"CCD-TEMP": -6.0})])["l"][0] == u.PENDING
    assert judge(base + [mk("dark", "d", IMAGETYP="DARK", **{"CCD-TEMP": -8.0})])["l"][0] == u.READY
    assert judge(base + [mk("dark", "d", IMAGETYP="DARK", GAIN=0)])["l"][0] == u.PENDING
    assert judge(base + [mk("dark", "d", IMAGETYP="DARK", OFFSET=10)])["l"][0] == u.PENDING
    assert judge(base + [mk("dark", "d", IMAGETYP="DARK", XBINNING=2, YBINNING=2)])["l"][0] == u.PENDING
    assert judge(base + [mk("dark", "d", IMAGETYP="DARK", INSTRUME="QHY268M")])["l"][0] == u.PENDING


def test_readout_mode_mismatch():
    rows = [mk("light", "l", READOUTM="Low Conversion Gain"), mk("flat", "f", IMAGETYP="FLAT"),
            mk("dark", "d", IMAGETYP="DARK", READOUTM="High Conversion Gain")]
    st, why = judge(rows)["l"]
    assert st == u.PENDING and "Low Conversion Gain" in why[0]
    assert judge(rows, matchReadoutMode=False)["l"][0] == u.READY


def test_flat_filter_and_rotator():
    rows = [mk("light", "l", ROTATOR=179.0), mk("dark", "d", IMAGETYP="DARK"),
            mk("flat", "f", IMAGETYP="FLAT", FILTER="OIII", ROTATOR=179.0)]
    assert "filter Ha" in judge(rows)["l"][1][0]
    rows[2] = mk("flat", "f", IMAGETYP="FLAT", ROTATOR=79.0)
    assert "rotator 179" in judge(rows)["l"][1][0]
    assert judge(rows, flatRotatorToleranceDeg=None)["l"][0] == u.READY
    rows[2] = mk("flat", "f", IMAGETYP="FLAT", ROTATOR=180.5)
    assert judge(rows)["l"][0] == u.READY


def test_bias_policy():
    rows = [mk("light", "l"), mk("flat", "f", IMAGETYP="FLAT"), mk("bias", "b", IMAGETYP="BIAS", EXPTIME=0.0)]
    st, why = judge(rows)["l"]
    assert st == u.READY and "bias" in why[0]
    assert judge(rows, darkPolicy="dark")["l"][0] == u.PENDING
    assert judge(rows[:1], darkPolicy="none", requireFlats=False)["l"][0] == u.READY


def test_unrecorded_settings_on_masters():
    master = mk("dark", "d", IMAGETYP="Master Dark", GAIN=None, OFFSET=None, **{"CCD-TEMP": None, "SET-TEMP": None})
    rows = [mk("light", "l"), mk("flat", "f", IMAGETYP="FLAT"), master]
    st, why = judge(rows)["l"]
    assert st == u.READY and "does not record" in why[0]
    assert judge(rows, allowUnrecorded=False)["l"][0] == u.PENDING


def test_calibration_problems_and_kind_conflicts():
    r = judge([mk("dark", "d", IMAGETYP="DARK", EXPTIME=None, EXPOSURE=None)])
    assert r["d"][0] == u.QUARANTINE
    r = judge([mk("flat", "f", IMAGETYP="FLAT", FILTER=None)])
    assert r["f"][0] == u.QUARANTINE
    r = judge([mk("dark", "d", IMAGETYP="LIGHT")])         # Astronebulae's darks
    assert r["d"][0] == u.READY and "kept as dark" in r["d"][1][0]
    r = judge([mk("light", "l", IMAGETYP="Dark Frame")])
    assert r["l"][0] == u.QUARANTINE and "dark box" in r["l"][1][0]


def test_quarantined_calibration_calibrates_nothing():
    rows = [mk("light", "l"), mk("flat", "f", IMAGETYP="FLAT"),
            mk("dark", "d", IMAGETYP="DARK", EXPTIME=None, EXPOSURE=None)]
    assert judge(rows)["l"][0] == u.PENDING


def test_clean_rules():
    assert u.clean_rules({"temperatureToleranceC": "5"}) == {"temperatureToleranceC": 5.0}
    assert u.clean_rules({"flatRotatorToleranceDeg": None}) == {"flatRotatorToleranceDeg": None}
    for bad in ({"nope": 1}, {"darkPolicy": "maybe"}, {"enforce": "yes"},
                {"temperatureToleranceC": -1}, {"temperatureToleranceC": None}, []):
        with pytest.raises(ValueError):
            u.clean_rules(bad)


def test_checklist_follows_rules():
    text = " ".join(u.checklist(u.DEFAULTS))
    assert "Flats for every filter" in text and "or bias" in text
    text = " ".join(u.checklist({**u.DEFAULTS, "darkPolicy": "dark", "requireFlats": False}))
    assert "Flats" not in text and "or bias" not in text


# -- the real M31 collaboration --------------------------------------------

def _real_rows():
    rows = collections.defaultdict(list)
    for line in HDRS.open():
        r = json.loads(line)
        if r["project"] != "d07d82eb861e":
            continue
        h = r["h"]
        hdr = {"cards": h, "format": r["name"].rsplit(".", 1)[-1],
               "width": u._int(h.get("NAXIS1")), "height": u._int(h.get("NAXIS2"))}
        rows[r["uname"]].append({"id": r["id"], "kind": r["kind"], "label": r["telescope"],
                                 "name": r["name"], "meta": u.facts(hdr, r["name"])})
    return rows


def _tally(rows, rules=None):
    verdicts = u.judge_all(rows, {**u.DEFAULTS, **(rules or {})})
    lights = [r for r in rows if r["kind"] == "light"]
    count = collections.Counter(verdicts[r["id"]][0] for r in lights)
    why = collections.Counter(x for r in lights for x in verdicts[r["id"]][1]
                              if verdicts[r["id"]][0] != u.READY)
    return count, why, verdicts


real = pytest.mark.skipif(not HDRS.exists(), reason="the M31 header dump is not on this machine")


@real
def test_real_astronebulae_readout_and_exposure_mismatch():
    count, why, verdicts = _tally(_real_rows()["Astronebulae"])
    assert count == {u.READY: 270, u.PENDING: 12}
    lcg = sum(n for w, n in why.items() if "Low Conversion Gain" in w)
    assert lcg == 3                                      # the 3 LCG subs dropped in the coadd
    odd = sum(n for w, n in why.items() if "480 s" in w or "400 s" in w)
    assert odd == 9                                      # 8 at 480 s + 1 at 400 s, no dark


@real
def test_real_eisenhorn_no_bias_or_dark():
    count, why, _ = _tally(_real_rows()["Inq. Eisenhorn"])
    assert count == {u.PENDING: 37}
    assert sum(n for w, n in why.items() if "rotator 179" in w) == 3


@real
def test_real_steve_no_long_darks():
    rows = _real_rows()["Steve G."]
    count, _, verdicts = _tally(rows)
    assert count == {u.READY: 313}                       # bias + flats, as stacked
    count, why, _ = _tally(rows, {"darkPolicy": "dark"})
    assert count == {u.PENDING: 313}


@real
def test_real_abc_has_no_calibration():
    count, _, _ = _tally(_real_rows()["abc12345"])
    assert count == {u.PENDING: 238}


@real
@pytest.mark.parametrize("who", ["ZaphodBeeblebrox", "des (UK) - B6-B1"])
def test_real_masters_without_settings_still_match(who):
    count, _, _ = _tally(_real_rows()[who])
    assert set(count) == {u.READY}


TWIN = STACK / "full/lists/dbx_twin_m31.json"
TWIN_CAL = STACK / "ha-trial/twin_cal_listing.json"
SAMPLES = STACK / "bray_hdr_samples.json"


@pytest.mark.skipif(not (TWIN.exists() and TWIN_CAL.exists() and SAMPLES.exists()),
                    reason="the Twin FSQ listings are not on this machine")
def test_real_twin_no_l_flats_no_300s_darks():
    """Twin FSQ as it sits in Dropbox: real light names, real calibration
    folder listing, headers from a real Twin sub with the frame type changed."""
    base = next(s["h"] for s in json.loads(SAMPLES.read_text()) if "Twin" in s["path"])
    names = sorted({e["name"] for e in json.loads(TWIN.read_text())["entries"]
                    if e.get("object_type") == "file" and e["name"].endswith(".fits")})
    rows = []
    for name in names:
        m = re.search(r"_([A-Z])_(\d+)s_", name)
        if not m:
            continue
        rows.append({"id": name, "kind": "light", "label": "",
                     "meta": u.facts({"cards": dict(base, FILTER=m.group(1), EXPTIME=m.group(2)),
                                      "width": 9576, "height": 6388}, name)})
    for e in json.loads(TWIN_CAL.read_text())["entries"]:
        if e["object_type"] != "file":
            continue
        part = e["path"].split("/Calibration/")[1]
        kind = {"Bias": "bias", "Darks": "dark", "Flats": "flat"}[part.split("/")[0]]
        cards = dict(base, IMAGETYP=kind.upper(), EXPTIME={"bias": "0", "dark": "600"}.get(kind, "1"))
        if kind == "flat":
            cards["FILTER"] = part.split("/")[2]
        rows.append({"id": e["path"], "kind": kind, "label": "",
                     "meta": u.facts({"cards": cards, "width": 9576, "height": 6388}, e["name"])})
    count, why, verdicts = _tally(rows)
    lights = [r for r in rows if r["kind"] == "light"]
    by = collections.Counter((r["meta"]["filter"], verdicts[r["id"]][0]) for r in lights)
    assert by[("L", u.PENDING)] == 15 and not by[("L", u.READY)]       # no L flats
    assert all(st == u.READY for (f, st) in by if f != "L")              # 300 s via bias
    count, why, verdicts = _tally(rows, {"darkPolicy": "dark"})
    by = collections.Counter((r["meta"]["filter"], r["meta"]["exposure"], verdicts[r["id"]][0])
                             for r in lights)
    assert not any(st == u.READY for (f, e, st) in by if e == 300.0)    # no 300 s darks
    assert any(st == u.READY for (f, e, st) in by if e == 600.0)


# -- real files on disk -----------------------------------------------------

REAL_FILES = sorted(list((STACK / "full/quarantine/OIII/astronebulae").glob("*.fits"))
                    + list((STACK / "full/quarantine/OIII/fsq106").glob("*.fit*"))[:10]
                    + list((STACK / "ha-trial/rigs/astronebulae").glob("**/*.fit*"))[:6]
                    + list((STACK / "full/cal/steve").glob("**/*.xisf"))[:4])


@pytest.mark.skipif(not REAL_FILES, reason="no real frames on this machine")
@pytest.mark.parametrize("path", REAL_FILES, ids=lambda p: p.name[:40])
def test_real_files_read_like_astropy(path):
    astropy = pytest.importorskip("astropy.io.fits")
    f = u.facts_of_file(path)
    assert "error" not in f, f.get("error")
    if path.suffix == ".xisf":
        assert f["format"] == "xisf"
        return
    with astropy.open(path) as hdul:
        h = hdul[1].header if path.name.endswith(".fz") else hdul[0].header
    if "EXPTIME" in h:
        assert f["exposure"] == pytest.approx(float(h["EXPTIME"]))
    if "FILTER" in h:
        assert f["filterRaw"] == str(h["FILTER"]).strip()
    if "GAIN" in h:
        assert f["gain"] == pytest.approx(float(h["GAIN"]))
    assert f["width"] == h.get("NAXIS1")
