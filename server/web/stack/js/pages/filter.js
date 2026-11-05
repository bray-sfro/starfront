// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Starfront (EKE Services LLC).

// /p/{slug}/filter/{F}: one stream in detail (plan 9): large preview, live / gold / side by side, a display preset,
// the frame histogram by contributor, and the gold master download for members.

import { api, describeError } from "../api.js";
import { html, setHtml, raw, safeUrl } from "../dom.js";
import { whoami } from "../session.js";
import { parseProjectPath, projectUrl, filterUrl } from "../route.js";
import { loadStatus, StatusError, previewFor, modeLabel, normaliseMode, resolveStreams, sortStreams, LICENCE_LABEL } from "../status.js";
import { formatHours, formatInt, formatArcsec, formatBytes, formatDate, formatDateTime, relativeTime, filterChipClass, streamLabel, streamShortLabel } from "../format.js";
import { barChart, chartDataTable } from "../svg-charts.js";
import { previewFigure, modeToggle, modeExplanation, wireModeToggle, modeFromUrl, writeModeToUrl, presetFilter, presetSelect } from "../components/preview.js";

const $ = (id) => document.getElementById(id);
const route = parseProjectPath(location.pathname);
const time = (iso) => (iso ? html`<time datetime="${iso}" title="${formatDateTime(iso)}">${relativeTime(iso)}</time>` : "never");

function showError(title, text, extra = "") {
  $("filter-loading").hidden = true;
  const el = $("filter-error");
  el.hidden = false;
  setHtml(el, html`<div class="empty"><h1 style="font-size:var(--size-h2)">${title}</h1><p>${text}</p>${raw(extra)}</div>`);
}

function figures(stream, mode, preset) {
  const one = (m) => {
    const set = previewFor(stream, m);
    return html`<div>${previewFigure({
      set, mode: m, label: modeLabel(m, stream), eager: true, width: 640, height: 480, open: true,
      alt: `${stream.label} ${m} stack`, caption: set ? html`${modeLabel(m, stream)} stack, updated ${time(set.updatedAt)}. <a href="${safeUrl(set.full)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline">Open full size</a>` : "",
    })}</div>`;
  };
  const cls = "preview--adjust";
  const body = mode === "compare"
    ? html`<div class="grid grid--2">${one("live")}${one("gold")}</div>`
    : one(normaliseMode(mode));
  return html`<div class="${cls}" style="--adjust:${presetFilter(preset)}">${body}</div>`;
}

function histogramHtml(stream) {
  const rows = stream.byContributor;
  if (!rows.length) return html`<div class="empty"><p>No accepted frames yet.</p></div>`;
  const items = rows.map((r) => ({ label: r.name, value: r.framesAccepted, valueLabel: `${formatInt(r.framesAccepted)} · ${formatHours(r.integrationS)}`, color: "var(--primary)" }));
  return html`
    ${raw(barChart({ items, ariaLabel: `Accepted frames per contributor, ${stream.label}`, desc: items.map((i) => `${i.label}: ${i.valueLabel}`).join("; "), valueWidth: 120 }))}
    ${raw(chartDataTable({ caption: `Accepted frames and integration per contributor, ${stream.label}`, columns: ["Contributor", "Frames", "Hours"], rows: rows.map((r) => [r.name, String(r.framesAccepted), (r.integrationS / 3600).toFixed(1)]) }))}
    <p class="muted" style="font-size:13px">Bar length is accepted frames; the figure after it is frames and integration time.</p>`;
}

function mastersShell(project, stream) {
  const m = project.my_membership;
  const lic = LICENCE_LABEL[project.licence] || project.licence;
  if (m && m.status === "active") {
    return html`<p>Gold masters of <strong>${stream.label}</strong> are licensed to members under <strong>${lic}</strong>. Links are valid for one hour; ask again when one expires.</p>
      <p><button class="btn btn-primary" type="button" id="get-masters"${stream.goldSeq ? "" : html` disabled`}>Get download links</button></p>
      ${stream.goldSeq ? "" : html`<p class="muted">No gold master has been published for this stream yet.</p>`}
      <div id="masters-out" role="status" aria-live="polite"></div>`;
  }
  return html`<p>Gold masters are available to members of this project, licensed under <strong>${lic}</strong>. ${project.status === "open" ? html`Ask the owner for the join code and enter it in Starfront or the N.I.N.A. plugin to take part.` : ""}</p>`;
}

async function wireMasters(project, stream) {
  const btn = $("get-masters");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const out = $("masters-out");
    out.textContent = "Fetching links…";
    try {
      const all = await api.listMasters(project.project_id);
      const mine = all.filter((x) => x.stream_key === stream.key);
      if (!mine.length) { out.textContent = "No gold master has been published for this stream yet."; return; }
      setHtml(out, html`<ul>${mine.map((x) => html`<li><a href="${safeUrl(x.url)}" download>${stream.label} gold #${x.gold_seq} (${x.format.toUpperCase()})</a> · ${formatBytes(x.size_bytes)} · ${formatHours(x.integration_s)} from ${formatInt(x.frames ?? 0)} frames · published ${formatDate(x.created_at)} · link expires ${time(x.url_expires_at)}</li>`)}</ul>`);
    } catch (e) { out.textContent = describeError(e); }
    finally { btn.disabled = false; }
  });
}

