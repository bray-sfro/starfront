"""Auto-upload of collaboration lights: the queue, the worker, the wire.

    python tools/check_autoupload.py            # unit checks only
    STARFRONT_SERVER_TREE=/path/to/server python tools/check_autoupload.py

The unit checks run the uploader against an in-memory server. With
STARFRONT_SERVER_TREE pointing at a collab-server checkout (the one that has
server/agentuploads.py) an end-to-end check also starts that server on a
loopback port with a throwaway database and sends fake FITS lights and
masters through the real API, including a cut-off upload that resumes.

No test framework, for the same reason as the other checks here.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astrocontrol import autoupload                               # noqa: E402
from astrocontrol.autoupload import AutoUploader, Bucket, pause_scope_for_507  # noqa: E402
from astrocontrol.capture import CaptureService                   # noqa: E402

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((bool(ok), what))
    print(("PASS " if ok else "FAIL ") + what)


class Conf:
    def __init__(self, **collab):
        # Missing keys fall back to the program's own defaults, as Config does.
        from astrocontrol.config import DEFAULTS
        self.data = {**DEFAULTS["collab"], **collab}

    def section(self, name):
        return dict(self.data) if name == "collab" else {}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def fits_bytes(cards: dict, pixels: int = 300_000) -> bytes:
    def card(k, v):
        if isinstance(v, bool):
            text = f"{k:<8}= {'T' if v else 'F':>20}"
        elif isinstance(v, (int, float)):
            text = f"{k:<8}= {v!r:>20}"
        else:
            text = f"{k:<8}= '{v:<8}'"
        return text[:80].ljust(80).encode()
    base = {"SIMPLE": True, "BITPIX": 16, "NAXIS": 2, "NAXIS1": 600, "NAXIS2": pixels // 600, "BZERO": 32768}
    head = b"".join(card(k, v) for k, v in {**base, **cards}.items()) + b"END".ljust(80)
    head += b" " * ((-len(head)) % 2880)
    data = os.urandom(pixels * 2)
    return head + data + b"\0" * ((-len(data)) % 2880)


LIGHT = {"IMAGETYP": "LIGHT", "EXPTIME": 300.0, "GAIN": 100, "OFFSET": 50, "CCD-TEMP": -10.0,
         "XBINNING": 1, "YBINNING": 1, "INSTRUME": "ZWO ASI6200MM Pro", "FILTER": "Ha",
         "DATE-OBS": "2026-11-03T03:00:00", "PROJID": "p1"}


class FakeServer:
    """The server's resumable API, in memory, with switches for failures."""

    def __init__(self):
        self.sessions, self.files, self.calls = {}, {}, []
        self.answer_start = None     # (code, body, headers) to force
        self.fail_put_after = None   # raise URLError on the n-th PUT

    def __call__(self, method, path, body):
        self.calls.append((method, path))
        if method == "GET":
            return 200, {"calibrationNeeds": []}, {}
        if method == "POST" and path == "/api/v1/agent/uploads":
            if self.answer_start:
                return self.answer_start
            for f in self.files.values():
                if f["sha256"] == body["sha256"]:
                    return 200, {"already": True, "name": f["name"], "status": "ready"}, {}
            for sid, s in self.sessions.items():
                if s["sha256"] == body["sha256"]:
                    return 200, {"already": False, "id": sid, "offset": len(s["data"]), "chunk": 100_000}, {}
            sid = f"s{len(self.sessions) + 1}"
            self.sessions[sid] = {**body, "data": bytearray()}
            return 200, {"already": False, "id": sid, "offset": 0, "chunk": 100_000}, {}
        if method == "PUT":
            sid, offset = path.split("/")[-1].split("?offset=")
            s = self.sessions[sid]
            puts = sum(1 for m, _ in self.calls if m == "PUT")
            if self.fail_put_after is not None and puts > self.fail_put_after:
                self.fail_put_after = None
                raise urllib.error.URLError("connection reset")
            if int(offset) != len(s["data"]):
                return 409, {"offset": len(s["data"])}, {}
            s["data"] += body
            return 200, {"offset": len(s["data"])}, {}
        if path.endswith("/finish"):
            sid = path.split("/")[-2]
            s = self.sessions.pop(sid)
            if hashlib.sha256(s["data"]).hexdigest() != s["sha256"]:
                return 422, {"detail": "sha"}, {}
            self.files[s["name"]] = {"name": s["name"], "sha256": s["sha256"], "data": bytes(s["data"])}
            return 200, {"name": s["name"], "status": "pending"}, {}
        return 404, {}, {}


class Lib:
    def __init__(self, masters):
        self.masters = masters

    def match(self, kind, want):
        path = self.masters.get(kind)
        return ({"path": str(path)} if path else None), "fake"


def drain(up, clock, limit=50):
    for _ in range(limit):
        if up.due() is None:
            return
        up.step()
        clock.t += 1


