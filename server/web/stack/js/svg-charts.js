// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// A small inline-SVG chart helper: bar, stacked bar and timeline. No framework, no dependency, no DOM: every
// function returns an SVG string, so the output can be tested under node --test and dropped into the page with
// setHtml(el, raw(svg)).
//
// Accessibility: each chart is role="img" with a <title> and <desc> wired through aria-labelledby, and
// chartDataTable() builds the same numbers as a visually hidden <table> that screen-reader users can read
// cell by cell. Colour is never the only carrier of meaning: rows are labelled and values are printed.
//
// Theme: nothing here hard-codes a colour. Fills come from CSS custom properties (css/collab.css, .chart*),
// which change with the light/dark theme; callers pass colours as `var(--f-ha)` strings.
//
// Safety: every label comes from user-supplied text (display names, stream keys), so all of it goes through esc().

import { esc } from "./dom.js";

let counter = 0;
const uid = () => `ch${++counter}`;

/** Categorical colours for series that have no natural colour (contributors). All pass AA in both themes. */
export const SERIES_COLORS = [
  "var(--f-blue)", "var(--f-ha)", "var(--f-oiii)", "var(--f-sii)",
  "var(--f-green)", "var(--f-other)", "var(--gold)", "var(--f-red)",
];
export const seriesColor = (i) => SERIES_COLORS[i % SERIES_COLORS.length];

function open({ width, height, ariaLabel, desc, id }) {
  const t = `${id}-t`;
  const d = `${id}-d`;
  return `<svg class="chart" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="${t}${desc ? " " + d : ""}" xmlns="http://www.w3.org/2000/svg">` +
    `<title id="${t}">${esc(ariaLabel)}</title>` + (desc ? `<desc id="${d}">${esc(desc)}</desc>` : "");
}

/** Width of the label column: the given one, or one that fits the longest label (about 7.5 px a character, 90-260 px). */
function labelColumn(labels, given) {
  if (given != null) return given;
  const longest = Math.max(0, ...labels.map((l) => String(l ?? "").length));
  return Math.round(Math.min(260, Math.max(90, 16 + 7.5 * longest)));
}
const fits = (labelWidth) => Math.max(6, Math.floor((labelWidth - 16) / 7.5));

function clip(s, n) {
  s = String(s ?? "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

const r1 = (n) => Math.round(n * 10) / 10;

/** Round a maximum up to a "nice" number so axis labels read cleanly. */
export function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * p) return m * p;
  return 10 * p;
}

/**
 * Horizontal bar chart.
 * items: [{ label, value, color?, valueLabel? }]. `format` renders a value when valueLabel is absent.
 */
export function barChart({ items, ariaLabel, desc, width = 640, rowHeight = 28, labelWidth: lw, valueWidth = 78, format = String, max } = {}) {
  const id = uid();
  const rows = items || [];
  const labelWidth = labelColumn(rows.map((r) => r.label), lw);
  const height = Math.max(rowHeight, rows.length * rowHeight) + 8;
  const areaW = width - labelWidth - valueWidth - 8;
  const top = max ?? Math.max(...rows.map((r) => r.value), 0);
  const parts = [open({ width, height, ariaLabel, desc, id })];
  rows.forEach((r, i) => {
    const y = 4 + i * rowHeight;
    const w = top > 0 && r.value > 0 ? Math.max(2, (r.value / top) * areaW) : 0;
    const cy = y + rowHeight / 2;
    parts.push(
      `<g><title>${esc(r.label)}: ${esc(r.valueLabel ?? format(r.value))}</title>` +
      `<text x="${labelWidth - 8}" y="${r1(cy + 4)}" text-anchor="end">${esc(clip(r.label, fits(labelWidth)))}</text>` +
      `<rect class="chart__bar" x="${labelWidth}" y="${r1(cy - 8)}" width="${r1(w)}" height="16" rx="3" style="--c:${esc(r.color || "var(--primary)")}"/>` +
      `<text class="chart__value" x="${r1(labelWidth + w + 6)}" y="${r1(cy + 4)}">${esc(r.valueLabel ?? format(r.value))}</text></g>`
    );
  });
  parts.push(`<line class="chart__axis" x1="${labelWidth}" y1="2" x2="${labelWidth}" y2="${height - 2}"/>`);
  parts.push("</svg>");
  return parts.join("");
}

/**
 * Horizontal stacked bar chart.
 * rows: [{ label, segments: [{ key, value }] }]; series: [{ key, label, color }].
 * Segments whose key is not in `series` are drawn in the first series colour rather than dropped.
 */
export function stackedBarChart({ rows, series, ariaLabel, desc, width = 640, rowHeight = 30, labelWidth: lw, valueWidth = 78, format = String } = {}) {
  const id = uid();
  const data = rows || [];
  const labelWidth = labelColumn(data.map((r) => r.label), lw);
  const colour = new Map((series || []).map((s, i) => [s.key, s.color || seriesColor(i)]));
  const height = Math.max(rowHeight, data.length * rowHeight) + 8;
  const areaW = width - labelWidth - valueWidth - 8;
  const totals = data.map((r) => r.segments.reduce((a, s) => a + Math.max(0, s.value), 0));
  const top = Math.max(...totals, 0);
  const parts = [open({ width, height, ariaLabel, desc, id })];
  data.forEach((r, i) => {
    const cy = 4 + i * rowHeight + rowHeight / 2;
    let x = labelWidth;
    parts.push(`<g><text x="${labelWidth - 8}" y="${r1(cy + 4)}" text-anchor="end">${esc(clip(r.label, fits(labelWidth)))}</text>`);
    for (const s of r.segments) {
      if (!(s.value > 0) || !(top > 0)) continue;
      const w = Math.max(1.5, (s.value / top) * areaW);
      const name = (series || []).find((x) => x.key === s.key)?.label ?? s.key;
      parts.push(
        `<rect class="chart__bar" x="${r1(x)}" y="${r1(cy - 9)}" width="${r1(w)}" height="18" style="--c:${esc(colour.get(s.key) || seriesColor(0))}">` +
        `<title>${esc(r.fullLabel ?? r.label)}, ${esc(name)}: ${esc(format(s.value))}</title></rect>`
      );
      x += w;
    }
    parts.push(`<text class="chart__value" x="${r1(x + 6)}" y="${r1(cy + 4)}">${esc(format(totals[i]))}</text></g>`);
  });
  parts.push(`<line class="chart__axis" x1="${labelWidth}" y1="2" x2="${labelWidth}" y2="${height - 2}"/>`);
  parts.push("</svg>");
  return parts.join("");
}

