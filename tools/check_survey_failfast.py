"""Survey cutouts fail fast while CDS is unreachable (2026-11-03).

Runs, with no real network to CDS:
  1. surveyimg.js in node: one fetch per picture, at most two in flight,
     two-minute back-off after a failure (or Retry-After), a bad request does
     not back off, recovery after the wait.
  2. survey.py against a blackholed endpoint (connect never answers), a 504
     endpoint, and a working local endpoint.
  3. GET /api/survey/image answers 503 + Retry-After at once while down.
  4. Sesame (catalog.resolve) skips CDS for two minutes after a failure and
     does not cache the failure for an hour.

    python tools/check_survey_failfast.py
"""
import http.server, socket, subprocess, sys, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
results = []
def check(name, ok, extra=""):
    results.append(ok); print(("PASS " if ok else "FAIL ") + name + (f"  ({extra})" if extra else ""))

# 1 ---------------------------------------------------------------- the page
js = (ROOT / "astrocontrol/web/surveyimg.js").read_text() + r"""
(async () => {
  let t = 1000; const now = () => t;
  let calls = 0, live = 0, peak = 0, mode = 'ok';
  const fetchImpl = async (url) => {
    calls++; live++; peak = Math.max(peak, live);
    await new Promise(r => setTimeout(r, 15)); live--;
    if (mode === 'dead') throw new TypeError('Failed to fetch');
    if (mode === '503') return { ok: false, status: 503, headers: { get: () => '30' }, json: async () => ({ detail: 'down' }) };
    if (mode === '400') return { ok: false, status: 400, headers: { get: () => null }, json: async () => ({ detail: 'bad' }) };
    return { ok: true, status: 200, blob: async () => url };
  };
  const s = globalThis.surveyImagesCreate(fetchImpl, async (b) => ({ img: b }), now);
  const out = [];
  await Promise.all([1,2,3,4,5].map(() => s.image('a')));
  out.push(['five redraws of one picture make one request', calls === 1]);
  await s.image('a'); out.push(['a loaded picture is not fetched again', calls === 1]);
  calls = 0; peak = 0;
  await Promise.all(['b','c','d','e','f','g','h','i'].map(u => s.image(u)));
  out.push(['eight pictures never hold more than two connections', peak <= 2 && calls === 8]);
  mode = 'dead'; calls = 0;
  const r = await Promise.allSettled(['j','k','l','m','n','o'].map(u => s.image(u)));
  out.push(['dead service: only the first two queued requests go out', calls === 2 && r.every(x => x.status === 'rejected')]);
  const before = calls; const t0 = Date.now();
  for (let i = 0; i < 100; i++) await s.image('p').catch(() => {});
  out.push(['100 redraws while backing off: no request, instant', calls === before && Date.now() - t0 < 200]);
  out.push(['back-off is two minutes', s.isDown() && s.retryInMs() === 120000]);
  mode = 'ok'; t += 120001;
  await s.image('p'); out.push(['after the wait a picture is fetched again', calls === before + 1 && !s.isDown()]);
  mode = '503'; await s.image('q').catch(() => {});
  out.push(['503 Retry-After: 30 backs off 30 s', s.isDown() && s.retryInMs() === 30000]);
  t += 30001; mode = '400'; await s.image('r').catch(() => {});
  out.push(['a 400 (bad request) does not back off', !s.isDown()]);
  for (const [n, ok] of out) console.log((ok ? 'PASS ' : 'FAIL ') + 'js: ' + n);
  process.exit(out.every(([, ok]) => ok) ? 0 : 1);
})();
"""
p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
print(p.stdout + p.stderr, end=""); results.append(p.returncode == 0)

# 2 ------------------------------------------------------------- survey.py
import tempfile
from astrocontrol import survey
from astrocontrol.devices.base import DeviceError

JPEG = b"\xff\xd8" + b"\0" * 64 + b"\xff\xd9"
class H(http.server.BaseHTTPRequestHandler):
    hits = 0
    def log_message(self, *a): pass
    def do_GET(self):
        H.hits += 1
        if self.path.startswith("/504"):
            self.send_response(504); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.end_headers(); self.wfile.write(JPEG)
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]

# A blackhole: a listening socket with backlog filled is unreliable, so use a
# socket that accepts the TCP connection and never answers (read timeout),
# which is how CDS behaved behind its load balancer, and a non-routable
# address for the connect-timeout case.
hole = socket.socket(); hole.bind(("127.0.0.1", 0)); hole.listen(64)
hole_port = hole.getsockname()[1]

def fresh(endpoint, *more, mt=30.0):
    survey.MIRRORS = tuple({"name": f"m{i}", "url": survey._hips2fits(e), "timeout": mt}
                           for i, e in enumerate((endpoint,) + more))
    return survey.SurveyImages(Path(tempfile.mkdtemp()))

s = fresh(f"http://127.0.0.1:{hole_port}/hips2fits")
t0 = time.monotonic()
try: s.cutout(10, 20, 1, 300, 200, timeout=3); ok = False
except survey.SurveyDown: ok = True
first = time.monotonic() - t0
check("blackholed CDS: first cutout gives up at the timeout", ok and 2.5 < first < 5, f"{first:.1f}s")
t0 = time.monotonic(); n = 0
for i in range(50):
    try: s.cutout(10 + i, 20, 1, 300, 200)
    except survey.SurveyDown as e: n += 1; ra = e.retry_after
