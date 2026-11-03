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

Everything here is public on purpose. A finished coadd is the point of a
collaboration and the people who shot it should not need to sign in to see
or fetch it; the release.json carries contributor *names* as the stacker
chose to credit them, never Discord ids or telescope tokens.

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

from fastapi import FastAPI, HTTPException
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

#: How long a release.json is trusted before it is read again. A release is
#: written once and then never changes; a new one is a new version folder.
_CACHE_SECONDS = 30.0


class Releases:
    """Read-only routes over a releases folder."""

    def __init__(self, app: FastAPI, store: Any, folder: Path) -> None:
        self.app = app
        self.store = store
        self.folder = Path(folder)
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._routes()

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

    def _public(self, project_id: str, version: str, found: dict[str, Any]) -> dict[str, Any]:
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
            files.append({**{k: v for k, v in entry.items() if k != "path"},
                          "name": name, "bytes": path.stat().st_size,
                          "url": f"/releases/{project_id}/{version}/{name}"})
        project = self.store.project(project_id) or {}
        return {**found, "project": project_id,
                "projectName": project.get("name") or found.get("projectName") or "",
                "version": version, "latest": self.latest_version(project_id),
                "versions": self.versions(project_id), "files": files,
                "totalBytes": sum(f["bytes"] for f in files)}

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
        def latest_release(project_id: str) -> JSONResponse:
            """A collaboration's latest published coadd. Public."""
            version = self.latest_version(project_id)
            found = self.manifest(project_id, version) if version else None
            if found is None:
                raise HTTPException(status_code=404, detail="nothing published for this collaboration yet")
            return JSONResponse(self._public(project_id, version, found),
                                headers={"Cache-Control": "public, max-age=60"})

        @app.get("/api/v1/releases/{project_id}/{version}")
        def one_release(project_id: str, version: str) -> JSONResponse:
            found = self.manifest(project_id, version)
            if found is None:
                raise HTTPException(status_code=404, detail="no such release")
            return JSONResponse(self._public(project_id, version, found),
                                headers={"Cache-Control": "public, max-age=300"})

        @app.get("/releases/{project_id}/{version}/{name}")
        def release_file(project_id: str, version: str, name: str):
            """One file of a release. Only names the release lists are served."""
            found = self.manifest(project_id, version)
            if found is None or not _NAME.match(name or ""):
                raise HTTPException(status_code=404, detail="no such file")
            listed = {str(e.get("name") or "") for e in found.get("files") or []}
            if name not in listed:
                raise HTTPException(status_code=404, detail="no such file")
            path = self.folder / project_id / version / name
            try:
                real = path.resolve(strict=True)
                real.relative_to((self.folder / project_id / version).resolve(strict=True))
            except (OSError, ValueError):
                raise HTTPException(status_code=404, detail="no such file") from None
            if not real.is_file():
                raise HTTPException(status_code=404, detail="no such file")
            kind = _TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
            inline = kind.startswith("image/") or kind.startswith("text/")
            return FileResponse(real, media_type=kind,
                                filename=None if inline else name,
                                headers={"Cache-Control": "public, max-age=86400, immutable"})

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
    const h = location.hash.slice(1).replace(/^release-/, '');
    const q = new URLSearchParams(location.search).get('project') || '';
    const stored = localStorage.getItem('starfront-files-project') || '';
    for (const id of [h, q, stored]) if (id && list.some((r) => r.project === id)) return id;
    return list.length ? list[0].project : '';
  }

  function draw(rel, list) {
    const pick = list.length > 1 ? `<select id="releasePick">${list.map((r) => `<option value="${esc(r.project)}"${r.project === rel.project ? ' selected' : ''}>${esc(r.name)}</option>`).join('')}</select>` : '';
    const filters = (rel.filters || []).map((f) => `<tr><td>${esc(f.filter)}${f.note ? ` <span class="small muted">${esc(f.note)}</span>` : ''}</td><td class="num">${num(f.hours, 1)} h</td><td class="num">${esc(f.subs)}</td><td class="num">${esc(f.rigs)}</td>
      <td>${(rel.files || []).filter((x) => x.filter === f.filter && x.role === 'master').map((x) => `<a href="${esc(x.url)}" download>${x.full ? 'full lossless FITS' : 'compressed FITS'}</a> <span class="small muted">${size(x.bytes)}</span>`).join('<br>')}</td></tr>`).join('');
    const people = (rel.contributors || []).map((c) => `<tr><td>${esc(c.name)}</td><td>${esc((c.rigs || []).join(', '))}</td><td class="num">${num(c.hours, 1)} h</td>
      ${(rel.filters || []).map((f) => `<td class="num">${c.share && c.share[f.filter] != null ? num(c.share[f.filter], 0) + '%' : ''}</td>`).join('')}</tr>`).join('');
    const previews = (rel.files || []).filter((x) => x.role === 'preview');
    const hero = previews.find((x) => x.hero) || previews[0];
    const others = (rel.files || []).filter((x) => x.role !== 'master' && x !== hero);
    const log = (rel.changelog || []).map((c) => `<li><b>${esc(c.version)}</b> ${esc(c.date)}: ${esc(c.text)}</li>`).join('');
    box.innerHTML = `
      <h2>Latest master ${pick}</h2>
      <p><b>${esc(rel.projectName)}</b> · <b>${esc(rel.version)}</b> released ${esc(rel.released)} · ${num(rel.totalHours, 1)} h from ${esc((rel.contributors || []).length)} contributors · ${size(rel.totalBytes)} in all</p>
      ${hero ? `<p><a href="${esc(hero.url)}"><img src="${esc(hero.thumb || hero.url)}" alt="${esc(hero.title || 'preview')}" style="max-width:100%;border-radius:8px"></a><br><span class="small muted">${esc(hero.title || '')}</span></p>` : ''}
      ${rel.about ? `<p class="small muted">${esc(rel.about)}</p>` : ''}
      <table><thead><tr><th>Filter</th><th class="num">Hours</th><th class="num">Subs</th><th class="num">Rigs</th><th>Download</th></tr></thead><tbody>${filters}</tbody></table>
      <h2>Who shot it</h2>
      <p class="small muted">Share = each contributor's part of the stack's weight over M31 in that filter.</p>
      <table><thead><tr><th>Contributor</th><th>Rigs</th><th class="num">Hours</th>${(rel.filters || []).map((f) => `<th class="num">${esc(f.filter)}</th>`).join('')}</tr></thead><tbody>${people}</tbody></table>
      ${others.length ? `<h2>Previews and notes</h2><p>${others.map((x) => `<a href="${esc(x.url)}">${esc(x.title || x.name)}</a> <span class="small muted">${size(x.bytes)}</span>`).join('<br>')}</p>` : ''}
      ${rel.elsewhere && rel.elsewhere.length ? `<p class="small">${rel.elsewhere.map((e) => `<a href="${esc(e.url)}">${esc(e.title)}</a>`).join(' · ')}</p>` : ''}
      ${log ? `<h2>Changes</h2><ul class="small">${log}</ul>` : ''}
      ${(rel.versions || []).length > 1 ? `<p class="small muted">Earlier versions: ${(rel.versions || []).filter((v) => v !== rel.version).map((v) => `<a href="/api/v1/releases/${esc(rel.project)}/${esc(v)}">${esc(v)}</a>`).join(', ')}</p>` : ''}`;
    box.hidden = false;
    const sel = document.getElementById('releasePick');
    if (sel) sel.addEventListener('change', () => show(sel.value, list));
  }

  async function show(id, list) {
    try { draw(await get(`/api/v1/releases/${encodeURIComponent(id)}`), list); }
    catch (e) { box.hidden = true; }
  }

  get('/api/v1/releases').then((r) => { const list = r.releases || []; const id = wanted(list); if (id) show(id, list); }).catch(() => {});
})();
"""
