// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Starfront (EKE Services LLC).

// /p/{slug}/combined: the combined (colour) preview in detail, as /p/{slug}/filter/{F} is one stream's: a large preview, live / gold /
// side by side and a display preset. The project page's combined preview links here, as its stream cards link to the filter pages.

import { api, describeError } from "../api.js";
import { html, setHtml, raw, safeUrl } from "../dom.js";
import { parseProjectPath, projectUrl, filterUrl } from "../route.js";
import { whoami } from "../session.js";
import { loadStatus, StatusError, previewFor, modeLabel, normaliseMode, sortStreams, combinedPalettes, resolvePalette } from "../status.js";
import { formatHours, formatInt, formatDateTime, relativeTime, filterChipClass, streamShortLabel } from "../format.js";
import { previewFigure, modeToggle, modeExplanation, wireModeToggle, modeFromUrl, writeModeToUrl, presetFilter, presetSelect, paletteToggle, wirePaletteToggle, paletteFromUrl, writePaletteToUrl } from "../components/preview.js";

const $ = (id) => document.getElementById(id);
const route = parseProjectPath(location.pathname);
const time = (iso) => (iso ? html`<time datetime="${iso}" title="${formatDateTime(iso)}">${relativeTime(iso)}</time>` : "never");

function showError(title, text, extra = "") {
  $("combined-loading").hidden = true;
  const el = $("combined-error");
  el.hidden = false;
  setHtml(el, html`<div class="empty"><h1 style="font-size:var(--size-h2)">${title}</h1><p>${text}</p>${raw(extra)}</div>`);
}

function figures(view, mode, preset, palette) {
  const gold = { goldSeq: view.totals.goldSeqMax };
  const palettes = combinedPalettes(view);
  const choice = resolvePalette(palette, palettes);
  const shown = choice === "both" ? palettes : palettes.filter((p) => p.palette === choice);
  const one = (pal, m) => {
    const set = previewFor(pal, m);
    return html`<div>${previewFigure({
      set, mode: m, label: modeLabel(m, gold), eager: true, width: 640, height: 480, open: true,
      alt: `${pal.palette} combined preview of ${view.project.name}, ${m} stack`,
      caption: set ? html`${pal.palette} palette, linear per-channel stretch, no post-processing. ${modeLabel(m, gold)} stack, updated ${time(set.updatedAt)}. <a href="${safeUrl(set.full)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline">Open full size</a>` : "",
    })}</div>`;
  };
  // Both palettes and both modes is four pictures: a row per palette, live beside gold.
  const modes = mode === "compare" ? ["live", "gold"] : [normaliseMode(mode)];
  const cells = shown.flatMap((pal) => modes.map((m) => one(pal, m)));
  const body = cells.length > 1 ? html`<div class="grid grid--2">${cells}</div>` : cells[0];
  return html`<div class="preview--adjust" style="--adjust:${presetFilter(preset)}">${body}</div>`;
}

