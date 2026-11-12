"""The fleet dashboard (0.2.52) against real Starfront servers on fake devices.

    python tools/check_fleet.py

Starts, as separate processes, two Starfront PCs with LAN access on and a
fleet token - A, a triple (three telescopes on one mount), and B, a single
with a slow mount - plus C, a PC that accepts the connection and never
answers, and D, a PC that is switched off. The real fleet dashboard polls them
over the box's LAN address (not loopback), so the token is really needed.

Checks: the token (none / wrong / right / loopback / LAN access off / the
websocket); the cards (every telescope of every PC, one card per PC that does
not answer); freshness (live, then stale and offline when B is frozen, then
live again); that C hanging never slows the page or A's polling; commands go
to the right PC and the right telescope and report pending -> done or failed;
unpark and slew refuse without confirm; offline alerts; the dashboard's own
token; and the page itself in jsdom (cards, summary, confirm dialog, command
feedback) when node and jsdom are there.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
TMP = Path(tempfile.mkdtemp(prefix="checkfleet-"))
os.environ["ASTRO_DATA_DIR"] = str(TMP / "data")

import httpx                                                        # noqa: E402
import uvicorn                                                      # noqa: E402

from astrocontrol.fleet import core                                 # noqa: E402
from astrocontrol.fleet.config import normalise                     # noqa: E402
from astrocontrol.fleet.core import Fleet                           # noqa: E402
from astrocontrol.fleet.server import create_app                    # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"  -- {detail}"), flush=True)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


IP = lan_ip()
LAN = not IP.startswith("127.")
TOKEN = "check-fleet-token-0123456789abcd"
DASH = "dash-token-0123456789"
procs = []


def start_node(port, *extra):
    proc = subprocess.Popen([sys.executable, str(ROOT / "tools" / "fleet_node.py"), "--port",
                             str(port), "--token", TOKEN, *extra], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    procs.append(proc)
    return proc


def wait_ready(proc, name):
    deadline = time.time() + 90
    lines = []
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line)
        if line.startswith("READY"):
            threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()
            return True
    print(f"node {name} did not start:\n" + "".join(lines[-30:]))
    return False


def slow_server(port):
    """C: takes the connection, reads the request, never answers."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.listen(64)
    held = []

    def serve():
        while True:
            conn, _ = sock.accept()
            held.append(conn)

    threading.Thread(target=serve, daemon=True).start()


def until(fn, timeout=10.0, step=0.1):
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(step)
    return value


