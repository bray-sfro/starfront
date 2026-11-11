"""The website around the files page: a front door and two paths through it.

People come here for one of two things, and the site says so on its first
screen:

* **Collaborations** (`/collabs`) - browse every shared target as a card
  with its latest picture and how far along it is, then *View* the image or
  *Contribute* frames. Both open the files page (`/files?project=<id>`),
  which stays what it was: the uploader, the rules, the releases viewer.
* **My imaging** (`/me`) - one person's own targets (server/personal.py):
  what their rigs have sent, what is ready, the latest stack, whether new
  frames are processed automatically. While personal projects are not
  open, it explains what they will be and does nothing else.

`/` is the front door to both. Every page here shares one header, and the
files page carries the same one, so moving between them is one click.

Pages are static strings; everything on them is fetched from the JSON
routes, so a page can be cached and nothing personal is ever in its HTML.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

#: Pages a sign-in may come back to.
BACK = ("/", "/collabs", "/me", "/telescopes", "/files")

#: How long since a check-in a telescope still counts as online, unless the
#: server says otherwise (app.py passes its own ONLINE_SECONDS).
ONLINE_SECONDS = 25 * 60


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class Site:
    def __init__(self, app: FastAPI, store: Any, files: Any, releases: Any, personal: Any,
                 figures: Callable[[str, dict[str, Any]], tuple[dict[str, float], dict[str, Any]]],
                 online_seconds: float = ONLINE_SECONDS) -> None:
        self.store = store
        self.files = files
        self.releases = releases
        self.personal = personal
        self.figures = figures
        self.online_seconds = float(online_seconds)
        self._routes(app)

    def _who(self, request: Request) -> dict[str, Any] | None:
        try:
            return self.files.who(request)
        except HTTPException:
            return None

    def telescope_card(self, agent: dict[str, Any], now: float) -> dict[str, Any]:
        """One of a person's own telescopes: whether it is online, what it
        last said it was doing, and the equipment it said it has. Never its
        token."""
        said = agent.get("presence") if isinstance(agent.get("presence"), dict) else {}
        profile = agent.get("profile") if isinstance(agent.get("profile"), dict) else {}
        seen = float(agent.get("seen") or 0.0)
        focal, pixel = _num(profile.get("focalLength")), _num(profile.get("pixelSize"))
        width, height = _num(profile.get("sensorWidth")), _num(profile.get("sensorHeight"))
        binning = max(1.0, _num(profile.get("binning")) or 1.0)
        scale = 206.265 * pixel * binning / focal if focal and pixel else None      # arcsec per pixel
        field = None
        if scale and width and height:
            field = [round(width / binning * scale / 3600, 2), round(height / binning * scale / 3600, 2)]
        filters = profile.get("filters") if isinstance(profile.get("filters"), dict) else {}
        work = []
        for task in self.store.tasks_for(agent["id"]):
            project = self.store.project(str(task.get("project") or ""))
            if project is None or project.get("status") != "open":
                continue
            work.append({"project": project["id"], "name": project["name"],
                         "state": str(task.get("state") or ""),
                         "personal": project.get("scope") == "personal"})
        # The name the rig gives itself on each check-in, over the one it was
        # enrolled under ("Telescope 1" on nearly every install).
        called = str(said.get("telescope") or profile.get("name") or "").strip()[:60]
        return {
            "id": agent["id"], "name": called or agent.get("name") or "",
            "enrolledAs": agent.get("name") or "",
            "added": agent.get("created"),
            "seen": seen or None,
            "ageSeconds": round(max(0.0, now - seen)) if seen else None,
            "online": bool(seen) and now - seen <= self.online_seconds,
            "state": str(said.get("state") or "")[:40],
            "target": str(said.get("target") or "")[:80],
            "ra": _num(said.get("ra")), "dec": _num(said.get("dec")),      # RA in hours, as the rig says it
            "equipment": {
                "focalLength": focal, "pixelSize": pixel,
                "sensor": [int(width), int(height)] if width and height else None,
                "binning": int(binning), "colour": bool(profile.get("colour")),
                "scale": round(scale, 2) if scale else None, "field": field,
                "filters": [str(name)[:24] for name in list(filters)[:16]],
                "rotation": _num(profile.get("rotation")),
            },
            "work": work,
        }

    def collab_card(self, project: dict[str, Any], who: dict[str, Any] | None) -> dict[str, Any] | None:
        """One collaboration as a card: what it is, how far along, its picture."""
        payload = project.get("payload") or {}
        closed = project.get("status") != "open"
        totals = self.store.file_totals(project["id"])
        release = self.releases.summary(project["id"]) if self.releases is not None else None
        collected, progress = self.figures(project["id"], payload)
        rows = self.store.contributions(project["id"])
        if closed and not (totals["count"] or release or rows):
            return None
        mine = self.store.file_totals_of(project["id"], who["id"]) if who else {"count": 0, "bytes": 0}
        notes = str(payload.get("notes") or "")
        return {
            "id": project["id"], "name": project["name"],
            "coordinator": project.get("coordinator") or "",
            "shape": payload.get("kind") or "mosaic",
            "created": project.get("created"),
            "closed": closed,
            "collecting": bool(payload.get("collectFiles")) and not closed,
            "autoUpload": bool(payload.get("autoUpload")),
            "notes": notes[:240] + ("…" if len(notes) > 240 else ""),
            "files": totals["count"], "bytes": totals["bytes"],
            "hours": round(sum(collected.values()), 1),
            "telescopes": len({r.get("agent") for r in rows if r.get("agent")}),
            "progress": progress,
            "release": release,
            "owned": bool(who) and str(project.get("owner_id") or "") == who["id"],
            "mine": mine["count"],
        }

    def _routes(self, app: FastAPI) -> None:

        @app.get("/api/v1/site/collabs")
        def collabs(request: Request) -> dict[str, Any]:
            """Every collaboration worth a card: open ones, and finished ones
            that hold something. Public; `owned`/`mine` only when signed in."""
            who = self._who(request)
            cards = [c for c in (self.collab_card(p, who) for p in self.store.projects()) if c]
            # Open before finished; then the ones with a picture; newest first.
            cards.sort(key=lambda c: (c["closed"], not c["release"], -(c["created"] or 0)))
            return {"collabs": cards, "time": time.time()}

        @app.get("/api/v1/site/me")
        def me(request: Request) -> dict[str, Any]:
            """Who is looking, and whether My imaging is open to them."""
            who = self._who(request)
            allowed = self.personal.entitlement(who) if self.personal is not None else \
                {"personal": False, "why": "personal projects are not open yet", "plan": ""}
            return {"signedIn": who is not None, "name": who["name"] if who else "",
                    "admin": bool(who and who.get("admin")),
                    "personal": {"available": allowed["personal"], "why": allowed["why"],
                                 "plan": allowed["plan"]}}

        @app.get("/api/v1/site/telescopes")
        def my_telescopes(request: Request) -> dict[str, Any]:
            """The signed-in person's own telescopes, online ones first."""
            who = self._who(request)
            if who is None:
                raise HTTPException(status_code=401, detail="sign in with Discord first")
            now = time.time()
            cards = [self.telescope_card(agent, now) for agent in self.store.agents_of(str(who["id"]))]
            cards.sort(key=lambda c: (not c["online"], -(c["seen"] or 0), c["name"].lower()))
            return {"telescopes": cards, "online": sum(1 for c in cards if c["online"]),
                    "onlineSeconds": self.online_seconds, "time": now}

        @app.get("/signin")
        def signin(back: str = Query(default="/", max_length=40)):
            """A browser sign-in that comes back to the page it began on."""
            self.store.sweep_logins(time.time() - 2 * 600)
            code = self.files.new_login_code()
            self.store.add_login(code, back=back if back in BACK else "/")
            return RedirectResponse(f"/auth/discord/start?code={code}", status_code=303)

        @app.get("/")
        def home() -> HTMLResponse:
            return HTMLResponse(page("Starfront", "home", _HOME))

        @app.get("/collabs")
        def collabs_page() -> HTMLResponse:
            return HTMLResponse(page("Collaborations · Starfront", "collabs", _COLLABS))

        @app.get("/me")
        def me_page() -> HTMLResponse:
            return HTMLResponse(page("My imaging · Starfront", "me", _ME))

        @app.get("/telescopes")
        def telescopes_page() -> HTMLResponse:
            return HTMLResponse(page("My telescopes · Starfront", "telescopes", _TELESCOPES))


