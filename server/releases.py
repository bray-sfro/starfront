"""Published coadds: the latest master of a collaboration, for everybody.

The rest of the server holds what people *sent*; this holds what came out
of stacking it. A release is a folder the coordinator's stacker writes onto
the server (over SSH, never through a web upload), and the server only ever
reads it:

    <releases>/<project id>/<version>/release.json   what it is
    <releases>/<project id>/<version>/<files>        the files it lists
    <releases>/<project id>/LATEST                   one line: the current version

`release.json` is written by the stacker and is the whole description: the
filters with their hours and subs, who contributed what share, the files
with their sizes and checksums, the preview image and the changelog. The
server serves nothing that is not listed in it, so a stray file in the
folder is never handed out, and a name can never climb out of the folder.

What a release *is* is public: the preview images (shown on the page, not
offered as downloads), the filter and hours table, who contributed how many
hours, and the changelog. The release.json carries
contributor *names* as the stacker chose to credit them, never Discord ids
or telescope tokens.

The masters themselves (the FITS files) are for contributors only (Bray,
2026-11-03): a signed-in person who has uploaded at least one file to this
collaboration or whose own telescopes had a session accepted on it, or to one of the collaborations it counts as its own for
this (its `releaseContributorProjects` setting; by default the one it was
`continuedFrom`). The server's owners and the collaboration's owner always
may. Everybody else gets a 403 with `LOCKED_MESSAGE`.

Releases live on the files volume, in `/var/lib/starfront/files/_releases`
(a name no project id can have), because a full set of lossless masters is
several GB. They are not rows in the files table, so the per-project upload
quota counts uploads only; the stacker that publishes checks the room left
itself. `STARFRONT_RELEASES` points it elsewhere.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

#: What a version and a file name may look like. Anything else is a 404
#: before the disk is touched.
_VERSION = re.compile(r"^v\d{1,4}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_PROJECT = re.compile(r"^[0-9a-f]{6,32}$")

#: What each kind of file is served as.
_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
          ".json": "application/json", ".csv": "text/csv; charset=utf-8",
          ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8"}

#: What a master is, when release.json does not say: the FITS files.
_MASTER_EXT = (".fits", ".fit", ".fts", ".fz", ".xisf")

#: What anybody who may not fetch a master is told, word for word.
LOCKED_MESSAGE = ("Masters are available to contributors. "
                  "Upload data to this collab to unlock downloads.")


def is_master(entry: dict[str, Any]) -> bool:
    name = str(entry.get("name") or "").lower()
    return entry.get("role") == "master" or name.endswith(_MASTER_EXT)


#: How long a release.json is trusted before it is read again. A release is
#: written once and then never changes; a new one is a new version folder.
_CACHE_SECONDS = 30.0


class Releases:
    """Read-only routes over a releases folder."""

    def __init__(self, app: FastAPI, store: Any, folder: Path,
                 who: Callable[[Request], dict[str, Any] | None] | None = None,
                 owns_project: Callable[[dict[str, Any], dict[str, Any]], bool] | None = None) -> None:
        self.app = app
        self.store = store
        self.folder = Path(folder)
        # Who is asking (the files page's: bearer token or browser cookie),
        # and whether they own a project. Without them nobody gets a master.
        self.who = who or (lambda request: None)
        self.owns_project = owns_project or (lambda who, project: False)
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._routes()

    # -- who may fetch the masters -------------------------------------------
    def contributor_projects(self, project: dict[str, Any]) -> list[str]:
        """The collaborations whose uploaders count as this one's contributors:
        itself, plus its `releaseContributorProjects` setting, which defaults
        to the collaboration it was `continuedFrom`."""
        payload = project.get("payload") or {}
        linked = payload.get("releaseContributorProjects")
        if linked is None:
            linked = [payload["continuedFrom"]] if payload.get("continuedFrom") else []
        ids = [project["id"]] + [str(x) for x in linked if _PROJECT.match(str(x))]
        return list(dict.fromkeys(ids))

    def access(self, request: Request, project_id: str) -> dict[str, Any]:
        """Whether whoever is asking may fetch this collaboration's masters."""
        try:
            who = self.who(request)
        except HTTPException:          # a telescope token with no owner
            who = None
        project = self.store.project(project_id)
        allowed, why = False, "anonymous"
        if who is not None and project is not None:
            if who.get("admin"):
                allowed, why = True, "server owner"
            elif self.owns_project(who, project):
                allowed, why = True, "collaboration owner"
            elif any(self.store.file_totals_of(pid, who["id"])["count"] > 0
                     for pid in self.contributor_projects(project)):
                allowed, why = True, "contributor"
            elif any(self._telescope_sessions(pid, who["id"]) > 0
                     for pid in self.contributor_projects(project)):
                # Frames reported by their own telescopes (N.I.N.A./Starfront)
                # count as much as a web upload.
                allowed, why = True, "contributor"
            else:
                why = "not a contributor"
        return {"allowed": allowed, "signedIn": who is not None, "why": why,
                "message": "" if allowed else LOCKED_MESSAGE,
                "signIn": "/files/signin"}

    def _telescope_sessions(self, project_id: str, user_id: str) -> int:
        totals = getattr(self.store, "contribution_totals_of", None)
        return int(totals(project_id, user_id)["sessions"]) if totals else 0

    # -- reading ---------------------------------------------------------
    def latest_version(self, project_id: str) -> str:
        if not _PROJECT.match(project_id or ""):
            return ""
        try:
            text = (self.folder / project_id / "LATEST").read_text("utf-8").strip()
        except OSError:
            return ""
        return text if _VERSION.match(text) else ""

    def versions(self, project_id: str) -> list[str]:
        if not _PROJECT.match(project_id or ""):
            return []
        try:
            names = [p.name for p in (self.folder / project_id).iterdir()
                     if p.is_dir() and _VERSION.match(p.name)
                     and (p / "release.json").is_file()]
        except OSError:
            return []
        return sorted(names, key=lambda v: int(v[1:]), reverse=True)

    def manifest(self, project_id: str, version: str) -> dict[str, Any] | None:
        """A version's release.json, or None. Cached briefly."""
        if not _PROJECT.match(project_id or "") or not _VERSION.match(version or ""):
            return None
        key = f"{project_id}/{version}"
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < _CACHE_SECONDS:
            return hit[1]
        path = self.folder / project_id / version / "release.json"
        try:
            found = json.loads(path.read_text("utf-8"))
            if not isinstance(found, dict):
                found = None
        except (OSError, ValueError):
            found = None
        self._cache[key] = (now, found)
        return found

    def summary(self, project_id: str) -> dict[str, Any] | None:
        """The short form, for the project summary every visitor gets."""
        version = self.latest_version(project_id)
        found = self.manifest(project_id, version) if version else None
        if not found:
            return None
        return {"version": version, "released": found.get("released") or "",
                "title": found.get("title") or "",
                "hours": found.get("totalHours"),
                "filters": [f.get("filter") for f in found.get("filters") or []],
                "page": f"/files#release-{project_id}",
                "api": f"/api/v1/releases/{project_id}"}

    def _public(self, project_id: str, version: str, found: dict[str, Any],
                access: dict[str, Any] | None = None) -> dict[str, Any]:
        """A release.json as served: every listed file that is really there,
        with the address to fetch it from."""
        base = self.folder / project_id / version
        files = []
        for entry in found.get("files") or []:
            name = str(entry.get("name") or "")
            if not _NAME.match(name) or name == "release.json":
                continue
            path = base / name
            if not path.is_file():
                continue
            locked = is_master(entry) and not (access or {}).get("allowed")
            files.append({**{k: v for k, v in entry.items() if k != "path"},
                          "name": name, "bytes": path.stat().st_size,
                          "url": f"/releases/{project_id}/{version}/{name}",
                          "locked": locked})
        project = self.store.project(project_id) or {}
        return {**found, "project": project_id,
                "projectName": project.get("name") or found.get("projectName") or "",
                "version": version, "latest": self.latest_version(project_id),
                "versions": self.versions(project_id), "files": files,
                "totalBytes": sum(f["bytes"] for f in files),
                "access": access or {"allowed": False, "message": LOCKED_MESSAGE}}

    def listing(self) -> list[dict[str, Any]]:
        """Every project that has a release, newest release first."""
        out = []
        try:
            folders = [p for p in self.folder.iterdir() if p.is_dir() and _PROJECT.match(p.name)]
        except OSError:
            folders = []
        for folder in folders:
            short = self.summary(folder.name)
            if short is None:
                continue
            project = self.store.project(folder.name)
            if project is None:
                continue
            out.append({"project": folder.name, "name": project.get("name") or "", **short})
        return sorted(out, key=lambda r: str(r.get("released") or ""), reverse=True)

    def stats(self) -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(self.folder if self.folder.exists() else self.folder.parent)
            return {"free": int(usage.free), "size": int(usage.total)}
        except OSError:
            return {"free": 0, "size": 0}

    # -- routes ------------------------------------------------------------
    def _routes(self) -> None:
        app = self.app

        @app.get("/api/v1/releases")
        def list_releases() -> dict[str, Any]:
            """Every collaboration with a published coadd. Public."""
            return {"releases": self.listing()}

        @app.get("/api/v1/releases/{project_id}")
        def latest_release(project_id: str, request: Request) -> JSONResponse:
            """A collaboration's latest published coadd. Public; which masters
            the asker may fetch is in `access` and each file's `locked`."""
            version = self.latest_version(project_id)
            found = self.manifest(project_id, version) if version else None
            if found is None:
                raise HTTPException(status_code=404, detail="nothing published for this collaboration yet")
            return JSONResponse(self._public(project_id, version, found,
                                             self.access(request, project_id)),
                                headers={"Cache-Control": "private, max-age=60",
                                         "Vary": "Cookie, Authorization"})

        @app.get("/api/v1/releases/{project_id}/{version}")
        def one_release(project_id: str, version: str, request: Request) -> JSONResponse:
            found = self.manifest(project_id, version)
            if found is None:
                raise HTTPException(status_code=404, detail="no such release")
            return JSONResponse(self._public(project_id, version, found,
                                             self.access(request, project_id)),
                                headers={"Cache-Control": "private, max-age=300",
                                         "Vary": "Cookie, Authorization"})

        @app.put("/api/v1/releases/{project_id}/access")
        async def set_access(project_id: str, request: Request) -> dict[str, Any]:
            """The collaboration's owner says which other collaborations'
            uploaders count as its contributors for the masters:
            {"contributorProjects": ["d07d82eb861e"]}, or null for the default
            (the one it was continued from)."""
            who = self.who(request)
            project = self.store.project(project_id)
            if project is None:
                raise HTTPException(status_code=404, detail="no such collaboration")
            if who is None or who.get("kind") == "agent" or not self.owns_project(who, project):
                raise HTTPException(status_code=403, detail="only the collaboration's owner sets this")
            try:
                body = await request.json()
            except ValueError:
                raise HTTPException(status_code=400, detail="send JSON") from None
            linked = body.get("contributorProjects") if isinstance(body, dict) else None
            payload = dict(project.get("payload") or {})
            if linked is None:
                payload.pop("releaseContributorProjects", None)
            else:
                if not isinstance(linked, list) or len(linked) > 20:
                    raise HTTPException(status_code=400, detail="contributorProjects must be a list of project ids")
                for pid in linked:
                    if not _PROJECT.match(str(pid)) or self.store.project(str(pid)) is None:
                        raise HTTPException(status_code=400, detail=f"no such collaboration: {pid}")
                payload["releaseContributorProjects"] = [str(p) for p in linked if str(p) != project_id]
            project = self.store.set_project(project_id, payload)
            return {"contributorProjects": self.contributor_projects(project)}

        @app.get("/releases/{project_id}/{version}/{name}")
        def release_file(project_id: str, version: str, name: str, request: Request):
            """One file of a release. Only names the release lists are served;
            the masters only to contributors."""
            found = self.manifest(project_id, version)
            if found is None or not _NAME.match(name or ""):
                raise HTTPException(status_code=404, detail="no such file")
            entries = {str(e.get("name") or ""): e for e in found.get("files") or []}
            if name not in entries:
                raise HTTPException(status_code=404, detail="no such file")
            path = self.folder / project_id / version / name
            try:
                real = path.resolve(strict=True)
                real.relative_to((self.folder / project_id / version).resolve(strict=True))
            except (OSError, ValueError):
                raise HTTPException(status_code=404, detail="no such file") from None
            if not real.is_file():
                raise HTTPException(status_code=404, detail="no such file")
            master = is_master(entries[name])
            if master and not self.access(request, project_id)["allowed"]:
                raise HTTPException(status_code=403, detail=LOCKED_MESSAGE)
            kind = _TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
            inline = kind.startswith("image/") or kind.startswith("text/")
            return FileResponse(real, media_type=kind,
                                filename=None if inline else name,
                                headers={"Cache-Control": ("private, max-age=86400" if master
                                                           else "public, max-age=86400, immutable")})

        @app.get("/releases/page.js")
        def page_js() -> Response:
            return Response(_PAGE_JS, media_type="application/javascript",
                            headers={"Cache-Control": "public, max-age=300"})


