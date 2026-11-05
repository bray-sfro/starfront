// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Starfront (EKE Services LLC).

// /p/{slug}: the public project page (plan 9). Everything a logged-out visitor sees comes from two public reads:
// GET /api/projects/{slug} (who and what the project is) and /public/{project_id}/status.json (the numbers and
// images). Signed-in members additionally get their membership, and a private project is read through the API.
//
// Privacy: only display names, an optional country and totals are drawn. The status document has nothing else.

import { api, describeError } from "../api.js";
import { config } from "../config.js";
import { html, setHtml, raw, safeUrl } from "../dom.js";
import { whoami } from "../session.js";
import { parseProjectPath, filterUrl, projectUrl, combinedUrl } from "../route.js";
import {
  loadStatus, StatusError, previewFor, modeLabel, normaliseMode, sortStreams, leaderboard, nightsSeries,
  nightsFor, nightStreamKeys, timelineToFeed, integrationByContributor, integrationByRig, hasRigs, rigsByStream,
  LICENCE_LABEL, combinedPalettes, resolvePalette,
} from "../status.js";
import { normaliseJoinCode, formatDuration, formatHours, formatInt, formatArcsec, formatDate, formatDateTime, relativeTime, countryFlag, countryName, filterChipClass, streamLabel, streamShortLabel, anyUnknownBandwidth } from "../format.js";
import { barChart, stackedBarChart, timelineChart, chartDataTable, legend, seriesColor } from "../svg-charts.js";
import { previewFigure, modeToggle, modeExplanation, wireModeToggle, modeFromUrl, writeModeToUrl, paletteToggle, wirePaletteToggle, paletteFromUrl, writePaletteToUrl } from "../components/preview.js";
import { statusBadge, VISIBILITY_TEXT } from "../components/project-card.js";
import { deletedSentence } from "../deletion-ui.js";
import { hasMosaic, panelsSectionHtml, wirePanelsSection } from "../components/project-panels.js";
import { deepZoomSectionHtml, wireDeepZoom, deepZoomIsFullscreen } from "../components/deep-zoom.js";
import { fetchPanelsDoc } from "../panels-doc.js";

const $ = (id) => document.getElementById(id);
const route = parseProjectPath(location.pathname);

const state = { project: null, view: null, user: null, mode: "live", palette: null, snapKey: null, snapIdx: null, timer: null, panelsDoc: null, mosaic: null };

const time = (iso) => (iso ? html`<time datetime="${iso}" title="${formatDateTime(iso)}">${relativeTime(iso)}</time>` : "never");

// ---- Pieces -----------------------------------------------------------------------------------------------------------

function headerHtml(p, view) {
  const lic = LICENCE_LABEL[p.licence] || p.licence;
  return html`
    <p class="eyebrow">${p.target_name || "Project"}</p>
    <h1>${p.name}</h1>
    <div class="row" style="margin-bottom:10px">
      ${statusBadge(p.status)}
      <span class="badge">${VISIBILITY_TEXT[p.visibility] || p.visibility}</span>
      <span class="badge" title="Licence of the gold masters">Masters: ${lic}</span>
      ${view && view.combined.palette !== "none" ? html`<span class="badge">${view.combined.palette} palette</span>` : ""}
      <span class="muted">Started by ${p.owner_name}</span>
    </div>
    ${p.status === "deleted" ? html`<div class="callout callout--error" data-deleted><p>${deletedSentence(p)}</p></div>` : ""}
    ${p.description ? html`<p class="muted" style="max-width:70ch">${p.description}</p>` : ""}`;
}