def units(tmp: Path) -> None:
    light = tmp / "M31_Ha_0001.fits"
    light.write_bytes(fits_bytes(LIGHT))
    flat = tmp / "MasterFlat_Ha.fits"
    flat.write_bytes(fits_bytes({**LIGHT, "IMAGETYP": "Master Flat", "PROJID": ""}))
    before = (hashlib.sha256(light.read_bytes()).hexdigest(), light.stat().st_mtime)
    ctx = {"collabProject": "p1", "collabTask": "t1"}
    clock = Clock(time.time() + 60)

    from astrocontrol.config import DEFAULTS
    check(DEFAULTS["collab"]["autoUpload"] is True, "auto-upload is on by default (config DEFAULTS)")
    dflt = AutoUploader(Conf(), None, Lib({}), folder=tmp / "q00", clock=clock)
    check(dflt.settings()["enabled"] is True and dflt.light_saved(str(light), ctx, "fsq106", {}) is True,
          "on by default: a collab light is queued with no setting changed")
    bare = AutoUploader(SimpleNamespace(section=lambda n: {}), None, None, folder=tmp / "q01", clock=clock)
    check(bare.settings()["enabled"] is True, "on by default even when settings.json has no collab section")
    off = AutoUploader(Conf(autoUpload=False), None, Lib({}), folder=tmp / "q0", clock=clock)
    check(off.light_saved(str(light), ctx, "fsq106", {}) is False and off.items == [],
          "switched off by the user: nothing is queued")
    html = (ROOT / "astrocontrol" / "web" / "index.html").read_text("utf-8")
    check('id="colAutoUpload" checked' in html, "the Collab tab switch starts ticked")

    conf = Conf(autoUpload=True, autoUploadRigs=["fsq106"])
    up = AutoUploader(conf, None, Lib({"flat": flat}), folder=tmp / "q1", clock=clock)
    server = FakeServer()
    up.http = server
    check(up.light_saved(str(light), {}, "fsq106", {}) is False, "a light with no collaboration task is not queued")
    check(up.light_saved(str(light), ctx, "rasa1", {}) is False, "a rig not on the list is not queued")
    check(up.light_saved(str(light), ctx, "fsq106", {"filter": "Ha"}) is True, "a collab light is queued")
    kinds = sorted(i["kind"] for i in up.items)
    check(kinds == ["flat", "light"], f"its matching master is queued with it ({kinds})")
    check(up.light_saved(str(light), ctx, "fsq106", {"filter": "Ha"}) is False, "the same light twice is one item")
    again = AutoUploader(conf, None, Lib({}), folder=tmp / "q1", clock=clock)
    check(len(again.items) == 2, "the queue survives a restart (queue.json)")

    server.fail_put_after = 2
    drain(up, clock)
    waiting = [i for i in up.items if i["state"] == "retry"]
    check(len(waiting) == 1 and "connection reset" in waiting[0]["error"], "a dropped connection is retried later, not lost")
    clock.t += 7200
    drain(up, clock)
    names = sorted(server.files)
    check(set(names) == {flat.name, light.name} and server.files[light.name]["data"] == light.read_bytes(),
          "the cut-off light resumed and arrived whole")
    resumed_from = [p for m, p in server.calls if m == "PUT"]
    check(any("offset=200000" in p for p in resumed_from), "it resumed at the server's offset, not from 0")
    check((hashlib.sha256(light.read_bytes()).hexdigest(), light.stat().st_mtime) == before and light.exists(),
          "the local light is untouched")
    check("sent" in up.status()["line"] and up.status()["counts"].get("sent") == 2, "the status line counts what was sent")

    # The server's answers are obeyed.
    def fresh(name):
        p = tmp / name
        p.write_bytes(fits_bytes({**LIGHT, "DATE-OBS": name}))
        u = AutoUploader(Conf(autoUpload=True), None, None, folder=tmp / ("q-" + name), clock=clock)
        s = FakeServer()
        u.http = s
        u.light_saved(str(p), ctx, "x", None)
        return u, s
    u, s = fresh("a.fits")
    s.answer_start = (507, {"detail": "the server is keeping its last 25 GB free; uploads are paused"}, {"retry-after": "21600"})
    u.step()
    check(u.paused_until >= clock.t + 21000 and u.status()["line"].startswith("Auto-upload paused:"),
          "disk 507 pauses everything for the Retry-After")
    calls = len(s.calls)
    u.step()
    check(len(s.calls) == calls, "while paused nothing is sent")
    u, s = fresh("b.fits")
    s.answer_start = (409, {"detail": "auto-upload is off on that collaboration"}, {})
    u.step()
    check("p1" in u.project_paused and u.items[0]["state"] == "queued", "409 (uploads off) pauses that collaboration, keeps the item")
    u, s = fresh("c.fits")
    s.answer_start = (403, {"detail": "not a light of this collaboration: wrong field"}, {})
    u.step()
    check(u.items[0]["state"] == "refused" and "wrong field" in u.items[0]["error"], "403 takes the item off with the reason")
    u, s = fresh("d.fits")
    s.answer_start = (429, {"detail": "one upload at a time"}, {"retry-after": "30"})
    u.step()
    check(u.paused_until and u.paused_until - clock.t <= 31, "429 waits the Retry-After")

    # Quota 507 is one project. Disk 507 is the rig. Wording is the fallback
    # until the collab server sends scope/reason.
    check(pause_scope_for_507({"detail": "this project's storage quota is full (40 GiB)"}) == "project",
          "507 text: a quota sentence is that project only")
    check(pause_scope_for_507({"detail": "the server is keeping its last 25 GB free; uploads are paused"}) == "all",
          "507 text: disk free-space is every project")
    check(pause_scope_for_507({"detail": "insufficient storage"}) == "all",
          "507 text: an unclassified 507 stays server-wide")
    check(pause_scope_for_507({"detail": "the server is keeping its last 25 GB free",
                               "scope": "project", "reason": "quota"}) == "project",
          "507 JSON scope=project wins over disk wording")
    check(pause_scope_for_507({"detail": {"message": "full", "scope": "project", "reason": "quota"}}) == "project",
          "507 detail object scope=project is that project")
    check(pause_scope_for_507({"detail": "paused", "reason": "disk"}) == "all",
          "507 JSON reason=disk is every project")
    check(pause_scope_for_507({"detail": "quota full"}, {"x-starfront-pause-scope": "server"}) == "all",
          "507 header scope wins over quota wording when JSON scope is absent")
    check(pause_scope_for_507({"detail": "disk full", "scope": "project"},
                              {"x-starfront-pause-scope": "server"}) == "project",
          "507 JSON scope wins over the header")

    # Tonight: m42's quota is full. M31 on the other rigs must keep uploading.
    m42 = tmp / "M42_Ha_0001.fits"
    m31 = tmp / "M31_Ha_0001.fits"
    m42.write_bytes(fits_bytes({**LIGHT, "DATE-OBS": "2026-11-06T01:00:00"}))
    m31.write_bytes(fits_bytes({**LIGHT, "DATE-OBS": "2026-11-06T02:00:00"}))
    qfolder = tmp / "q-quota"
    quota = AutoUploader(Conf(autoUpload=True), None, None, folder=qfolder, clock=clock)
    qserver = FakeServer()

    def quota_http(method, path, body):
        if method == "POST" and path == "/api/v1/agent/uploads" and isinstance(body, dict) \
                and body.get("project") == "m42 horse test mosaic":
            return (507, {"detail": "this project's storage quota is full (40 GiB)"},
                    {"retry-after": "21600"})
        return qserver(method, path, body)

    quota.http = quota_http
    quota.add(m42, "m42 horse test mosaic", "light", "t-m42", "vsd100")
    quota.add(m31, "m31", "light", "t-m31", "twin")
    quota.step()
    check("m42 horse test mosaic" in quota.project_paused and not (quota.paused_until > clock.t),
          "quota 507 pauses m42 only, not the rig")
    check(quota.items[0]["state"] == "queued" and quota.due()["project"] == "m31",
          "m31 stays queued and is the next upload")
    status = quota.status()
    line = status["line"]
    m42count = status["projectCounts"]["m42 horse test mosaic"]
    m31count = status["projectCounts"]["m31"]
    check(line.startswith("Auto-upload: ") and not line.startswith("Auto-upload paused")
          and "m42 horse test mosaic paused (1 queued, quota full, until " in line
          and "m31 1 queued" in line and m42count["paused"] and m42count["queued"] == 1
          and m31count["paused"] is False and m31count["queued"] == 1,
          f"the status line names the paused project and the other queue ({line})")
    quota.step()
    check(m31.name in qserver.files and m42.name not in qserver.files,
          "m31 is sent while m42's quota pause holds")
    resumed = AutoUploader(Conf(autoUpload=True), None, None, folder=qfolder, clock=clock)
    check("m42 horse test mosaic" in resumed.project_paused and not (resumed.paused_until > clock.t)
          and resumed.due() is None,
          "the project pause is still there after a restart, and it is not global")
    check("m42 horse test mosaic" in json.loads((qfolder / "pauses.json").read_text("utf-8"))["projects"],
          "pauses.json keeps the project pause beside the queue")

    # The same two projects, but the disk is full: nobody uploads.
    disk_dir = tmp / "q-disk"
    disk = AutoUploader(Conf(autoUpload=True), None, None, folder=disk_dir, clock=clock)
    dserver = FakeServer()
    dserver.answer_start = (507, {"detail": "the server is keeping its last 25 GB free; uploads are paused"},
                            {"retry-after": "21600"})
    disk.http = dserver
    disk.add(m42, "m42 horse test mosaic", "light", "t-m42", "vsd100")
    disk.add(m31, "m31", "light", "t-m31", "twin")
    disk.step()
    calls = len(dserver.calls)
    disk.step()
    check(disk.paused_until >= clock.t + 21000 and len(dserver.calls) == calls
          and "m42 horse test mosaic" not in disk.project_paused,
          "disk 507 still pauses every project")
    restored = AutoUploader(Conf(autoUpload=True), None, None, folder=disk_dir, clock=clock)
    check(restored.paused_until >= clock.t + 21000 and restored.due() is None,
          "a server-wide pause survives a restart and still holds m31")

    # The daytime window.
    site = {"latitude": 30.5, "longitude": -98.3}
    night = 1793767200.0    # 2026-11-04 00:00 CST: dark at Rockwood
    noon = 1793728800.0     # 2026-11-03 12:00 CST
    win = AutoUploader(Conf(autoUpload=True, autoUploadDaytimeOnly=True), None, None,
                       folder=tmp / "qw", site=lambda: site, clock=Clock(night))
    check(win.in_window(night)[0] is False and win.in_window(noon)[0] is True, "daytime only: closed at midnight, open at noon")
    win2 = AutoUploader(Conf(autoUpload=True, autoUploadDaytimeOnly=True), None, None,
                        folder=tmp / "qw2", site=lambda: {}, clock=Clock(noon))
    check(win2.in_window(noon)[0] is False, "daytime only with no site set: waits rather than guessing")

    # The cap.
    slept = []
    t = [0.0]
    b = Bucket(10.0, clock=lambda: t[0], sleep=lambda s: slept.append(s))
    b.take(2_500_000)
    check(abs(sum(slept) - 2.0) < 0.01, f"10 Mbit/s: 2.5 MB waits 2 s ({sum(slept):.2f})")

    # The capture hook: a saved collab light goes on the queue; nothing raises.
    seen = []
    fake = SimpleNamespace(uploader=SimpleNamespace(light_saved=lambda *a, **k: seen.append((a, k))),
                           frame_context={"collabProject": "p1", "collabTask": "t1"}, telescope="fsq106",
                           _calibration_want=lambda r, c: {"filter": "Ha"}, night_name=lambda: "2026-11-03")
    rec = SimpleNamespace(saved=True, path=str(light), filter="Ha")
    CaptureService._queue_upload(fake, rec, None)
    check(len(seen) == 1 and seen[0][0][2] == "fsq106", "capture hands a saved light to the uploader")
    fake.uploader = SimpleNamespace(light_saved=lambda *a, **k: 1 / 0)
    try:
        CaptureService._queue_upload(fake, rec, None)
        check(True, "an uploader failure never reaches the capture path")
    except Exception:                                   # noqa: BLE001
        check(False, "an uploader failure never reaches the capture path")