# ---------------------------------------------------------------------------
# The shared look
# ---------------------------------------------------------------------------

#: The header every page has, the files page included (it inlines a copy).
#: `data-nav` marks which link is the current page.
NAV_CSS = r"""
  .sfnav { position: sticky; top: 0; z-index: 20; background: rgba(13,16,23,.92);
           backdrop-filter: blur(8px); border-bottom: 1px solid #1f2634; }
  .sfnav .in { max-width: 1080px; margin: 0 auto; padding: 0 20px; height: 56px;
               display: flex; align-items: center; gap: 22px; }
  .sfnav .brand { color: #f3f6fb; font-weight: 700; letter-spacing: .2px; text-decoration: none;
                  display: flex; align-items: center; gap: 8px; font-size: 16px; }
  .sfnav .brand i { width: 10px; height: 10px; border-radius: 50%; background: #7ee7a5;
                    box-shadow: 0 0 12px #7ee7a588; }
  .sfnav .links { display: flex; gap: 4px; }
  .sfnav .links a { color: #aab5c6; text-decoration: none; padding: 7px 12px; border-radius: 8px;
                    font-size: 14px; font-weight: 500; }
  .sfnav .links a:hover { color: #f3f6fb; background: #151b26; }
  .sfnav .links a[aria-current=page] { color: #0d1017; background: #7ee7a5; }
  .sfnav .who { margin-left: auto; font-size: 13px; color: #8f9bb0; display: flex; gap: 10px; align-items: center; }
  .sfnav .who a, .sfnav .who button { color: #dbe3ee; background: #1a2130; border: 0; border-radius: 8px;
                    padding: 6px 12px; font: inherit; font-size: 13px; cursor: pointer; text-decoration: none; }
  @media (max-width: 640px) {
    .sfnav .in { gap: 10px; padding: 0 12px; }
    .sfnav .brand span { display: none; }
    .sfnav .links a { padding: 6px 8px; font-size: 13px; white-space: nowrap; text-transform: capitalize; }
    .sfnav .links .my { display: none; }
    .sfnav .who .nm { display: none; }
  }
"""

NAV_HTML = r"""<nav class="sfnav" aria-label="Starfront"><div class="in">
  <a class="brand" href="/"><i></i><span>Starfront</span></a>
  <div class="links">
    <a href="/collabs" data-nav="collabs">Collaborations</a>
    <a href="/me" data-nav="me"><span class="my">My </span>imaging</a>
    <a href="/telescopes" data-nav="telescopes"><span class="my">My </span>telescopes</a>
  </div>
  <div class="who" id="sfWho"></div>
</div></nav>"""

#: Fills in who is signed in, and marks the current page. `here` is the page key.
NAV_JS = r"""
(function () {
  const here = document.body.dataset.page || '';
  document.querySelectorAll('.sfnav [data-nav]').forEach((a) => {
    if (a.dataset.nav === here) a.setAttribute('aria-current', 'page');
  });
  const box = document.getElementById('sfWho');
  if (!box || box.dataset.managed) return;
  fetch('/api/v1/site/me', { credentials: 'same-origin' }).then((r) => r.json()).then((me) => {
    const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    if (me.signedIn) {
      box.innerHTML = `<span class="nm">${esc(me.name)}</span><button type="button" id="sfOut">Sign out</button>`;
      document.getElementById('sfOut').addEventListener('click', async () => {
        await fetch('/files/signout', { method: 'POST' }); location.reload();
      });
    } else {
      box.innerHTML = `<a href="/signin?back=${encodeURIComponent(location.pathname)}">Sign in</a>`;
    }
    document.dispatchEvent(new CustomEvent('sf:me', { detail: me }));
  }).catch(() => {});
})();
"""

