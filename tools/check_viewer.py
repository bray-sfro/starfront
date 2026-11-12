"""Page-side checks for 0.2.50 (H5, H6, H7, H9): run pieces of app.js in Node.

The functions under test are cut out of astrocontrol/web/app.js as they are
and run against a small fake DOM, fake fetch and a controllable clock, so
these exercise the page's own code, not a copy of it.

    python tools/check_viewer.py [path/to/app.js]

Needs `node` on PATH. Prints PASS/FAIL per case and N/M passed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "astrocontrol" / "web" / "app.js"

FUNCTIONS = ["actionName", "beginAction", "endAction", "api", "apiRaw", "requestTimeout",
             "trackTask", "settleTasks", "toast", "refreshImages", "steppableImages",
             "frameIndex", "updateFramePosition", "newestFrame", "selectImage", "setText",
             "noteFreshStatus", "linkBanner", "updateLinkState", "rigList"]
DECLS = ["ACTION_NAMES", "pressed", "REQUEST_TIMEOUT_MS", "LONG_REQUEST_MS", "LONG_REQUESTS",
         "pendingTasks", "imageListRequest", "imageSelectRequest", "link", "LINK_STALE_MS",
         "LINK_DEAD_MS"]


def block_end(text: str, start: int) -> int:
    """Index just past the brace block opening at or after `start`."""
    i = text.index("{", start)
    depth = 0
    quote = None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif c == "/" and text[i + 1] == "/":
            i = text.index("\n", i)
            continue
        elif c == "/" and text[i + 1] == "*":
            i = text.index("*/", i) + 2
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced")


def extract(text: str) -> tuple[str, list[str]]:
    out, missing = [], []
    for name in DECLS:
        m = re.search(rf"^(const|let) {name}\b[^\n]*", text, re.M)
        if not m:
            missing.append(name)
            continue
        line = m.group(0)
        if line.rstrip().endswith(";"):
            out.append(line.replace("const ", "var ", 1).replace("let ", "var ", 1))
        else:                                  # a multi-line literal
            end = text.index("\n];", m.start()) + 3 if line.rstrip().endswith("[") \
                else block_end(text, m.start()) + 1
            out.append(text[m.start():end].replace("const ", "var ", 1).replace("let ", "var ", 1))
    for name in FUNCTIONS:
        m = re.search(rf"^(async )?function {name}\(", text, re.M)
        if not m:
            missing.append(name)
            continue
        out.append(text[m.start():block_end(text, m.start())])
    return "\n\n".join(out), missing


HARNESS = r"""
'use strict';
let now = 1_000_000;
const realSetTimeout = setTimeout;
const timers = [];
global.performance = { now: () => now };
Date.now = () => now;
global.setTimeout = (fn, ms) => { timers.push({ at: now + (ms || 0), fn }); return timers.length; };
global.clearTimeout = (id) => { if (timers[id - 1]) timers[id - 1].fn = null; };
global.setInterval = () => 0;
global.clearInterval = () => {};
async function advance(ms) {
  const until = now + ms;
  for (;;) {
    await new Promise((r) => realSetImmediate(r));
    const due = timers.filter((t) => t.fn && t.at <= until).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    now = Math.max(now, due.at);
    const fn = due.fn; due.fn = null; fn();
  }
  now = until;
  await new Promise((r) => realSetImmediate(r));
}
const realSetImmediate = setImmediate;
const flush = () => new Promise((r) => realSetImmediate(r));

function el(id) {
  const node = {
    id, textContent: '', className: '', hidden: false, disabled: false, title: '', href: '',
    style: {}, dataset: {}, children: [], innerHTML: '',
    classList: {
      set: new Set(),
      add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); },
      toggle(c, on) { if (on === undefined ? !this.set.has(c) : on) this.set.add(c); else this.set.delete(c); },
      contains(c) { return this.set.has(c); },
    },
    appendChild(child) { this.children.push(child); child.parent = this; return child; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); },
    querySelector() { return el('q'); }, addEventListener() {},
  };
  return node;
}
const nodes = {};
global.document = {
  getElementById: (id) => nodes[id] || (nodes[id] = el(id)),
  createElement: () => el(''),
  querySelectorAll: () => [],
  body: el('body'),
};
global.document.body.appendChild = (child) => { nodes[child.id] = child; child.parent = global.document.body; return child; };
global.$ = (id) => document.getElementById(id);
global.state = { images: [], currentId: null, browsing: false };
global.stackView = { on: false };
global.viewer = {};
global.toastLog = [];
global.setStretchSliders = () => {}; global.drawHistogram = () => {};
global.loadPreview = async () => {};
global.$ = (id) => document.getElementById(id);
$('chkAuto').checked = false;
$('btnDownload').classList = el('x').classList;

