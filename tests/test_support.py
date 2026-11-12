"""Get help: a signed-in person's question becomes a help desk ticket with context."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.agentuploads import AgentUploads  # noqa: E402
from server.files import Files  # noqa: E402
from server.jobs import Jobs  # noqa: E402
from server.personal import Personal  # noqa: E402
from server.releases import Releases  # noqa: E402
from server.site import Site  # noqa: E402
from server.store import Store  # noqa: E402
from server.support import Support  # noqa: E402

BRAY = {"authorization": "Bearer person-bray"}
ZOE = {"authorization": "Bearer person-zoe"}
PEOPLE = {"person-bray": {"kind": "user", "id": "u-bray", "name": "astrofalls", "roles": [], "admin": False},
          "person-zoe": {"kind": "user", "id": "u-zoe", "name": "zoe", "roles": [], "admin": False}}
ASK = {"message": "My M31 stack failed last night, what happened?", "email": "bray@example.com",
       "project": "bbbbbb000003"}


class Desk:
    """A help desk that takes every call and remembers it."""

    def __init__(self, status=201):
        self.calls, self.status = [], status

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body)))
        if method == "POST":
            return self.status, {"ticket": {"id": 4711}} if self.status == 201 else {"error": "nope"}
        return 200, {}


def build(tmp_path, *, enabled=True, token="zd-test-token", desk=None):
    store = Store(tmp_path / "db.sqlite")
    app = FastAPI()
    owns = lambda who, project: project.get("owner_id") == who["id"]  # noqa: E731
    files = Files(app, store, tmp_path / "files", PEOPLE.get, owns, [], "http://t", lambda: "c")
    up = AgentUploads(app, store, files)
    rel = tmp_path / "files" / "_releases"
    jobs = Jobs(app, store, rel, files.who, owns, tmp_path / "job-token.txt", latest=lambda pid: None)
    releases = Releases(app, store, rel, files.who, owns)
    personal = Personal(app, store, files, up.agent, files.who, [], enabled=True, beta=["u-bray"],
                        storage_gb=10, releases=releases, jobs=jobs)
    site = Site(app, store, files, releases, personal, lambda pid, payload: ({}, {}))
    desk = desk or Desk()
    clock = {"now": 1_800_000_000.0}
    support = Support(app, store, files.who, personal, site, jobs, enabled=enabled, subdomain="example",
                      email="agent@example.com", token=token, help_url="https://help.example.com/",
                      send=desk, clock=lambda: clock["now"])
    store.upsert_user("u-bray", "web-b", "astrofalls", "", [])
    store.upsert_user("u-zoe", "web-z", "zoe", "", [])
    store.add_agent("aaaaaaaa1111", "rig-secret-a", "FSQ106", "astrofalls", "u-bray")
    store.add_project("bbbbbb000003", "My M31", "astrofalls", {"collectFiles": True}, owner_id="u-bray",
                      scope="personal")
    store.add_file("f1", "bbbbbb000003", "u-bray", "L_0001.fits", 2 * 1024 ** 3, kind="light")
    store.set_file_statuses([("f1", "quarantine", ["no FILTER in header"])])
    (rel / "bbbbbb000003").mkdir(parents=True)
    (rel / "bbbbbb000003" / "JOB.json").write_text(json.dumps(
        {"status": "failed", "message": "registration failed: too few stars", "updated": clock["now"]}))
    return TestClient(app), store, support, desk, clock


def test_off_by_default_and_without_settings(tmp_path, monkeypatch):
    for name in ("STARFRONT_SUPPORT", "STARFRONT_ZENDESK_TOKEN", "STARFRONT_ZENDESK_EMAIL",
                 "STARFRONT_ZENDESK_SUBDOMAIN"):
        monkeypatch.delenv(name, raising=False)
    app = FastAPI()
    support = Support(app, Store(tmp_path / "a.sqlite"), lambda request: None)
    assert support.enabled is False
    client = TestClient(app)
    assert client.get("/api/v1/support").json()["available"] is False
    assert client.post("/api/v1/support", json=ASK, headers=BRAY).status_code == 404
    # Switched on, but with no token: still off, and nothing is ever called.
    client, _store, support, desk, _ = build(tmp_path / "b", token="")
    assert support.enabled is False
    assert client.post("/api/v1/support", json=ASK, headers=BRAY).status_code == 404 and not desk.calls


def test_a_question_becomes_a_ticket_with_a_private_note(tmp_path):
    client, store, _support, desk, _ = build(tmp_path)
    assert client.get("/api/v1/support").json() == {"available": True, "helpUrl": "https://help.example.com/"}
    assert client.post("/api/v1/support", json=ASK).status_code == 401
    r = client.post("/api/v1/support", json=ASK, headers=BRAY)
    assert r.status_code == 200 and r.json() == {"sent": True, "ticket": "4711"}
    (method, url, headers, made), (method2, url2, _h, noted) = desk.calls
    assert (method, url) == ("POST", "https://example.zendesk.com/api/v2/tickets.json")
    assert base64.b64decode(headers["Authorization"][6:]).decode() == "agent@example.com/token:zd-test-token"
    ticket = made["ticket"]
    assert ticket["requester"] == {"name": "astrofalls", "email": "bray@example.com"}
    assert ticket["comment"] == {"body": ASK["message"], "public": True}        # only their own words
    assert ticket["subject"].startswith("[Auto-processing] My M31 stack failed")
    assert "auto-processing" in ticket["tags"] and ticket["external_id"].startswith("starfront-help-")
    assert (method2, url2) == ("PUT", "https://example.zendesk.com/api/v2/tickets/4711.json")
    note = noted["ticket"]["comment"]
    assert note["public"] is False
    for expected in ("Discord: astrofalls (id u-bray)", "Plan: beta", "storage 2.0 GB of 10.0 GB",
                     "Project: My M31 (id bbbbbb000003, personal, open)", "Stack: job failed",
                     "registration failed: too few stars", "quarantine: L_0001.fits (light): no FILTER in header",
                     "FSQ106 (id aaaaaaaa1111): offline, last check-in never"):
        assert expected in note["body"], expected
    assert "rig-secret" not in json.dumps(desk.calls[1][3]) and "zd-test-token" not in r.text
    row = store.support_requests_of("u-bray")[0]
    assert row["state"] == "sent" and row["ticket"] == "4711" and row["project"] == "bbbbbb000003"


def test_somebody_elses_personal_project_is_never_in_the_note(tmp_path):
    client, _store, _support, desk, _ = build(tmp_path)
    r = client.post("/api/v1/support", json=dict(ASK, email="zoe@example.com"), headers=ZOE)
    assert r.status_code == 200
    body = desk.calls[1][3]["ticket"]["comment"]["body"]
    assert "My M31" not in body and "Discord: zoe (id u-zoe)" in body and "Telescopes: none connected" in body


def test_bad_email_and_too_many(tmp_path):
    client, _store, _support, desk, clock = build(tmp_path)
    assert client.post("/api/v1/support", json=dict(ASK, email="not-an-email"), headers=BRAY).status_code == 400
    assert client.post("/api/v1/support", json=dict(ASK, message="short"), headers=BRAY).status_code == 422
    assert not desk.calls
    assert client.post("/api/v1/support", json=ASK, headers=BRAY).status_code == 200
    assert client.post("/api/v1/support", json=ASK, headers=BRAY).status_code == 429     # inside a minute
    for _ in range(4):
        clock["now"] += 120
        assert client.post("/api/v1/support", json=ASK, headers=BRAY).status_code == 200
    clock["now"] += 120
    assert client.post("/api/v1/support", json=ASK, headers=BRAY).status_code == 429     # five a day


def test_a_help_desk_that_refuses_is_a_502_and_does_not_count(tmp_path):
    desk = Desk(status=422)
    client, store, _support, _desk, _ = build(tmp_path, desk=desk)
    r = client.post("/api/v1/support", json=ASK, headers=BRAY)
    assert r.status_code == 502 and len(desk.calls) == 1
    assert store.support_requests_of("u-bray")[0]["state"] == "failed"
    desk.status = 201
    assert client.post("/api/v1/support", json=ASK, headers=BRAY).status_code == 200