_BASE_CSS = r"""
  :root { color-scheme: dark; --bg: #0d1017; --panel: #11161f; --panel2: #151b26; --line: #1f2634;
          --line2: #2a3344; --text: #dbe3ee; --hi: #f3f6fb; --muted: #8f9bb0; --accent: #7ee7a5;
          --warn: #e5c07b; --bad: #ff8a7a; }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 1080px; margin: 0 auto; padding: 36px 20px 72px; }
  h1 { font-size: 30px; line-height: 1.2; margin: 0 0 8px; color: var(--hi); letter-spacing: -.2px; }
  h2 { font-size: 18px; margin: 0 0 6px; color: var(--hi); }
  h3 { font-size: 15px; margin: 0 0 4px; color: var(--hi); }
  p { margin: 6px 0; }
  a { color: #9fe4bd; }
  .muted { color: var(--muted); } .small { font-size: 13px; }
  .lede { font-size: 17px; color: #b9c4d4; max-width: 640px; }
  .btn { display: inline-flex; align-items: center; gap: 6px; background: var(--accent); color: #0d1017;
         font-weight: 600; padding: 9px 16px; border-radius: 8px; border: 0; cursor: pointer;
         text-decoration: none; font-size: 14px; font-family: inherit; }
  .btn.ghost { background: #1a2130; color: var(--text); }
  .btn.small { padding: 6px 11px; font-size: 13px; }
  .btn:disabled { opacity: .5; cursor: default; }
  .badge { display: inline-block; font-size: 11.5px; font-weight: 600; padding: 2px 8px; border-radius: 999px;
           background: #1a2130; color: var(--muted); letter-spacing: .2px; white-space: nowrap; }
  .badge.on { background: #173326; color: var(--accent); }
  .badge.warn { background: #33291a; color: var(--warn); }
  .badge.off { background: #1a2130; color: var(--muted); }
  .bar { height: 6px; background: #1a2130; border-radius: 3px; overflow: hidden; }
  .bar > i { display: block; height: 100%; background: var(--accent); }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 20px; }
  .steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin: 18px 0 0; padding: 0; list-style: none; counter-reset: s; }
  .steps li { counter-increment: s; background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
  .steps li::before { content: counter(s); display: inline-grid; place-items: center; width: 22px; height: 22px;
           border-radius: 50%; background: #1a2130; color: var(--accent); font-size: 12px; font-weight: 700; margin-bottom: 6px; }
  .steps b { display: block; color: var(--hi); }
  .sky { background: radial-gradient(120% 90% at 30% 20%, #1c2a44 0%, #0f1522 55%, #0b0e15 100%); position: relative; overflow: hidden; }
  .sky::after { content: ""; position: absolute; inset: 0; opacity: .55;
           background-image: radial-gradient(1px 1px at 12% 30%, #fff 50%, transparent 51%), radial-gradient(1px 1px at 68% 22%, #fff 50%, transparent 51%),
             radial-gradient(1.5px 1.5px at 40% 70%, #cfe 50%, transparent 51%), radial-gradient(1px 1px at 85% 64%, #fff 50%, transparent 51%),
             radial-gradient(1px 1px at 25% 85%, #fff 50%, transparent 51%), radial-gradient(1px 1px at 55% 45%, #dfe 50%, transparent 51%); }
  .empty { text-align: center; padding: 40px 20px; color: var(--muted); }
  @media (max-width: 640px) { main { padding: 24px 16px 56px; } h1 { font-size: 24px; } }
"""

_COMMON_JS = r"""
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const gb = (b) => { b = Number(b) || 0; if (b >= 1024 ** 4) return (b / 1024 ** 4).toFixed(2) + ' TB';
  if (b >= 1024 ** 3) return (b / 1024 ** 3).toFixed(1) + ' GB'; if (b >= 1024 ** 2) return Math.round(b / 1024 ** 2) + ' MB';
  return Math.max(0, Math.round(b / 1024)) + ' kB'; };
const when = (t) => { if (!t) return ''; const d = typeof t === 'number' ? new Date(t * 1000) : new Date(t);
  return isNaN(d) ? String(t) : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }); };
async function api(path, opts) {
  const r = await fetch(path, { credentials: 'same-origin', ...(opts || {}) });
  let body = {}; try { body = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok) { const e = new Error(body.detail || r.statusText); e.status = r.status; throw e; }
  return body;
}
"""


def page(title: str, key: str, body: str) -> str:
    """A whole page: the shared look and header around one page's own parts."""
    css, html, js = body.split("<!--split-->")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{title}</title><style>{_BASE_CSS}{NAV_CSS}{css}</style></head>"
            f"<body data-page=\"{key}\">{NAV_HTML}<main>{html}</main>"
            f"<script>{NAV_JS}{_COMMON_JS}{js}</script></body></html>")


# ---------------------------------------------------------------------------
# /  - the front door
# ---------------------------------------------------------------------------

