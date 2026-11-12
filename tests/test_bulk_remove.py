"""Removing many files at once from the files page."""
from test_files_rules_api import put, world  # noqa: F401

OWNER = {"authorization": "Bearer owner-token"}
ALICE = {"authorization": "Bearer alice-token"}


def _count(files, monkeypatch):
    calls = []
    real = files.recheck
    monkeypatch.setattr(files, "recheck", lambda p, u, only_light=None: calls.append(u) or real(p, u, only_light))
    return calls


def test_bulk_remove_own_and_one_recheck(world, monkeypatch):  # noqa: F811
    client, store, tmp, files = world
    put(client, tmp, "L_Panel_3_0001.fits", "light")
    put(client, tmp, "L_Panel_3_0002.fits", "light")
    put(client, tmp, "flat_0001.fits", "flat", IMAGETYP="FLAT", EXPTIME=2.0)
    put(client, tmp, "dark_0001.fits", "dark", IMAGETYP="DARK")
    listing = client.get("/api/v1/projects/p1/files", headers=ALICE).json()["files"]
    light = next(f for f in listing if f["kind"] == "light")
    assert light["panel"] == "3" and light["filter"] and light["night"]
    calls = _count(files, monkeypatch)
    body = {"files": [{"name": "L_Panel_3_0001.fits", "kind": "light"},
                      {"name": "flat_0001.fits", "kind": "flat"},
                      {"name": "dark_0001.fits", "kind": "dark"},
                      {"name": "nope.fits", "kind": "light"}]}
    r = client.post("/api/v1/projects/p1/files/remove", json=body, headers=ALICE).json()
    assert r["removed"] == 3 and r["failed"] == 1
    assert calls == ["u-alice"]
    assert not files.path_of("p1", "u-alice", "flat_0001.fits", "flat").exists()
    left = [f["name"] for f in client.get("/api/v1/projects/p1/files", headers=ALICE).json()["files"]]
    assert left == ["L_Panel_3_0002.fits"]


def test_owner_removes_others(world):  # noqa: F811
    client, store, tmp, files = world
    put(client, tmp, "bad_0001.fits", "light")
    r = client.post("/api/v1/projects/p1/files/remove", headers=OWNER,
                    json={"files": [{"name": "bad_0001.fits", "kind": "light", "user": "u-alice"}]}).json()
    assert r["removed"] == 1 and r["results"][0]["ok"]
    assert store.files_in("p1", "u-alice") == []


def test_non_owner_refused_for_others(world):  # noqa: F811
    client, store, tmp, files = world
    put(client, tmp, "mine_0001.fits", "light", token="owner-token")
    r = client.post("/api/v1/projects/p1/files/remove", headers=ALICE,
                    json={"files": [{"name": "mine_0001.fits", "kind": "light", "user": "u-owner"}]}).json()
    assert r["removed"] == 0 and r["results"][0]["error"] == "not your file"
    assert len(store.files_in("p1", "u-owner")) == 1


def test_empty_list_refused(world):  # noqa: F811
    client, *_ = world
    assert client.post("/api/v1/projects/p1/files/remove", json={"files": []}, headers=ALICE).status_code == 400