function statsHtml(view, p) {
  const t = view ? view.totals : null;
  const c = p.counts || {};
  const hours = t ? t.integrationS : c.integration_s ?? 0;
  const frames = t ? t.framesAccepted : c.frames_accepted ?? 0;
  const last = t ? view.updatedAt : c.last_update;
  return html`<div class="stats" role="list">
    <div class="stat-tile" role="listitem"><div class="stat-tile__label">Integration</div><div class="stat-tile__value">${formatHours(hours)}</div><div class="stat-tile__sub">${formatDuration(hours)}</div></div>
    <div class="stat-tile" role="listitem"><div class="stat-tile__label">Frames accepted</div><div class="stat-tile__value">${formatInt(frames)}</div><div class="stat-tile__sub">${t ? `${formatInt(t.framesRejected)} rejected` : ""}</div></div>
    <div class="stat-tile" role="listitem"><div class="stat-tile__label">Contributors</div><div class="stat-tile__value">${formatInt(t ? t.contributors : 0)}</div><div class="stat-tile__sub">${formatInt(t ? t.memberCount : c.member_count ?? 0)} members</div></div>
    <div class="stat-tile" role="listitem"><div class="stat-tile__label">Last update</div><div class="stat-tile__value" style="font-size:22px">${time(last)}</div><div class="stat-tile__sub">${t && t.goldSeqMax ? `Gold stack #${t.goldSeqMax}` : "No gold stack yet"}</div></div>
  </div>`;
}

function previewSection(view, mode) {
  const p = view.project;
  const palettes = combinedPalettes(view);
  const choice = resolvePalette(state.palette, palettes);
  const shown = choice === "both" ? palettes : palettes.filter((x) => x.palette === choice);
  const m = normaliseMode(mode);
  const one = (pal) => {
    const set = previewFor(pal, mode);
    const href = `${combinedUrl(p.slug)}?mode=${m}&palette=${pal.palette}`;
    return previewFigure({
      set, mode, label: modeLabel(mode, { goldSeq: view.totals.goldSeqMax }),
      alt: `${pal.palette} combined preview of ${p.name}, ${m} stack`, eager: true, width: 640, height: 480,
      href, linkTitle: `Open the ${pal.palette} combined preview`,
      caption: set ? html`${pal.palette} palette, linear per-channel stretch, no post-processing. Updated ${time(set.updatedAt)}. <a href="${href}" style="color:inherit;text-decoration:underline">Open the combined preview</a>` : "",
    });
  };
  const figure = !palettes.length
    ? html`<div class="empty"><p>This project has no colour palette, so there is no combined preview. Each filter has its own stack below.</p></div>`
    : shown.length > 1
      ? html`<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr))">${shown.map(one)}</div>`
      : one(shown[0]);
  // A second palette (contract 1.30.0) gets a control; "Both" widens the cell so the two pictures sit side by side.
  const toggle = palettes.length > 1
    ? paletteToggle({ id: "palette", palettes: palettes.map((x) => x.palette), current: choice, legend: "Combined preview: which palette" })
    : "";
  const cov = view.coverage;
  const covFig = cov
    ? html`<figure class="preview"><img src="${safeUrl(cov.url)}" alt="Coverage map: frames per pixel over the project frame, up to ${cov.maxN} frames" width="400" height="300" loading="lazy" decoding="async"><figcaption>Brighter means more frames. Peak ${formatInt(cov.maxN)} frames per pixel. Updated ${time(cov.updatedAt)}.</figcaption></figure>`
    : html`<div class="empty"><p>The coverage map appears after the first accepted frames.</p></div>`;
  return html`
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr))">
      <div${choice === "both" ? html` style="grid-column:1/-1"` : ""}><div class="toolbar" style="margin-bottom:10px"><h3 style="margin:0">Combined preview</h3>${toggle}</div>${figure}</div>
      <div><h3>Coverage</h3>${covFig}
        ${view.project.grid ? html`<p class="muted" style="margin-top:10px;font-size:14px">Project frame ${formatInt(view.project.grid.widthPx)} × ${formatInt(view.project.grid.heightPx)} px at ${view.project.grid.scaleArcsecPx}″/px, centred on RA ${view.project.grid.centerRaDeg.toFixed(3)}°, Dec ${view.project.grid.centerDecDeg.toFixed(3)}°.</p>` : ""}
      </div>
    </div>`;
}