_HOME = r"""
  .hero { padding: 18px 0 30px; }
  .paths { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  .path { display: flex; flex-direction: column; border-radius: 16px; overflow: hidden; border: 1px solid var(--line);
          background: var(--panel); text-decoration: none; color: inherit; transition: border-color .15s, transform .15s; }
  .path:hover { border-color: #3a4a62; transform: translateY(-2px); }
  .path .pic { height: 190px; background-size: cover; background-position: center; }
  .path .txt { padding: 18px 20px 20px; display: flex; flex-direction: column; gap: 6px; flex: 1; }
  .path .txt .go { margin-top: auto; padding-top: 10px; display: flex; gap: 10px; align-items: center; }
  .path h2 { font-size: 20px; display: flex; gap: 10px; align-items: center; }
  .strip { display: grid; grid-template-columns: repeat(3, 1fr); gap: 2px; height: 190px; }
  .strip div { background-size: cover; background-position: center; }
  .facts { display: flex; gap: 22px; flex-wrap: wrap; font-size: 13px; color: var(--muted); }
  .facts b { color: var(--hi); font-size: 15px; display: block; }
  .more { margin-top: 34px; display: flex; gap: 10px; flex-wrap: wrap; }
  @media (max-width: 760px) { .paths { grid-template-columns: 1fr; } }
<!--split-->
<section class="hero">
  <h1>Image the sky together, or have your own targets processed for you.</h1>
  <p class="lede">Starfront connects telescopes. Join a shared target and add your frames to a deeper image than any one rig could make, or let the server keep and stack your own imaging automatically.</p>
</section>
<div class="paths">
  <a class="path" href="/collabs">
    <div class="pic sky" id="collabPic"></div>
    <div class="txt">
      <h2>Collaborations</h2>
      <p class="muted">Pick a shared target, shoot your share of it, and see the combined image grow as everyone's frames come in.</p>
      <div class="facts" id="collabFacts"></div>
      <div class="go"><span class="btn">Browse collaborations</span></div>
    </div>
  </a>
  <a class="path" href="/me">
    <div class="pic sky"></div>
    <div class="txt">
      <h2>My imaging <span class="badge" id="meBadge" hidden></span></h2>
      <p class="muted">Your own targets: your rig sends each frame as it is saved, the server calibrates and stacks it, and the result is waiting for you in the morning.</p>
      <p class="small muted" id="meNote"></p>
      <div class="go"><span class="btn ghost" id="meGo">Open My imaging</span></div>
    </div>
  </a>
</div>
<div class="more">
  <a class="btn ghost small" href="/download">Get the Starfront app</a>
  <a class="btn ghost small" href="/nina">N.I.N.A. plugin</a>
  <a class="btn ghost small" href="/sky">Who is imaging now</a>
</div>
<!--split-->
api('/api/v1/site/collabs').then((d) => {
  const open = d.collabs.filter((c) => !c.closed);
  const pics = d.collabs.map((c) => c.release && c.release.preview).filter(Boolean).slice(0, 3);
  if (pics.length) {
    const strip = document.createElement('div'); strip.className = 'strip';
    strip.innerHTML = pics.map((u) => `<div style="background-image:url('${encodeURI(u)}')"></div>`).join('');
    const pic = $('collabPic'); pic.className = 'pic'; pic.replaceWith(strip);
  }
  const hours = d.collabs.reduce((a, c) => a + (c.hours || 0), 0);
  const scopes = d.collabs.reduce((a, c) => a + (c.telescopes || 0), 0);
  $('collabFacts').innerHTML = `<span><b>${open.length}</b>open</span><span><b>${Math.round(hours)} h</b>collected</span>`
    + `<span><b>${d.collabs.filter((c) => c.release).length}</b>with images</span>`;
}).catch(() => {});
document.addEventListener('sf:me', (e) => {
  const me = e.detail; const p = me.personal || {};
  const b = $('meBadge');
  if (p.available) { b.hidden = true; return; }
  $('meNote').textContent = me.signedIn ? '' : 'Sign in with Discord to see your own targets.';
  if (/sign in/i.test(p.why || '')) { b.hidden = true; return; }
  b.hidden = false; b.className = 'badge warn';
  b.textContent = /plan/.test(p.why || '') ? 'Needs a plan' : 'Coming soon';
  $('meNote').textContent = me.signedIn ? '' : 'Sign in with Discord to see your own targets.';
  $('meGo').textContent = 'See what it will do';
});
"""


# ---------------------------------------------------------------------------
# /collabs  - every shared target, as cards
# ---------------------------------------------------------------------------

