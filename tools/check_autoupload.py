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
from astrocontrol.autoupload import AutoUploader, Bucket          # noqa: E402
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
    check(u.paused_until >= clock.t + 21000 and "paused" in u.status()["line"], "507 pauses everything for the Retry-After")
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


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        units(Path(tmp))
        per_frame(Path(tmp))
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