function streamCards(view, mode) {
  const slug = view.project.slug;
  const streams = sortStreams(view.streams);
  if (!streams.length) return html`<div class="empty"><p>No stacks yet. They appear after the first accepted frames.</p></div>`;
  return html`<div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(min(100%,250px),1fr))">${streams.map((s) => {
    const set = previewFor(s, mode);
    return html`<a class="card stream-card" href="${filterUrl(slug, s.key)}?mode=${normaliseMode(mode)}" style="color:var(--text)">
      ${previewFigure({ set, size: "thumb", mode, label: modeLabel(mode, s), alt: `${s.label} ${normaliseMode(mode)} stack preview`, width: 160, height: 120 })}
      <div class="stream-card__body">
        <div class="stream-card__head"><h3><span class="chip chip--${filterChipClass(s.key)}" title="${s.label}" aria-label="${s.label}">${s.shortLabel}</span></h3><span class="muted" style="font-size:13px">${s.contributors} contributor${s.contributors === 1 ? "" : "s"}</span></div>
        <dl>
          <div><dt>Frames</dt><dd>${formatInt(s.framesAccepted)}</dd></div>
          <div><dt>Integration</dt><dd>${formatHours(s.integrationS)}</dd></div>
          <div><dt>Median FWHM</dt><dd>${formatArcsec(s.medianFwhm)}</dd></div>
          <div><dt>Gold</dt><dd>${s.goldSeq ? `#${s.goldSeq}` : "none yet"}</dd></div>
          <div style="grid-column:1/-1"><dt>Last update</dt><dd>${time(s.lastUpdate)}</dd></div>
        </dl>
      </div>
    </a>`;
  })}</div>`;
}

const chip = (k) => html`<span class="chip chip--${filterChipClass(k)}" style="margin:0 4px 4px 0" title="${streamLabel(k)}" aria-label="${streamLabel(k)}">${streamShortLabel(k)}</span>`;
const reasonText = (list) => list.map((r) => `${r.reason.replace(/_/g, " ")}: ${r.count}`).join(", ");
const formatShare = (x) => (Number.isFinite(x) ? `${(x * 100).toFixed(1)} %` : "—");

function rejectedCell(n, reasons) {
  if (!n) return html`<td class="num">0</td>`;
  return html`<td class="num" title="${reasonText(reasons)}">${formatInt(n)}<span class="visually-hidden"> (${reasonText(reasons)})</span></td>`;
}