/** A legend as an HTML list (not SVG) so it wraps with the page. */
export function legend(series) {
  return `<ul class="chart-legend">` + (series || []).map((s, i) =>
    `<li><i style="--c:${esc(s.color || seriesColor(i))}"></i>${esc(s.label)}</li>`).join("") + `</ul>`;
}

/**
 * Timeline (area + line) of a cumulative quantity.
 * points: [{ t, v, label? }] with t an ISO string or epoch ms, ordered by t.
 * markers: [{ t, v, label }] drawn as dots (gold publishes, milestones).
 * selected: index into points to draw a cursor at, or null.
 */
export function timelineChart({ points, markers = [], selected = null, ariaLabel, desc, width = 720, height = 220, yFormat = (v) => String(Math.round(v)), color = "var(--primary)", markerColor = "var(--gold)" } = {}) {
  const id = uid();
  const pts = (points || []).map((p) => ({ ...p, x: typeof p.t === "number" ? p.t : Date.parse(p.t) })).filter((p) => Number.isFinite(p.x));
  const L = 46, R = 14, T = 12, B = 30;
  const parts = [open({ width, height, ariaLabel, desc, id })];
  if (pts.length === 0) {
    parts.push(`<text class="chart__muted" x="${width / 2}" y="${height / 2}" text-anchor="middle">No history yet</text></svg>`);
    return parts.join("");
  }
  const xmin = pts[0].x;
  const xmax = pts[pts.length - 1].x;
  const span = xmax - xmin;
  const ymax = niceMax(Math.max(...pts.map((p) => p.v), ...markers.map((m) => m.v ?? 0)));
  const sx = (x) => (span > 0 ? L + ((x - xmin) / span) * (width - L - R) : (L + width - R) / 2);
  const sy = (v) => T + (1 - v / ymax) * (height - T - B);
  const base = height - B;

  for (const f of [0, 0.5, 1]) {
    const y = sy(ymax * f);
    parts.push(`<line class="${f === 0 ? "chart__axis" : "chart__grid"}" x1="${L}" y1="${r1(y)}" x2="${width - R}" y2="${r1(y)}"/>`);
    parts.push(`<text class="chart__muted chart__value" x="${L - 6}" y="${r1(y + 4)}" text-anchor="end">${esc(yFormat(ymax * f))}</text>`);
  }
  const line = pts.map((p, i) => `${i ? "L" : "M"}${r1(sx(p.x))} ${r1(sy(p.v))}`).join(" ");
  parts.push(`<path class="chart__area" style="--c:${esc(color)}" d="${line} L${r1(sx(xmax))} ${base} L${r1(sx(xmin))} ${base} Z"/>`);
  parts.push(`<path class="chart__line" style="--c:${esc(color)}" d="${line}"/>`);

  const day = (x) => new Date(x).toISOString().slice(0, 10);
  parts.push(`<text class="chart__muted" x="${L}" y="${height - 8}" text-anchor="start">${esc(day(xmin))}</text>`);
  if (span > 0) parts.push(`<text class="chart__muted" x="${width - R}" y="${height - 8}" text-anchor="end">${esc(day(xmax))}</text>`);

  for (const m of markers) {
    const x = typeof m.t === "number" ? m.t : Date.parse(m.t);
    if (!Number.isFinite(x)) continue;
    parts.push(`<circle class="chart__marker" style="--c:${esc(markerColor)}" cx="${r1(sx(x))}" cy="${r1(sy(m.v ?? 0))}" r="4.5"><title>${esc(m.label)}</title></circle>`);
  }
  if (selected !== null && pts[selected]) {
    const p = pts[selected];
    parts.push(`<line class="chart__cursor" x1="${r1(sx(p.x))}" y1="${T}" x2="${r1(sx(p.x))}" y2="${base}"/>`);
    parts.push(`<circle class="chart__marker" style="--c:${esc(color)}" cx="${r1(sx(p.x))}" cy="${r1(sy(p.v))}" r="5"/>`);
  }
  parts.push("</svg>");
  return parts.join("");
}

/** The same numbers as a table only assistive technology sees. `rows` are arrays of already-formatted cells.
 *  The table sits in a clipped wrapper: a table ignores width:1px and would otherwise widen the page. */
export function chartDataTable({ caption, columns, rows }) {
  return `<div class="visually-hidden"><table><caption>${esc(caption)}</caption><thead><tr>` +
    columns.map((c) => `<th scope="col">${esc(c)}</th>`).join("") + `</tr></thead><tbody>` +
    rows.map((r) => `<tr>${r.map((c, i) => (i === 0 ? `<th scope="row">${esc(c)}</th>` : `<td>${esc(c)}</td>`)).join("")}</tr>`).join("") +
    `</tbody></table></div>`;
}