_COLLABS = r"""
  .head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 18px; }
  .tools { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .chip { background: #151b26; color: #aab5c6; border: 1px solid var(--line); border-radius: 999px; padding: 6px 12px;
          font: inherit; font-size: 13px; cursor: pointer; }
  .chip[aria-pressed=true] { background: #173326; color: var(--accent); border-color: #24503a; }
  input[type=search] { background: var(--panel); color: var(--text); border: 1px solid var(--line2); border-radius: 8px;
          padding: 7px 10px; font: inherit; font-size: 14px; width: 200px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 18px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; overflow: hidden; display: flex; flex-direction: column; }
  .card .pic { height: 170px; background-size: cover; background-position: center; position: relative; }
  .card .pic .tag { position: absolute; left: 10px; top: 10px; }
  .card .pic .ver { position: absolute; right: 10px; bottom: 10px; font-size: 12px; background: rgba(13,16,23,.8);
          padding: 2px 8px; border-radius: 6px; color: #cfd8e5; }
  .card .pic .nm { position: absolute; left: 14px; bottom: 12px; font-size: 20px; font-weight: 700; color: #fff; text-shadow: 0 1px 8px #000; }
  .card .body { padding: 14px 16px 16px; display: flex; flex-direction: column; gap: 8px; flex: 1; }
  .card .who { font-size: 13px; color: var(--muted); }
  .card .nums { display: flex; gap: 16px; font-size: 12.5px; color: var(--muted); flex-wrap: wrap; }
  .card .nums b { color: var(--text); font-weight: 600; }
  .goals { display: grid; grid-template-columns: auto 1fr auto; gap: 4px 10px; align-items: center; font-size: 12px; }
  .goals .f { color: #cfd8e5; font-weight: 600; } .goals .p { color: var(--muted); text-align: right; }
  .card .acts { margin-top: auto; padding-top: 6px; display: flex; gap: 8px; flex-wrap: wrap; }
  .card .notes { font-size: 13px; color: #b0bccd; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .how { margin-top: 44px; }
<!--split-->
<div class="head">
  <div>
    <h1>Collaborations</h1>
    <p class="muted">Shared targets anyone in the Starfront Discord can join. Open one to see its image, or to send your frames.</p>
  </div>
  <div class="tools">
    <button class="chip" data-show="open" aria-pressed="true">Open</button>
    <button class="chip" data-show="images" aria-pressed="false">With images</button>
    <button class="chip" data-show="mine" aria-pressed="false" hidden id="chipMine">Mine</button>
    <button class="chip" data-show="all" aria-pressed="false">All</button>
    <input type="search" id="q" placeholder="Search targets" aria-label="Search collaborations">
  </div>
</div>
<div class="grid" id="grid"><p class="muted">Loading…</p></div>
<p class="empty" id="empty" hidden>Nothing matches.</p>
<section class="how">
  <h2>How to take part</h2>
  <ol class="steps">
    <li><b>Join from your rig</b><span class="small muted">In the Starfront app or the N.I.N.A. plugin, sign in with Discord and press Join on a collaboration.</span></li>
    <li><b>Shoot your share</b><span class="small muted">The server hands your telescope the part of the sky it fits best, at your own exposures.</span></li>
    <li><b>Your frames arrive here</b><span class="small muted">Starfront sends each light as it is saved, or drop a folder on the collaboration's page.</span></li>
  </ol>
</section>
<!--split-->
let all = []; let show = 'open';
function card(c) {
  const r = c.release || null;
  const pic = r && r.preview ? `style="background-image:url('${encodeURI(r.preview)}')"` : '';
  const status = c.closed ? '<span class="badge off">Finished</span>'
    : c.collecting ? '<span class="badge on">Taking frames</span>' : '<span class="badge warn">Not taking frames</span>';
  const goals = Object.entries(c.progress || {});
  const goalRows = goals.length ? `<div class="goals">${goals.map(([f, g]) =>
      `<span class="f">${esc(f)}</span><span class="bar"><i style="width:${Math.round((g.average || 0) * 100)}%"></i></span>`
      + `<span class="p">${Math.round((g.average || 0) * 100)}% of ${esc(g.goalHours)} h</span>`).join('')}</div>` : '';
  const page = `/files?project=${encodeURIComponent(c.id)}`;
  const acts = [];
  if (r) acts.push(`<a class="btn small" href="${page}#release-${encodeURIComponent(c.id)}">View image</a>`);
  if (c.collecting) acts.push(`<a class="btn small ${r ? 'ghost' : ''}" href="${page}">Contribute frames</a>`);
  if (!r && !c.collecting) acts.push(`<a class="btn small ghost" href="${page}">Open</a>`);
  return `<article class="card">
    <div class="pic ${pic ? '' : 'sky'}" ${pic}>
      <span class="tag">${status}</span>
      ${pic ? '' : `<span class="nm">${esc(c.name)}</span>`}
      ${r ? `<span class="ver">${esc(r.version)} · ${esc(when(r.released))}</span>` : ''}
    </div>
    <div class="body">
      <h3>${esc(c.name)} ${c.owned ? '<span class="badge on">You run this</span>' : c.mine ? '<span class="badge">You contribute</span>' : ''}</h3>
      <div class="who">${c.coordinator ? 'Started by ' + esc(c.coordinator) : ''}${c.shape === 'single' ? ' · single field' : c.shape ? ' · mosaic' : ''}</div>
      ${c.notes ? `<div class="notes">${esc(c.notes)}</div>` : ''}
      <div class="nums">${c.telescopes ? `<span><b>${esc(c.hours)} h</b> collected</span><span><b>${esc(c.telescopes)}</b> telescope${c.telescopes === 1 ? '' : 's'}</span>` : '<span>No nights reported yet</span>'}${c.files ? `<span><b>${gb(c.bytes)}</b> of frames</span>` : ''}</div>
      ${goalRows}
      <div class="acts">${acts.join('')}</div>
    </div>
  </article>`;
}
function draw() {
  const q = $('q').value.trim().toLowerCase();
  const list = all.filter((c) => (show === 'all' || (show === 'open' && !c.closed) || (show === 'images' && c.release)
      || (show === 'mine' && (c.owned || c.mine))) && (!q || (c.name + ' ' + c.coordinator).toLowerCase().includes(q)));
  $('grid').innerHTML = list.map(card).join('');
  $('empty').hidden = list.length > 0;
}
document.querySelectorAll('[data-show]').forEach((b) => b.addEventListener('click', () => {
  show = b.dataset.show;
  document.querySelectorAll('[data-show]').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
  draw();
}));
$('q').addEventListener('input', draw);
api('/api/v1/site/collabs').then((d) => {
  all = d.collabs;
  if (all.some((c) => c.owned || c.mine)) $('chipMine').hidden = false;
  draw();
}).catch((e) => { $('grid').innerHTML = `<p class="muted">Could not load collaborations (${esc(e.message)}).</p>`; });
"""


# ---------------------------------------------------------------------------
# /me  - one person's own targets
# ---------------------------------------------------------------------------

