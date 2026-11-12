"""The "Start new stack" button: owners queue one request at a time; the box claims it."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.jobs import REQUEST_EXPIRE_SECONDS, STALE_SECONDS, Jobs  # noqa: E402
from server.releases import Releases  # noqa: E402
from server.store import Store  # noqa: E402

PID = "d07d82eb861e"
TOKEN = "j" * 40
PEOPLE = {"Bearer owner": {"kind": "user", "id": "u-bray", "name": "Bray", "admin": False},
          "Bearer admin": {"kind": "admin", "id": "", "name": "coordinator", "admin": True},
          "Bearer other": {"kind": "user", "id": "u-x", "name": "X", "admin": False},
          "Bearer rig": {"kind": "agent", "id": "u-bray", "name": "rig", "admin": False}}
H = lambda who: {"Authorization": f"Bearer {who}"}  # noqa: E731


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
    releases = Releases(app, store, rel, who, owns)
    return TestClient(app), rel, clock


@pytest.mark.parametrize("who,code", [("", 403), ("other", 403), ("rig", 403), (TOKEN, 403), ("owner", 202), ("admin", 202)])
def test_who_may_press(world, who, code):
    client, rel, _ = world
    r = client.post(f"/api/v1/releases/{PID}/stack", headers=H(who) if who else {})
    assert r.status_code == code
    assert (rel / PID / "STACK_REQUEST.json").exists() == (code == 202)


def test_can_stack_flag_and_404(world):
    client, *_ = world
    assert client.get(f"/api/v1/releases/{PID}/stack", headers=H("owner")).json()["canStack"] is True
    assert client.get(f"/api/v1/releases/{PID}/stack", headers=H("other")).json()["canStack"] is False
    assert client.get(f"/api/v1/releases/{PID}/stack").json()["canStack"] is False
    assert client.post("/api/v1/releases/abcdef123456/stack", headers=H("owner")).status_code == 404
    # /stack is not read as a release version
    assert "by" not in client.get(f"/api/v1/releases/{PID}/stack").json()


def test_one_at_a_time_then_free_again(world):
    client, rel, clock = world
    r = client.post(f"/api/v1/releases/{PID}/stack", headers=H("owner"))
    assert r.status_code == 202 and r.json()["state"] == "queued" and r.json()["busy"]
    req = json.loads((rel / PID / "STACK_REQUEST.json").read_text())
    assert req["by"] == "u-bray" and req["state"] == "queued"
    assert client.post(f"/api/v1/releases/{PID}/stack", headers=H("owner")).status_code == 409
    # the box claims it and runs: still blocked
    req.update(state="running", updated=clock[0]); (rel / PID / "STACK_REQUEST.json").write_text(json.dumps(req))
    assert client.post(f"/api/v1/releases/{PID}/stack", headers=H("admin")).status_code == 409
    req.update(state="done"); (rel / PID / "STACK_REQUEST.json").write_text(json.dumps(req))
    assert client.post(f"/api/v1/releases/{PID}/stack", headers=H("owner")).status_code == 202


def test_running_job_blocks_unless_stale(world):
    client, rel, clock = world
    run = {"status": "running", "steps": ["Stack"], "step": 0}
    assert client.put(f"/api/v1/releases/{PID}/job", json=run, headers=H(TOKEN)).status_code == 200
    assert client.post(f"/api/v1/releases/{PID}/stack", headers=H("owner")).status_code == 409
    clock[0] += STALE_SECONDS + 5
    assert client.post(f"/api/v1/releases/{PID}/stack", headers=H("owner")).status_code == 202


def test_abandoned_request_expires(world):
    client, rel, clock = world
    client.post(f"/api/v1/releases/{PID}/stack", headers=H("owner"))
    clock[0] += REQUEST_EXPIRE_SECONDS + 5
    assert client.get(f"/api/v1/releases/{PID}/stack").json()["active"] is False
    assert client.post(f"/api/v1/releases/{PID}/stack", headers=H("owner")).status_code == 202


def test_cancel_only_queued(world):
    client, rel, clock = world
    assert client.delete(f"/api/v1/releases/{PID}/stack", headers=H("owner")).status_code == 409
    client.post(f"/api/v1/releases/{PID}/stack", headers=H("owner"))
    assert client.delete(f"/api/v1/releases/{PID}/stack", headers=H("other")).status_code == 403
    r = client.delete(f"/api/v1/releases/{PID}/stack", headers=H("owner"))
    assert r.status_code == 200 and r.json()["state"] == "cancelled" and not r.json()["busy"]
