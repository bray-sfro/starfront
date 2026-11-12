"""tools/import_historical.py: a server-side import gives the same row as the
rig's own auto-upload, and keeps the page's PUT rules."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tools"))

from server import agentuploads  # noqa: E402
from server.agentuploads import AgentUploads  # noqa: E402
from server.files import Files  # noqa: E402
from server.store import Store  # noqa: E402
from fitsfake import light_cards, write_fits  # noqa: E402
import import_historical as imp  # noqa: E402

RIG = {"authorization": "Bearer rig-a"}
M31 = {"ra": 10.68, "dec": 41.27, "width": 3.0, "height": 2.0}
COLS = ["rig", "kind", "filter", "night", "size", "target_name", "url", "src_path"]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(agentuploads, "AGENT_KEEP_FREE", 0)
    monkeypatch.setattr(imp, "URL_SCHEMES", ("https://", "file://"))
    db = tmp_path / "db.sqlite"
    store = Store(db)
    app = FastAPI()
    owns = lambda who, project: project.get("owner_id") == who["id"]  # noqa: E731
    files = Files(app, store, tmp_path / "files", lambda t: None, owns, [], "http://t", lambda: "c")
    up = AgentUploads(app, store, files)
    store.upsert_user("u-bray", "web-x", "astrofalls", "", [])
    store.add_agent("aaaaaaaa1111", "rig-a", "Telescope 1", "astrofalls", "u-bray")
    store.add_agent("bbbbbbbb2222", "rig-b", "vsd100", "astrofalls", "u-bray")
    store.add_project("p1", "M31", "Bray", {"collectFiles": True, "autoUpload": True,
                                            "region": M31, "fileQuotaGB": 10}, owner_id="u-bray")
    for i, agent in enumerate(("aaaaaaaa1111", "bbbbbbbb2222")):
        store.add_task({"id": f"t{i}", "project": "p1", "agent": agent, "state": "accepted",
                        "version": 1, "issued": time.time()})
    (tmp_path / "src").mkdir()
    return {"client": TestClient(app), "store": store, "files": files, "tmp": tmp_path, "db": db}


def frame(w, name, **cards):
    cards.setdefault("PROJID", "p1")
    return write_fits(w["tmp"] / "src" / name, light_cards(**cards), width=256, height=200)


def agent_send(w, name, blob, kind="light"):
    client = w["client"]
    sha = hashlib.sha256(blob).hexdigest()
    r = client.post("/api/v1/agent/uploads", headers=RIG,
                    json={"project": "p1", "name": name, "kind": kind, "bytes": len(blob), "sha256": sha})
    body = r.json()
    if body.get("already"):
        return body
    client.put(f"/api/v1/agent/uploads/{body['id']}?offset=0", content=blob, headers=RIG)
    return client.post(f"/api/v1/agent/uploads/{body['id']}/finish", headers=RIG).json()


def manifest(w, rows, name="m.csv"):
    path = w["tmp"] / name
    with open(path, "w", newline="") as h:
        out = csv.DictWriter(h, fieldnames=COLS)
        out.writeheader()
        for r in rows:
            out.writerow({c: r.get(c, "") for c in COLS})
    return path


def row_for(path: Path, rig="aaaaaaaa", kind="light", night="2026-10-19", name=None, how="src"):
    r = {"rig": rig, "kind": kind, "filter": "Ha", "night": night, "size": path.stat().st_size,
         "target_name": name or path.name}
    if how == "src":
        r["src_path"] = str(path)
    else:
        r["url"] = path.as_uri()
    return r


def run(w, path, *extra):
    return imp.main(["--manifest", str(path), "--project", "p1", "--user", "u-bray",
                     "--db", str(w["db"]), "--files", str(w["tmp"] / "files"),
                     "--state-dir", str(w["tmp"] / "state"), "--margin-gb", "0",
                     "--no-agent-floor", "--rate", "100M", *extra])


SAME = ("kind", "telescope", "agent", "task", "status", "reasons", "meta")


def strip(row):
    row = dict(row)
    meta = json.loads(row["meta"])
    return {k: (meta if k == "meta" else row[k]) for k in SAME} | {"bytes": row["bytes"]}


def test_import_gives_the_same_row_as_the_rigs_own_upload(world):
    w = world
    a = frame(w, "M31_Ha_0001.fits", OBJECT="A")
    b = frame(w, "M31_Ha_0002.fits", OBJECT="B")
    c = frame(w, "M31_Ha_0003.fits", OBJECT="C")
    assert agent_send(w, a.name, a.read_bytes())["name"] == "aaaaaaaa-M31_Ha_0001.fits"
    code = run(w, manifest(w, [row_for(b), row_for(c, rig="Telescope 1", how="url")]))
    assert code == 0
    s = w["store"]
    ra = s.file("p1", "u-bray", "aaaaaaaa-M31_Ha_0001.fits", "light")
    rb = s.file("p1", "u-bray", "aaaaaaaa-M31_Ha_0002.fits", "light")
    rc = s.file("p1", "u-bray", "aaaaaaaa-M31_Ha_0003.fits", "light")
    assert strip(rb) == strip(ra) == strip(rc)
    assert rb["sha256"] == hashlib.sha256(b.read_bytes()).hexdigest()
    assert rc["sha256"] == hashlib.sha256(c.read_bytes()).hexdigest()
    assert rb["status"] == "pending" and rb["agent"] == "aaaaaaaa1111" and rb["task"] == "t0"
    assert w["files"].path_of("p1", "u-bray", rb["name"]).read_bytes() == b.read_bytes()
    assert b.exists()                      # copied, not moved, without --move-src
    assert not list((w["tmp"] / "files").rglob("*.part"))
    state = json.loads((w["tmp"] / "state" / "state.json").read_text())
    assert sorted(v["status"] for v in state.values()) == ["done", "done"]


def test_rerun_skips_without_fetching_and_hash_dedupes(world):
    w = world
    b = frame(w, "M31_Ha_0002.fits")
    assert run(w, manifest(w, [row_for(b)])) == 0
    gone = row_for(b, how="url")
    gone["url"] = "file:///nonexistent/never-read.fits"      # would fail if fetched
    assert run(w, manifest(w, [gone])) == 0
    # The same bytes under another name: not stored twice.
    assert run(w, manifest(w, [row_for(b, name="renamed.fits")])) == 0
    assert [r["name"] for r in w["store"].files_in("p1")] == ["aaaaaaaa-M31_Ha_0002.fits"]
    assert not list((w["tmp"] / "files").rglob("*.part"))


def test_name_from_another_night_is_a_collision_not_a_skip(world):
    w = world
    b = frame(w, "L_0001.fits", **{"DATE-OBS": "2026-11-05T03:00:00"})
    assert run(w, manifest(w, [row_for(b, night="2026-11-04")])) == 0
    older = frame(w, "old.fits", **{"DATE-OBS": "2026-10-12T03:00:00"})
    assert run(w, manifest(w, [row_for(older, night="2026-10-11", name="L_0001.fits")])) == 1
    state = json.loads((w["tmp"] / "state" / "state.json").read_text())
    assert "name collision" in state["aaaaaaaa1111|light|aaaaaaaa-L_0001.fits"]["reason"]


def test_duplicate_names_in_manifest_are_refused(world):
    w = world
    b = frame(w, "x.fits")
    assert run(w, manifest(w, [row_for(b), row_for(b)])) == 2
    assert w["store"].files_in("p1") == []


def test_dry_run_writes_nothing(world):
    w = world
    b = frame(w, "x.fits")
    before = w["db"].read_bytes()
    assert run(w, manifest(w, [row_for(b)]), "--dry-run") == 0
    assert w["store"].files_in("p1") == [] and not (w["tmp"] / "state").exists()
    assert w["db"].read_bytes() == before


def test_disk_floor_stops_cleanly(world):
    w = world
    b = frame(w, "x.fits")
    code = imp.main(["--manifest", str(manifest(w, [row_for(b)])), "--project", "p1",
                     "--user", "u-bray", "--db", str(w["db"]), "--files", str(w["tmp"] / "files"),
                     "--state-dir", str(w["tmp"] / "state"), "--margin-gb", "100000"])
    assert code == 3 and w["store"].files_in("p1") == []
    assert not list((w["tmp"] / "files").rglob("*.part"))


def test_limit_and_resume(world):
    w = world
    frames = [frame(w, f"M31_Ha_{i:04d}.fits", OBJECT=str(i)) for i in range(4)]
    m = manifest(w, [row_for(f) for f in frames])
    assert run(w, m, "--limit", "1") == 0
    assert len(w["store"].files_in("p1")) == 1
    assert run(w, m) == 0
    assert len(w["store"].files_in("p1")) == 4


def test_moved_source_goes_back_when_refused(world):
    w = world
    b = frame(w, "x.fits", PROJID="someother")
    staged = w["tmp"] / "files" / "_staging" / "x.fits"
    staged.parent.mkdir(parents=True)
    shutil.move(b, staged)
    assert run(w, manifest(w, [row_for(staged)]), "--move-src") == 1
    assert staged.is_file() and w["store"].files_in("p1") == []
    ok = frame(w, "y.fits")
    staged2 = staged.parent / "y.fits"
    shutil.move(ok, staged2)
    assert run(w, manifest(w, [row_for(staged2)], "m2.csv"), "--move-src") == 0
    assert not staged2.exists() and len(w["store"].files_in("p1")) == 1


def test_calibration_releases_lights_and_test_removal(world):
    w = world
    light = frame(w, "M31_Ha_0001.fits")
    flat = write_fits(w["tmp"] / "src" / "FLAT_Ha.fits",
                      light_cards(IMAGETYP="FLAT", EXPTIME=1.0, PROJID=None), width=256, height=200)
    dark = write_fits(w["tmp"] / "src" / "DARK_300.fits",
                      light_cards(IMAGETYP="DARK", FILTER=None, PROJID=None), width=256, height=200)
    assert run(w, manifest(w, [row_for(light)])) == 0
    assert w["store"].file("p1", "u-bray", "aaaaaaaa-M31_Ha_0001.fits")["status"] == "pending"
    assert run(w, manifest(w, [row_for(flat, kind="flat"), row_for(dark, kind="dark")], "c.csv")) == 0
    assert w["store"].file("p1", "u-bray", "aaaaaaaa-M31_Ha_0001.fits")["status"] == "ready"
    # Only names with the test mark can be removed by the tool.
    base = ["--project", "p1", "--user", "u-bray", "--db", str(w["db"]),
            "--files", str(w["tmp"] / "files")]
    assert imp.main(base + ["--remove-test", "aaaaaaaa-M31_Ha_0001.fits"]) == 2
    t = frame(w, "IMPORTTEST_x.fits", OBJECT="t")
    assert run(w, manifest(w, [row_for(t)], "t.csv")) == 0
    assert imp.main(base + ["--remove-test", "aaaaaaaa-IMPORTTEST_x.fits"]) == 0
    assert w["store"].file("p1", "u-bray", "aaaaaaaa-IMPORTTEST_x.fits") is None
    assert not w["files"].path_of("p1", "u-bray", "aaaaaaaa-IMPORTTEST_x.fits").exists()
