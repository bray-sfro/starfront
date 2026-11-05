// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// The public project page's mosaic section (MOSAIC_PLAN.md section 4.4): the panel grid over the coverage map, per-panel progress against the owner's
// targets, and a panel picker with the panel's own live and gold previews. Only for a project with a mosaic layout; a single field never reaches this file.
//
// The data: the layout (Project.mosaic), status.json's `panels` index -> panels.json (sparse: panels with data only, display names only), and optionally
// GET mosaic for who each panel is assigned to. The outlines are derived from the layout and drawn on ONE canvas over the coverage map (the map covers the
// project frame, which is the layout plus a small margin); clicking it finds the panel by arithmetic. The picker is a native select, so it works from the keyboard.

import { html, setHtml, safeUrl } from "../dom.js";
import { formatHours, formatInt, formatArcsec, streamLabel } from "../format.js";
import { formatRa, formatDec } from "../grid-math.js";
import { derive, derivedGrid, panelAtOffset, panelRectsOnGrid } from "../mosaic-math.js";
import { indexPanels, overallProgress, panelsWithData, previewStreams } from "../mosaic-model.js";
import { previewFigure, modeToggle, wireModeToggle } from "./preview.js";

/** What survives a repaint of the page (the 90-second refresh redraws the whole body). */
const memory = { id: null, stream: null, mode: "live" };

const css = (name, fallback) => (getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback);
const fmtH = (s) => (s >= 36000 ? `${(s / 3600).toFixed(0)} h` : `${(s / 3600).toFixed(1)} h`);

export function hasMosaic(project) { return !!project?.mosaic; }

/** The section's HTML. `d`: { project, view, doc (panels.json or null), mosaic (MosaicState or null) }. "" for a single field. */
export function panelsSectionHtml(d) {
  const { project, view, doc } = d;
  if (!hasMosaic(project)) return "";
  const layout = project.mosaic;
  const total = layout.rows * layout.cols;
  const withData = doc ? doc.totals.panels_with_data : view?.panels?.withData ?? 0;
  const hours = doc ? doc.totals.integration_s / 3600 : 0;
  const grid = project.grid ?? derivedGrid(layout);
  const cov = view?.coverage;
  return html`
    <section class="section--tight" aria-labelledby="pm-h" style="margin-top:36px" id="pm-section">
      <div class="section-header"><h2 id="pm-h">Mosaic panels</h2>
        <p id="pm-summary-line"><strong>${formatInt(withData)} of ${formatInt(total)} panels have data</strong>${hours ? `, ${formatHours(hours * 3600)} in all` : ""}. The project is a ${layout.rows} × ${layout.cols} grid with ${layout.overlap_pct}% overlap. Each accepted frame counts in the panel whose centre is nearest the middle of the frame.</p></div>
      <div class="mosaic-grid-2">
        <div>
          <div class="mosaic-canvas-wrap" id="pm-map" style="aspect-ratio:${grid.width_px} / ${grid.height_px};max-height:70vh;position:relative;padding:0;overflow:hidden">
            ${cov ? html`<img id="pm-img" src="${safeUrl(cov.url)}" alt="" width="400" height="300" decoding="async" style="position:absolute;inset:0;width:100%;height:100%;object-fit:fill;opacity:.85">` : ""}
            <canvas id="pm-canvas" class="mosaic-canvas mosaic-canvas--public" role="img" aria-label="Map of the ${total} panels over the coverage of the project frame. Choose a panel from the list, or click it here." style="position:absolute;inset:0;width:100%;height:100%"></canvas>
          </div>
          <p class="muted" style="font-size:13px;margin:8px 0 0">${cov ? "Brighter coverage means more frames. " : "The coverage map appears after the first accepted frames. "}Outlines are the panels; filled ones have data, the more filled the closer to their target.</p>
        </div>
        <div>
          <div class="form__row"><label for="pm-pick">Panel</label><select id="pm-pick" class="mosaic-pick"></select></div>
          <div id="pm-detail" style="margin-top:14px" aria-live="polite"></div>
        </div>
      </div>
      <div id="pm-table" style="margin-top:22px"></div>
    </section>`;
}

