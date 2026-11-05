// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// Preview figures, the live / gold toggle and the display presets, shared by the project page, the filter pages and the combined page.
// Plan 13 Q4: public pages show live AND gold, each clearly labelled, with a toggle. A mode that has not published
// yet is shown as an explained empty state; it is never silently replaced by the other one.

import { html, safeUrl } from "../dom.js";
import { normaliseMode, MODE_TEXT } from "../status.js";

export const modeBadge = (mode, label) => html`<span class="mode mode--${normaliseMode(mode)}">${label}</span>`;

const EMPTY = {
  live: "No live stack yet. It appears after the first batch of accepted frames.",
  gold: "No gold stack yet. The first gold integration is published nightly, or when enough new frames arrive.",
};

/**
 * One preview. `set` is a preview set { full, thumb, updatedAt } or null. `size` picks full or thumb.
 * The mode label sits on the image so a screenshot of it stays honest. `open`: the image is also a link to the
 * full-size picture in a new tab (never inside another link, such as a stream card). `href`: the image is instead a link to that page, in
 * the same tab (the project page's combined preview opens the combined page, as a stream card opens its filter page); `linkTitle` names it.
 */
export function previewFigure({ set, size = "full", mode, label, alt, caption, eager = false, width = 640, height = 480, open = false, href = null, linkTitle = "" }) {
  const m = normaliseMode(mode);
  const url = set ? set[size] : null;
  const img = url ? html`<img src="${safeUrl(url)}" alt="${alt}" width="${width}" height="${height}" ${eager ? "" : html`loading="lazy" `}decoding="async">` : "";
  return html`<figure class="preview">
    ${url
      ? href
        ? html`<a class="preview__open" href="${href}" title="${linkTitle}">${img}</a>`
        : open && set.full
          ? html`<a class="preview__open" href="${safeUrl(set.full)}" target="_blank" rel="noopener" title="Open full size in a new tab">${img}</a>`
          : img
      : html`<div class="preview__ph" role="img" aria-label="${alt}: ${EMPTY[m]}"><span>${EMPTY[m]}</span></div>`}
    <div class="preview__badges">${modeBadge(m, label)}</div>
    ${caption ? html`<figcaption>${caption}</figcaption>` : ""}
  </figure>`;
}

/**
 * The live / gold control as a radio group (native arrow-key behaviour, no JS needed to be operable).
 * `compare` adds a side-by-side option (filter page).
 */
export function modeToggle({ id, current, compare = false, legend = "Stack shown" }) {
  const opts = [["live", "Live"], ["gold", "Gold"], ...(compare ? [["compare", "Side by side"]] : [])];
  return html`<fieldset class="seg" id="${id}" data-mode-toggle><legend class="visually-hidden">${legend}</legend>
    ${opts.map(([v, l]) => html`<input type="radio" name="${id}-mode" id="${id}-${v}" value="${v}"${current === v ? html` checked` : ""}><label for="${id}-${v}">${l}</label>`)}
  </fieldset>`;
}

export const modeExplanation = (mode) => (mode === "compare" ? `${MODE_TEXT.live} ${MODE_TEXT.gold}` : MODE_TEXT[normaliseMode(mode)]);

/** Calls onChange(value) when the radio group changes. */
export function wireModeToggle(root, onChange) {
  root.querySelectorAll("[data-mode-toggle] input").forEach((i) => i.addEventListener("change", () => { if (i.checked) onChange(i.value); }));
}

// Display presets are applied in the browser to the published, already-stretched preview (SVG gamma filters in the page). They change
// what you see, never what is stored or what the stack contains.
export const DISPLAY_PRESETS = [
  ["none", "As published"],
  ["lift", "Lift shadows"],
  ["lift2", "Lift shadows more"],
  ["dark", "Darker background"],
];
const PRESET_FILTER = { none: "none", lift: "url(#adj-lift)", lift2: "url(#adj-lift2)", dark: "url(#adj-dark)" };

/** The CSS filter of a preset, for the `--adjust` custom property of a `.preview--adjust` wrapper. */
export const presetFilter = (preset) => PRESET_FILTER[preset] ?? "none";

/** The "Display" select. */
export function presetSelect({ id = "preset", current = "none", hintId = "preset-hint" }) {
  return html`<div class="form__row" style="width:auto;flex-direction:row;align-items:center;gap:10px"><label for="${id}">Display</label>
    <select id="${id}" aria-describedby="${hintId}" style="background:var(--surface-1);color:var(--text);border:1px solid var(--border-input);border-radius:10px;padding:8px 12px;font:inherit">${DISPLAY_PRESETS.map(([v, l]) => html`<option value="${v}"${v === current ? html` selected` : ""}>${l}</option>`)}</select></div>`;
}

/**
 * The combined preview's palette control (contract 1.30.0): each palette the project has, and "Both". Only worth showing with two palettes,
 * so the caller leaves it out otherwise. A radio group, like the live / gold control.
 */
export function paletteToggle({ id, palettes, current, legend = "Combined preview palette" }) {
  const opts = [...palettes.map((p) => [p, p]), ["both", "Both"]];
  return html`<fieldset class="seg" id="${id}" data-palette-toggle><legend class="visually-hidden">${legend}</legend>
    ${opts.map(([v, l]) => html`<input type="radio" name="${id}-palette" id="${id}-${v}" value="${v}"${current === v ? html` checked` : ""}><label for="${id}-${v}">${l}</label>`)}
  </fieldset>`;
}

/** Calls onChange(value) when the palette group changes. */
export function wirePaletteToggle(root, onChange) {
  root.querySelectorAll("[data-palette-toggle] input").forEach((i) => i.addEventListener("change", () => { if (i.checked) onChange(i.value); }));
}

/** ?palette=LRGB (or "both") in the address bar, so a shared link shows the same picture; null when absent. Checked against the project's palettes by the caller. */
export function paletteFromUrl(search = location.search) {
  const p = new URLSearchParams(search).get("palette");
  return p ? (p.toLowerCase() === "both" ? "both" : p.toUpperCase()) : null;
}

export function writePaletteToUrl(palette) {
  const u = new URL(location.href);
  u.searchParams.set("palette", palette);
  history.replaceState(null, "", u);
}

/** ?mode=gold in the address bar, so a shared link shows the same stack. Returns "live" | "gold" | "compare" | null. */
export function modeFromUrl(search = location.search, allowCompare = false) {
  const m = new URLSearchParams(search).get("mode");
  return m === "live" || m === "gold" || (allowCompare && m === "compare") ? m : null;
}

export function writeModeToUrl(mode) {
  const u = new URL(location.href);
  u.searchParams.set("mode", mode);
  history.replaceState(null, "", u);
}