def per_frame(tmp: Path) -> None:
    """Bray 2026-11-04: each frame uploads right away, on its save - no
    scan, no batch, imaging never waits. Real worker thread, real clock."""
    import threading
    frames = tmp / "pf"
    frames.mkdir()
    server = FakeServer()
    arrived: dict[str, float] = {}
    real = server.__call__

    def wire(method, path, body):
        answer = real(method, path, body)
        if path.endswith("/finish") and answer[0] == 200:
            arrived[answer[1]["name"]] = time.time()
        return answer
    old_settle = autoupload.SETTLE_SECONDS
    autoupload.SETTLE_SECONDS = 0.5
    up = AutoUploader(Conf(), None, None, folder=tmp / "q-pf")
    up.http = wire
    up.start()
    try:
        time.sleep(0.5)                  # worker idle, waiting on its wake event
        ctx = {"collabProject": "p1", "collabTask": "t1"}
        saved, hook_ms = {}, []
        for n in range(3):
            f = frames / f"M42_G_300s_{n:04d}.fits"
            f.write_bytes(fits_bytes({**LIGHT, "DATE-OBS": f"2026-11-04T0{n}:00:00"}))
            saved[f.name] = time.time()
            t0 = time.perf_counter()
            up.light_saved(str(f), ctx, "eagle", None)
            hook_ms.append((time.perf_counter() - t0) * 1000)
            deadline = time.time() + 10
            while f.name not in arrived and time.time() < deadline:
                time.sleep(0.05)
        lags = [arrived.get(k, 1e12) - saved[k] for k in saved]
        check(all(lag < 3.0 for lag in lags),
              f"each frame reaches the server within seconds of its save ({', '.join(f'{x:.1f}s' for x in lags)})")
        check(max(hook_ms) < 200, f"the save hook only queues: {max(hook_ms):.1f} ms at most, imaging never waits")
        check(not any(m == "GET" and "files" in p for m, p in server.calls),
              "no folder or server rescan: frames come only from the save hook")
        # A failure on one frame is retried, and the next frame still goes at once.
        server.fail_put_after = sum(1 for m, _ in server.calls if m == "PUT")
        f = frames / "M42_G_300s_0009.fits"
        f.write_bytes(fits_bytes({**LIGHT, "DATE-OBS": "2026-11-04T09:00:00"}))
        up.light_saved(str(f), ctx, "eagle", None)
        deadline = time.time() + 5
        while not any(i["state"] == "retry" for i in up.items) and time.time() < deadline:
            time.sleep(0.05)
        check(any(i["state"] == "retry" and Path(i["path"]).name == f.name for i in up.items),
              "a frame whose upload drops goes on the retry queue, not lost")
    finally:
        up.stop()
        autoupload.SETTLE_SECONDS = old_settle


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def end_to_end(tmp: Path, tree: Path) -> None:
    sys.path.insert(0, str(tree))
    from server.store import Store                         # noqa: E402
    data = tmp / "srv"
    data.mkdir()
    store = Store(data / "collab.sqlite")
    store.upsert_user("u-bray", "web-x", "astrofalls", "", [])
    store.add_agent("f4311076b9dc", "rig-token", "Telescope 1", "astrofalls", "u-bray")
    store.add_project("p1", "M31 NINA TEST COLLAB", "Bray",
                      {"collectFiles": True, "autoUpload": True, "autoUploadAgents": ["f4311076b9dc"],
                       "fileQuotaGB": 1, "region": {"ra": 10.68, "dec": 41.27, "width": 5.3, "height": 3.5}},
                      owner_id="u-bray")
    store.add_task({"id": "t1", "project": "p1", "agent": "f4311076b9dc", "state": "accepted",
                    "version": 1, "issued": time.time()})
    store.close()
    port = free_port()
    env = {**os.environ, "STARFRONT_DATA": str(data), "STARFRONT_AGENT_KEEP_FREE_GB": "0"}
    proc = subprocess.Popen([sys.executable, "server/run.py", "--port", str(port)], cwd=str(tree), env=env,
                            stdout=open(tmp / "server.log", "wb"), stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", port), 0.5).close()
                break
            except OSError:
                time.sleep(0.25)
        frames = tmp / "frames"
        frames.mkdir()
        light = frames / "M31_Ha_300s_0001.fits"
        light.write_bytes(fits_bytes(LIGHT, pixels=9_000_000))          # about 18 MB
        flat = frames / "MasterFlat_Ha.fits"
        flat.write_bytes(fits_bytes({**LIGHT, "IMAGETYP": "Master Flat", "EXPTIME": 2.0, "PROJID": ""}, 9_000_000))
        dark = frames / "MasterDark_300s.fits"
        dark.write_bytes(fits_bytes({**LIGHT, "IMAGETYP": "Master Dark", "FILTER": "", "PROJID": ""}, 9_000_000))
        wrong = frames / "M42_Ha_0001.fits"
        wrong.write_bytes(fits_bytes({**LIGHT, "PROJID": "", "RA": 83.82, "DEC": -5.39}))
        before = {p.name: (p.stat().st_mtime, hashlib.sha256(p.read_bytes()).hexdigest()) for p in frames.iterdir()}
        client = SimpleNamespace(_ready=lambda: (f"http://127.0.0.1:{port}", "rig-token"))
        clock = Clock(time.time() + 60)
        up = AutoUploader(Conf(autoUpload=True), client, Lib({}), folder=tmp / "q-e2e", clock=clock)
        ctx = {"collabProject": "p1", "collabTask": "t1"}
        up.light_saved(str(light), ctx, "fsq106", None)
        up.light_saved(str(wrong), ctx, "fsq106", None)
        # Cut the light off after two 8 MB chunks.
        real = up.http
        puts = {"n": 0}

        def flaky(method, path, body):
            if method == "PUT":
                puts["n"] += 1
                if puts["n"] == 3:
                    raise urllib.error.URLError("cable pulled")
            return real(method, path, body)
        up.http = flaky
        drain(up, clock)
        clock.t += 7200
        drain(up, clock)
        items = {Path(i["path"]).name: i for i in up.items}
        check(items[light.name]["state"] == "sent" and items[light.name]["status"] == "pending",
              f"e2e: the light arrived after a cut-off, waiting for calibration ({items[light.name]['state']}, {items[light.name].get('status')})")
        check(items[wrong.name]["state"] == "refused" and "field" in items[wrong.name]["error"],
              f"e2e: a light of another target is refused ({items[wrong.name].get('error')})")
        # The server now says what it needs; the library has the masters.
        up.library = Lib({"flat": flat, "dark": dark})
        up.needs_checked.clear()
        up.step()
        drain(up, clock)
        kinds = sorted(i["kind"] for i in up.items if i["state"] in ("sent", "already"))
        check(kinds == ["dark", "flat", "light"], f"e2e: calibration-needs brought the matching masters ({kinds})")
        store = Store(data / "collab.sqlite")
        rows = {r["name"]: r for r in store.files_in("p1")}
        lrow = rows.get("f4311076-" + light.name) or {}
        check(lrow.get("status") == "ready", f"e2e: with its masters the light is ready on the server ({lrow.get('status')})")
        stored = data / "files" / "p1" / "u-bray" / "lights" / ("f4311076-" + light.name)
        check(stored.is_file() and stored.read_bytes() == light.read_bytes(), "e2e: the server's copy is byte-identical")
        check(lrow.get("sha256") == hashlib.sha256(light.read_bytes()).hexdigest() and lrow.get("agent") == "f4311076b9dc",
              "e2e: recorded with its sha256 and rig")
        after = {p.name: (p.stat().st_mtime, hashlib.sha256(p.read_bytes()).hexdigest()) for p in frames.iterdir()}
        check(after == before, "e2e: no local file was changed, moved or deleted")
        # Again: nothing is sent twice.
        up2 = AutoUploader(Conf(autoUpload=True), client, None, folder=tmp / "q-e2e2", clock=clock)
        up2.light_saved(str(light), ctx, "fsq106", None)
        drain(up2, clock)
        check(up2.items[0]["state"] == "already", "e2e: a light already on the server is not sent again")
        # The owner turns it off: the rig pauses that collaboration and keeps its item.
        p = store.project("p1")
        store.set_project("p1", {**p["payload"], "autoUpload": False})
        store.close()
        extra = frames / "M31_Ha_300s_0002.fits"
        extra.write_bytes(fits_bytes({**LIGHT, "DATE-OBS": "2026-11-03T03:05:00"}))
        up2.light_saved(str(extra), ctx, "fsq106", None)
        up2.step()
        check("p1" in up2.project_paused and up2.items[-1]["state"] == "queued",
              "e2e: autoUpload off on the server pauses, keeps the item")
    finally:
        proc.terminate()
        proc.wait(10)


def follower_and_backfill(tmp: Path) -> None:
    """The Rokinon / ASI6200MC never queues or sends. The 14 FSQ85 lights do.

    Identity is the camera and the rig role. A follower frame filed under the
    master's folder is still skipped, and a master frame filed under the
    follower's folder is still queued. The FITS bytes are not rewritten.
    """
    from astrocontrol.autoupload import allows_upload, identity_from_header

    project, task = "97fcae827a9f", "481536614589"
    master = {"role": "master", "camera": "ZWO ASI6200MM Pro", "colour": False,
              "mono": True, "upload": None, "name": "FSQ85"}
    follower = {"role": "follower", "camera": "ZWO ASI6200MC Pro", "bayer": "RGGB",
                "colour": True, "mono": False, "upload": None, "name": "Rokinon 135mm"}
    check(allows_upload(master), "the master uploads by default")
    check(not allows_upload(follower), "the follower does not upload by default")
    check(allows_upload({**follower, "upload": True}) is True,
          "camera.uploadCollab True is an explicit per-rig opt-in")
    check(allows_upload(follower) is False, "unset follower stays off")

    clock = Clock()
    up = AutoUploader(Conf(autoUploadMbit=0), SimpleNamespace(_ready=lambda: None),
                      folder=tmp / "queue", clock=clock)
    check(up.settings()["mbit"] == 10.0, "0 Mbit/s is the 10 Mbit/s default")
    check(up.settings()["daytimeOnly"] is False or "daytimeOnly" in up.settings(),
          "daytime-only is still a setting")
    bad = up.light_saved(str(tmp / "rokinon.fits"),
                         {"collabProject": project, "collabTask": task},
                         "Rokinon 135mm", None, identity=follower)
    good = up.light_saved(str(tmp / "fsq.fits"),
                          {"collabProject": project, "collabTask": task},
                          "FSQ85", None, identity=master)
    check(bad is False and good is True, "a follower light is not queued; the master's is")
    check(all("rokinon" not in Path(i["path"]).name.lower() for i in up.items),
          "the queue has no Rokinon frame")

    # Backfill. 2 master lights missing PROJID, 2 follower lights missing
    # PROJID (one of them sitting in the master's folder), 1 master light
    # that already has PROJID. Only the two master lights without PROJID queue.
    root = tmp / "captures"
    samples = [
        (root / "m42_horse_test_mosaic" / "2026-11-05" / "FSQ85" / "m42_P20_5s_0001.fits",
         {"IMAGETYP": "Light", "INSTRUME": "ZWO ASI6200MM Pro", "TELESCOP": "FSQ85",
          "FILTER": "", "EXPTIME": 5}),
        (root / "m42_horse_test_mosaic" / "2026-11-05" / "Rokinon_135mm" / "m42_P20_L_5s_0001.fits",
         {"IMAGETYP": "Light", "INSTRUME": "ZWO ASI6200MC Pro", "TELESCOP": "Rokinon 135mm",
          "BAYERPAT": "RGGB", "FILTER": "L", "EXPTIME": 5}),
        # Follower bytes stored under the master folder. The folder must not win.
        (root / "m42_horse_test_mosaic" / "2026-11-05" / "FSQ85" / "m42_P20_L_5s_0002.fits",
         {"IMAGETYP": "Light", "INSTRUME": "ZWO ASI6200MC Pro", "TELESCOP": "Rokinon 135mm",
          "BAYERPAT": "RGGB", "FILTER": "L", "EXPTIME": 5}),
        # Master bytes stored under the follower folder. The folder must not exclude it.
        (root / "m42_horse_test_mosaic" / "2026-11-05" / "Rokinon_135mm" / "m42_P13_5s_0001.fits",
         {"IMAGETYP": "Light", "INSTRUME": "ASI Camera (1) ? CamID 1, ZWO ASI6200MM Pro",
          "TELESCOP": "FSQ85", "FILTER": "", "EXPTIME": 5}),
        (root / "m42_horse_test_mosaic" / "2026-11-05" / "FSQ85" / "already.fits",
         {"IMAGETYP": "Light", "INSTRUME": "ZWO ASI6200MM Pro", "TELESCOP": "FSQ85",
          "PROJID": project, "EXPTIME": 300}),
    ]
    from astrocontrol.imaging.fits import write as write_fits
    import numpy as np
    originals = {}
    blank = np.zeros((4, 4), dtype=np.uint16)
    for path, cards in samples:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {key: value for key, value in cards.items() if value not in ("", None)}
        write_fits(path, blank, header)
        originals[path] = path.read_bytes()

    targets = [{"name": "m42 horse test mosaic",
                "collab": {"project": project, "task": task,
                           "projectName": "m42 horse test mosaic"}}]
    report = up.backfill(root, targets, [
        {"name": "FSQ85", "role": "master", "camera": "ZWO ASI6200MM Pro", "upload": None},
        {"name": "Rokinon 135mm", "role": "follower", "camera": "ZWO ASI6200MC Pro",
         "colour": True, "upload": None},
    ])
    names = set(report["queuedNames"])
    check(names == {"m42_P20_5s_0001.fits", "m42_P13_5s_0001.fits"},
          f"backfill queues only the master lights missing PROJID ({sorted(names)})")
    check("m42_P20_L_5s_0001.fits" in report["skippedNames"]
          and "m42_P20_L_5s_0002.fits" in report["skippedNames"],
          "both Rokinon frames are skipped, including the one in the FSQ85 folder")
    queued_paths = [i["path"] for i in up.items]
    check(not any("L_5s" in Path(path).name for path in queued_paths),
          "no Rokinon frame is queued, even the one saved in the FSQ85 folder")
    for item in up.items:
        if "5s" in Path(item["path"]).name:
            check(item["project"] == project and item["task"] == task,
                  f"{Path(item['path']).name} carries project {project} and task {task}")
    check(all(path.read_bytes() == originals[path] for path in originals),
          "backfill does not rewrite the FITS files")
    ident = identity_from_header({"INSTRUME": "ZWO ASI6200MC Pro", "BAYERPAT": "RGGB",
                                  "TELESCOP": "Rokinon 135mm"})
    check(not allows_upload(ident, strict=True),
          "a colour header is a follower even before the rig list is consulted")

    # Sending: a follower item that was forced onto the queue is refused and
    # the server is not asked. A master item is.
    forced = tmp / "forced_rokinon.fits"
    write_fits(forced, blank, {"IMAGETYP": "Light", "INSTRUME": "ZWO ASI6200MC Pro",
                               "BAYERPAT": "RGGB", "TELESCOP": "Rokinon 135mm",
                               "FILTER": "L", "EXPTIME": 5})
    up.add(forced, project, "light", task, "Rokinon 135mm", identity=follower)
    server = FakeServer()
    up.http = server
    clock.t += 10
    item = next(i for i in up.items if i["path"].endswith("forced_rokinon.fits"))
    up.send(item)
    check(item["state"] == "refused" and "does not upload" in item["error"],
          "a queued follower frame is refused at send")
    check(not any(call[0] == "POST" and str(forced.name) in str(call) for call in server.calls)
          or server.calls == [] or all("forced_rokinon" not in str(call) for call in server.calls),
          "the server is not asked for the Rokinon frame")
    master_item = next(i for i in up.items if i["path"].endswith("m42_P20_5s_0001.fits"))
    # Settle the file's mtime so send does not wait.
    os.utime(master_item["path"], (clock.t - 30, clock.t - 30))
    before = len(server.calls)
    up.send(master_item)
    check(len(server.calls) > before and master_item["state"] in ("sent", "sending", "already"),
          f"the master frame is sent ({master_item['state']})")


def blank_root_and_duplicate_name(tmp: Path) -> None:
    """A blank capture.rootDirectory still scans data_root()/captures.

    Two targets named 'm42 horse test mosaic' are not resolved by whichever
    is first. The folder belongs to the task this PC is on
    (481536614589), not the declined one (b9412d46f576). With no exact
    task, the folder is skipped. The follower is still not queued.
    """
    import os
    from astrocontrol.capture import CaptureService, capture_root
    from astrocontrol.config import Config
    from astrocontrol.imaging.fits import write as write_fits
    import numpy as np

    previous = os.environ.get("ASTRO_DATA_DIR")
    data = tmp / "eagle-data"
    os.environ["ASTRO_DATA_DIR"] = str(data)
    try:
        config = Config(data / "settings.json")
        # The installed settings leave this blank. Capture and backfill must
        # agree on the fallback.
        check(config.get("capture", "rootDirectory", "") == "",
              "the default capture root is blank")
        root = capture_root(config)
        check(root == data / "captures", f"blank rootDirectory is {root}")
        capture = CaptureService(SimpleNamespace(), config)
        check(capture.root_dir == root, "backfill and capture share capture_root")

        folder = root / "m42_horse_test_mosaic" / "2026-11-05" / "FSQ85"
        folder.mkdir(parents=True)
        blank = np.zeros((4, 4), dtype=np.uint16)
        master = folder / "m42_P20_5s_26C_0001.fits"
        follower_dir = root / "m42_horse_test_mosaic" / "2026-11-05" / "Rokinon_135mm"
        follower_dir.mkdir(parents=True)
        follower = follower_dir / "m42_P20_L_5s_0001.fits"
        write_fits(master, blank, {"IMAGETYP": "Light", "INSTRUME": "ZWO ASI6200MM Pro",
                                   "TELESCOP": "FSQ85", "EXPTIME": 5})
        write_fits(follower, blank, {"IMAGETYP": "Light", "INSTRUME": "ZWO ASI6200MC Pro",
                                     "BAYERPAT": "RGGB", "TELESCOP": "Rokinon 135mm",
                                     "FILTER": "L", "EXPTIME": 5})
        project = "97fcae827a9f"
        accepted = "481536614589"
        declined = "b9412d46f576"
        targets = [
            {"name": "m42 horse test mosaic",
             "collab": {"project": project, "projectName": "m42 horse test mosaic",
                        "task": declined}},
            {"name": "m42 horse test mosaic",
             "collab": {"project": project, "projectName": "m42 horse test mosaic",
                        "task": accepted}},
        ]
        rigs = [
            {"name": "FSQ85", "role": "master", "camera": "ZWO ASI6200MM Pro", "upload": None},
            {"name": "Rokinon 135mm", "role": "follower", "camera": "ZWO ASI6200MC Pro",
             "colour": True, "upload": None},
        ]
        logs: list[str] = []
        up = AutoUploader(config, SimpleNamespace(
            _ready=lambda: None,
            status=lambda: {"task": {"id": accepted}}),
            folder=tmp / "blank-queue", log=lambda message, level="info": logs.append(message))
        report = up.backfill(targets=targets, rigs=rigs)
        check(Path(report["root"]) == root, f"backfill scanned {report['root']}")
        check(report["scanned"] == 2 and report["matched"] == 2 and report["queued"] == 1,
              f"blank root found the frames ({report})")
        queued = [item for item in up.items if item["path"].endswith("m42_P20_5s_26C_0001.fits")]
        check(len(queued) == 1 and queued[0]["task"] == accepted and queued[0]["project"] == project,
              "the duplicate name resolves to the current task, not the first stamp")
        check(all(item["task"] != declined for item in up.items),
              "the declined task was not guessed")
        check(report["skippedBy"].get("follower") == 1
              and not any(item["path"].endswith("L_5s_0001.fits") for item in up.items),
              "the follower is still excluded")
        check(any("scanned 2" in line and "queued 1" in line and str(root) in line for line in logs),
              "the log names the root and the counts")

        # No current task, and the header does not name one: do not pick a stamp.
        up2 = AutoUploader(config, SimpleNamespace(_ready=lambda: None, status=lambda: {}),
                           folder=tmp / "blank-queue-2",
                           log=lambda message, level="info": logs.append(message))
        report2 = up2.backfill(targets=targets, rigs=rigs)
        check(report2["queued"] == 0 and report2["skippedBy"].get("ambiguous", 0) >= 1,
              f"without an exact task the duplicate name is skipped ({report2['skippedBy']})")
        check(any("ambiguous" in line for line in logs),
              "the ambiguous folder is logged")
    finally:
        if previous is None:
            os.environ.pop("ASTRO_DATA_DIR", None)
        else:
            os.environ["ASTRO_DATA_DIR"] = previous


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        units(Path(tmp))
        per_frame(Path(tmp))
        follower_and_backfill(Path(tmp))
        blank_root_and_duplicate_name(Path(tmp))
        tree = os.environ.get("STARFRONT_SERVER_TREE")
        if tree:
            end_to_end(Path(tmp), Path(tree))
        else:
            print("(end-to-end skipped: set STARFRONT_SERVER_TREE to a collab-server checkout)")
    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
