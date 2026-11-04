"""The coordinator's Collecting files / Rig auto-upload switches on the files page."""
from test_files_rules_api import world  # noqa: F401


def test_owner_switches_collecting_and_autoupload(world):  # noqa: F811
    client, store, _tmp, _ = world
    h = {"authorization": "Bearer owner-token"}
    r = client.put("/api/v1/projects/p1/collecting", json={"autoUpload": True}, headers=h)
    assert r.status_code == 200 and r.json() == {"collecting": True, "autoUpload": True}
    assert store.project("p1")["payload"]["autoUpload"] is True
    listed = client.get("/api/v1/files/projects", headers=h).json()["projects"]
    assert listed[0]["autoUpload"] is True and listed[0]["collecting"] is True
    r = client.put("/api/v1/projects/p1/collecting", json={"collectFiles": False}, headers=h)
    assert r.json() == {"collecting": False, "autoUpload": True}


def test_only_the_coordinator_switches(world):  # noqa: F811
    client, store, _tmp, _ = world
    r = client.put("/api/v1/projects/p1/collecting", json={"collectFiles": False},
                   headers={"authorization": "Bearer alice-token"})
    assert r.status_code == 403 and store.project("p1")["payload"]["collectFiles"] is True
