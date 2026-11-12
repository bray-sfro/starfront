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
    assert on["work"] == [{"project": "aaaaaa000001", "name": "M42 horse", "state": "accepted", "active": False,
                           "lastSent": None, "lastNight": None, "personal": False}]
    assert on["replaces"] == [] and d["notSeenLately"] == []
    assert not off["online"] and off["seen"] is None and off["ageSeconds"] is None
    # Long after its last check-in it is offline, with when it was last seen.
    store._run("UPDATE agents SET seen = seen - 7200 WHERE id = 'aaaaaaaa1111'")
    late = client.get("/api/v1/site/telescopes", headers=BRAY).json()["telescopes"][0]
    assert not late["online"] and 7190 < late["ageSeconds"] < 7300


FSQ = {"name": "Default", "focalLength": 389.0, "pixelSize": 3.76, "sensorWidth": 9576, "sensorHeight": 6388,
       "filters": {"L": None}, "colour": False, "client": "nina"}


def _agent(store, agent_id, name, created, seen, profile, presence=None, owner_id="u-bray"):
    store.add_agent(agent_id, "rig-secret-" + agent_id, name, "astrofalls", owner_id)
    store.seen(agent_id, profile, presence or {"telescope": name, "state": "idle"})
    store._run("UPDATE agents SET created = ?, seen = ? WHERE id = ?", created, seen, agent_id)


def test_a_telescope_connected_again_is_one_card_not_one_per_enrolment(tmp_path):
    import time as _t
    client, store = build(tmp_path)
    now = _t.time()
    day = 86400.0
    # The N.I.N.A. plugin on one rig, connected four times: each enrolment
    # stopped checking in just before the next one was made.
    _agent(store, "nina00000001", "Default", now - 40 * day, now - 30 * day, dict(FSQ, sensorWidth=None))
    _agent(store, "nina00000002", "Default", now - 30 * day + 300, now - 20 * day, FSQ)
    _agent(store, "nina00000003", "Default", now - 20 * day + 60, now - 5 * day, FSQ)
    _agent(store, "nina00000004", "Default", now - 5 * day + 120, now - 60, FSQ)
    # Same optics, same name, but both checking in at once: two telescopes.
    _agent(store, "twin00000001", "Telescope 1", now - 50 * day, now - 30, dict(FSQ, client=""))
    _agent(store, "twin00000002", "Telescope 1", now - 45 * day, now - 90, dict(FSQ, client=""))
    # A different telescope under the same enrolment name is never folded.
    _agent(store, "vsd000000001", "Default", now - 25 * day, now - 4 * day, dict(FSQ, focalLength=300.0))
    # Never reported optics: nothing to tell it apart by, so it stays itself.
    _agent(store, "empty0000001", "Telescope 1", now - 60 * day, now - 59 * day, {"name": "Telescope 1"})
    d = client.get("/api/v1/site/telescopes", headers=BRAY).json()
    ids = sorted(c["id"] for c in d["telescopes"])
    assert ids == ["nina00000004", "twin00000001", "twin00000002", "vsd000000001"]
    nina = next(c for c in d["telescopes"] if c["id"] == "nina00000004")
    assert sorted(nina["replaces"]) == ["nina00000001", "nina00000002", "nina00000003"]
    assert abs(nina["firstAdded"] - (now - 40 * day)) < 5
    # Not seen in a month: listed apart, not among the telescopes in use.
    assert [c["id"] for c in d["notSeenLately"]] == ["empty0000001"]
    assert "rig-secret" not in client.get("/api/v1/site/telescopes", headers=BRAY).text
    assert "connected again" in client.get("/telescopes").text


def test_working_on_needs_the_rig_to_be_on_it_not_just_a_task(tmp_path):
    import time as _t
    client, store = build(tmp_path)
    now = _t.time()
    store.add_project("cccccc000004", "California Dreaming", "Bray", {"collectFiles": True}, owner_id="u-bray")
    store.add_project("dddddd000005", "Brand new", "Bray", {"collectFiles": True}, owner_id="u-bray")
    _agent(store, "fsq106000001", "Telescope 1", now - 90 * 86400, now - 30, dict(FSQ, client=""),
           {"telescope": "fsq106", "state": "imaging", "project": "aaaaaa000001"})
    for tid, project in (("t-m42", "aaaaaa000001"), ("t-cal", "cccccc000004"), ("t-new", "dddddd000005")):
        store.add_task({"id": tid, "project": project, "agent": "fsq106000001", "state": "accepted",
                        "version": 1, "issued": 1.0})
    # Its last night on California Dreaming was weeks ago; the target has since
    # left the rig's plan, but the server still holds the task as accepted.
    store._run("INSERT INTO contributions (id, project, agent, task, night, filter, seconds, accepted, received,"
               " payload) VALUES ('c1', 'cccccc000004', 'fsq106000001', 't-cal', '2026-10-19', 'L', 3600, 1, ?, '{}')",
               now - 23 * 86400)
    work = {w["name"]: w for w in client.get("/api/v1/site/telescopes", headers=BRAY).json()["telescopes"][0]["work"]}
    assert work["M42 horse"]["active"]                                   # pointed at it right now
    assert not work["California Dreaming"]["active"] and work["California Dreaming"]["lastNight"] == "2026-10-19"
    assert not work["Brand new"]["active"] and work["Brand new"]["lastNight"] is None
    # A night reported lately makes it "working on" again.
    store._run("UPDATE contributions SET received = ? WHERE id = 'c1'", now - 2 * 86400)
    work = {w["name"]: w for w in client.get("/api/v1/site/telescopes", headers=BRAY).json()["telescopes"][0]["work"]}
    assert work["California Dreaming"]["active"]
    page = client.get("/telescopes").text
    assert "'Joined'" in page and "nothing sent since" in page


def test_pages_carry_the_help_form_and_the_plan_controls(tmp_path):
    client, _ = build(tmp_path)
    me = client.get("/me").text
    for part in ('id="sfHelpDlg"', 'id="sfFoot" hidden', 'id="plans"', 'id="linkForm"', "Manage plan",
                 "/api/v1/me/billing/link", "/api/v1/support"):
        assert part in me, part
    assert 'id="sfHelpDlg"' in client.get("/telescopes").text