// fetch: each call is held until the test answers it.
const requests = [];
global.fetch = (path, options = {}) => new Promise((resolve, reject) => {
  const req = { path, options, answer(status, body) {
    resolve({ ok: status < 400, status, statusText: '', text: async () => JSON.stringify(body) });
  } };
  requests.push(req);
  if (options.signal) options.signal.addEventListener('abort', () => {
    const e = new Error('The operation was aborted'); e.name = 'AbortError'; reject(e);
  });
});

/*CODE*/

const results = [];
function check(name, ok, detail = '') { results.push({ name, ok: !!ok, detail: String(detail) }); }
async function test(name, fn) {
  try { await fn(); } catch (error) { check(name, false, 'threw: ' + (error && error.message)); }
}
const sub = (id, t) => ({ id, frame_type: 'light', saved: true, timestamp: t, filename: id + '.fits',
  exposure: 60, gain: 100, binning: 1 });

(async () => {
  await test('H5: a request nobody answers gives up with a message, not a forever-busy button', async () => {
    const button = el('btn'); pressed = { button, at: now };
    requests.length = 0;
    let error = null;
    const p = api('/api/mount/slew', 'POST', { ra: 1, dec: 2 }).catch((e) => { error = e; });
    await flush();
    const busy = button.disabled;
    await advance(120000);
    await Promise.race([p, flush()]);   // an app.js without a timeout never settles it
    await advance(1000);
    check('H5: a request nobody answers gives up with a message, not a forever-busy button',
      busy && error && /no answer after/.test(error.message) && !button.disabled,
      `busy=${busy} error=${error && error.message} disabledAfter=${button.disabled}`);
  });

  await test('H5: a lost connection is an error that says so', async () => {
    const saved = global.fetch;
    global.fetch = () => Promise.reject(new TypeError('Failed to fetch'));
    let error = null;
    await apiRaw('/api/status').catch((e) => { error = e; });
    global.fetch = saved;
    check('H5: a lost connection is an error that says so',
      error && /could not reach Starfront/.test(error.message), error && error.message);
  });

  await test('H4 page: a pending command keeps its button busy until the status says it is done', async () => {
    const button = el('btn2'); pressed = { button, at: now };
    requests.length = 0;
    const p = api('/api/mount/park', 'POST', {});
    await flush();
    requests[0].answer(200, { ok: true, pending: true, task: { id: 7, key: 'mount:park', state: 'running' } });
    await p; await advance(1000);
    if (typeof settleTasks !== 'function') throw new Error('this app.js does not follow pending commands');
    const busyWhilePending = button.disabled;
    settleTasks([{ id: 7, state: 'running' }]); await advance(1000);
    const stillBusy = button.disabled;
    settleTasks([{ id: 7, state: 'done' }]); await advance(1000);
    const toasts = $('toasts').children.map((n) => n.textContent);
    check('H4 page: a pending command keeps its button busy until the status says it is done',
      busyWhilePending && stillBusy && !button.disabled,
      `pending=${busyWhilePending} running=${stillBusy} after=${button.disabled} ${JSON.stringify(toasts)}`);
  });

  await test('H4 page: a pending command that fails shows the error', async () => {
    const button = el('btn3'); pressed = { button, at: now };
    requests.length = 0;
    const p = api('/api/filterwheel/position', 'POST', { position: 2 });
    await flush();
    requests[0].answer(200, { ok: true, pending: true, task: { id: 8, state: 'running' } });
    await p;
    settleTasks([{ id: 8, state: 'error', error: 'wheel jammed' }]); await advance(400);
    const texts = $('toasts').children.map((n) => n.textContent);
    check('H4 page: a pending command that fails shows the error',
      texts.some((t) => /wheel jammed/.test(t)) && !button.disabled, JSON.stringify(texts));
  });

  await test('H6: the viewer keeps following the camera when a newer sub arrives', async () => {
    state.images = [sub('s1', 1)]; state.currentId = 's1'; state.browsing = false;
    updateFramePosition();
    state.images = [sub('s2', 2), sub('s1', 1)];        // the next sub arrives
    updateFramePosition();                                // what refreshImages' finally does
    check('H6: the viewer keeps following the camera when a newer sub arrives',
      state.browsing === false, `browsing=${state.browsing}`);
  });

  await test('H6: stepping back to an older sub does stop following', async () => {
    state.images = [sub('s3', 3), sub('s2', 2), sub('s1', 1)]; state.currentId = 's3';
    state.status = { capture: { latestImageId: 's3' }, rigs: [] };
    requests.length = 0;
    const p = selectImage('s2', true);
    await flush();
    requests[0].answer(200, { image: { id: 's2', width: 10, height: 10, filename: 's2', saved: true }, stats: {} });
    await p;
    const back = state.browsing;
    const q = selectImage('s3', true, false);
    await flush();
    requests[1].answer(200, { image: { id: 's3', width: 10, height: 10, filename: 's3', saved: true }, stats: {} });
    await q;
    check('H6: stepping back to an older sub does stop following, Newest resumes it',
      back === true && state.browsing === false, `back=${back} newest=${state.browsing}`);
  });

  await test('H6 triple: a frame from another telescope does not stop this one following', async () => {
    const a = (id, t, rig) => ({ ...sub(id, t), rig });
    state.status = { rigs: [{ id: 'r1', capture: { latestImageId: 'a1' } }, { id: 'r2', capture: { latestImageId: 'b1' } }] };
    state.images = [a('b1', 3, 'r2'), a('a1', 2, 'r1')]; state.currentId = 'a1'; state.browsing = false;
    updateFramePosition();
    const afterOther = state.browsing;
    requests.length = 0;
    const p = selectImage('a1', true);                  // a click on this telescope's newest frame
    await flush();
    requests[0].answer(200, { image: { id: 'a1', width: 10, height: 10, filename: 'a1', saved: true }, stats: {} });
    await p;
    check('H6 triple: a frame from another telescope does not stop this one following',
      afterOther === false && state.browsing === false, `afterOther=${afterOther} afterClick=${state.browsing}`);
  });

  await test('H7: image lists answering out of order leave the newest list drawn', async () => {
    state.images = [];
    requests.length = 0;
    const first = refreshImages();
    const second = refreshImages();
    await flush();
    requests[1].answer(200, { images: [sub('n2', 2), sub('n1', 1)] });
    await second;
    requests[0].answer(200, { images: [sub('n1', 1)] });
    await first;
    check('H7: image lists answering out of order leave the newest list drawn',
      state.images.length === 2 && state.images[0].id === 'n2', JSON.stringify(state.images.map((i) => i.id)));
  });

  await test('H7: a slow answer for an older frame does not replace the newest one', async () => {
    requests.length = 0;
    const older = selectImage('n1', true, false);
    const newer = selectImage('n2', true, false);
    await flush();
    requests[1].answer(200, { image: { id: 'n2', width: 10, height: 10, filename: 'n2', saved: true }, stats: {} });
    await newer;
    requests[0].answer(200, { image: { id: 'n1', width: 10, height: 10, filename: 'n1', saved: true }, stats: {} });
    await older;
    check('H7: a slow answer for an older frame does not replace the newest one',
      state.currentId === 'n2', `currentId=${state.currentId}`);
  });

  await test('H9: the page says when its status is not current', async () => {
    if (typeof link === 'undefined') throw new Error('no freshness tracking in this app.js');
    link.open = true;
    noteFreshStatus({ serverTime: 1, seq: 1 });
    const fresh = $('linkState').textContent;
    const bannerFresh = document.getElementById('linkBanner').hidden;
    now += 8000;
    updateLinkState();
    const stale = $('linkState').textContent;
    const banner = document.getElementById('linkBanner');
    check('H9: the page says when its status is not current',
      fresh === 'live' && bannerFresh && /no data 8s/.test(stale) && !banner.hidden && /8s ago/.test(banner.textContent),
      `${fresh} -> ${stale} | ${banner.textContent}`);
    link.open = false; updateLinkState();
    check('H9: ...and when the connection is gone',
      $('linkState').textContent === 'offline' && /lost/.test(banner.textContent), banner.textContent);
  });

  process.stdout.write(JSON.stringify(results));
})();
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("FAIL  node is not installed")
        return 1
    text = APP.read_text(encoding="utf-8").replace("\r\n", "\n")
    code, missing = extract(text)
    if missing:
        print(f"note: not in this app.js: {', '.join(missing)}")
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "harness.js"
        script.write_text(HARNESS.replace("/*CODE*/", code), encoding="utf-8")
        proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=120)
    try:
        results = json.loads(proc.stdout)
    except ValueError:
        print("FAIL  harness crashed\n" + proc.stdout[-2000:] + proc.stderr[-2000:])
        return 1
    passed = 0
    for r in results:
        passed += r["ok"]
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['name']}" + (f"  — {r['detail']}" if r["detail"] else ""))
    print(f"{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