/** Wire the section after it is in the page. `d` as for panelsSectionHtml. */
export function wirePanelsSection(root, d) {
  const sec = root.querySelector("#pm-section");
  if (!sec) return;
  const { project, doc, mosaic } = d;
  const layout = project.mosaic;
  const grid = project.grid ?? derivedGrid(layout);
  const byId = indexPanels(doc);
  const targets = doc?.targets ?? mosaic?.targets ?? { default: {}, panels: {} };
  const derived = derive(layout);
  const info = new Map(derived.panels.map((p) => [p.panel_id, p]));
  const names = new Map((mosaic?.panels ?? []).map((p) => [p.panel_id, p]));
  const rects = panelRectsOnGrid(layout, grid);
  const canvas = sec.querySelector("#pm-canvas");
  const pick = sec.querySelector("#pm-pick");
  const isPrivate = project.visibility === "private";

  if (!memory.id || !info.has(memory.id)) memory.id = (doc?.panels[0]?.panel_id) ?? null;

  setHtml(pick, html`<option value="">Choose a panel</option>${derived.panels.map((p) => {
    const e = byId.get(p.panel_id);
    return html`<option value="${p.panel_id}"${p.panel_id === memory.id ? html` selected` : ""}>${p.panel_id}${e ? ` · ${fmtH(e.integration_s)}` : " · no data yet"}</option>`;
  })}`);

  // ---- The map ----
  function draw() {
    const w = canvas.clientWidth || 600, h = canvas.clientHeight || 400;
    const dpr = Math.min(2, globalThis.devicePixelRatio || 1);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
    const c = canvas.getContext("2d");
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, w, h);
    const primary = css("--primary", "#4ecdc4"), gold = css("--gold", "#ffd166");
    const small = (rects[0] ? (rects[0].x1 - rects[0].x0) * w : 40) < 34;
    c.font = `600 ${Math.max(9, Math.min(13, ((rects[0]?.x1 - rects[0]?.x0) * w) / 4 || 11))}px ${css("--font-prop", "sans-serif")}`;
    c.textAlign = "center"; c.textBaseline = "middle";
    for (const r of rects) {
      const x = r.x0 * w, y = r.y0 * h, pw = (r.x1 - r.x0) * w, ph = (r.y1 - r.y0) * h;
      const e = byId.get(r.panel_id);
      if (e) {
        const p = overallProgress(e, r.panel_id, targets);
        const f = p.fraction ?? 0.5;
        c.fillStyle = primary; c.globalAlpha = 0.12 + 0.5 * Math.min(1, f); c.fillRect(x, y, pw, ph); c.globalAlpha = 1;
      }
      c.strokeStyle = r.panel_id === memory.id ? gold : primary; c.lineWidth = r.panel_id === memory.id ? 3 : 1; c.globalAlpha = r.panel_id === memory.id ? 1 : 0.8;
      c.strokeRect(x + 0.5, y + 0.5, pw - 1, ph - 1); c.globalAlpha = 1;
      if (!small) { c.fillStyle = "#fff"; c.strokeStyle = "rgba(0,0,0,.65)"; c.lineWidth = 3; c.strokeText(r.panel_id, x + pw / 2, y + ph / 2); c.fillText(r.panel_id, x + pw / 2, y + ph / 2); }
    }
  }
  const hit = (ev) => {
    const b = canvas.getBoundingClientRect();
    const fx = (ev.clientX - b.left) / b.width, fy = (ev.clientY - b.top) / b.height;
    const wDeg = (grid.width_px * grid.scale_arcsec_px) / 3600, hDeg = (grid.height_px * grid.scale_arcsec_px) / 3600;
    return panelAtOffset(layout, (fx - 0.5) * wDeg, (0.5 - fy) * hDeg)?.panel_id ?? null;
  };
  canvas.addEventListener("click", (ev) => { const id = hit(ev); if (id) choose(id); });
  canvas.style.cursor = "pointer";
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => draw()).observe(canvas);
  pick.addEventListener("change", () => choose(pick.value || null));

  function choose(id) {
    memory.id = id; memory.stream = null;
    pick.value = id ?? "";
    draw(); paintDetail();
  }

  // ---- The picked panel ----
  function paintDetail() {
    const box = sec.querySelector("#pm-detail");
    const id = memory.id;
    if (!id) { setHtml(box, html`<p class="muted">Choose a panel from the list, or click one on the map, to see its progress${isPrivate ? "" : " and its own images"}.</p>`); return; }
    const p = info.get(id), e = byId.get(id), n = names.get(id);
    const who = n ? [n.assigned_to ? `assigned to ${n.assigned_to}` : null, n.claimed_by.length ? `claimed by ${n.claimed_by.join(", ")}` : null].filter(Boolean).join("; ") : "";
    const head = html`<h3 style="margin-top:0">Panel ${id}</h3>
      <p class="muted" style="margin:0 0 10px">Centre ${formatRa(p.ra_deg)} ${formatDec(p.dec_deg)}, camera angle ${p.pa_deg.toFixed(1)}°.${who ? ` ${who[0].toUpperCase()}${who.slice(1)}.` : ""}</p>`;
    if (!e) { setHtml(box, html`${head}<div class="empty"><p>No accepted frame has been attributed to this panel yet.</p></div>`); return; }
    const keys = Object.keys(e.streams).sort();
    const rows = keys.map((k) => {
      const st = e.streams[k];
      const t = st.target_s;
      const f = t ? Math.min(1, st.integration_s / t) : null;
      return { key: k, st, f };
    });
    if (!memory.stream || !e.streams[memory.stream]) memory.stream = previewStreams(e, memory.mode)[0] ?? previewStreams(e, memory.mode === "live" ? "gold" : "live")[0] ?? keys[0];
    const streamKeys = keys.filter((k) => e.streams[k].previews);
    const prev = e.streams[memory.stream]?.previews?.[memory.mode] ?? null;
    setHtml(box, html`${head}
      <div class="table-scroll"><table class="data-table">
        <caption class="visually-hidden">Progress of panel ${id} by filter</caption>
        <thead><tr><th scope="col">Filter</th><th scope="col" class="num">Hours</th><th scope="col">Against target</th><th scope="col" class="num">Frames</th><th scope="col" class="num">Median FWHM</th><th scope="col" class="num">Coverage</th></tr></thead>
        <tbody>${rows.map((r) => html`<tr>
          <th scope="row">${streamLabel(r.key)}</th>
          <td class="num">${formatHours(r.st.integration_s)}</td>
          <td style="min-width:120px">${r.f === null ? html`<span class="muted">no target</span>` : html`<span class="meter" role="img" aria-label="${(r.st.integration_s / 3600).toFixed(1)} of ${(r.st.target_s / 3600).toFixed(1)} hours, ${Math.round(r.f * 100)} percent"><span style="width:${Math.round(r.f * 100)}%"></span></span><span class="muted" style="font-size:12px">${Math.round(r.f * 100)}% of ${formatHours(r.st.target_s)}</span>`}</td>
          <td class="num">${formatInt(r.st.frames_accepted)}</td>
          <td class="num">${formatArcsec(r.st.median_fwhm_arcsec)}</td>
          <td class="num">${r.st.coverage === null ? "—" : `${Math.round(r.st.coverage * 100)}%`}</td></tr>`)}</tbody></table></div>
      <p class="muted" style="margin:10px 0 0">Contributors: ${e.contributors.length ? e.contributors.join(", ") : "none"}.</p>
      ${isPrivate ? html`<p class="muted" style="margin-top:14px">Per-panel images are not published for private projects.</p>`
        : streamKeys.length ? html`<div class="toolbar" style="margin-top:16px;margin-bottom:8px">
            <div class="form__row" style="width:auto"><label for="pm-stream">Filter</label><select id="pm-stream" class="mosaic-pick">${streamKeys.map((k) => html`<option value="${k}"${k === memory.stream ? html` selected` : ""}>${streamLabel(k)}</option>`)}</select></div>
            ${modeToggle({ id: "pm-mode", current: memory.mode, legend: "Panel image: live or gold" })}</div>
          ${previewFigure({ set: prev ? { full: prev.full, thumb: prev.thumb, updatedAt: prev.updated_at } : null, mode: memory.mode, label: memory.mode === "gold" ? "Gold" : "Live", alt: `Panel ${id}, ${streamLabel(memory.stream)}, ${memory.mode} stack`, width: 480, height: 360, eager: true,
            caption: prev ? html`Panel ${id} alone, stretched on its own. Updated ${new Date(prev.updated_at).toISOString().slice(0, 16).replace("T", " ")} UTC.` : "" })}`
        : html`<p class="muted" style="margin-top:14px">No image of this panel has been rendered yet. They are made a few panels at a time after each batch.</p>`}`);
    box.querySelector("#pm-stream")?.addEventListener("change", (ev) => { memory.stream = ev.target.value; paintDetail(); box.querySelector("#pm-stream")?.focus(); });
    wireModeToggle(box, (m) => { memory.mode = m === "gold" ? "gold" : "live"; paintDetail(); box.querySelector(`#pm-mode-${memory.mode}`)?.focus(); });
  }

  // ---- The table that stands in for the picture ----
  const rowsData = panelsWithData(doc);
  setHtml(sec.querySelector("#pm-table"), rowsData.length ? html`
    <h3>Panels with data</h3>
    <div class="table-scroll"><table class="data-table">
      <caption class="visually-hidden">Panels that have data, most integration first</caption>
      <thead><tr><th scope="col">Panel</th><th scope="col" class="num">Hours</th><th scope="col" class="num">Frames</th><th scope="col">Against target</th><th scope="col">Contributors</th></tr></thead>
      <tbody>${rowsData.map((e) => { const pr = overallProgress(e, e.panel_id, targets); return html`<tr>
        <th scope="row"><button type="button" class="linklike" data-pick="${e.panel_id}" style="text-decoration:underline;color:var(--link)">${e.panel_id}</button></th>
        <td class="num">${formatHours(e.integration_s)}</td><td class="num">${formatInt(e.frames_accepted)}</td>
        <td>${pr.fraction === null ? html`<span class="muted">no target</span>` : `${Math.round(pr.fraction * 100)}%`}</td>
        <td>${e.contributors.join(", ")}</td></tr>`; })}</tbody></table></div>` : html`<p class="muted">No panel has data yet, so there is nothing to compare with the targets.</p>`);
  sec.querySelector("#pm-table").addEventListener("click", (ev) => { const b = ev.target.closest("button[data-pick]"); if (b) { choose(b.dataset.pick); pick.focus(); } });

  draw();
  paintDetail();
}
