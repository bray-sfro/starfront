"""Auto-upload from telescopes: resumable, hashed, gated, throttled."""

from __future__ import annotations

import hashlib
import sys
import time
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
from server.ratelimit import Limiter  # noqa: E402
from server.store import Store  # noqa: E402
from fitsfake import light_cards, write_fits  # noqa: E402

RIG = {"authorization": "Bearer rig-a"}
RIG_B = {"authorization": "Bearer rig-b"}
STRANGER = {"authorization": "Bearer rig-c"}
M31 = {"ra": 10.68, "dec": 41.27, "width": 3.0, "height": 2.0}


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(agentuploads, "AGENT_KEEP_FREE", 0)
    store = Store(tmp_path / "db.sqlite")
    app = FastAPI()
    owns = lambda who, project: project.get("owner_id") == who["id"]  # noqa: E731
    files = Files(app, store, tmp_path / "files", lambda t: None, owns, [], "http://t", lambda: "c")
    up = AgentUploads(app, store, files)
    store.upsert_user("u-bray", "web-x", "astrofalls", "", [])
    store.add_agent("aaaaaaaa1111", "rig-a", "fsq106", "astrofalls", "u-bray")
    store.add_agent("bbbbbbbb2222", "rig-b", "vsd100", "astrofalls", "u-bray")
    store.add_agent("cccccccc3333", "rig-c", "other", "someone", "u-bray")
    store.add_project("p1", "M31", "Bray", {"collectFiles": True, "autoUpload": True,
                                            "region": M31, "fileQuotaGB": 10}, owner_id="u-bray")
    for i, agent in enumerate(("aaaaaaaa1111", "bbbbbbbb2222")):
        store.add_task({"id": f"t{i}", "project": "p1", "agent": agent, "state": "accepted",
                        "version": 1, "issued": time.time()})
    return TestClient(app), store, tmp_path, files, up


def frame(tmp, name, **cards):
    cards.setdefault("PROJID", "p1")
    return write_fits(tmp / name, light_cards(**cards), width=256, height=200).read_bytes()


def send(client, name, blob, kind="light", headers=RIG, chunk=10000, stop_after=None):
    sha = hashlib.sha256(blob).hexdigest()
    r = client.post("/api/v1/agent/uploads", headers=headers,
                    json={"project": "p1", "name": name, "kind": kind, "bytes": len(blob), "sha256": sha})
    assert r.status_code == 200, r.text
    body = r.json()
    if body["already"]:
        return body
    off = body["offset"]
    sent = 0
    while off < len(blob):
        if stop_after is not None and sent >= stop_after:
            return {"id": body["id"], "offset": off, "partial": True}
        piece = blob[off:off + chunk]
        r = client.put(f"/api/v1/agent/uploads/{body['id']}?offset={off}", content=piece, headers=headers)
        assert r.status_code == 200, r.text
        off = r.json()["offset"]
        sent += 1
    r = client.post(f"/api/v1/agent/uploads/{body['id']}/finish", headers=headers)
    return r.json() | {"_code": r.status_code}


def test_light_lands_under_owner_with_rig_prefix(world):
    client, store, tmp, files, _ = world
    blob = frame(tmp, "M31_Ha_0001.fits")
    out = send(client, "M31_Ha_0001.fits", blob)
    assert out["_code"] == 200 and out["name"] == "aaaaaaaa-M31_Ha_0001.fits"
    assert out["status"] == "pending"
    row = store.file("p1", "u-bray", "aaaaaaaa-M31_Ha_0001.fits", "light")
    assert row["telescope"] == "fsq106" and row["agent"] == "aaaaaaaa1111"
    assert row["sha256"] == hashlib.sha256(blob).hexdigest() and row["task"] == "t0"
    assert files.path_of("p1", "u-bray", row["name"]).read_bytes() == blob
    assert store.uploads() == []


def test_two_rigs_same_name_are_two_files(world):
    client, store, tmp, *_ = world
    a = frame(tmp, "a.fits", EXPTIME=300.0)
    b = frame(tmp, "b.fits", EXPTIME=600.0)
    assert send(client, "L_0001.fits", a)["_code"] == 200
    assert send(client, "L_0001.fits", b, headers=RIG_B)["_code"] == 200
    names = sorted(r["name"] for r in store.files_in("p1", "u-bray"))
    assert names == ["aaaaaaaa-L_0001.fits", "bbbbbbbb-L_0001.fits"]


