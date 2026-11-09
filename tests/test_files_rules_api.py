"""The files routes under the upload rules, end to end on a bare app."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.files import Files  # noqa: E402
from server.store import Store  # noqa: E402
from fitsfake import light_cards, write_fits  # noqa: E402

PEOPLE = {"owner-token": {"kind": "user", "id": "u-owner", "name": "Bray", "roles": [], "admin": False},
          "alice-token": {"kind": "user", "id": "u-alice", "name": "Alice", "roles": [], "admin": False}}


@pytest.fixture()
def world(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    app = FastAPI()
    owns = lambda who, project: who["admin"] or project.get("owner_id") == who["id"]  # noqa: E731
    files = Files(app, store, tmp_path / "files", PEOPLE.get, owns, [], "http://test", lambda: "c")
    store.add_project("p1", "M31 test", "Bray", {"collectFiles": True, "region": {}}, owner_id="u-owner")
    return TestClient(app), store, tmp_path, files


def put(client, tmp, name, kind, token="alice-token", **cards):
    path = write_fits(tmp / name, light_cards(**cards))
    return client.put(f"/api/v1/projects/p1/files/{name}?kind={kind}&telescope=RC8",
                      content=path.read_bytes(), headers={"authorization": f"Bearer {token}"})


def test_light_waits_for_calibration_then_counts(world):
    client, store, tmp, _ = world
    r = put(client, tmp, "L_0001.fits", "light")
    assert r.status_code == 200
    f = r.json()["file"]
    assert f["status"] == "pending" and any("flats in filter Ha" in x for x in f["reasons"])
    assert r.json()["myLights"] == {"pending": 1}
    r = put(client, tmp, "flat_0001.fits", "flat", IMAGETYP="FLAT", EXPTIME=2.0)
    assert r.json()["file"]["status"] == "ready"
    r = put(client, tmp, "dark_0001.fits", "dark", IMAGETYP="DARK")
    assert r.json()["moved"]["ready"] == 1
    listing = client.get("/api/v1/projects/p1/files", headers={"authorization": "Bearer alice-token"}).json()
    light = next(x for x in listing["files"] if x["kind"] == "light")
    assert light["status"] == "ready" and listing["myLights"] == {"ready": 1}
    assert listing["gaps"] == [] and listing["rules"]["checklist"]
    # Take the dark off again: the light goes back to waiting.
    r = client.delete("/api/v1/projects/p1/files/dark_0001.fits?kind=dark",
                      headers={"authorization": "Bearer alice-token"})
    assert r.json()["moved"]["pending"] == 1


def test_bad_header_is_quarantined_not_deleted(world):
    client, store, tmp, files = world
    r = put(client, tmp, "nofilter.fits", "light", FILTER=None)
    f = r.json()["file"]
    assert f["status"] == "quarantine" and "no filter" in f["reasons"][0]
    assert files.path_of("p1", "u-alice", "nofilter.fits").is_file()
    listing = client.get("/api/v1/projects/p1/files", headers={"authorization": "Bearer alice-token"}).json()
    assert listing["gaps"][0]["status"] == "quarantine"


def test_header_kind_used_when_none_given(world):
    client, store, tmp, files = world
    path = write_fits(tmp / "x.fits", light_cards(IMAGETYP="FLAT"))
    r = client.put("/api/v1/projects/p1/files/M31_0001.fits", content=path.read_bytes(),
                   headers={"authorization": "Bearer alice-token"})
    assert r.json()["file"]["kind"] == "flat"
    assert files.path_of("p1", "u-alice", "M31_0001.fits", "flat").is_file()


def test_not_fits_still_refused_outright(world):
    client, store, tmp, _ = world
    r = client.put("/api/v1/projects/p1/files/a.fits", content=b"hello world",
                   headers={"authorization": "Bearer alice-token"})
    assert r.status_code == 400


def test_owner_changes_rules_and_everything_is_rejudged(world):
    client, store, tmp, _ = world
    put(client, tmp, "L_0001.fits", "light")
    put(client, tmp, "flat_0001.fits", "flat", IMAGETYP="FLAT")
    put(client, tmp, "bias_0001.fits", "bias", IMAGETYP="BIAS", EXPTIME=0.0)
    listing = client.get("/api/v1/projects/p1/files", headers={"authorization": "Bearer alice-token"}).json()
    assert listing["myLights"] == {"ready": 1}
    r = client.put("/api/v1/projects/p1/upload-rules", json={"rules": {"darkPolicy": "dark"}},
                   headers={"authorization": "Bearer alice-token"})
    assert r.status_code == 403
    r = client.put("/api/v1/projects/p1/upload-rules", json={"rules": {"darkPolicy": "maybe"}},
                   headers={"authorization": "Bearer owner-token"})
    assert r.status_code == 400
    r = client.put("/api/v1/projects/p1/upload-rules", json={"rules": {"darkPolicy": "dark"}},
                   headers={"authorization": "Bearer owner-token"})
    assert r.status_code == 200 and r.json()["moved"]["pending"] == 1
    assert store.project("p1")["payload"]["uploadRules"] == {"darkPolicy": "dark"}
    assert client.get("/api/v1/projects/p1/upload-rules").json()["values"]["darkPolicy"] == "dark"
    # Back to the default: stored as no change at all.
    client.put("/api/v1/projects/p1/upload-rules", json={"rules": {"darkPolicy": "darkOrBias"}},
               headers={"authorization": "Bearer owner-token"})
    assert store.project("p1")["payload"]["uploadRules"] == {}


def test_recheck_grandfathered_files(world):
    client, store, tmp, files = world
    # A file from before the rules: on disk and in the table, no facts, no status.
    folder = files.path_of("p1", "u-alice", "old.fits").parent
    folder.mkdir(parents=True, exist_ok=True)
    write_fits(folder / "old.fits", light_cards())
    store.add_file("old1", "p1", "u-alice", "old.fits", (folder / "old.fits").stat().st_size, "", "light")
    listing = client.get("/api/v1/projects/p1/files", headers={"authorization": "Bearer owner-token"}).json()
    assert listing["files"][0]["status"] == "" and listing["lights"] == {"": 1}
    assert client.post("/api/v1/projects/p1/files/recheck",
                       headers={"authorization": "Bearer alice-token"}).status_code == 403
    r = client.post("/api/v1/projects/p1/files/recheck", headers={"authorization": "Bearer owner-token"})
    assert r.json()["read"] == 1 and r.json()["lights"] == {"pending": 1}


def test_page_has_the_checklist(world):
    client, *_ = world
    html = client.get("/files").text
    assert "Upload rules" in html and "Pending calibration" in html
    assert "Everything on this collaboration" not in html and 'id="all"' not in html
    assert '<details class="view" id="sec-release"' in html and 'id="collabSel"' in html and 'class="sfnav"' in html and 'href="/collabs"' in html and 'id="topWho"' in html


def test_darkflat_no_longer_offered_but_still_taken(world):
    client, store, tmp, files = world
    html = client.get("/files").text
    assert 'data-kind="darkflat"' not in html and "<h3>Dark flats</h3>" not in html
    info = client.get("/api/v1/files/projects", headers={"authorization": "Bearer alice-token"}).json()
    assert "darkflat" not in info["uploadKinds"]
    assert any(k["id"] == "darkflat" for k in info["kinds"])           # old ones still labelled
    # An older client sending kind=darkflat: kept apart, never matched, told why.
    r = put(client, tmp, "DARKFLAT_0001.fits", "darkflat", IMAGETYP="DARKFLAT", EXPTIME=2.0)
    assert r.status_code == 200
    f = r.json()["file"]
    assert f["kind"] == "darkflat" and f["status"] == "ready"
    assert "no longer collected" in f["reasons"][0]
    assert files.path_of("p1", "u-alice", "DARKFLAT_0001.fits", "darkflat").is_file()
    # It calibrates nothing: a light with only a dark flat still waits.
    r = put(client, tmp, "L_0001.fits", "light")
    assert r.json()["file"]["status"] == "pending"
    # And it still downloads.
    r = client.get("/api/v1/projects/p1/files/u-alice/darkflat/DARKFLAT_0001.fits",
                   headers={"authorization": "Bearer alice-token"})
    assert r.status_code == 200 and r.content.startswith(b"SIMPLE")
    rules = " ".join(client.get("/api/v1/projects/p1/upload-rules").json()["checklist"]).lower()
    assert "dark flat" not in rules


def test_collab_list_is_public_and_ordered(world):
    client, store, tmp, _ = world
    store.add_project("p2", "Old one", "Bray", {"collectFiles": True}, owner_id="u-owner")
    store.set_project_status("p2", "closed") if hasattr(store, "set_project_status") else None
    store.add_project("p3", "Not collecting", "Bray", {"collectFiles": False}, owner_id="u-owner")
    put(client, tmp, "L_0001.fits", "light")
    info = client.get("/api/v1/files/projects").json()            # signed out
    assert info["signedIn"] is False
    ids = [c["id"] for c in info["collabs"]]
    assert ids[0] == "p1" and "p3" not in ids
    p1 = info["collabs"][0]
    assert p1["files"] == 1 and p1["bytes"] > 0 and p1["collecting"] and not p1["closed"]
    assert set(p1) == {"id", "name", "coordinator", "closed", "collecting", "files", "bytes"}


def test_page_has_the_viewer_and_no_preview_downloads(world):
    client, *_ = world
    html = client.get("/files").text
    assert 'id="viewer"' in html and 'id="viewerImg"' in html and 'id="heroImg"' in html and 'id="heroTools"' in html
    assert "data-full" in html and "max-height: 320px" not in html
    assert " download>" not in html                       # nothing on this page is offered as a download


def test_page_has_the_stars_toggle(world):
    client, *_ = world
    html = client.get("/files").text
    assert 'data-stars="starless"' in html and "setStars" in html and "starlessOf" in html
    assert "e.key === 't'" in html and "small" in html


def test_page_has_the_image_picker(world):
    client, *_ = world
    html = client.get("/files").text
    # one button per preview (kind / filter), hidden when there is only one; choice in the hash
    assert 'data-view="' in html and "views.length > 1" in html and "p.kind || p.filter" in html
    assert "new URLSearchParams({ view: cur(v).key, stars:" in html and "No starless version of" in html
    assert "vReshape" in html and "ghost" in html