# The "Latest master" section of the files page, drawn for everybody -
# signed in or not - into <section id="release">. Plain DOM, no framework,
# the same look as the page around it.
_PAGE_JS = r"""
(function () {
  const box = document.getElementById('release');
  if (!box) return;
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const size = (n) => n >= 1e9 ? (n / 1e9).toFixed(2) + ' GB' : (n >= 1e6 ? (n / 1e6).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1e3)) + ' kB');
  const num = (x, d) => (x == null || x === '') ? '' : Number(x).toFixed(d);
  async function get(url) { const r = await fetch(url, { credentials: 'same-origin' }); if (!r.ok) throw new Error(r.status); return r.json(); }

  function wanted(list) {
    if (document.getElementById('collabSel')) {
      // On the files page the collab picker has already put the collab in the URL.
      const id = new URLSearchParams(location.search).get('project') || '';
      return list.some((r) => r.project === id) ? id : '';
    }
    const h = location.hash.slice(1).replace(/^release-/, '');
    const q = new URLSearchParams(location.search).get('project') || '';
    const stored = localStorage.getItem('starfront-files-project') || '';
    for (const id of [h, q, stored]) if (id && list.some((r) => r.project === id)) return id;
    return list.length ? list[0].project : '';
  }

  // Only the masters are offered for download; previews are shown on the
  // page (the hero image), never listed here. Everything comes from
  // release.json, so a new version draws itself.
  function draw(rel, list) {
    const access = rel.access || {};
    const lockMsg = access.message || 'Masters are available to contributors. Upload data to this collab to unlock downloads.';
    const masters = (rel.files || []).filter((x) => x.role === 'master');
    const locked = masters.some((x) => x.locked);
    const lockNote = locked ? `<p class="small lockline" id="releaseLocked">🔒 ${esc(lockMsg)}${access.signedIn ? '' : ` <a href="${esc(access.signIn || '/files/signin')}">Sign in</a> if you already have.`}</p>` : '';
    // The files page has its own collab picker; it drives this.
    const pick = (list.length > 1 && !document.getElementById('collabSel')) ? `<select id="releasePick">${list.map((r) => `<option value="${esc(r.project)}"${r.project === rel.project ? ' selected' : ''}>${esc(r.name)}</option>`).join('')}</select>` : '';
    const order = (rel.filters || []).map((f) => f.filter);
    for (const x of masters) if (x.filter && !order.includes(x.filter)) order.push(x.filter);
    const byF = Object.fromEntries((rel.filters || []).map((f) => [f.filter, f]));
    const cell = (x, what) => !x ? '<td class="muted">-</td>' : `<td class="dlc">${x.locked
      ? `<span class="btn dl locked" aria-disabled="true" title="${esc(lockMsg)}">🔒 Locked</span>`
      : `<a class="btn dl" href="${esc(x.url)}" download>Download</a>`} <span class="small muted">${what} · ${size(x.bytes)}</span></td>`;
    const rows = order.map((f) => {
      const mine = masters.filter((x) => x.filter === f);
      const fz = mine.find((x) => !x.full && /\.fz$/i.test(x.name)) || mine.find((x) => !x.full);
      const full = mine.find((x) => x.full);
      const info = byF[f] || {};
      return `<tr><td><b>${esc(f)}</b></td><td class="num">${num(info.hours, 1)} h</td>${cell(fz, '.fits.fz')}${cell(full, 'full .fits')}</tr>`;
    }).join('');
    const people = (rel.contributors || []).slice().sort((a, b) => (b.hours || 0) - (a.hours || 0));
    const total = people.reduce((t, c) => t + (Number(c.hours) || 0), 0);
    const peopleRows = people.map((c) => `<tr><td>${esc(c.name)}</td><td class="small muted">${esc((c.rigs || []).join(', '))}</td><td class="num">${num(c.hours, 1)} h</td></tr>`).join('');
    const last = (rel.changelog || [])[0];
    box.innerHTML = `
      <h2>Masters ${pick}</h2>
      <p class="small"><b>${esc(rel.version)}</b> released ${esc(rel.released)} · ${num(rel.totalHours, 1)} h · ${esc(people.length)} contributors</p>
      ${lockNote}
      <div class="scroll"><table class="masters"><thead><tr><th>Filter</th><th class="num">Hours</th><th>Compressed</th><th>Full</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="small muted">Compressed = tile-compressed FITS (Siril, PixInsight, astropy open it). Full = lossless float32 with weight, coverage and hours maps. Every filter is on the same pixel grid.</p>
      <h2>Contributors</h2>
      <div class="scroll"><table class="people"><thead><tr><th>Name</th><th>Rigs</th><th class="num">Hours</th></tr></thead><tbody>${peopleRows}
        <tr class="tot"><td><b>Total</b></td><td></td><td class="num"><b>${num(total, 1)} h</b></td></tr></tbody></table></div>
      ${last ? `<p class="small muted">${esc(last.version)} ${esc(last.date)}: ${esc(last.text)}</p>` : ''}`;
    box.hidden = false;
    // The files page folds this into a section and summarises it.
    document.dispatchEvent(new CustomEvent('starfront:release', { detail: rel }));
    const sel = document.getElementById('releasePick');
    if (sel) sel.addEventListener('change', () => show(sel.value, list));
  }

  async function show(id, list) {
    try { draw(await get(`/api/v1/releases/${encodeURIComponent(id)}`), list); }
    catch (e) { box.hidden = true; document.dispatchEvent(new CustomEvent('starfront:release', { detail: null })); }
  }

  let known = [];
  // The files page's switcher says which collab is in view.
  document.addEventListener('starfront:project', (e) => {
    const id = (e.detail || {}).id;
    if (known.some((r) => r.project === id)) show(id, known);
    else { box.hidden = true; document.dispatchEvent(new CustomEvent('starfront:release', { detail: null })); }
  });
  get('/api/v1/releases').then((r) => { const list = r.releases || []; known = list; const id = wanted(list); if (id) show(id, list); }).catch(() => {});
})();
"""
