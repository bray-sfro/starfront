"""The public releases routes on a bare app: only listed files, no escapes, anonymous."""
from __future__ import annotations
import json, sys
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.releases import Releases  # noqa: E402
from server.store import Store  # noqa: E402

PID = "99e2cd9deed9"

@pytest.fixture()
def client(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_project(PID, "M31 Andromeda", "Bray", {"region": {}}, owner_id="u1")
    rel = tmp_path / "rel"; v = rel / PID / "v1"; v.mkdir(parents=True)
    (v / "M31_B_v1.fits.fz").write_bytes(b"SIMPLE  = T" + b"\0" * 100)
    (v / "M31_B_v1_preview.jpg").write_bytes(b"\xff\xd8jpeg")
    (v / "secret.txt").write_text("no")
    (tmp_path / "outside.txt").write_text("no")
    (v / "link.fits").symlink_to(tmp_path / "outside.txt")
    (v / "release.json").write_text(json.dumps({"version": "v1", "released": "2026-11-03", "totalHours": 1.5,
        "filters": [{"filter": "B", "hours": 1.5, "subs": 3, "rigs": 1}],
        "contributors": [{"name": "Bray", "rigs": ["fsq106"], "hours": 1.5, "share": {"B": 100}}],
        "files": [{"name": "M31_B_v1.fits.fz", "role": "master", "filter": "B"},
                  {"name": "M31_B_v1_preview.jpg", "role": "preview"},
                  {"name": "link.fits", "role": "master"}, {"name": "../outside.txt"},
                  {"name": "missing.fits"}]}))
    (rel / PID / "LATEST").write_text("v1\n")
    app = FastAPI(); Releases(app, store, rel)
    return TestClient(app), store

def test_listing_and_latest_are_public(client):
    c, _ = client
    r = c.get("/api/v1/releases").json()["releases"]
    assert r[0]["project"] == PID and r[0]["version"] == "v1"
    d = c.get(f"/api/v1/releases/{PID}").json()
    names = [f["name"] for f in d["files"]]
    assert "M31_B_v1.fits.fz" in names and "missing.fits" not in names and "../outside.txt" not in names
    assert d["projectName"] == "M31 Andromeda" and d["versions"] == ["v1"]

def test_download_and_refusals(client):
    c, _ = client
    r = c.get(f"/releases/{PID}/v1/M31_B_v1.fits.fz")
    assert r.status_code == 403                      # masters: contributors only
    assert c.get(f"/releases/{PID}/v1/M31_B_v1_preview.jpg").headers["content-type"] == "image/jpeg"
    for bad in ["secret.txt", "release.json", "link.fits", "..%2Foutside.txt", "missing.fits"]:
        assert c.get(f"/releases/{PID}/v1/{bad}").status_code == 404, bad
    assert c.get(f"/releases/{PID}/v2/M31_B_v1.fits.fz").status_code == 404
    assert c.get("/api/v1/releases/nothex").status_code == 404

def test_summary_and_no_release(client, tmp_path):
    c, store = client
    app = FastAPI(); r = Releases(app, store, tmp_path / "empty")
    assert r.summary(PID) is None
    assert TestClient(app).get(f"/api/v1/releases/{PID}").status_code == 404
