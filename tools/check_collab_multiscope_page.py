"""The real Plan tab, a triple RASA on a collaboration chunk.

    python tools/check_collab_multiscope_page.py

Starts Starfront with three telescopes, no wheels, R, G and B fitted in the
drawers, and the real "m42 horse test mosaic" task standing in for the
collaboration server; loads the real page in jsdom, opens the Plan tab and
reads the collaboration block. Before the fix it listed R with frames and a
dash against G and B; now every filter names the telescope shooting it and
its frames, and a line gives each telescope's own share for tonight.

Needs node and jsdom, like check_triple_page.py; without them it says SKIP.
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

main.watchdog.stop()
main.config.update("site", {"latitude": 31.547, "longitude": -99.38, "useMount": False})
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
for name in ("rasa2", "rasa3"):
    http.post("/api/equipment/telescopes", json={"name": name})
rigs = list(main.rigs.all)
http.post("/api/telescopes/filters", json={"filters": dict(zip([r.id for r in rigs], ["R", "G", "B"]))})

TASK = {
    "id": "653493b1b6ae", "project": "97fcae827a9f", "projectName": "m42 horse test mosaic",
    "agent": "c9fbb7afa00b", "version": 2, "state": "accepted", "kind": "mosaic",
    "region": {"ra": 84.0, "dec": -3.5, "width": 12.0, "height": 9.0, "rotation": 270.0},
    "filters": [{"filter": "R", "exposure": 180.0, "hours": 25.0, "channels": ["R"]},
                {"filter": "G", "exposure": 300.0, "hours": 25.0, "channels": ["G"]},
                {"filter": "B", "exposure": 300.0, "hours": 25.0, "channels": ["B"]}],
    "share": list(range(1, 15)),
    "visit": {"seconds": 1800.0, "frames": {"R": 10}, "filter": "R", "moon": 0.075},
}
main.collab_client._task = TASK
main.collab_client._tasks = [TASK]
main.collab_client._open = []
main.collab_client._by_project = {"97fcae827a9f": {"minFramesPerVisit": 10}}
target = main.targets.create(
    "m42 horse test mosaic", 84.0 / 15.0, -3.5, rotation=0.0,
    panel_width=3.37, panel_height=2.25, rows=2, columns=7,
    collab={"project": "97fcae827a9f", "projectName": "m42 horse test mosaic",
            "task": "653493b1b6ae", "version": 2})
main.targets.stamp_collab(target["id"], {"share": list(range(1, 15))})
http.post("/api/plan/entries", json={"targetId": target["id"]})
spec = {"base": BASE, "jsdom": jsdom, "names": [r.name for r in rigs]}

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
        const u = new URL(url, spec.base);
        // No survey pictures in a test: nothing here is about them.
        if (/survey|hips|alasky|skyview/i.test(u.href)) return Promise.reject(new Error('no survey in test'));
        return fetch(u, o);
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
  for (let i = 0; i < 60; i++) { await sleep(250); try { if (ev('state.status && (state.status.rigs||[]).length') === 3) break; } catch {} }
  ev("window.astro.showTab('plan')");
  let block = null;
  for (let i = 0; i < 60 && !block; i++) { await sleep(250); block = d.querySelector('.plan-collab'); }
  check('the Plan tab draws the collaboration block', block);
  if (block) {
    const rows = [...block.querySelectorAll('.collab-grid')].map((g) => [...g.children].map((c) => c.textContent.trim()));
    const cells = rows[0] || [];
    const row = (f) => { const i = cells.findIndex((c) => c === f || c.startsWith(f + ' ·')); return i < 0 ? [] : cells.slice(i, i + 5); };
    const [r, g, b] = ['R', 'G', 'B'].map(row);
    check('R: rasa1 (master), 180 s, 10 a panel', r[0].includes(spec.names[0]) && r[1] === '180s' && r[2] === '10', JSON.stringify(r));
    check('G: rasa2, 300 s, 10 a panel (was a dash)', r.length && g[0].includes(spec.names[1]) && g[1] === '300s' && g[2] === '10', JSON.stringify(g));
    check('B: rasa3, 300 s, 10 a panel (was a dash)', b[0] && b[0].includes(spec.names[2]) && b[1] === '300s' && b[2] === '10', JSON.stringify(b));
    const line = (block.querySelector('.collab-scopes') || {}).textContent || '';
    check('a line gives every telescope its own share tonight',
      spec.names.every((n) => line.includes(n)) && /G 10×300s/.test(line) && /B 10×300s/.test(line), line);
    const note = [...block.querySelectorAll('.collab-spec .small.muted')].map((n) => n.textContent).join(' ');
    check('the note says each drawer shoots its own filter', /each telescope\s+with a filter in its drawer/.test(note), note.slice(0, 200));
  }
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