function leaderboardHtml(view) {
  const rows = leaderboard(view);
  if (!rows.length) return html`<div class="empty"><p>No contributions yet. The leaderboard ranks contributors by integration time.</p></div>`;
  const byRig = hasRigs(view);
  const top = Math.max(...rows.map((c) => c.integrationS), 0);
  const chart = byRig ? integrationByRig(view) : integrationByContributor(view);
  const hasChart = chart.rows.some((r) => r.segments.length);
  const who = byRig ? "Contributor and rig" : "Contributor";
  const table = chart.rows.flatMap((r) => r.segments.map((s) => [r.fullLabel ?? r.label, chart.series.find((x) => x.key === s.key)?.label ?? s.key, `${s.value.toFixed(1)} h`]));
  const rigRow = (r) => html`<tr class="leader__rig">
        <td class="rank"></td>
        <td><span class="leader__rig-label">${r.label}</span></td>
        <td class="num">${formatHours(r.integrationS)}</td>
        <td style="min-width:90px"><span class="meter meter--sub" aria-hidden="true"><span style="width:${Math.max(2, Math.round((top > 0 ? r.integrationS / top : 0) * 100))}%"></span></span></td>
        <td class="num">${formatInt(r.framesAccepted)}</td>
        ${rejectedCell(r.framesRejected, r.rejectedByReason)}
        <td class="num">${formatArcsec(r.medianFwhm)}</td>
        <td>${r.streams.filter((s) => s.framesAccepted > 0).map((s) => chip(s.key))}</td>
      </tr>`;
  return html`
    <div class="table-scroll"><table class="leader">
      <caption class="visually-hidden">Contributors ranked by integration time${byRig ? ", each followed by their rigs" : ""}</caption>
      <thead><tr><th scope="col" class="rank">#</th><th scope="col">${byRig ? "Contributor / rig" : "Contributor"}</th><th scope="col" class="num">Integration</th><th scope="col"><span class="visually-hidden">Share of the leader</span></th><th scope="col" class="num">Frames</th>${byRig ? html`<th scope="col" class="num">Rejected</th><th scope="col" class="num">Median FWHM</th>` : ""}<th scope="col">Filters</th></tr></thead>
      <tbody>${rows.map((c) => html`<tr class="leader__person">
        <td class="rank">${c.rank}</td>
        <td>${c.name}${c.country ? html` <span title="${countryName(c.country)}"><span aria-hidden="true">${countryFlag(c.country)}</span><span class="visually-hidden">${countryName(c.country)}</span></span>` : ""}</td>
        <td class="num">${formatHours(c.integrationS)}</td>
        <td style="min-width:90px"><span class="meter" aria-hidden="true"><span style="width:${Math.max(2, Math.round(c.share * 100))}%"></span></span></td>
        <td class="num">${formatInt(c.framesAccepted)}</td>
        ${byRig ? html`${c.rigs.length ? rejectedCell(c.rigs.reduce((a, r) => a + r.framesRejected, 0), mergeReasons(c.rigs)) : html`<td class="num">—</td>`}<td class="num">${c.rigs.length === 1 ? formatArcsec(c.rigs[0].medianFwhm) : ""}</td>` : ""}
        <td>${c.streams.map(chip)}</td>
      </tr>${byRig && c.rigs.length ? c.rigs.map(rigRow) : ""}`)}</tbody></table></div>
    ${byRig ? rigsComparedHtml(view) : ""}
    ${hasChart ? html`<h3 style="margin-top:28px">Hours per filter, by ${who.toLowerCase()}</h3>
      ${raw(stackedBarChart({
        rows: chart.rows, series: chart.series.map((s, i) => ({ ...s, color: seriesColor(i) })), format: (v) => `${v.toFixed(1)} h`,
        ariaLabel: `Integration hours per filter, split by ${who.toLowerCase()}`,
        desc: chart.rows.map((r) => `${r.label}: ${r.segments.reduce((a, s) => a + s.value, 0).toFixed(1)} hours`).join("; "),
      }))}
      ${raw(legend(chart.series.map((s, i) => ({ ...s, color: seriesColor(i) }))))}
      ${anyUnknownBandwidth(chart.rows.map((r) => r.key)) ? html`<p class="muted" style="font-size:13px">A “?” after a filter means its bandwidth is not known, so those frames are stacked apart from the same filter of a known bandwidth. The project owner can set it in the console’s Filters tab.</p>` : ""}
      ${raw(chartDataTable({ caption: `Integration hours per filter and ${who.toLowerCase()}`, columns: ["Filter", who, "Hours"], rows: table }))}` : ""}`;
}

/** One contributor's reject reasons summed over their rigs. */
function mergeReasons(rigs) {
  const m = new Map();
  for (const r of rigs) for (const x of r.rejectedByReason) m.set(x.reason, (m.get(x.reason) || 0) + x.count);
  return [...m.entries()].map(([reason, count]) => ({ reason, count })).sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason));
}