_ME = r"""
  .head { display: flex; justify-content: space-between; gap: 18px; flex-wrap: wrap; align-items: flex-end; margin-bottom: 20px; }
  .meter { min-width: 260px; }
  .meter .bar { height: 8px; margin: 6px 0 2px; }
  .list { display: flex; flex-direction: column; gap: 14px; }
  .row { display: grid; grid-template-columns: 168px 1fr auto; gap: 18px; background: var(--panel);
         border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }
  .row .pic { background-size: cover; background-position: center; min-height: 128px; }
  .row .mid { padding: 14px 0; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  .row .side { padding: 14px 16px 14px 0; display: flex; flex-direction: column; gap: 8px; align-items: flex-end; justify-content: space-between; }
  .counts { display: flex; gap: 14px; flex-wrap: wrap; font-size: 13px; color: var(--muted); }
  .counts b { color: var(--text); }
  .counts .ok b { color: var(--accent); } .counts .wait b { color: var(--warn); } .counts .no b { color: var(--bad); }
  .switch { display: flex; gap: 8px; align-items: center; font-size: 13px; color: #cfd8e5; cursor: pointer; user-select: none; }
  .switch input { appearance: none; width: 34px; height: 20px; border-radius: 999px; background: #2a3344; position: relative; cursor: pointer; margin: 0; transition: background .15s; }
  .switch input::after { content: ""; position: absolute; top: 3px; left: 3px; width: 14px; height: 14px; border-radius: 50%; background: #dbe3ee; transition: left .15s; }
  .switch input:checked { background: var(--accent); } .switch input:checked::after { left: 17px; background: #0d1017; }
  .stack { font-size: 12.5px; color: var(--muted); text-align: right; }
  .target { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; overflow: hidden; }
  .target > .row { border: 0; border-radius: 0; background: none; }
  .parts { border-top: 1px solid var(--line); display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); background: #121722; overflow: hidden; }
  .part { box-shadow: 1px 0 0 var(--line), 0 1px 0 var(--line); display: grid; grid-template-columns: 56px 1fr; gap: 6px 12px; padding: 12px 14px; align-items: start; }
  .part .pic { width: 56px; height: 56px; border-radius: 8px; background-size: cover; background-position: center; }
  .part h3 { margin: 0 0 4px; font-size: 14px; }
  .part .counts { font-size: 12px; gap: 10px; }
  .part .foot { grid-column: 1 / -1; display: flex; justify-content: flex-end; gap: 8px; }
  .part .stack { text-align: left; font-size: 12px; margin-top: 4px; }
  .kind { font-size: 11.5px; font-weight: 600; letter-spacing: .02em; color: #9fb0c8; background: #1b2231; border: 1px solid var(--line); padding: 2px 8px; border-radius: 999px; vertical-align: middle; margin-left: 8px; }
  .intro { display: grid; grid-template-columns: 1.2fr 1fr; gap: 24px; align-items: start; }
  .state { display: flex; gap: 12px; align-items: center; margin: 14px 0 4px; }
  @media (max-width: 760px) {
    .row { grid-template-columns: 1fr; } .row .pic { min-height: 150px; }
    .row .mid { padding: 0 16px; } .row .side { padding: 0 16px 16px; align-items: flex-start; }
    .intro { grid-template-columns: 1fr; }
  }
<!--split-->
<section id="intro" hidden>
  <div class="intro">
    <div>
      <h1>My imaging</h1>
      <p class="lede">Your own targets, processed for you. Starfront sends each frame your rig saves to the server, matches it with your flats, darks and bias, and stacks it, so a finished image of every target is waiting here.</p>
      <div class="state"><span class="badge warn" id="introBadge">Coming soon</span><span class="small muted" id="introWhy"></span></div>
      <p id="introSignin" hidden><a class="btn" href="/signin?back=/me">Sign in with Discord</a></p>
    </div>
    <div class="panel">
      <h3>What you will see here</h3>
      <ul class="small muted" style="padding-left:18px;margin:8px 0 0">
        <li>Every target your telescopes have shot, with its latest stacked image</li>
        <li>Which lights are ready, and which still wait for calibration</li>
        <li>Automatic processing, on or off for each target</li>
        <li>How much of your storage you are using</li>
      </ul>
    </div>
  </div>
  <ol class="steps">
    <li><b>Turn it on in Starfront</b><span class="small muted">One switch: upload and process my own imaging.</span></li>
    <li><b>Image as usual</b><span class="small muted">Each target becomes its own project here, from any of your rigs.</span></li>
    <li><b>Get the stack</b><span class="small muted">New frames are calibrated and stacked; the result appears on this page.</span></li>
  </ol>
</section>

<section id="mine" hidden>
  <div class="head">
    <div>
      <h1>My imaging</h1>
      <p class="muted">Your own targets. Only you can see them.</p>
    </div>
    <div class="meter">
      <div class="small"><b id="usedTxt">0</b> <span class="muted">of <span id="roomTxt">0</span> used</span></div>
      <div class="bar"><i id="usedBar" style="width:0"></i></div>
      <div class="small muted" id="planTxt"></div>
    </div>
  </div>
  <div class="list" id="list"></div>
  <div class="panel empty" id="none" hidden>
    <h3>No targets yet</h3>
    <p class="small">Turn on <b>upload and process my own imaging</b> in Starfront and shoot a target. It will appear here with the first frame.</p>
  </div>
</section>
<!--split-->
function stackLine(p) {
  const s = p.stack || {};
  if (s.busy) return s.state === 'queued' ? 'Stack queued' : `Stacking${s.message ? ': ' + esc(s.message) : '…'}`;
  if (p.release) return `Stacked ${esc(when(p.release.released))} · ${esc(p.release.version)}`;
  return 'Not stacked yet';
}
function counts(L, bytes) {
  return `<span class="ok"><b>${L.ready || 0}</b> ready</span>
        <span class="wait"><b>${L.pending || 0}</b> waiting${bytes === undefined ? '' : ' for calibration'}</span>
        ${L.quarantine ? `<span class="no"><b>${L.quarantine}</b> set aside</span>` : ''}
        ${bytes === undefined ? '' : `<span><b>${gb(bytes)}</b></span>`}`;
}
function part(q) {
  const pic = q.release && q.release.preview;
  const open = pic ? `<a class="btn small ghost" href="${encodeURI(pic)}" target="_blank" rel="noopener">Open</a>` : '';
  return `<div class="part" data-id="${esc(q.id)}">
    <div class="pic ${pic ? '' : 'sky'}" ${pic ? `style="background-image:url('${encodeURI(pic)}')"` : ''}></div>
    <div>
      <h3>${esc((q.part || {}).label || q.name)}</h3>
      <div class="counts">${counts(q.lights || {})}<span>${gb(q.bytes)}</span></div>
      <div class="stack">${stackLine(q)}</div>
    </div>
    <div class="foot">${open}
      <button class="btn small ghost" data-stack ${q.files && !(q.stack || {}).busy ? '' : 'disabled'}>Stack</button>
    </div>
  </div>`;
}
function row(p) {
  if (p.parts && p.parts.length) return mosaic(p);
  const L = p.lights || {}; const R = p.reports || {};
  const pic = p.release && p.release.preview;
  const filters = (p.release && p.release.filters || []).join(' ');
  return `<article class="row" data-id="${esc(p.id)}">
    <div class="pic ${pic ? '' : 'sky'}" ${pic ? `style="background-image:url('${encodeURI(pic)}')"` : ''}></div>
    <div class="mid">
      <h2>${esc(p.name)}</h2>
      <div class="counts">${counts(L, p.bytes)}</div>
      <div class="small muted">${R.telescopes ? esc(R.telescopes) + ' telescope' + (R.telescopes === 1 ? '' : 's') + ' · ' : ''}${R.hours ? esc(Math.round(R.hours * 10) / 10) + ' h reported · ' : ''}since ${esc(when(p.created))}${filters ? ' · ' + esc(filters) : ''}</div>
    </div>
    <div class="side">
      <label class="switch"><input type="checkbox" data-auto ${p.autoProcess ? 'checked' : ''}> Process automatically</label>
      <div class="stack">${stackLine(p)}</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end">
        ${pic ? `<a class="btn small ghost" href="${encodeURI(pic)}" target="_blank" rel="noopener">Open image</a>` : ''}
        <button class="btn small" data-stack ${p.files && !(p.stack || {}).busy ? '' : 'disabled'}>Stack now</button>
      </div>
    </div>
  </article>`;
}
function mosaic(p) {
  const L = p.lights || {};
  const shown = p.parts.find((q) => q.release && q.release.preview);
  const pic = (p.release && p.release.preview) || (shown && shown.release.preview);
  const stacked = p.parts.filter((q) => q.release).length;
  // Frames shot under the plain name, of the field as a whole, sit on the
  // target itself: shown as a part of their own so they can be stacked too.
  const own = p.own || {};
  const whole = own.files ? part({ id: p.id, part: { label: 'Whole field' }, lights: own.lights, bytes: own.bytes,
    files: own.files, release: p.release, stack: p.stack }) : '';
  return `<div class="target">
    <article class="row" data-id="${esc(p.id)}">
      <div class="pic ${pic ? '' : 'sky'}" ${pic ? `style="background-image:url('${encodeURI(pic)}')"` : ''}></div>
      <div class="mid">
        <h2>${esc(p.name)}<span class="kind">Mosaic · ${p.parts.length} panel${p.parts.length === 1 ? '' : 's'}</span></h2>
        <div class="counts">${counts(L, p.bytes)}</div>
        <div class="small muted">since ${esc(when(p.created))}</div>
      </div>
      <div class="side">
        <label class="switch"><input type="checkbox" data-auto ${p.autoProcess ? 'checked' : ''}> Process automatically</label>
        <div class="stack">${stacked} of ${p.parts.length} panels stacked</div>
        <div></div>
      </div>
    </article>
    <div class="parts">${whole}${p.parts.map(part).join('')}</div>
  </div>`;
}
async function load() {
  let d;
  try { d = await api('/api/v1/me/personal'); }
  catch (e) {
    $('intro').hidden = false;
    if (e.status === 401) { $('introSignin').hidden = false; $('introBadge').hidden = true; $('introWhy').textContent = 'Sign in to see your own targets.'; }
    return;
  }
  if (!d.available) {
    $('intro').hidden = false;
    $('introBadge').textContent = /plan/.test(d.why || '') ? 'Needs a plan' : 'Coming soon';
    $('introWhy').textContent = d.why ? d.why[0].toUpperCase() + d.why.slice(1) + '.' : '';
    if (!d.projects.length) return;
  }
  $('mine').hidden = false;
  $('usedTxt').textContent = gb(d.usedBytes);
  $('roomTxt').textContent = gb(d.storageBytes);
  $('usedBar').style.width = Math.min(100, d.storageBytes ? 100 * d.usedBytes / d.storageBytes : 0) + '%';
  $('planTxt').textContent = d.plan ? `Plan: ${d.plan}` : '';
  $('list').innerHTML = d.projects.map(row).join('');
  $('none').hidden = d.projects.length > 0;
}
$('list').addEventListener('change', async (e) => {
  const box = e.target.closest('[data-auto]'); if (!box) return;
  const id = box.closest('[data-id]').dataset.id;
  box.disabled = true;
  try { await api(`/api/v1/me/personal/${id}`, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ autoProcess: box.checked }) }); }
  catch (err) { box.checked = !box.checked; alert(err.message); }
  box.disabled = false;
});
$('list').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-stack]'); if (!b) return;
  const id = b.closest('[data-id]').dataset.id;
  const label = b.textContent;
  b.disabled = true; b.textContent = 'Asking…';
  try { await api(`/api/v1/releases/${id}/stack`, { method: 'POST' }); await load(); }
  catch (err) { b.textContent = label; b.disabled = false; alert(err.message); }
});
load();
"""