function detailHtml(project, view, state) {
  const t = view.totals;
  const chips = sortStreams(view.streams).map((s) => html`<a class="chip chip--${filterChipClass(s.key)}" href="${filterUrl(project.slug, s.key)}" title="${s.label}" aria-label="${s.label}">${s.shortLabel ?? streamShortLabel(s.key)}</a>`);
  return html`
    <p class="eyebrow">${project.name}</p>
    <h1>Combined preview</h1>
    <p class="row" aria-label="The filters of this project"><span class="muted">Filters:</span> ${chips}</p>
    <div class="stats" role="list" style="margin:18px 0 28px">
      <div class="stat-tile" role="listitem"><div class="stat-tile__label">Palette</div><div class="stat-tile__value">${combinedPalettes(view).map((p) => p.palette).join(" + ") || view.combined.palette}</div><div class="stat-tile__sub">${formatInt(view.streams.length)} filter stack${view.streams.length === 1 ? "" : "s"}</div></div>
      <div class="stat-tile" role="listitem"><div class="stat-tile__label">Integration</div><div class="stat-tile__value">${formatHours(t.integrationS)}</div><div class="stat-tile__sub">${formatInt(t.framesAccepted)} frames</div></div>
      <div class="stat-tile" role="listitem"><div class="stat-tile__label">Contributors</div><div class="stat-tile__value">${formatInt(t.contributors)}</div><div class="stat-tile__sub">${formatInt(t.memberCount)} members</div></div>
      <div class="stat-tile" role="listitem"><div class="stat-tile__label">Gold</div><div class="stat-tile__value">${t.goldSeqMax ? `#${t.goldSeqMax}` : "none"}</div><div class="stat-tile__sub">updated ${time(view.updatedAt)}</div></div>
    </div>

    <div class="toolbar">
      ${modeToggle({ id: "cmode", current: state.mode, compare: true, legend: "Stack shown: live, gold or both" })}
      ${combinedPalettes(view).length > 1 ? paletteToggle({ id: "cpalette", palettes: combinedPalettes(view).map((p) => p.palette), current: resolvePalette(state.palette, combinedPalettes(view)), legend: "Palette shown" }) : ""}
      ${presetSelect({ current: state.preset })}
    </div>
    <p class="muted" id="mode-explain" role="status" style="max-width:80ch">${modeExplanation(state.mode)}</p>
    <p class="muted" id="preset-hint" style="font-size:13px;margin-top:-6px">Display presets brighten or darken the published preview in your browser only. The previews are already auto-stretched; they are not processed images.</p>
    <div id="figures">${figures(view, state.mode, state.preset, state.palette)}</div>
    <p class="muted" style="max-width:80ch;margin-top:18px">The filters' stacks combined in the ${view.combined.palette} palette${view.combined.alternates.length ? `, and in ${view.combined.alternates.map((a) => a.palette).join(" and ")} because every filter it needs has a stack too` : ""}. Open a filter above to see its own stack, its contributors and its gold master.</p>
    <p style="margin-top:32px"><a href="${projectUrl(project.slug)}">Back to ${project.name}</a></p>`;
}

async function main() {
  if (!route || route.view !== "combined") { showError("No project here", "This address does not name a project.", '<p><a class="btn btn-primary" href="/stack/">Back to projects</a></p>'); return; }
  $("crumb-project").textContent = route.slug;
  $("crumb-project").href = projectUrl(route.slug);

  const [user, pr] = await Promise.all([whoami(), api.getProject(route.slug).then((p) => ({ p }), (e) => ({ e }))]);
  if (pr.e) {
    showError(pr.e.code === "not_found" ? "Project not found" : "Could not load this project",
      pr.e.code === "not_found" ? "There is no project at this address, or it is private. Members: sign in to see it." : describeError(pr.e),
      '<p><a class="btn btn-primary" href="/stack/">Back to projects</a></p>');
    return;
  }
  const project = pr.p;
  $("crumb-project").textContent = project.name;
  const back = `<p><a href="${projectUrl(project.slug)}">Back to ${project.name.replace(/[<>&"]/g, "")}</a></p>`;

  let view;
  try { view = (await loadStatus({ project, api })).view; }
  catch (e) {
    showError("Nothing published yet", e instanceof StatusError && e.code === "unsupported_schema" ? "This page needs an update to read the project's data." : "No stacks have been published for this project yet.", back);
    return;
  }
  if (view.combined.palette === "none") {
    showError("No combined preview", "This project has no colour palette, so there is no combined preview. Each filter has its own stack.",
      `<p>Filters in this project: ${view.streams.map((s) => `<a href="${filterUrl(project.slug, s.key)}">${s.label.replace(/[<>&"]/g, "")}</a>`).join(", ") || "none yet"}.</p>${back}`);
    return;
  }

  $("combined-loading").hidden = true;
  document.title = `Combined preview — ${project.name} — Starfront`;
  const state = { mode: modeFromUrl(location.search, true) || (view.combined.gold ? "gold" : "live"), preset: "none", palette: paletteFromUrl() };
  setHtml($("combined-body"), detailHtml(project, view, state));
  wireModeToggle($("combined-body"), (m) => {
    state.mode = m;
    writeModeToUrl(m);
    setHtml($("figures"), figures(view, m, state.preset, state.palette));
    $("mode-explain").textContent = modeExplanation(m);
  });
  wirePaletteToggle($("combined-body"), (p) => {
    state.palette = p;
    writePaletteToUrl(p);
    setHtml($("figures"), figures(view, state.mode, state.preset, state.palette));
  });
  $("preset").addEventListener("change", (ev) => {
    state.preset = ev.target.value;
    setHtml($("figures"), figures(view, state.mode, state.preset, state.palette));
  });
}

main().catch((e) => showError("Something went wrong", describeError(e)));
