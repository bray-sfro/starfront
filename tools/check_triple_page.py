"""The real page, three telescopes: switcher, cards, views and tiles.

    python tools/check_triple_page.py

Starts Starfront (uvicorn) with three telescopes of fake devices, each with a
frame and its own focuser position, and loads the real index.html + app.js
into jsdom (a browser DOM for node) talking to it over HTTP and /ws. Then
checks the page draws three of everything multi-scope: the telescope
switcher, the Observatory cards, the per-telescope view strip and the
all-scopes tiles; that selecting a telescope shows that telescope's
focuser; and that the viewer follows the selected telescope's newest frame.

Needs node and jsdom (`npm i jsdom` in tools/, the repo root, or a folder
named by STARFRONT_JSDOM). Without them it says SKIP.
"""

import json
import os
import shutil
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
TMP = Path(tempfile.mkdtemp(prefix="triplepage-"))
os.environ["ASTRO_DATA_DIR"] = str(TMP / "data")
os.environ["HOME"] = str(TMP / "home")
os.environ["LOCALAPPDATA"] = str(TMP / "lad")


def find_jsdom():
    for base in filter(None, [os.environ.get("STARFRONT_JSDOM"), str(ROOT / "tools"), str(ROOT),
                              "/workspace/unified/jsdom"]):
        if (Path(base) / "node_modules" / "jsdom").is_dir():
            return str(Path(base) / "node_modules" / "jsdom")
    return None


node, jsdom = shutil.which("node"), find_jsdom()
if not node or not jsdom:
    print("SKIP  needs node and jsdom")
    sys.exit(0)

import httpx                                                        # noqa: E402
import uvicorn                                                      # noqa: E402

from astrocontrol import main                                      # noqa: E402
from fakes import FakeCamera, FakeFocuser, FakeMount, FakeWheel, install  # noqa: E402

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
BASE = f"http://127.0.0.1:{PORT}"
http = httpx.Client(base_url=BASE, timeout=30)
for _ in range(100):
    try:
        http.get("/api/status")
        break
    except Exception:                                               # noqa: BLE001
        time.sleep(0.1)
for name in ("RASA2", "RASA3"):
    http.post("/api/equipment/telescopes", json={"name": name})
rigs = list(main.rigs.all)
install(main.rigs.master, "mount", FakeMount("mount"))
for i, r in enumerate(rigs):
    install(r, "camera", FakeCamera(f"cam-{r.name}", seed=i))
    install(r, "filterwheel", FakeWheel(f"wheel-{r.name}"))
    f = install(r, "focuser", FakeFocuser(f"foc-{r.name}"))
    f.move_to(31000 + 1000 * i)
    q = "" if r is main.rigs.master else f"?rig={r.id}"
    http.post(f"/api/camera/expose{q}", json={"exposure": 0.02, "frameType": "light"})
time.sleep(2.0)
latest = {x["id"]: x["capture"]["latestImageId"] for x in http.get("/api/status").json()["rigs"]}
spec = {"base": BASE, "jsdom": jsdom, "rigs": [{"id": r.id, "name": r.name, "focus": 31000 + 1000 * i,
                                                "latest": latest[r.id]} for i, r in enumerate(rigs)]}