def test_resume_after_cut_and_content_dedupe(world):
    client, store, tmp, *_ = world
    blob = frame(tmp, "x.fits")
    part = send(client, "x.fits", blob, stop_after=2)
    assert part["partial"] and part["offset"] == 20000
    # A new start with the same file resumes at the same session and offset.
    sha = hashlib.sha256(blob).hexdigest()
    r = client.post("/api/v1/agent/uploads", headers=RIG,
                    json={"project": "p1", "name": "x.fits", "bytes": len(blob), "sha256": sha}).json()
    assert r["id"] == part["id"] and r["offset"] == 20000
    # A wrong offset is told the right one.
    r2 = client.put(f"/api/v1/agent/uploads/{r['id']}?offset=0", content=b"x" * 10, headers=RIG)
    assert r2.status_code == 409 and r2.json()["offset"] == 20000
    assert client.get(f"/api/v1/agent/uploads/{r['id']}", headers=RIG).json()["offset"] == 20000
    assert send(client, "x.fits", blob)["_code"] == 200
    # Same content again, even under another name: already here.
    again = send(client, "renamed.fits", blob)
    assert again["already"] is True


def test_sha_mismatch_is_refused_and_forgotten(world):
    client, store, tmp, *_ = world
    blob = frame(tmp, "x.fits")
    r = client.post("/api/v1/agent/uploads", headers=RIG,
                    json={"project": "p1", "name": "x.fits", "bytes": len(blob), "sha256": "0" * 64}).json()
    client.put(f"/api/v1/agent/uploads/{r['id']}?offset=0", content=blob, headers=RIG)
    f = client.post(f"/api/v1/agent/uploads/{r['id']}/finish", headers=RIG)
    assert f.status_code == 422 and store.uploads() == [] and store.files_in("p1") == []


def test_gates(world):
    client, store, tmp, *_ = world
    blob = frame(tmp, "x.fits")
    sha = hashlib.sha256(blob).hexdigest()
    start = lambda h=RIG: client.post("/api/v1/agent/uploads", headers=h, json={  # noqa: E731
        "project": "p1", "name": "x.fits", "bytes": len(blob), "sha256": sha})
    assert start(STRANGER).status_code == 403            # no accepted task
    assert start({"authorization": "Bearer nope"}).status_code == 401
    p = store.project("p1")
    store.set_project("p1", {**p["payload"], "autoUploadAgents": ["bbbbbbbb2222"]})
    assert start().status_code == 409                    # not on the allowlist
    assert start(RIG_B).status_code == 200
    store.set_project("p1", {**p["payload"], "autoUpload": False})
    r = start(RIG_B)
    assert r.status_code == 409 and "auto-upload is off" in r.json()["detail"]
    assert client.get("/api/v1/agent/uploads?project=p1", headers=RIG_B).json()["enabled"] is False


def test_wrong_target_light_refused_but_calibration_is_not_checked(world):
    client, store, tmp, *_ = world
    other = frame(tmp, "o.fits", PROJID="someelse")
    assert send(client, "o.fits", other)["_code"] == 403
    far = write_fits(tmp / "f.fits", light_cards(RA=83.8, DEC=-5.4), 256, 200).read_bytes()
    out = send(client, "f.fits", far)
    assert out["_code"] == 403 and "from the collaboration's field" in out["detail"]
    near = write_fits(tmp / "n.fits", light_cards(OBJCTRA="00 42 44", OBJCTDEC="+41 16 09"), 256, 200).read_bytes()
    assert send(client, "n.fits", near)["_code"] == 200
    flat = write_fits(tmp / "fl.fits", light_cards(IMAGETYP="FLAT", EXPTIME=2.0), 256, 200).read_bytes()
    assert send(client, "MasterFlat_Ha.fits", flat, kind="flat")["_code"] == 200