dt = time.monotonic() - t0
check("blackholed CDS: next 50 cutouts fail at once", n == 50 and dt < 0.5, f"{dt*1000:.0f} ms, retry_after={ra}")
check("down_status reports it", s.down_status()["down"] is True)

s = fresh("http://10.255.255.1:81/hips2fits")
t0 = time.monotonic()
try: s.cutout(10, 20, 1, 300, 200, timeout=2)
except survey.SurveyDown: pass
check("non-routable CDS (connect timeout) also trips the breaker", s.down_status()["down"], f"{time.monotonic()-t0:.1f}s")
check("default timeout is 12 s", survey.FETCH_TIMEOUT == 12.0 and survey.DOWN_SECONDS == 120.0)

s = fresh(f"http://127.0.0.1:{port}/504")
try: s.cutout(10, 20, 1, 300, 200); ok = False
except survey.SurveyDown: ok = True
check("HTTP 504 from CDS trips the breaker", ok and s.down_status()["down"])

s = fresh(f"http://127.0.0.1:{port}/hips2fits", mt=1.5)
H.hits = 0
a, hit1 = s.cutout(10, 20, 1, 300, 200)
b, hit2 = s.cutout(10, 20, 1, 300, 200)
check("working survey: fetched once, then from the cache", a == JPEG and not hit1 and hit2 and H.hits == 1)
s._down_until = time.monotonic() + 100
_, hit3 = s.cutout(10, 20, 1, 300, 200)
check("while down, cached pictures are still served", hit3)
s._down_until = 0
try: s.cutout(10, 20, 1, 300, 200, hips="nope"); ok = False
except survey.SurveyDown: ok = False
except DeviceError: ok = True
check("a bad request is a plain error, not a breaker trip", ok and not s.down_status()["down"])

# 2b ----------------------------------------------------------- mirrors
s = fresh(f"http://127.0.0.1:{hole_port}/hips2fits", f"http://127.0.0.1:{port}/504",
          f"http://127.0.0.1:{port}/hips2fits", mt=1.5)
H.hits = 0; t0 = time.monotonic()
a, _ = s.cutout(11, 21, 1, 300, 200, timeout=4)
first = time.monotonic() - t0
check("mirror fallback: dead primary + 504 second -> third answers", a == JPEG, f"{first:.1f}s")
t0 = time.monotonic(); a, _ = s.cutout(12, 21, 1, 300, 200, timeout=4)
check("mirror fallback: failed mirrors skipped for 2 min (next cutout fast)",
      a == JPEG and time.monotonic() - t0 < 0.5, f"{(time.monotonic()-t0)*1000:.0f} ms")
s = fresh(f"http://127.0.0.1:{hole_port}/a", f"http://127.0.0.1:{hole_port}/b", mt=4)
t0 = time.monotonic()
try: s.cutout(13, 21, 1, 300, 200, timeout=5); ok = False
except survey.SurveyDown: ok = True
check("all mirrors dead: one attempt stays inside the timeout budget", ok and time.monotonic() - t0 < 5.5,
      f"{time.monotonic()-t0:.1f}s")
import importlib; importlib.reload(survey)
names = [m["name"] for m in survey.MIRRORS]
u = survey._skyview("CDS/P/DSS2/color", 83.8, -5.4, 2, 480, 320, 0)
check("default mirrors: alasky, alaskybis, skyview", names == ["alasky", "alaskybis", "skyview"] and "Pixels=480%2C320" in u)

# 3 ------------------------------------------------------------- the route
try:
    from fastapi.testclient import TestClient
    from astrocontrol import main
    main.surveys = fresh(f"http://127.0.0.1:{hole_port}/hips2fits")
    main.surveys._down_until = time.monotonic() + 90; main.surveys._down_reason = "timed out"
    c = TestClient(main.app)
    t0 = time.monotonic()
    r = c.get("/api/survey/image?ra=1&dec=2&fov=1&width=300&height=200")
    check("route: 503 + Retry-After while down, at once",
          r.status_code == 503 and 85 <= int(r.headers.get("retry-after", 0)) <= 90 and time.monotonic() - t0 < 1,
          f"{r.status_code} retry-after={r.headers.get('retry-after')}")
    main.surveys = fresh(f"http://127.0.0.1:{port}/hips2fits")
    r = c.get("/api/survey/image?ra=1&dec=2&fov=1&width=300&height=200")
    check("route: working survey answers the JPEG", r.status_code == 200 and r.content == JPEG)
    r = c.get("/api/survey/image?ra=1&dec=2&fov=1&hips=nope")
    check("route: bad survey is still a 400", r.status_code == 400)
except ImportError as e:
    check("route test (fastapi missing)", False, str(e))

# 4 ------------------------------------------------------------- Sesame
from astrocontrol import catalog
catalog._SESAME = f"http://127.0.0.1:{hole_port}/sesame?"
catalog._cache.clear(); catalog._down_until = 0
t0 = time.monotonic(); catalog.resolve("Zzzfoo", timeout=2); first = time.monotonic() - t0
t0 = time.monotonic()
for q in ("a1", "b2", "c3", "d4"): catalog.resolve(q)
dt = time.monotonic() - t0
check("Sesame: after one failure, lookups skip CDS at once", 1.5 < first < 4 and dt < 0.2, f"first {first:.1f}s, next 4 {dt*1000:.0f} ms")
check("Sesame: the failure is not cached for an hour", "zzzfoo" not in catalog._cache)

print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
