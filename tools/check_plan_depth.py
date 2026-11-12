"""The Plan tab's depth map must not be refetched on every redraw.

Before 0.2.53 one PC with the Plan tab open asked the collab server for the
same depth map about once a second (116k requests in a day). This runs the
cache part of plan.js (between the depth-cache markers) in node and checks:
redraws within the refresh window fetch once, overlapping redraws share one
fetch, a failure is not retried at once, and a stale map is fetched again.

    python tools/check_plan_depth.py
"""
import re, subprocess, sys
from pathlib import Path

src = (Path(__file__).resolve().parent.parent / "astrocontrol/web/plan.js").read_text()
part = re.search(r"// depth-cache:begin.*?\n(.*?)// depth-cache:end", src, re.S).group(1)
script = part + r"""
(async () => {
  const cache = new Map(), inflight = new Map();
  let calls = 0, fail = false;
  const get = async (p) => { calls++; await new Promise(r => setTimeout(r, 20)); if (fail) throw new Error('down'); return {p}; };
  // what wrap.show() does on each redraw
  async function show(p, now) {
    const held = cache.get(p) || {};
    if (depthFresh(held, now)) return 'fresh';
    if (!depthMayTry(held, now)) return 'wait';
    try { await depthFetch(cache, inflight, p, get); return 'fetched'; } catch (e) { return 'failed'; }
  }
  const out = [];
  await Promise.all([show('a', Date.now()), show('a', Date.now()), show('a', Date.now())]);
  out.push(['overlapping redraws share one fetch', calls === 1]);
  for (let i = 0; i < 50; i++) await show('a', Date.now());
  out.push(['50 redraws inside the window fetch nothing more', calls === 1]);
  await show('a', Date.now() + DEPTH_REFRESH_MS + 1);
  out.push(['a stale map is fetched again', calls === 2]);
  fail = true;
  const before = calls;
  await show('b', Date.now());
  for (let i = 0; i < 20; i++) await show('b', Date.now());
  out.push(['a failure is not retried at once', calls === before + 1]);
  await show('b', Date.now() + DEPTH_RETRY_MS + 1);
  out.push(['...but is retried after the wait', calls === before + 2]);
  for (const [name, ok] of out) console.log((ok ? 'PASS ' : 'FAIL ') + name);
  process.exit(out.every(([, ok]) => ok) ? 0 : 1);
})();
"""
result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
print(result.stdout + result.stderr, end="")
sys.exit(result.returncode)