HARNESS = r"""
const spec = JSON.parse(process.argv[2]);
const { JSDOM, VirtualConsole } = require(spec.jsdom);
const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => { if (!/Not implemented|getContext|canvas/i.test(String(e && e.message))) errors.push('jsdom: ' + e.message); });
vc.on('error', (...a) => errors.push('console.error: ' + a.map(String).join(' ').slice(0, 200)));
const results = [];
const check = (name, ok, detail = '') => results.push({ name, ok: !!ok, detail: String(detail) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const dom = await JSDOM.fromURL(spec.base + '/', {
    runScripts: 'dangerously', resources: 'usable', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (url, options = {}) => {
        const o = { ...options };
        if (o.signal) { const c = new AbortController(); o.signal.addEventListener('abort', () => c.abort()); o.signal = c.signal; }
        return fetch(new URL(url, spec.base), o);
      };
      const ctx = new Proxy({}, { get: (t, k) => (k === 'canvas' ? w.document.createElement('canvas')
        : (k === 'measureText' ? () => ({ width: 10 }) : (k === 'getImageData' || k === 'createImageData'
          ? () => ({ data: new Uint8ClampedArray(4) }) : (typeof k === 'string' && /^[a-z]/.test(k) ? () => ctx : undefined)))),
        set: () => true });
      w.HTMLCanvasElement.prototype.getContext = () => ctx;
      w.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
      w.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
      w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {} });
      w.HTMLDialogElement && (w.HTMLDialogElement.prototype.showModal = function () { this.open = true; });
      w.HTMLDialogElement && (w.HTMLDialogElement.prototype.close = function () { this.open = false; });
      w.scrollTo = () => {};
    },
  });
  const w = dom.window, d = w.document;
  const ev = (code) => w.eval(code);
  for (let i = 0; i < 60; i++) { await sleep(250); try { if (ev('state.status && state.equipment && (state.status.rigs||[]).length') === 3) break; } catch {} }
  await sleep(2000);
  const ids = spec.rigs.map((r) => r.id);
  const got = (sel) => [...d.querySelectorAll(sel)].map((n) => n.dataset.rig);
  const same = (a) => JSON.stringify([...a].sort()) === JSON.stringify([...ids].sort());
  check('page loaded with three telescopes in its status', ev('(state.status.rigs||[]).length') === 3, ev('(state.status.rigs||[]).length'));
  check('the socket is live', d.getElementById('linkState').textContent === 'live', d.getElementById('linkState').textContent);
  const sw = d.getElementById('rigSwitch');
  check('telescope switcher: one button per telescope, shown', !sw.hidden && same(got('#rigSwitch [data-rig]')), got('#rigSwitch [data-rig]'));
  check('Observatory: one card per telescope', same(got('#rigOverview .rig-card')), got('#rigOverview .rig-card'));
  check('image tab: one view per telescope', same(got('#rigViews .rig-view')), got('#rigViews .rig-view'));
  ev('setAllScopes(true)'); await sleep(1500);
  check('all-scopes view: three tiles', same(got('#multiView .multi-tile')) && !d.getElementById('multiView').hidden,
    got('#multiView .multi-tile'));
  ev('setAllScopes(false)'); await sleep(300);
  for (const rig of spec.rigs) {
    d.querySelector(`#rigSwitch [data-rig="${rig.id}"]`).click();
    await sleep(1500);
    const pos = d.getElementById('focPos').textContent;
    check(`${rig.name} selected: the page shows its focuser (${rig.focus})`,
      ev('state.rig') === rig.id && Number(pos) === rig.focus, `state.rig=${ev('state.rig')} focPos=${pos}`);
  }
  // RASA3 is selected now. A frame on RASA3 comes into view; one on RASA2 after it does not take it away.
  const last = spec.rigs[2], other = spec.rigs[1];
  const shoot = (rig) => w.fetch(`/api/camera/expose?rig=${rig.id}`, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ exposure: 0.02, frameType: 'light' }) });
  await shoot(last); await sleep(2500);
  const mine = ev('currentRig().capture.latestImageId');
  check('the viewer follows the selected telescope\'s new frame', mine && mine !== last.latest
    && ev('state.currentId') === mine && ev('state.browsing') === false, `currentId=${ev('state.currentId')} latest=${mine}`);
  await shoot(other); await sleep(2500);
  await shoot(last); await sleep(2500);
  const mine2 = ev('currentRig().capture.latestImageId');
  check('...and keeps following after another telescope\'s frame lands in between',
    mine2 !== mine && ev('state.currentId') === mine2 && ev('state.browsing') === false,
    `currentId=${ev('state.currentId')} latest=${mine2} browsing=${ev('state.browsing')}`);
  check('no script errors on the page', errors.length === 0, errors.slice(0, 5).join(' | '));
  process.stdout.write('@@' + JSON.stringify(results));
  w.close();
  process.exit(0);
})().catch((e) => { process.stdout.write('@@' + JSON.stringify([{ name: 'harness', ok: false, detail: e.stack }])); process.exit(0); });
"""

with tempfile.TemporaryDirectory() as tmp:
    script = Path(tmp) / "page.js"
    script.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(script), json.dumps(spec)], capture_output=True, text=True, timeout=180)
out = proc.stdout.split("@@", 1)
try:
    results = json.loads(out[1])
except (IndexError, ValueError):
    print("FAIL  harness crashed\n" + proc.stdout[-1500:] + proc.stderr[-1500:])
    sys.exit(1)
passed = 0
for r in results:
    passed += r["ok"]
    print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['name']}" + (f"  — {r['detail']}" if r["detail"] else ""))
server.should_exit = True
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