/** Every rig side by side, per filter: what each brought, how sharp it was, and how much of the stack it carries. */
function rigsComparedHtml(view) {
  const groups = rigsByStream(view);
  if (!groups.length) return "";
  return html`
    <h3 style="margin-top:28px" id="rigs-h">Rigs compared, by filter</h3>
    <p class="muted" style="max-width:72ch;font-size:14px">A rig is named by its camera and telescope. “Share of the stack” is the rig’s part of the summed frame weights in that filter’s live stack: sharper, deeper frames weigh more, so it can differ from the share of the hours.</p>
    <div class="table-scroll"><table class="leader leader--rigs" aria-labelledby="rigs-h">
      <thead><tr><th scope="col">Contributor</th><th scope="col">Rig</th><th scope="col" class="num">Frames</th><th scope="col" class="num">Rejected</th><th scope="col" class="num">Hours</th><th scope="col" class="num">Median FWHM</th><th scope="col" class="num">Share of the stack</th><th scope="col"><span class="visually-hidden">Share of the stack, as a bar</span></th></tr></thead>
      ${groups.map((g) => html`<tbody>
        <tr class="leader__group"><th scope="rowgroup" colspan="8">${chip(g.key)}</th></tr>
        ${g.rigs.map((r) => html`<tr>
          <td>${r.contributor}</td>
          <td>${r.rig}</td>
          <td class="num">${formatInt(r.framesAccepted)}</td>
          <td class="num">${formatInt(r.framesRejected)}</td>
          <td class="num">${formatHours(r.integrationS)}</td>
          <td class="num">${formatArcsec(r.medianFwhm)}</td>
          <td class="num">${formatShare(r.weightShare)}</td>
          <td style="min-width:90px"><span class="meter" aria-hidden="true"><span style="width:${Number.isFinite(r.weightShare) ? Math.max(2, Math.round(r.weightShare * 100)) : 0}%"></span></span></td>
        </tr>`)}
      </tbody>`)}
    </table></div>`;
}

function rejectionsHtml(view) {
  if (!view.rejections.length) return html`<p class="muted">No frames have been rejected.</p>`;
  const items = view.rejections.map((r) => ({ label: r.reason.replace(/_/g, " "), value: r.count, color: "var(--f-sii)" }));
  return html`
    <p class="muted" style="max-width:68ch">${formatInt(view.totals.framesRejected)} frames were rejected out of ${formatInt(view.totals.framesAccepted + view.totals.framesRejected)} judged. Each reason, and what to do about it, is explained under <a href="/docs/rejections">what gets rejected, and why</a>.</p>
    ${raw(barChart({ items, ariaLabel: "Rejected frames by reason", desc: items.map((i) => `${i.label}: ${i.value}`).join("; "), format: formatInt, labelWidth: 170 }))}
    ${raw(chartDataTable({ caption: "Rejected frames by reason", columns: ["Reason", "Frames"], rows: items.map((i) => [i.label, String(i.value)]) }))}`;
}

function progressHtml(view) {
  const keys = nightStreamKeys(view);
  if (!keys.length) {
    return html`<div class="empty"><p>The progress time-lapse appears after the first gold integration. It then shows the stack as it stood after each capture night, however long ago the frames were taken.</p></div>`;
  }
  return html`
    <div class="scrub">
      <div class="row row--between">
        <div class="form__row" style="width:auto"><label for="scrub-stream">Filter</label>
          <select id="scrub-stream" style="background:var(--surface-1);color:var(--text);border:1px solid var(--border-input);border-radius:10px;padding:8px 12px;font:inherit">${keys.map((k) => html`<option value="${k}"${k === state.snapKey ? html` selected` : ""}>${streamLabel(k)}</option>`)}</select></div>
        <div class="row"><button type="button" class="btn btn-ghost btn--sm" id="scrub-play" aria-pressed="false">Play</button><span class="mode mode--live">Stack after that night</span></div>
      </div>
      <figure class="preview" id="scrub-fig"><img id="scrub-img" alt="" width="480" height="360" decoding="async"></figure>
      <label for="scrub-range" class="visually-hidden">Capture night</label>
      <input type="range" id="scrub-range" min="0" max="0" value="0" step="1" aria-describedby="scrub-meta">
      <div class="scrub__meta" id="scrub-meta" aria-live="polite"></div>
      <div id="scrub-chart"></div>
    </div>`;
}

