"""The coordinator's processing-job status: who may set it, what anybody may read."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.jobs import STALE_SECONDS, Jobs  # noqa: E402
from server.releases import Releases  # noqa: E402
from server.store import Store  # noqa: E402

PID = "d07d82eb861e"
TOKEN = "j" * 40
PEOPLE = {"Bearer owner": {"kind": "user", "id": "u-bray", "name": "Bray", "admin": False},
          "Bearer admin": {"kind": "admin", "id": "", "name": "coordinator", "admin": True},
          "Bearer other": {"kind": "user", "id": "u-x", "name": "X", "admin": False},
          "Bearer rig": {"kind": "agent", "id": "u-bray", "name": "rig", "admin": False}}


@pytest.fixture()
def world(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_project(PID, "M31", "Bray", {"region": {}}, owner_id="u-bray")
    rel = tmp_path / "rel"; (rel / PID).mkdir(parents=True)
    (rel / PID / "LATEST").write_text("v1\n")
    (tmp_path / "job-token.txt").write_text(TOKEN + "\n")
    clock = [1_800_000_000.0]
    app = FastAPI()
    who = lambda request: PEOPLE.get(request.headers.get("authorization", ""))  # noqa: E731
    owns = lambda w, p: w["admin"] or p.get("owner_id") == w["id"]  # noqa: E731
    Jobs(app, store, rel, who, owns, tmp_path / "job-token.txt", latest=lambda pid: releases.latest_version(pid), clock=lambda: clock[0])
    releases = Releases(app, store, rel, who, owns)   # after Jobs, as in app.py
    return TestClient(app), rel, clock


RUN = {"status": "running", "version": "v3", "steps": ["Pull", "Stack Ha", "Render"], "step": 1, "percent": 42,
       "message": "Stacking Ha (3/7)", "started": "2026-11-03T20:00:00Z", "eta": 1_800_000_600}


def test_idle_when_nothing_sent(world):
    client, *_ = world
    r = client.get(f"/api/v1/releases/{PID}/job")
    assert r.status_code == 200 and r.json()["status"] == "idle" and r.json()["published"] == "v1"
    assert "max-age=5" in r.headers["cache-control"]
    assert client.get("/api/v1/releases/abcdef123456/job").status_code == 404


@pytest.mark.parametrize("auth,code", [("", 403), ("Bearer other", 403), ("Bearer rig", 403), ("Bearer " + "x" * 40, 403),
                                       ("Bearer owner", 200), ("Bearer admin", 200), ("Bearer " + TOKEN, 200)])
def test_who_may_set_it(world, auth, code):
    client, *_ = world
    r = client.put(f"/api/v1/releases/{PID}/job", json=RUN, headers={"Authorization": auth} if auth else {})
    assert r.status_code == code


def test_running_then_stale_and_cleaned(world):
    client, rel, clock = world
    body = dict(RUN, secret="nope", message="x" * 1000)
    r = client.put(f"/api/v1/releases/{PID}/job", json=body, headers={"Authorization": "Bearer " + TOKEN})
    j = r.json()
    assert j["status"] == "running" and j["step"] == 1 and j["percent"] == 42 and j["stale"] is False
    assert j["steps"][1] == {"name": "Stack Ha", "state": "pending"} and len(j["message"]) == 300
    assert "secret" not in j and j["started"] == pytest.approx(1793736000.0) and j["updated"] == clock[0]
    assert "secret" not in json.loads((rel / PID / "JOB.json").read_text())
    clock[0] += STALE_SECONDS + 1
    assert client.get(f"/api/v1/releases/{PID}/job").json()["stale"] is True
    # the release listing ignores the job file
    assert client.get(f"/api/v1/releases/{PID}").status_code == 404


@pytest.mark.parametrize("bad", [{"status": "walking"}, {"status": "running", "steps": "x"},
                                 {"status": "running", "percent": "lots"}, {"status": "running", "steps": [{"state": "done"}]},
                                 [1, 2]])
def test_nonsense_refused(world, bad):
    client, *_ = world
    assert client.put(f"/api/v1/releases/{PID}/job", json=bad, headers={"Authorization": "Bearer " + TOKEN}).status_code == 400


def test_step_by_name(world):
    client, *_ = world
    j = client.put(f"/api/v1/releases/{PID}/job", json=dict(RUN, step="Render"), headers={"Authorization": "Bearer " + TOKEN}).json()
    assert j["step"] == 2
