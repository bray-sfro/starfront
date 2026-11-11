"""The website around the files page: front door, collaboration cards, My imaging."""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.agentuploads import AgentUploads  # noqa: E402
from server.files import Files  # noqa: E402
from server.personal import Personal  # noqa: E402
from server.releases import Releases  # noqa: E402
from server.site import Site  # noqa: E402
from server.store import Store  # noqa: E402

BRAY = {"authorization": "Bearer person-bray"}
PEOPLE = {"person-bray": {"kind": "user", "id": "u-bray", "name": "astrofalls", "roles": [], "admin": False}}


def build(tmp_path, enabled=False):
    store = Store(tmp_path / "db.sqlite")
    app = FastAPI()
    owns = lambda who, project: project.get("owner_id") == who["id"]  # noqa: E731
    codes = (f"code{i}" for i in itertools.count(1))
    files = Files(app, store, tmp_path / "files", PEOPLE.get, owns, [], "http://t", lambda: next(codes))
    up = AgentUploads(app, store, files)
    rel = tmp_path / "files" / "_releases"
    releases = Releases(app, store, rel, files.who, owns)
    personal = Personal(app, store, files, up.agent, files.who, [], enabled=enabled, beta=["u-bray"],
                        releases=releases)
    Site(app, store, files, releases, personal,
         lambda pid, payload: ({"L": 2.5}, {"L": {"goalHours": 10, "average": 0.25, "atGoal": 0, "thinnest": 0}}))
    store.upsert_user("u-bray", "web-b", "astrofalls", "", [])
    store.add_project("aaaaaa000001", "M42 horse", "Bray", {"collectFiles": True, "notes": "x" * 400},
                      owner_id="u-bray")
    store.add_project("aaaaaa000002", "Old closed, empty", "Bray", {})
    store.set_project("aaaaaa000002", {}, status="closed")
    store.add_project("bbbbbb000003", "My M31", "astrofalls", {"collectFiles": True}, owner_id="u-bray",
                      scope="personal")
    v = rel / "aaaaaa000001" / "v2"
    v.mkdir(parents=True)
    for name in ("M42_starless_preview.jpg", "M42_preview.jpg", "M42_L.fits"):
        (v / name).write_bytes(b"x")
    (v / "release.json").write_text(json.dumps({"version": "v2", "released": "2026-11-08", "files": [
        {"name": "M42_starless_preview.jpg", "role": "preview"},
        {"name": "M42_preview.jpg", "role": "preview"},
        {"name": "M42_L.fits", "role": "master"}]}))
    (rel / "aaaaaa000001" / "LATEST").write_text("v2\n")
    return TestClient(app, follow_redirects=False), store


def test_pages_render_with_the_shared_header(tmp_path):
    client, _ = build(tmp_path)
    for path, key in (("/", "home"), ("/collabs", "collabs"), ("/me", "me"), ("/telescopes", "telescopes")):
        r = client.get(path)
        assert r.status_code == 200 and f'data-page="{key}"' in r.text and 'class="sfnav"' in r.text, path


def test_cards_are_collaborations_only_with_a_picture(tmp_path):
    client, _ = build(tmp_path)
    cards = client.get("/api/v1/site/collabs").json()["collabs"]
    assert [c["id"] for c in cards] == ["aaaaaa000001"]          # no personal, no empty closed one
    card = cards[0]
    assert card["release"]["preview"] == "/releases/aaaaaa000001/v2/M42_preview.jpg"   # with stars first
    assert card["hours"] == 2.5 and card["progress"]["L"]["average"] == 0.25
    assert card["owned"] is False and len(card["notes"]) <= 241
    assert client.get("/api/v1/site/collabs", headers=BRAY).json()["collabs"][0]["owned"] is True


def test_me_says_what_is_open(tmp_path):
    client, _ = build(tmp_path)
    me = client.get("/api/v1/site/me").json()
    assert me["signedIn"] is False and me["personal"]["available"] is False
    me = client.get("/api/v1/site/me", headers=BRAY).json()
    assert me["signedIn"] is True and "not open yet" in me["personal"]["why"]
    client, _ = build(tmp_path / "on", enabled=True)
    assert client.get("/api/v1/site/me", headers=BRAY).json()["personal"]["available"] is True


def test_signin_only_comes_back_to_our_pages(tmp_path):
    client, store = build(tmp_path)
    assert client.get("/signin?back=/me").status_code == 303
    assert store._one("SELECT back FROM logins WHERE code = 'code1'")["back"] == "/me"
    client.get("/signin?back=https://evil.example")
    assert store._one("SELECT back FROM logins WHERE code = 'code2'")["back"] == "/"


def test_my_telescopes_are_mine_alone_and_say_if_they_are_online(tmp_path):
    client, store = build(tmp_path)
    assert client.get("/api/v1/site/telescopes").status_code == 401
    store.add_agent("aaaaaaaa1111", "rig-secret-a", "Telescope 1", "astrofalls", "u-bray")
    store.add_agent("bbbbbbbb2222", "rig-secret-b", "Telescope 2", "astrofalls", "u-bray")
    store.add_agent("zzzzzzzz9999", "rig-secret-z", "Redcat", "zoe", "u-zoe")
    store.seen("aaaaaaaa1111",
               {"name": "FSQ106", "focalLength": 530, "pixelSize": 3.76, "sensorWidth": 6248, "sensorHeight": 4176,
                "filters": {"L": None, "Ha": 3.0}, "colour": False},
               {"telescope": "FSQ106 pier 12", "state": "imaging", "target": "M31", "ra": 0.71, "dec": 41.27})
    store.add_task({"id": "t1", "project": "aaaaaa000001", "agent": "aaaaaaaa1111", "state": "accepted",
                    "version": 1, "issued": 1.0})
    r = client.get("/api/v1/site/telescopes", headers=BRAY)
    assert r.status_code == 200 and "rig-secret" not in r.text          # never the token
    d = r.json()
    assert [t["id"] for t in d["telescopes"]] == ["aaaaaaaa1111", "bbbbbbbb2222"] and d["online"] == 1
    on, off = d["telescopes"]
    assert on["online"] and on["name"] == "FSQ106 pier 12" and on["enrolledAs"] == "Telescope 1"
    assert on["state"] == "imaging" and on["target"] == "M31"
    assert on["equipment"]["scale"] == 1.46 and on["equipment"]["field"] == [2.54, 1.7]
    assert on["equipment"]["filters"] == ["L", "Ha"]
    assert on["work"] == [{"project": "aaaaaa000001", "name": "M42 horse", "state": "accepted", "personal": False}]
    assert not off["online"] and off["seen"] is None and off["ageSeconds"] is None
    # Long after its last check-in it is offline, with when it was last seen.
    store._run("UPDATE agents SET seen = seen - 7200 WHERE id = 'aaaaaaaa1111'")
    late = client.get("/api/v1/site/telescopes", headers=BRAY).json()["telescopes"][0]
    assert not late["online"] and 7190 < late["ageSeconds"] < 7300