function renderBodyHtml(view) {
  const mode = state.mode;
  return html`
    <section aria-labelledby="stacks-h" class="stack" style="--gap:22px">
      <div class="toolbar">
        <h2 id="stacks-h" style="margin:0">Stacks</h2>
        <div class="row">${modeToggle({ id: "mode", current: mode, legend: "Stack shown: live or gold" })}</div>
      </div>
      <p class="muted" id="mode-explain" role="status" style="max-width:80ch;margin:0">${modeExplanation(mode)}</p>
      ${previewSection(view, mode)}
      <h3>By filter</h3>
      ${streamCards(view, mode)}
    </section>

    ${deepZoomSectionHtml(view)}

    ${panelsSectionHtml({ project: state.project, view, doc: state.panelsDoc, mosaic: state.mosaic })}

    <section class="section--tight" aria-labelledby="who-h" style="margin-top:36px">
      <div class="section-header"><h2 id="who-h">Contributors</h2><p>Ranked by integration time. Names are the display names members chose; no emails, no locations.</p></div>
      ${leaderboardHtml(view)}
    </section>

    <section class="section--tight" aria-labelledby="progress-h" style="margin-top:36px">
      <div class="section-header"><h2 id="progress-h">Progress</h2><p>The stack after each capture night, as of the last gold integration. Drag the slider, or focus it and use the arrow keys.</p></div>
      ${progressHtml(view)}
    </section>

    <section class="section--tight" aria-labelledby="rej-h" style="margin-top:36px">
      <div class="section-header"><h2 id="rej-h">Quality gate</h2><p>Frames are checked before they join a stack.</p></div>
      ${rejectionsHtml(view)}
    </section>`;
}

// ---- Scrubber ---------------------------------------------------------------------------------------------------------------

let playTimer = null;
function stopPlaying() {
  clearInterval(playTimer); playTimer = null;
  const b = $("scrub-play");
  if (b) { b.textContent = "Play"; b.setAttribute("aria-pressed", "false"); }
}

function wireScrubber(view) {
  const range = $("scrub-range");
  if (!range) return;
  const sel = $("scrub-stream");
  const { points } = nightsSeries(view);

  const snaps = () => nightsFor(view, state.snapKey);
  function show() {
    const list = snaps();
    if (!list.length) return;
    state.snapIdx = Math.min(Math.max(state.snapIdx ?? list.length - 1, 0), list.length - 1);
    const e = list[state.snapIdx];
    const label = streamLabel(state.snapKey);
    const img = $("scrub-img");
    img.src = safeUrl(e.image.full);
    img.alt = `${label} stack after the night of ${e.night}`;
    range.max = String(list.length - 1);
    range.value = String(state.snapIdx);
    range.setAttribute("aria-valuetext", `Night of ${e.night}, ${formatHours(e.image.integrationS)} of ${label}`);
    setHtml($("scrub-meta"), html`<span><strong>Night of ${e.night}</strong></span><span>${formatHours(e.image.integrationS)} of ${label} (${formatInt(e.image.framesAccepted)} frames)</span><span>${formatHours(e.integrationS)} in all filters</span><span>${formatInt(e.contributors)} ${e.contributors === 1 ? "contributor" : "contributors"}, ${formatInt(e.sessions)} ${e.sessions === 1 ? "session" : "sessions"}</span><span class="muted">${state.snapIdx + 1} of ${list.length}</span>`);
    const at = view.nights.findIndex((n) => n.night === e.night);
    setHtml($("scrub-chart"), html`${raw(timelineChart({
      points, selected: at >= 0 ? at : null, yFormat: (v) => `${Math.round(v)} h`,
      ariaLabel: "Cumulative integration by capture night", desc: `Grows to ${formatHours(view.nights[view.nights.length - 1].integrationS)} over ${view.nights.length} capture nights.`,
    }))}
      ${raw(chartDataTable({ caption: "Cumulative integration by capture night", columns: ["Capture night", "Sessions", "Hours"], rows: view.nights.map((n) => [n.night, String(n.sessions), (n.integrationS / 3600).toFixed(1)]) }))}
      <p class="muted" style="font-size:13px">Capture nights are the nights the frames were taken, not the days they were processed.</p>`);
  }
  range.addEventListener("input", () => { state.snapIdx = parseInt(range.value, 10); stopPlaying(); show(); });
  sel.addEventListener("change", () => { state.snapKey = sel.value; state.snapIdx = null; stopPlaying(); show(); });
  $("scrub-play").addEventListener("click", () => {
    if (playTimer) { stopPlaying(); return; }
    const list = snaps();
    if (state.snapIdx >= list.length - 1) state.snapIdx = 0;
    const b = $("scrub-play");
    b.textContent = "Pause"; b.setAttribute("aria-pressed", "true");
    playTimer = setInterval(() => {
      const l = snaps();
      if (state.snapIdx >= l.length - 1) { stopPlaying(); return; }
      state.snapIdx++; show();
    }, 800);
  });
  show();
}