try:
    # ---------------------------------------------------------------- pure
    now = time.time()
    status = {
        "masterRig": "main", "tasks": [],
        "warnings": {"active": [{"key": "camera.hung", "level": "critical", "title": "hung"}]},
        "rigs": [{"id": "main", "name": "R1", "role": "master", "devices": {
                     "mount": {"connected": True, "ra": 1.0, "dec": 2.0, "atPark": False,
                               "slewing": True, "tracking": True, "sideOfPier": "pierWest"},
                     "camera": {"connected": True, "temperature": -9.8, "coolerOn": True,
                                "coolerPower": 40, "setpoint": -10},
                     "guider": {"connected": True, "rmsRa": 0.3, "rmsDec": 0.4, "state": "Guiding"}},
                  "capture": {"target": "M31", "busy": True}},
                 {"id": "r2", "name": "R2", "role": "slave", "devices": {}, "capture": {}}],
        "sequence": {"running": True, "state": "imaging", "watchdog": {"state": "stalled",
                                                                         "reason": "no frame"},
                     "telescopes": {"main": {"frame": 3, "frames": 10}, "r2": {}},
                     "rigRecovery": {"r2": {"state": "failed", "reason": "camera gone",
                                            "attempt": 6, "attempts": 6}}},
    }
    fresh = {"state": "live", "age": 0.2}
    c1 = core.rig_card("N", status, status["rigs"][0], [], fresh)
    c2 = core.rig_card("N", status, status["rigs"][1], [], fresh)
    check("card: mount state, RA/Dec, pier side from the master",
          c1["mount"]["state"] == "slewing" and c1["mount"]["ra"] == 1.0
          and c1["mount"]["pierSide"] == "pierWest" and c2["mount"]["shared"], c1["mount"])
    check("card: frame n of m, target, guiding RMS (from RA/Dec RMS), camera temp/cooler",
          c1["sequence"]["frame"] == 3 and c1["sequence"]["frames"] == 10
          and c1["sequence"]["target"] == "M31" and c1["guiding"]["rms"] == 0.5
          and c1["camera"]["temperature"] == -9.8 and c1["camera"]["coolerOn"] is True,
          (c1["sequence"], c1["guiding"]))
    check("card: imaging / recovery failed state",
          c1["state"] == "imaging" and c2["state"] == "failed"
          and c2["recovery"]["attempt"] == 6, (c1["state"], c2["state"]))
    pure = Fleet(normalise({"nodes": []}))
    alerts = pure.alerts([{"name": "N", "state": "live", "sequence": {"watchdog": status[
        "sequence"]["watchdog"]}, "warnings": status["warnings"]["active"]}], [c1, c2], now)
    ids = {a["id"].split(":")[0] for a in alerts}
    check("alerts: stalled, recovery failed, critical warning",
          {"stalled", "recfail", "warn"} <= ids, ids)
    off = pure.alerts([{"name": "D", "state": "offline", "downSince": now - 400}], [], now)
    soon = pure.alerts([{"name": "D", "state": "offline", "downSince": now - 30}], [], now)
    check("alerts: offline only after offlineAlertMinutes", len(off) == 1 and not soon,
          (off, soon))

    # ---------------------------------------------------------------- nodes
    PA, PB, PC, PD = free_port(), free_port(), free_port(), free_port()
    a = start_node(PA, "--triple")
    b = start_node(PB, "--mount-delay", "3.0")
    slow_server(PC)
    ok = wait_ready(a, "A") and wait_ready(b, "B")
    check("two fake Starfront PCs started", ok)
    if not ok:
        raise SystemExit(1)
    host = IP
    local_a = httpx.Client(base_url=f"http://127.0.0.1:{PA}", timeout=20)
    local_b = httpx.Client(base_url=f"http://127.0.0.1:{PB}", timeout=20)

    # ---------------------------------------------------------------- token
    if LAN:
        url = f"http://{IP}:{PA}/api/status"
        none = httpx.get(url, timeout=10).status_code
        wrong = httpx.get(url, headers={"X-Fleet-Token": "nope" * 8}, timeout=10).status_code
        right = httpx.get(url, headers={"X-Fleet-Token": TOKEN}, timeout=10).status_code
        loop = local_a.get("/api/status").status_code
        check("token: LAN request without it refused (403)", none == 403, none)
        check("token: wrong token refused (403)", wrong == 403, wrong)
        check("token: right token answered (200)", right == 200, right)
        check("token: this PC itself needs none (200)", loop == 200, loop)
        posted = httpx.post(f"http://{IP}:{PA}/api/mount/park", timeout=10).status_code
        check("token: a LAN command without it is refused too", posted == 403, posted)
        shown = local_a.get("/api/settings").json().get("settings", local_a.get(
            "/api/settings").json())
        text = json.dumps(shown)
        check("token: not shown in /api/settings", TOKEN not in text, text[:200])
        from websockets.sync.client import connect
        try:
            with connect(f"ws://{IP}:{PA}/ws", open_timeout=10) as ws:
                ws.recv(timeout=5)
            refused = False
        except Exception:                                          # noqa: BLE001
            refused = True
        check("token: the websocket from the LAN without it is refused", refused)
        try:
            with connect(f"ws://{IP}:{PA}/ws?fleetToken={TOKEN}", open_timeout=10) as ws:
                got = bool(ws.recv(timeout=10))
        except Exception as exc:                                   # noqa: BLE001
            got = exc
        check("token: the websocket with it works", got is True, got)
        local_b.post("/api/settings/server", json={"lanAccess": False})
        off_code = httpx.get(f"http://{IP}:{PB}/api/status", headers={"X-Fleet-Token": TOKEN},
                             timeout=10).status_code
        local_b.post("/api/settings/server", json={"lanAccess": True})
        back = httpx.get(f"http://{IP}:{PB}/api/status", headers={"X-Fleet-Token": TOKEN},
                         timeout=10).status_code
        check("token: LAN access off refuses the LAN even with the token", off_code == 403,
              off_code)
        check("token: LAN access back on answers again", back == 200, back)
    else:
        print("SKIP  token checks: the box has no LAN address")

    # ---------------------------------------------------------------- fleet
    settings = normalise({
        "token": TOKEN, "dashboardToken": DASH, "pollSeconds": 0.5, "timeoutSeconds": 1.0,
        "staleSeconds": 3.0, "offlineSeconds": 6.0, "offlineAlertMinutes": 0.05,
        "nodes": [{"name": "A", "host": host, "port": PA}, {"name": "B", "host": host, "port": PB},
                  {"name": "C", "host": host, "port": PC}, {"name": "D", "host": host, "port": PD}]})
    fleet = Fleet(settings)
    fleet.start()
    PF = free_port()
    fsrv = uvicorn.Server(uvicorn.Config(create_app(fleet), host="0.0.0.0", port=PF,
                                         log_level="warning"))
    threading.Thread(target=fsrv.run, daemon=True).start()
    dash = httpx.Client(base_url=f"http://127.0.0.1:{PF}", timeout=10)
    for _ in range(100):
        try:
            dash.get("/api/fleet")
            break
        except Exception:                                          # noqa: BLE001
            time.sleep(0.1)
    time.sleep(4.0)
    snap = dash.get("/api/fleet").json()
    by_node = {n["name"]: n for n in snap["nodes"]}
    cards = snap["cards"]
    names = sorted(c["name"] for c in cards if c["node"] == "A")
    check("aggregation: three cards for the triple, one for the single, one each for C and D",
          len(cards) == 6 and len(names) == 3 and "RASA2" in names and "RASA3" in names
          and sum(c["node"] == "B" for c in cards) == 1, [(c["node"], c["name"]) for c in cards])
    check("aggregation: summary counts", snap["summary"]["rigs"] == 6
          and snap["summary"]["offline"] == 2 and snap["summary"]["idle"] == 4, snap["summary"])
    check("freshness: A and B live", by_node["A"]["state"] == "live"
          and by_node["B"]["state"] == "live", (by_node["A"]["state"], by_node["B"]["state"]))
    check("freshness: C (hangs) and D (off) offline, with why",
          by_node["C"]["state"] == "offline" and "in time" in (by_node["C"]["error"] or "")
          and by_node["D"]["state"] == "offline" and "connect" in (by_node["D"]["error"] or ""),
          (by_node["C"], by_node["D"]))
    card_a = next(c for c in cards if c["node"] == "A" and c["role"] == "master")
    check("card from a real node: mount, filter, focuser, last frame",
          card_a["mount"]["state"] == "tracking" and card_a["filter"]
          and card_a["focuser"]["position"] == 30000 and card_a["lastFrame"], card_a)
    thumb = dash.get(card_a["lastFrame"]["thumb"])
    check("thumbnail proxied from the node", thumb.status_code == 200
          and thumb.content[:4] == b"\x89PNG", thumb.status_code)
    check("version from /api/fleet/node", by_node["A"].get("version"), by_node["A"])

    # isolation: C never answers; the page and A carry on regardless
    polls_before = fleet.nodes["A"].polls
    worst = 0.0
    t0 = time.time()
    while time.time() - t0 < 3.0:
        started = time.monotonic()
        dash.get("/api/fleet")
        worst = max(worst, time.monotonic() - started)
        time.sleep(0.2)
    polled = fleet.nodes["A"].polls - polls_before
    check("isolation: /api/fleet answers fast while C hangs", worst < 0.5, f"{worst:.2f}s")
    check("isolation: A keeps being polled at its own pace", polled >= 4, polled)

    alerts = dash.get("/api/fleet").json()["alerts"]
    check("alerts: C and D offline alerts", {"offline:C", "offline:D"} <= {x["id"] for x in alerts},
          [x["id"] for x in alerts])

    # ---------------------------------------------------------------- commands
    def run(node, action, **kw):
        reply = dash.post(f"/api/nodes/{node}/command", json={"action": action, **kw})
        return reply

    def settle(cid, timeout=15.0):
        seen = []

        def done():
            record = dash.get(f"/api/commands/{cid}").json()
            if not seen or seen[-1] != record["state"]:
                seen.append(record["state"])
            return record if record["state"] in ("done", "failed", "unknown") else None
        record = until(done, timeout, 0.1)
        return record, seen

    rigs_a = {r["name"]: r["id"] for r in local_a.get("/api/status").json()["rigs"]}
    for name, rid in rigs_a.items():
        q = "" if name not in ("RASA2", "RASA3") else f"?rig={rid}"
        local_a.post(f"/api/camera/expose{q}", json={"exposure": 30, "frameType": "light"})
    until(lambda: all(r["capture"]["busy"] for r in local_a.get("/api/status").json()["rigs"]), 5)
    reply = run("A", "camera.abort", rig=rigs_a["RASA2"])
    record, _ = settle(reply.json()["id"])
    time.sleep(0.5)
    busy = {r["name"]: r["capture"]["busy"] for r in local_a.get("/api/status").json()["rigs"]}
    check("routing: abort A/RASA2 stops only RASA2", record and record["state"] == "done"
          and busy["RASA2"] is False and busy["RASA3"] and sum(busy.values()) == 2,
          (record, busy))
    local_a.post("/api/camera/abort?all=true")

    reply = run("B", "mount.park")
    record, seen = settle(reply.json()["id"])
    parked_b = local_b.get("/api/status").json()["devices"]["mount"].get("atPark")
    parked_a = local_a.get("/api/status").json()["devices"]["mount"].get("atPark")
    check("routing: park on B goes pending, then done from B's task status",
          "pending" in seen and record["state"] == "done", seen)
    check("routing: B parked, A untouched", parked_b is True and not parked_a,
          (parked_b, parked_a))
    refused = run("B", "mount.unpark")
    check("unpark without confirm is refused (400)", refused.status_code == 400, refused.text)
    refused = run("B", "mount.slew", ra=3.0, dec=10.0)
    check("slew without confirm is refused (400)", refused.status_code == 400, refused.text)

    # The page: cards, summary, confirm dialog, command feedback (B is parked now).
    node_bin = shutil.which("node")
    jsdom = next((str(Path(p) / "node_modules" / "jsdom") for p in filter(None, [
        os.environ.get("STARFRONT_JSDOM"), str(ROOT / "tools"), str(ROOT),
        "/workspace/unified/jsdom"]) if (Path(p) / "node_modules" / "jsdom").is_dir()), None)
    if node_bin and jsdom:
        harness = r"""
const spec = JSON.parse(process.argv[2]);
const { JSDOM, VirtualConsole } = require(spec.jsdom);
const errors = []; const vc = new VirtualConsole();
vc.on('jsdomError', (e) => { if (!/Not implemented/i.test(String(e && e.message))) errors.push('jsdom: ' + e.message); });
const out = [];
const check = (name, ok, detail = '') => out.push({ name, ok: !!ok, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  let confirmAnswer = false; let confirms = 0;
  const dom = await JSDOM.fromURL(spec.base + '/', { runScripts: 'dangerously', resources: 'usable',
    pretendToBeVisual: true, virtualConsole: vc, beforeParse(w) {
      w.fetch = (url, o = {}) => fetch(new URL(url, spec.base), o);
      w.confirm = () => { confirms++; return confirmAnswer; };
    } });
  const w = dom.window, d = w.document;
  for (let i = 0; i < 40 && !d.querySelectorAll('.card').length; i++) await sleep(250);
  await sleep(600);
  const cards = [...d.querySelectorAll('.card')];
  check('page: a card per telescope plus one per silent PC', cards.length === 6, cards.length);
  check('page: summary bar', /4\s*idle/.test(d.getElementById('summary').textContent.replace(/\s+/g, ' ')) &&
    /2\s*offline/.test(d.getElementById('summary').textContent), d.getElementById('summary').textContent);
  const offline = cards.filter((c) => c.classList.contains('offline')).map((c) => c.textContent);
  check('page: offline cards say why', offline.length === 2 && offline.some((t) => /in time/.test(t)) &&
    offline.some((t) => /connect/.test(t)), offline.join(' | ').slice(0, 300));
  check('page: offline alerts listed', d.querySelectorAll('#alerts .alert').length >= 2,
    d.getElementById('alerts').textContent.slice(0, 200));
  check('page: fresh labels', /live/.test(d.querySelector('section[data-node="A"] .fresh').textContent) &&
    /offline/.test(d.querySelector('section[data-node="D"] .fresh').textContent));
  const unpark = () => d.querySelector('section[data-node="B"] button[data-action="mount.unpark"]');
  for (let i = 0; i < 20 && (!unpark() || unpark().disabled); i++) await sleep(250);
  const before = (await (await fetch(spec.base + '/api/fleet')).json()).commands.length;
  confirmAnswer = false; unpark().click(); await sleep(800);
  const mid = (await (await fetch(spec.base + '/api/fleet')).json()).commands.length;
  check('page: unpark asks first, and No sends nothing', confirms === 1 && mid === before, `${confirms} ${before} ${mid}`);
  confirmAnswer = true; unpark().click();
  let text = '';
  for (let i = 0; i < 60; i++) { await sleep(250); text = d.querySelector('section[data-node="B"] .card').textContent; if (/mount\.unpark: done/.test(text)) break; }
  const states = (await (await fetch(spec.base + '/api/fleet')).json()).commands.filter((c) => c.action === 'mount.unpark').map((c) => c.state);
  check('page: Yes sends it, and the card follows it to done', /mount\.unpark: done/.test(text) && states.includes('done'), text.slice(-200));
  check('page: no script errors', errors.length === 0, errors.join(' | '));
  console.log(JSON.stringify(out)); process.exit(0);
})().catch((e) => { console.log(JSON.stringify([{ name: 'page harness', ok: false, detail: String(e && e.stack) }])); process.exit(0); });
"""
        script = TMP / "fleetpage.js"
        script.write_text(harness)
        proc = subprocess.run([node_bin, str(script), json.dumps(
            {"base": f"http://127.0.0.1:{PF}", "jsdom": jsdom})], capture_output=True,
            text=True, timeout=120)
        try:
            page = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:                                          # noqa: BLE001
            page = [{"name": "page harness ran", "ok": False,
                     "detail": (proc.stdout + proc.stderr)[-800:]}]
        for item in page:
            check(item["name"], item["ok"], item["detail"])
        parked = until(lambda: local_b.get("/api/status").json()["devices"]["mount"].get(
            "atPark") is False, 10)
        check("the page's unpark reached B", parked, "still parked")
    else:
        print("SKIP  page checks: needs node and jsdom")
        reply = run("B", "mount.unpark", confirm=True)
        settle(reply.json()["id"])

    reply = run("B", "mount.slew", ra=3.25, dec=12.5, confirm=True)
    record, seen = settle(reply.json()["id"], 20)
    ra = local_b.get("/api/status").json()["devices"]["mount"].get("ra")
    check("routing: a confirmed slew on B moves B's mount", record["state"] == "done"
          and abs((ra or 0) - 3.25) < 1e-6, (seen, ra))
    reply = run("D", "mount.park")
    record, _ = settle(reply.json()["id"])
    check("a command to a PC that is off fails, saying so", record["state"] == "failed"
          and "did not answer" in record["detail"], record)
    started = time.monotonic()
    reply = run("C", "sequence.stop")
    quick = time.monotonic() - started
    check("a command to the hanging PC does not hold the page", quick < 0.5
          and reply.json()["state"] == "sending", f"{quick:.2f}s")
    check("unknown PC 404, unknown command 400",
          run("Z", "mount.park").status_code == 404 and run("A", "rm -rf").status_code == 400)
    reply = run("A", "sequence.stop")
    record, _ = settle(reply.json()["id"])
    check("sequence.stop routed to A", record["state"] == "done", record)

    # ---------------------------------------------------------------- dashboard token
    if LAN:
        base = f"http://{IP}:{PF}"
        codes = (httpx.get(f"{base}/api/fleet", timeout=10).status_code,
                 httpx.get(f"{base}/api/fleet", headers={"X-Dashboard-Token": DASH},
                           timeout=10).status_code,
                 httpx.get(f"{base}/api/fleet?token={DASH}", timeout=10).status_code,
                 httpx.post(f"{base}/api/quit", headers={"X-Dashboard-Token": DASH},
                            timeout=10).status_code)
        check("dashboard token: needed from another PC (403/200/200), quit is local-only (403)",
              codes == (403, 200, 200, 403), codes)

    # ---------------------------------------------------------------- freshness over time
    b.send_signal(signal.SIGSTOP)
    stale = until(lambda: dash.get("/api/fleet").json()["nodes"][1]["state"] == "stale", 8)
    gone = until(lambda: dash.get("/api/fleet").json()["nodes"][1]["state"] == "offline", 10)
    card_b = next(c for c in dash.get("/api/fleet").json()["cards"] if c["node"] == "B")
    b.send_signal(signal.SIGCONT)
    back = until(lambda: dash.get("/api/fleet").json()["nodes"][1]["state"] == "live", 10)
    check("freshness: a frozen PC goes stale, then offline, then live again",
          stale and gone and back, (stale, gone, back))
    check("freshness: the frozen PC's cards keep its last values, marked offline",
          card_b["state"] == "offline" and card_b["mount"] is not None, card_b["state"])
    fleet.stop()
finally:
    for proc in procs:
        with __import__("contextlib").suppress(Exception):
            proc.send_signal(signal.SIGCONT)
            proc.kill()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
