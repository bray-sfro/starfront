"""Release masters for contributors only; everything else about a release public."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.files import Files  # noqa: E402
from server.releases import LOCKED_MESSAGE, Releases  # noqa: E402
from server.store import Store  # noqa: E402

PID, OLD, OTHER = "99e2cd9deed9", "d07d82eb861e", "68997c457855"
PEOPLE = {
    "admin": {"kind": "admin", "id": "", "name": "coordinator", "roles": [], "admin": True},
    "bray": {"kind": "user", "id": "u-bray", "name": "Bray", "roles": [], "admin": False},
    "old": {"kind": "user", "id": "u-old", "name": "Steve", "roles": [], "admin": False},
    "new": {"kind": "user", "id": "u-new", "name": "Alice", "roles": [], "admin": False},
    "elsewhere": {"kind": "user", "id": "u-else", "name": "Jon", "roles": [], "admin": False},
    "nobody": {"kind": "user", "id": "u-none", "name": "Pat", "roles": [], "admin": False},
}
BODY = b"SIMPLE  =                    T" + bytes(range(256)) * 40


@pytest.fixture()
def world(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_project(OLD, "M31 NINA TEST COLLAB", "Bray", {"region": {}}, owner_id="u-bray")
    store.add_project(PID, "M31 Andromeda", "Bray", {"region": {}, "continuedFrom": OLD}, owner_id="u-bray")
    store.add_project(OTHER, "M45", "Jon", {"region": {}}, owner_id="u-else")
    store.add_file("f1", OLD, "u-old", "a.fits", 10, "", "light")
    store.add_file("f2", PID, "u-new", "b.fits", 10, "", "flat")
    store.add_file("f3", OTHER, "u-else", "c.fits", 10, "", "light")
    rel = tmp_path / "rel"
    v = rel / PID / "v1"
    v.mkdir(parents=True)
    (v / "M31_Ha_v1.fits.fz").write_bytes(BODY)
    (v / "M31_Ha_v1_full.fits").write_bytes(BODY)
    (v / "M31_Ha_v1_preview.jpg").write_bytes(b"\xff\xd8jpeg")
    (v / "README.txt").write_text("hello")
    (v / "release.json").write_text(json.dumps({
        "version": "v1", "released": "2026-11-03", "totalHours": 94.3,
        "filters": [{"filter": "Ha", "hours": 94.3, "subs": 746, "rigs": 8}],
        "contributors": [{"name": "Steve G.", "rigs": ["Tak 130"], "hours": 11.8, "share": {"Ha": 4.4}}],
        "changelog": [{"version": "v1", "date": "2026-11-03", "text": "first"}],
        "files": [{"name": "M31_Ha_v1.fits.fz", "role": "master", "filter": "Ha"},
                  {"name": "M31_Ha_v1_full.fits", "role": "master", "filter": "Ha", "full": True},
                  {"name": "M31_Ha_v1_preview.jpg", "role": "preview", "filter": "Ha"},
                  {"name": "README.txt", "role": "extra"}]}))
    (rel / PID / "LATEST").write_text("v1\n")
    app = FastAPI()
    owns = lambda who, project: who["admin"] or project.get("owner_id") == who["id"]  # noqa: E731
    files = Files(app, store, tmp_path / "files", PEOPLE.get, owns, [], "http://t", lambda: "c")
    Releases(app, store, rel, files.who, owns)
    return TestClient(app), store


def H(token):
    return {"authorization": f"Bearer {token}"} if token else {}


@pytest.mark.parametrize("token,ok", [("admin", True), ("bray", True), ("old", True), ("new", True),
                                      ("elsewhere", False), ("nobody", False), (None, False)])
def test_who_gets_the_masters(world, token, ok):
    c, _ = world
    for name in ("M31_Ha_v1.fits.fz", "M31_Ha_v1_full.fits"):
        r = c.get(f"/releases/{PID}/v1/{name}", headers=H(token))
        if ok:
            assert r.status_code == 200 and r.content == BODY
            assert r.headers["cache-control"].startswith("private")
        else:
            assert r.status_code == 403 and r.json()["detail"] == LOCKED_MESSAGE
    d = c.get(f"/api/v1/releases/{PID}", headers=H(token)).json()
    assert d["access"]["allowed"] is ok
    assert {f["name"]: f["locked"] for f in d["files"]} == {
        "M31_Ha_v1.fits.fz": not ok, "M31_Ha_v1_full.fits": not ok,
        "M31_Ha_v1_preview.jpg": False, "README.txt": False}


@pytest.mark.parametrize("token", [None, "nobody"])
def test_the_rest_stays_public(world, token):
    c, _ = world
    assert c.get(f"/releases/{PID}/v1/M31_Ha_v1_preview.jpg", headers=H(token)).status_code == 200
    assert c.get(f"/releases/{PID}/v1/README.txt", headers=H(token)).status_code == 200
    d = c.get(f"/api/v1/releases/{PID}", headers=H(token)).json()
    assert d["filters"][0]["hours"] == 94.3 and d["contributors"][0]["name"] == "Steve G."
    assert d["changelog"] and d["access"]["message"] == LOCKED_MESSAGE
    assert c.get("/api/v1/releases", headers=H(token)).json()["releases"][0]["project"] == PID


def test_range_requests_for_contributors(world):
    c, _ = world
    r = c.get(f"/releases/{PID}/v1/M31_Ha_v1_full.fits", headers={**H("old"), "range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == BODY[100:200]
    assert r.headers["content-range"] == f"bytes 100-199/{len(BODY)}"
    r = c.get(f"/releases/{PID}/v1/M31_Ha_v1_full.fits", headers={"range": "bytes=0-9"})
    assert r.status_code == 403


def test_predecessor_link_is_a_setting(world):
    c, store = world
    # Only the owner may set it.
    assert c.put(f"/api/v1/releases/{PID}/access", json={"contributorProjects": []},
                 headers=H("new")).status_code == 403
    assert c.put(f"/api/v1/releases/{PID}/access", json={"contributorProjects": ["ffffffffffff"]},
                 headers=H("bray")).status_code == 400
    # No predecessor: Steve (old collab only) loses access, Alice keeps it.
    r = c.put(f"/api/v1/releases/{PID}/access", json={"contributorProjects": []}, headers=H("bray"))
    assert r.json()["contributorProjects"] == [PID]
    assert c.get(f"/releases/{PID}/v1/M31_Ha_v1.fits.fz", headers=H("old")).status_code == 403
    assert c.get(f"/releases/{PID}/v1/M31_Ha_v1.fits.fz", headers=H("new")).status_code == 200
    # Link another: its uploader gets in.
    c.put(f"/api/v1/releases/{PID}/access", json={"contributorProjects": [OTHER]}, headers=H("bray"))
    assert c.get(f"/releases/{PID}/v1/M31_Ha_v1.fits.fz", headers=H("elsewhere")).status_code == 200
    # Back to the default (continuedFrom).
    r = c.put(f"/api/v1/releases/{PID}/access", json={"contributorProjects": None}, headers=H("bray"))
    assert r.json()["contributorProjects"] == [PID, OLD]
    assert "releaseContributorProjects" not in store.project(PID)["payload"]


def test_telescope_contributor_gets_the_masters(world):
    """Frames reported by your own telescope count like an upload (Bray, 2026-11-03)."""
    c, store = world
    PEOPLE["scope"] = {"kind": "user", "id": "u-scope", "name": "Max", "roles": [], "admin": False}
    try:
        url = f"/releases/{PID}/v1/M31_Ha_v1.fits.fz"
        assert c.get(url, headers=H("scope")).status_code == 403
        store.add_agent("ag1", "tok-ag1", "Rig", owner="Max", owner_id="u-scope")
        store._run("INSERT INTO contributions (id, project, agent, task, night, filter, panel, seconds,"
                   " accepted, overridden, received, payload, verdict) VALUES"
                   " ('c1', ?, 'ag1', 't1', '2026-10-01', 'Ha', '', 7200, 1, 0, 0, '{}', '{}')", OLD)
        t = store.contribution_totals_of(OLD, "u-scope")
        assert t["sessions"] == 1 and t["hours"] == 2.0 and t["telescopes"] == 1
        assert t["filters"] == [{"filter": "Ha", "sessions": 1, "hours": 2.0}]
        assert c.get(url, headers=H("scope")).status_code == 200      # OLD counts for PID (continuedFrom)
        # A rejected night does not count.
        store._run("UPDATE contributions SET accepted = 0 WHERE id = 'c1'")
        assert c.get(url, headers=H("scope")).status_code == 403
    finally:
        PEOPLE.pop("scope", None)


def test_page_offers_only_masters(world):
    c, _ = world
    js = c.get("/releases/page.js").text
    assert "Previews and notes" not in js and "Who shot it" not in js
    assert "role === 'master'" in js and ">Download<" in js and "<h2>Contributors</h2>" in js