// ---- Activity feed ----------------------------------------------------------------------------------------------------------

async function loadFeed() {
  const ul = $("feed");
  const more = $("feed-more");
  let cursor = null;
  const draw = (events, append) => {
    const rows = events.map((e) => html`<li><time datetime="${e.ts}" title="${formatDateTime(e.ts)}">${relativeTime(e.ts)}</time><span>${e.text}</span></li>`);
    const frag = rows.map((r) => r.value).join("");
    if (append) ul.insertAdjacentHTML("beforeend", frag); else ul.innerHTML = frag;
  };
  async function page(append) {
    try {
      const r = await api.listEvents(state.project.project_id, { limit: 15, cursor });
      if (!append && !r.events.length) { ul.innerHTML = ""; $("feed-empty").hidden = false; }
      draw(r.events, append);
      cursor = r.next_cursor;
      more.hidden = !cursor;
    } catch {
      // Events are a convenience; the status document's own timeline tells much the same story.
      if (state.view && !append) { draw(timelineToFeed(state.view).slice(0, 15), false); more.hidden = true; }
      else { $("feed-empty").hidden = false; }
    }
  }
  more.addEventListener("click", async () => { more.disabled = true; await page(true); more.disabled = false; });
  await page(false);
}

// ---- Joining ----------------------------------------------------------------------------------------------------------------

function joinHtml(p, user) {
  const m = p.my_membership;
  const isMember = m && m.status === "active";
  // The API writes the code grouped (XXXX-XXXX-XXXX); normalise either form, so a stray hyphen is never doubled.
  const code = p.join_code ? normaliseJoinCode(p.join_code).display : null;
  let action = "";
  if (isMember) {
    action = html`<div class="callout"><p>You are a <strong>${m.role}</strong> of this project as <strong>${m.display_name}</strong>. See your frames on <a href="/stack/dashboard">My projects</a>.</p></div>`;
  } else if (m && m.status === "pending") {
    action = html`<div class="callout callout--warning"><p>Your membership is waiting for the owner's approval.</p></div>`;
  } else if (p.status !== "open") {
    action = html`<div class="callout callout--warning"><p>This project is <strong>${p.status}</strong> and is not accepting new members right now.</p></div>`;
  }
  return html`
    <ol class="prose" style="max-width:72ch">
      <li><strong>Get the join code</strong> from the project's owner${code ? "" : ". The code is for members and invited people; it is not shown on a public page"}.</li>
      <li><strong>Install Starfront</strong> or the Starfront N.I.N.A. plugin (<a href="/download">download</a>).</li>
      <li><strong>Enter the join code</strong> in Starfront's Collab tab or in the plugin. You accept the terms of contribution and pick a display name.</li>
      <li><strong>Map your filter names once</strong>, then image as usual. Your frames go to the project's hub and appear here once they are stacked.</li>
    </ol>
    ${code ? html`<p>Join code for your project: <span class="secret" style="display:inline-block;padding:4px 10px">${code}</span></p>` : ""}
    ${action}`;
}

// ---- Load / refresh ---------------------------------------------------------------------------------------------------------

function showError(title, text, extra = "") {
  $("project-loading").hidden = true;
  const el = $("project-error");
  el.hidden = false;
  setHtml(el, html`<div class="empty"><h1 style="font-size:var(--size-h2)">${title}</h1><p>${text}</p>${raw(extra)}</div>`);
}