def test_quota_and_disk_floor_pause(world, monkeypatch):
    client, store, tmp, files, _ = world
    p = store.project("p1")
    store.set_project("p1", {**p["payload"], "fileQuotaGB": 0.0001})   # about 107 KB
    big = frame(tmp, "b.fits") * 2
    r = client.post("/api/v1/agent/uploads", headers=RIG, json={
        "project": "p1", "name": "b.fits", "bytes": 200_000, "sha256": "a" * 64})
    assert r.status_code == 507 and int(r.headers["retry-after"]) == agentuploads.FULL_PAUSE
    store.set_project("p1", p["payload"])
    monkeypatch.setattr(agentuploads, "AGENT_KEEP_FREE", 10 ** 15)
    r = client.post("/api/v1/agent/uploads", headers=RIG, json={
        "project": "p1", "name": "b.fits", "bytes": len(big), "sha256": "a" * 64})
    assert r.status_code == 507 and "paused" in r.json()["detail"]
    assert client.get("/api/v1/agent/uploads?project=p1", headers=RIG).json()["pausedSeconds"] > 0


def test_reservations_count_against_quota(world):
    client, store, tmp, *_ = world
    p = store.project("p1")
    store.set_project("p1", {**p["payload"], "fileQuotaGB": 150_000 / 1024 ** 3})
    ok = client.post("/api/v1/agent/uploads", headers=RIG, json={
        "project": "p1", "name": "a.fits", "bytes": 100_000, "sha256": "a" * 64})
    assert ok.status_code == 200
    second = client.post("/api/v1/agent/uploads", headers=RIG_B, json={
        "project": "p1", "name": "b.fits", "bytes": 100_000, "sha256": "b" * 64})
    assert second.status_code == 507


def test_one_chunk_at_a_time_per_rig_and_global_cap(world, monkeypatch):
    *_, up = world
    up.acquire("aaaaaaaa1111")
    with pytest.raises(Exception) as e:
        up.acquire("aaaaaaaa1111")
    assert e.value.status_code == 429
    monkeypatch.setattr(agentuploads, "GLOBAL_STREAMS", 1)
    with pytest.raises(Exception) as e:
        up.acquire("bbbbbbbb2222")
    assert e.value.status_code == 429
    up.release("aaaaaaaa1111")
    up.acquire("bbbbbbbb2222")


def test_calibration_needs_then_ready(world):
    client, store, tmp, *_ = world
    send(client, "L1.fits", frame(tmp, "l.fits"))
    st = client.get("/api/v1/agent/uploads?project=p1", headers=RIG).json()
    assert st["enabled"] and st["lights"] == {"pending": 1}
    kinds = sorted(k for n in st["calibrationNeeds"] for k in n["kinds"])
    assert kinds == ["bias", "dark", "flat"]
    need = next(n for n in st["calibrationNeeds"] if n["kinds"] == ["flat"])
    assert need["filterRaw"] == "Ha" and need["lights"] == 1
    send(client, "MasterFlat.fits", write_fits(tmp / "f.fits", light_cards(IMAGETYP="FLAT", EXPTIME=2.0), 256, 200).read_bytes(), kind="flat")
    send(client, "MasterDark.fits", write_fits(tmp / "d.fits", light_cards(IMAGETYP="DARK"), 256, 200).read_bytes(), kind="dark")
    st = client.get("/api/v1/agent/uploads?project=p1", headers=RIG).json()
    assert st["lights"] == {"ready": 1} and st["calibrationNeeds"] == []


def test_stale_sessions_swept(world):
    client, store, tmp, files, up = world
    r = client.post("/api/v1/agent/uploads", headers=RIG, json={
        "project": "p1", "name": "a.fits", "bytes": 1000, "sha256": "a" * 64}).json()
    client.put(f"/api/v1/agent/uploads/{r['id']}?offset=0", content=b"S" * 100, headers=RIG)
    assert up.part(r["id"]).is_file()
    assert up.sweep(now=time.time() + agentuploads.STALE + 10) == 1
    assert not up.part(r["id"]).exists() and store.uploads() == []


def test_page_assets_not_rate_limited_but_masters_are():
    lim = Limiter()
    for _ in range(200):
        assert lim.allow("GET", "/releases/page.js", "1.2.3.4")[0]
        assert lim.allow("GET", "/releases/p1/v2/preview_Ha.jpg", "1.2.3.4")[0]
    hits = [lim.allow("GET", "/releases/p1/v2/master_L.fits.fz", "1.2.3.4")[0] for _ in range(61)]
    assert hits.count(False) == 1