# ---------------------------------------------------------------------------
# /telescopes  - one person's own telescopes
# ---------------------------------------------------------------------------

_TELESCOPES = r"""
  .head { display: flex; justify-content: space-between; gap: 18px; flex-wrap: wrap; align-items: flex-end; margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
  .scope { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px 20px; display: flex; flex-direction: column; gap: 12px; }
  .scope.on { border-color: #254a38; }
  .scope .top { display: flex; gap: 12px; align-items: flex-start; }
  .dot { flex: none; width: 12px; height: 12px; border-radius: 50%; background: #3a4558; margin-top: 6px; }
  .on .dot { background: var(--accent); box-shadow: 0 0 0 4px #7ee7a522, 0 0 14px #7ee7a588; }
  .scope h2 { margin: 0; overflow-wrap: anywhere; }
  .scope .status { font-size: 13px; color: var(--muted); }
  .on .status b { color: var(--accent); }
  .scope .status b { color: #c3cddb; font-weight: 600; }
  .doing { background: #121a26; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; font-size: 13.5px; }
  .doing .k { color: var(--muted); font-size: 12px; display: block; }
  .specs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 14px; font-size: 13px; margin: 0; }
  .specs div { min-width: 0; } .specs dt { color: var(--muted); font-size: 12px; } .specs dd { margin: 0; color: var(--text); }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #1a2130; color: #c3cddb; }
  .work { font-size: 13px; display: flex; flex-direction: column; gap: 4px; }
  .work a { text-decoration: none; }
  @media (max-width: 420px) { .grid { grid-template-columns: 1fr; } }
<!--split-->
<div class="head">
  <div>
    <h1>My telescopes</h1>
    <p class="muted" id="sub">The telescopes connected to your Starfront account.</p>
  </div>
  <div class="small muted" id="fresh"></div>
</div>
<div class="grid" id="grid"></div>
<div class="panel empty" id="signin" hidden>
  <h3>Sign in to see your telescopes</h3>
  <p class="small">They are listed under the Discord account they were connected with.</p>
  <p><a class="btn" href="/signin?back=/telescopes">Sign in with Discord</a></p>
</div>
<div class="panel empty" id="none" hidden>
  <h3>No telescopes connected yet</h3>
  <p class="small">Connect one from the Starfront app or the N.I.N.A. plugin, signed in with this Discord account. It appears here on its first check-in.</p>
  <p style="display:flex;gap:10px;justify-content:center;flex-wrap:wrap"><a class="btn" href="/download">Get the Starfront app</a><a class="btn ghost" href="/nina">N.I.N.A. plugin</a></p>
</div>
<!--split-->
function ago(s) {
  if (s == null) return '';
  if (s < 90) return 'just now';
  if (s < 3600) return Math.round(s / 60) + ' min ago';
  if (s < 86400 * 2) return Math.round(s / 3600) + ' h ago';
  return Math.round(s / 86400) + ' days ago';
}
function coords(t) {
  if (t.ra == null || t.dec == null) return '';
  const h = Math.floor(t.ra), m = Math.round((t.ra - h) * 60);
  const sign = t.dec < 0 ? '−' : '+', d = Math.abs(t.dec);
  return `${h}h ${String(m === 60 ? 59 : m).padStart(2, '0')}m, ${sign}${d.toFixed(1)}°`;
}
const STATES = { imaging: 'Imaging', slewing: 'Slewing', idle: 'Idle', parked: 'Parked', focusing: 'Focusing',
  guiding: 'Guiding', waiting: 'Waiting', flats: 'Taking flats', darks: 'Taking darks' };
function card(t) {
  const e = t.equipment || {};
  const status = t.online ? `<b>Online</b> · checked in ${esc(ago(t.ageSeconds))}`
    : t.seen ? `<b>Offline</b> · last seen ${esc(ago(t.ageSeconds))} (${esc(when(t.seen))})`
    : '<b>Never checked in</b>';
  const state = STATES[(t.state || '').toLowerCase()] || (t.state ? t.state[0].toUpperCase() + t.state.slice(1) : '');
  const where = coords(t);
  const doing = (state || t.target || where) ? `<div class="doing"><span class="k">${t.online ? 'Right now' : 'When last seen'}</span>
      ${esc([state, t.target].filter(Boolean).join(' · ') || 'Pointing')}${where ? ` <span class="muted">(${esc(where)})</span>` : ''}</div>` : '';
  const specs = [
    e.focalLength ? ['Focal length', Math.round(e.focalLength) + ' mm'] : null,
    e.sensor ? ['Camera', `${e.sensor[0]} × ${e.sensor[1]} px${e.pixelSize ? ', ' + e.pixelSize + ' µm' : ''}`] : null,
    e.scale ? ['Image scale', e.scale + '″ per pixel' + (e.binning > 1 ? ` (bin ${e.binning})` : '')] : null,
    e.field ? ['Field of view', `${e.field[0]}° × ${e.field[1]}°`] : null,
    ['Sensor', e.colour ? 'One-shot colour' : 'Mono'],
    e.rotation != null ? ['Camera angle', Math.round(e.rotation) + '° (fixed)'] : null,
  ].filter(Boolean);
  const known = e.focalLength || e.sensor || (e.filters || []).length;
  const work = (t.work || []).map((w) => `<span>${w.state === 'accepted' ? 'Working on' : 'Offered'} ${
    w.personal ? esc(w.name) : `<a href="/files?project=${encodeURIComponent(w.project)}">${esc(w.name)}</a>`}</span>`).join('');
  return `<article class="scope ${t.online ? 'on' : ''}">
    <div class="top"><span class="dot" aria-hidden="true"></span>
      <div><h2>${esc(t.name || 'Telescope')}</h2><div class="status">${status}</div></div></div>
    ${doing}
    ${known ? `<dl class="specs">${specs.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>`
      : '<div class="small muted">No equipment details yet. They arrive with its next check-in.</div>'}
    ${(e.filters || []).length ? `<div class="chips">${e.filters.map((f) => `<span class="chip">${esc(f)}</span>`).join('')}</div>` : ''}
    ${work ? `<div class="work">${work}</div>` : ''}
    <div class="small muted">Connected ${esc(when(t.added))}${t.enrolledAs && t.enrolledAs !== t.name ? ' as ' + esc(t.enrolledAs) : ''}</div>
  </article>`;
}
async function load() {
  let d;
  try { d = await api('/api/v1/site/telescopes'); }
  catch (e) {
    if (e.status === 401) { $('signin').hidden = false; $('grid').innerHTML = ''; }
    return;
  }
  $('signin').hidden = true;
  $('grid').innerHTML = d.telescopes.map(card).join('');
  $('none').hidden = d.telescopes.length > 0;
  const n = d.telescopes.length;
  $('sub').textContent = n ? `${d.online} of ${n} online. A telescope counts as online for ${Math.round(d.onlineSeconds / 60)} minutes after its last check-in.`
    : 'The telescopes connected to your Starfront account.';
  $('fresh').textContent = 'Updated ' + new Date().toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}
load();
setInterval(() => { if (!document.hidden) load(); }, 60000);
"""