function detailHtml(project, view, stream, state) {
  const chips = sortStreams(view.streams).map((s) => html`<a class="chip chip--${filterChipClass(s.key)}" href="${filterUrl(project.slug, s.key)}"${s.key === stream.key ? html` aria-current="page" style="font-weight:700;border-width:2px"` : ""} title="${s.label}" aria-label="${s.label}">${s.shortLabel ?? streamShortLabel(s.key)}</a>`);
  return html`
    <p class="eyebrow">${project.name}</p>
    <h1>${stream.label} stack</h1>
    <p class="row" aria-label="Other filters in this project"><span class="muted">Filters:</span> ${chips}</p>
    <div class="stats" role="list" style="margin:18px 0 28px">
      <div class="stat-tile" role="listitem"><div class="stat-tile__label">Frames</div><div class="stat-tile__value">${formatInt(stream.framesAccepted)}</div><div class="stat-tile__sub">${formatInt(stream.framesRejected)} rejected</div></div>
      <div class="stat-tile" role="listitem"><div class="stat-tile__label">Integration</div><div class="stat-tile__value">${formatHours(stream.integrationS)}</div><div class="stat-tile__sub">${stream.contributors} contributor${stream.contributors === 1 ? "" : "s"}</div></div>
      <div class="stat-tile" role="listitem"><div class="stat-tile__label">Median FWHM</div><div class="stat-tile__value">${formatArcsec(stream.medianFwhm)}</div><div class="stat-tile__sub">${stream.liveN} frames in the live stack</div></div>
      <div class="stat-tile" role="listitem"><div class="stat-tile__label">Gold</div><div class="stat-tile__value">${stream.goldSeq ? `#${stream.goldSeq}` : "none"}</div><div class="stat-tile__sub">updated ${time(stream.lastUpdate)}</div></div>
    </div>

    <div class="toolbar">
      ${modeToggle({ id: "fmode", current: state.mode, compare: true, legend: "Stack shown: live, gold or both" })}
      ${presetSelect({ current: state.preset })}
    </div>
    <p class="muted" id="mode-explain" role="status" style="max-width:80ch">${modeExplanation(state.mode)}</p>
    <p class="muted" id="preset-hint" style="font-size:13px;margin-top:-6px">Display presets brighten or darken the published preview in your browser only. The previews are already auto-stretched; they are not processed images.</p>
    <div id="figures">${figures(stream, state.mode, state.preset)}</div>

    <section style="margin-top:36px" aria-labelledby="hist-h"><div class="section-header"><h2 id="hist-h">Frames by contributor</h2><p>Display names only.</p></div>${histogramHtml(stream)}</section>
    <section style="margin-top:36px" aria-labelledby="dl-h"><div class="section-header"><h2 id="dl-h">Download the gold master</h2></div>${mastersShell(project, stream)}</section>
    <p style="margin-top:32px"><a href="${projectUrl(project.slug)}">Back to ${project.name}</a></p>`;
}

async function main() {
  if (!route || route.view !== "filter") { showError("No filter here", "This address does not name a project filter.", '<p><a class="btn btn-primary" href="/stack/">Back to projects</a></p>'); return; }
  $("crumb-project").textContent = route.slug;
  $("crumb-project").href = projectUrl(route.slug);
  $("crumb-filter").textContent = route.filter;

  const [user, pr] = await Promise.all([whoami(), api.getProject(route.slug).then((p) => ({ p }), (e) => ({ e }))]);
  if (pr.e) {
    showError(pr.e.code === "not_found" ? "Project not found" : "Could not load this project",
      pr.e.code === "not_found" ? "There is no project at this address, or it is private. Members: sign in to see it." : describeError(pr.e),
      '<p><a class="btn btn-primary" href="/stack/">Back to projects</a></p>');
    return;
  }
  const project = pr.p;
  $("crumb-project").textContent = project.name;

  let view;
  try { view = (await loadStatus({ project, api })).view; }
  catch (e) {
    showError("Nothing published yet", e instanceof StatusError && e.code === "unsupported_schema" ? "This page needs an update to read the project's data." : "No stacks have been published for this project yet.", `<p><a href="${projectUrl(project.slug)}">Back to ${project.name.replace(/[<>&"]/g, "")}</a></p>`);
    return;
  }

  const found = resolveStreams(view, route.filter);
  $("filter-loading").hidden = true;
  if (found.length === 0) {
    showError("No such filter in this project", `${route.filter} has no stack here.`, `<p>Filters in this project: ${view.streams.map((s) => `<a href="${filterUrl(project.slug, s.key)}">${s.label.replace(/[<>&"]/g, "")}</a>`).join(", ") || "none yet"}.</p>`);
    return;
  }
  if (found.length > 1) {
    $("crumb-filter").textContent = route.filter;
    setHtml($("filter-body"), html`<h1>${route.filter}: choose a stack</h1><p class="muted">This project keeps more than one stack for ${route.filter}, because the bandwidths or sensor types differ.</p>
      <div class="grid">${found.map((s) => html`<a class="card" href="${filterUrl(project.slug, s.key)}"><h2 class="card__title">${s.label}</h2><p class="card__body">${formatHours(s.integrationS)} from ${formatInt(s.framesAccepted)} frames</p></a>`)}</div>`);
    document.title = `${route.filter} — ${project.name} — Starfront`;
    return;
  }

  const stream = found[0];
  $("crumb-filter").textContent = stream.label;
  document.title = `${stream.label} — ${project.name} — Starfront`;
  const state = { mode: modeFromUrl(location.search, true) || (stream.goldSeq ? "gold" : "live"), preset: "none" };
  setHtml($("filter-body"), detailHtml(project, view, stream, state));
  wireModeToggle($("filter-body"), (m) => {
    state.mode = m;
    writeModeToUrl(m);
    setHtml($("figures"), figures(stream, m, state.preset));
    $("mode-explain").textContent = modeExplanation(m);
  });
  $("preset").addEventListener("change", (ev) => {
    state.preset = ev.target.value;
    setHtml($("figures"), figures(stream, state.mode, state.preset));
  });
  wireMasters(project, stream);
}

main().catch((e) => showError("Something went wrong", describeError(e)));
