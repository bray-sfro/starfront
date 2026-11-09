"""Personal projects: one person's own target, private to them, off by default."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import agentuploads  # noqa: E402
from server.agentuploads import AgentUploads  # noqa: E402
from server.files import Files  # noqa: E402
from server.jobs import Jobs  # noqa: E402
from server.personal import Personal, target_key  # noqa: E402
from server.releases import Releases  # noqa: E402
from server.store import Store  # noqa: E402
from fitsfake import light_cards, write_fits  # noqa: E402

RIG = {"authorization": "Bearer rig-a"}          # Bray's telescope
OTHER_RIG = {"authorization": "Bearer rig-z"}    # somebody else's
BRAY = {"authorization": "Bearer person-bray"}
ZOE = {"authorization": "Bearer person-zoe"}
PEOPLE = {"person-bray": {"kind": "user", "id": "u-bray", "name": "astrofalls", "roles": [], "admin": False},
          "person-zoe": {"kind": "user", "id": "u-zoe", "name": "zoe", "roles": [], "admin": False}}


def build(tmp_path, monkeypatch, *, enabled, beta=("u-bray",), storage_gb=10.0):
    monkeypatch.setattr(agentuploads, "AGENT_KEEP_FREE", 0)
    store = Store(tmp_path / "db.sqlite")
    app = FastAPI()
    owns = lambda who, project: who.get("admin") or project.get("owner_id") == who["id"]  # noqa: E731
    files = Files(app, store, tmp_path / "files", PEOPLE.get, owns, [], "http://t", lambda: "c")
    up = AgentUploads(app, store, files)
    rel_dir = tmp_path / "files" / "_releases"
    jobs = Jobs(app, store, rel_dir, files.who, owns, tmp_path / "job-token.txt",
                latest=lambda pid: None)
    releases = Releases(app, store, rel_dir, files.who, owns)
    personal = Personal(app, store, files, up.agent, files.who, [], enabled=enabled,
                        beta=list(beta), storage_gb=storage_gb, releases=releases, jobs=jobs)
    up.personal = personal
    store.upsert_user("u-bray", "web-b", "astrofalls", "", [])
    store.upsert_user("u-zoe", "web-z", "zoe", "", [])
    store.add_agent("aaaaaaaa1111", "rig-a", "fsq106", "astrofalls", "u-bray")
    store.add_agent("zzzzzzzz9999", "rig-z", "redcat", "zoe", "u-zoe")
    # A collaboration alongside, to check the two never mix.
    store.add_project("c0ffee000001", "M31 collab", "Bray", {"collectFiles": True, "autoUpload": True},
                      owner_id="u-bray")
    return TestClient(app), store, personal, rel_dir


def light(tmp, name, **cards):
    cards.setdefault("PROJID", None)
    return write_fits(tmp / name, light_cards(RA=83.82, DEC=-5.39, **cards),
                      width=128, height=96).read_bytes()


def send(client, project, name, blob, headers=RIG):
    sha = hashlib.sha256(blob).hexdigest()
    r = client.post("/api/v1/agent/uploads", headers=headers,
                    json={"project": project, "name": name, "kind": "light",
                          "bytes": len(blob), "sha256": sha})
    if r.status_code != 200 or r.json().get("already"):
        return r
    up = r.json()
    r = client.put(f"/api/v1/agent/uploads/{up['id']}?offset=0", content=blob, headers=headers)
    assert r.status_code == 200, r.text
    return client.post(f"/api/v1/agent/uploads/{up['id']}/finish", headers=headers)


def test_target_key_ignores_spelling():
    assert target_key("M 42") == target_key("m42") == target_key("M-42") == "m42"
    assert target_key("NGC 1499 (California)") == "ngc1499california"


def test_off_by_default_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("STARFRONT_PERSONAL_UPLOADS", raising=False)
    client, store, personal, _ = build(tmp_path, monkeypatch, enabled=None)
    assert personal.enabled is False
    assert client.get("/api/v1/agent/personal", headers=RIG).json()["available"] is False
    r = client.post("/api/v1/agent/personal", headers=RIG, json={"target": "M42"})
    assert r.status_code == 403 and "not open yet" in r.json()["detail"]
    mine = client.get("/api/v1/me/personal", headers=BRAY).json()
    assert mine["available"] is False and mine["projects"] == []
    assert store.personal_projects_of("u-bray") == []


def test_one_project_per_target_whatever_the_spelling(tmp_path, monkeypatch):
    client, store, *_ = build(tmp_path, monkeypatch, enabled=True)
    a = client.post("/api/v1/agent/personal", headers=RIG,
                    json={"target": "M 42", "ra": 83.82, "dec": -5.39}).json()
    b = client.post("/api/v1/agent/personal", headers=RIG, json={"target": "m42"}).json()
    assert a["created"] is True and b["created"] is False and a["project"] == b["project"]
    row = store.project(a["project"])
    assert row["scope"] == "personal" and row["owner_id"] == "u-bray"
    assert row["payload"]["region"]["ra"] == 83.82


def test_never_listed_with_collaborations(tmp_path, monkeypatch):
    client, store, *_ = build(tmp_path, monkeypatch, enabled=True)
    pid = client.post("/api/v1/agent/personal", headers=RIG, json={"target": "M42"}).json()["project"]
    assert [p["id"] for p in store.projects()] == ["c0ffee000001"]
    assert [p["id"] for p in store.projects_of("u-bray")] == ["c0ffee000001"]
    listed = client.get("/api/v1/files/projects", headers=BRAY).json()
    assert pid not in [p["id"] for p in listed["projects"]]
    assert pid not in [p["id"] for p in listed["collabs"]]


def test_upload_needs_no_task_and_lands_as_the_owners(tmp_path, monkeypatch):
    client, store, *_ = build(tmp_path, monkeypatch, enabled=True)
    pid = client.post("/api/v1/agent/personal", headers=RIG,
                      json={"target": "M42", "ra": 83.82, "dec": -5.39}).json()["project"]
    r = send(client, pid, "M42_L_0001.fits", light(tmp_path, "a.fits", FILTER="L"))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "aaaaaaaa-M42_L_0001.fits"
    assert store.file(pid, "u-bray", "aaaaaaaa-M42_L_0001.fits", "light") is not None
    status = client.get(f"/api/v1/agent/uploads?project={pid}", headers=RIG).json()
    assert status["enabled"] is True
    mine = client.get("/api/v1/me/personal", headers=BRAY).json()
    assert mine["available"] and mine["projects"][0]["files"] == 1 and mine["usedBytes"] > 0


def test_a_light_of_another_target_is_refused(tmp_path, monkeypatch):
    client, *_ = build(tmp_path, monkeypatch, enabled=True)
    pid = client.post("/api/v1/agent/personal", headers=RIG,
                      json={"target": "M31", "ra": 10.68, "dec": 41.27}).json()["project"]
    r = send(client, pid, "x.fits", light(tmp_path, "x.fits"))   # points at M42
    assert r.status_code == 403


def test_nobody_else_can_see_or_send(tmp_path, monkeypatch):
    client, *_ = build(tmp_path, monkeypatch, enabled=True)
    pid = client.post("/api/v1/agent/personal", headers=RIG, json={"target": "M42"}).json()["project"]
    sha = "0" * 64
    r = client.post("/api/v1/agent/uploads", headers=OTHER_RIG,
                    json={"project": pid, "name": "x.fits", "kind": "light", "bytes": 10, "sha256": sha})
    assert r.status_code == 404
    assert client.get(f"/api/v1/agent/uploads?project={pid}", headers=OTHER_RIG).status_code == 404
    for path in (f"/api/v1/projects/{pid}/files", f"/api/v1/projects/{pid}/upload-rules",
                 f"/api/v1/releases/{pid}/stack", f"/api/v1/releases/{pid}/job",
                 f"/api/v1/me/personal/{pid}"):
        assert client.get(path, headers=ZOE).status_code in (401, 404), path
        assert client.get(path).status_code in (401, 404), path
    assert client.get(f"/api/v1/projects/{pid}/files", headers=BRAY).status_code == 200
    assert client.get(f"/api/v1/me/personal/{pid}", headers=BRAY).status_code == 200


def test_releases_are_the_owners_alone(tmp_path, monkeypatch):
    client, store, _, rel = build(tmp_path, monkeypatch, enabled=True)
    pid = client.post("/api/v1/agent/personal", headers=RIG, json={"target": "M42"}).json()["project"]
    v = rel / pid / "v1"
    v.mkdir(parents=True)
    (v / "M42_preview.jpg").write_bytes(b"\xff\xd8jpeg")
    (v / "release.json").write_text(json.dumps({"version": "v1", "files": [
        {"name": "M42_preview.jpg", "role": "preview"}]}))
    (rel / pid / "LATEST").write_text("v1\n")
    assert pid not in [r["project"] for r in client.get("/api/v1/releases").json()["releases"]]
    assert client.get(f"/api/v1/releases/{pid}").status_code == 404
    assert client.get(f"/api/v1/releases/{pid}", headers=ZOE).status_code == 404
    assert client.get(f"/releases/{pid}/v1/M42_preview.jpg").status_code == 404
    assert client.get(f"/api/v1/releases/{pid}", headers=BRAY).status_code == 200
    r = client.get(f"/releases/{pid}/v1/M42_preview.jpg", headers=BRAY)
    assert r.status_code == 200 and r.headers["cache-control"].startswith("private")


def test_without_a_plan_it_says_so(tmp_path, monkeypatch):
    client, *_ = build(tmp_path, monkeypatch, enabled=True, beta=())
    r = client.post("/api/v1/agent/personal", headers=RIG, json={"target": "M42"})
    assert r.status_code == 402 and "plan" in r.json()["detail"]


def test_storage_is_the_persons_across_targets(tmp_path, monkeypatch):
    client, *_ = build(tmp_path, monkeypatch, enabled=True, storage_gb=0.00004)   # ~43 kB
    m42 = client.post("/api/v1/agent/personal", headers=RIG, json={"target": "M42"}).json()["project"]
    m43 = client.post("/api/v1/agent/personal", headers=RIG, json={"target": "M43"}).json()["project"]
    blob = light(tmp_path, "a.fits", FILTER="L")
    assert len(blob) < 43000 < 2 * len(blob)
    assert send(client, m42, "a.fits", blob).status_code == 200
    r = send(client, m43, "b.fits", light(tmp_path, "b.fits", FILTER="R"))
    assert r.status_code == 507 and "personal storage" in r.json()["detail"]


def test_owner_turns_auto_processing_on(tmp_path, monkeypatch):
    client, *_ = build(tmp_path, monkeypatch, enabled=True)
    pid = client.post("/api/v1/agent/personal", headers=RIG, json={"target": "M42"}).json()["project"]
    r = client.put(f"/api/v1/me/personal/{pid}", headers=BRAY, json={"autoProcess": True, "name": "Orion"})
    assert r.status_code == 200 and r.json()["autoProcess"] is True and r.json()["name"] == "Orion"
    assert client.put(f"/api/v1/me/personal/{pid}", headers=ZOE, json={"autoProcess": False}).status_code == 404