function paintBody() {
  const view = state.view;
  stopPlaying();
  setHtml($("project-body"), renderBodyHtml(view));
  wireModeToggle($("project-body"), (m) => {
    state.mode = normaliseMode(m);
    writeModeToUrl(state.mode);
    paintBody();
    $(`mode-${state.mode}`)?.focus();
  });
  wirePaletteToggle($("project-body"), (p) => {
    state.palette = p;
    writePaletteToUrl(p);
    paintBody();
    $(`palette-${p}`)?.focus();
  });
  wireScrubber(view);
  wireDeepZoom($("project-body"), view);
  wirePanelsSection($("project-body"), { project: state.project, view, doc: state.panelsDoc, mosaic: state.mosaic });
}

/** A mosaic project's panel progress (panels.json, named by status.json) and who has which panel; nothing at all for a single field. */
async function loadPanelExtras() {
  if (!hasMosaic(state.project)) return;
  const [doc, mosaic] = await Promise.all([
    state.view?.panels?.url ? fetchPanelsDoc(state.view.panels.url) : null,
    api.mosaicGet(state.project.project_id).catch(() => null),
  ]);
  state.panelsDoc = doc;
  state.mosaic = mosaic;
}

function paintHeader() {
  setHtml($("project-head"), html`${headerHtml(state.project, state.view)}${statsHtml(state.view, state.project)}`);
  document.title = `${state.project.name} — ${config.siteName}`;
  document.querySelector('meta[name="description"]').setAttribute("content", `${state.project.name}: collaborative stacks of ${state.project.target_name || "one target"}, ${formatHours(state.project.counts?.integration_s ?? 0)} so far.`);
}

async function refresh() {
  if (document.visibilityState !== "visible" || deepZoomIsFullscreen()) return;
  try {
    const { view } = await loadStatus({ project: state.project, api });
    if (view.generatedAt !== state.view?.generatedAt) { state.view = view; await loadPanelExtras(); paintHeader(); paintBody(); }
  } catch { /* keep what is on screen */ }
}

async function main() {
  if (!route || route.view !== "project") { showError("No project here", "This address does not name a project.", '<p><a class="btn btn-primary" href="/stack/">Back to projects</a></p>'); return; }
  $("crumb-name").textContent = route.slug;

  const [user, projectRes] = await Promise.all([whoami(), api.getProject(route.slug).then((p) => ({ p }), (e) => ({ e }))]);
  state.user = user;
  if (projectRes.e) {
    const e = projectRes.e;
    if (e.code === "not_found") {
      showError("Project not found", "There is no project at this address, or it is private. If you are a member of a private project, open it from My projects after signing in.",
        '<p class="row" style="justify-content:center"><a class="btn btn-primary" href="/stack/dashboard">My projects</a><a class="btn btn-ghost" href="/stack/">Back to projects</a></p>');
    } else showError("Could not load this project", describeError(e));
    return;
  }
  state.project = projectRes.p;
  $("crumb-name").textContent = state.project.name;

  try {
    state.view = (await loadStatus({ project: state.project, api })).view;
  } catch (e) {
    state.view = null;
    state.statusError = e instanceof StatusError ? e : null;
  }

  await loadPanelExtras();
  $("project-loading").hidden = true;
  const requested = modeFromUrl();
  state.palette = paletteFromUrl();
  state.mode = requested || (state.view?.hasGold ? "gold" : "live");
  state.snapKey = state.view ? nightStreamKeys(state.view)[0] ?? null : null;

  paintHeader();
  if (state.view) paintBody();
  else {
    const unsupported = state.statusError?.code === "unsupported_schema";
    setHtml($("project-body"), html`<div class="empty"><h2>${unsupported ? "This page needs updating" : "Nothing published yet"}</h2><p>${unsupported ? "The project's data uses a newer format than this page understands. Reload; if it persists, the site needs an update." : "No stacks have been published for this project yet. They appear after the first accepted frames."}</p></div>`);
  }
  setHtml($("join-body"), joinHtml(state.project, user));
  $("lower").hidden = false;
  loadFeed();

  if (config.refreshSeconds > 0) state.timer = setInterval(refresh, config.refreshSeconds * 1000);
}

main().catch((e) => showError("Something went wrong", describeError(e)));
