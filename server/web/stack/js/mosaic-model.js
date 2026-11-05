// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// The owner console's Mosaic tab and the public page's panel grid, minus the DOM: the layout form, the canvas geometry (hit-testing
// by arithmetic, so 625 panels need no per-panel element), the selection, the filters, the heat bins, the targets and the operations
// the tab sends. Pure functions on plain data, so every rule has a unit test (test/mosaic-model.test.mjs).
//
// Privacy: panels carry DISPLAY NAMES (`assigned_to`, `claimed_by`). The owner's member list also carries user ids, which this file
// only ever returns inside an operation body (`user_id`), never as text to show.

import { panelId, parsePanelId, allPanelIds, compressSelection, validateLayout, derive, derivedGrid, MAX_OPERATIONS, SINGLE_FIELD_EDGE, MAX_GRID_EDGE, isPanelId } from "./mosaic-math.js";
import { parseRa, parseDec, formatRa, formatDec } from "./grid-math.js";

// ---- The layout form ------------------------------------------------------------------------------------------------------------------

const num = (s) => (/^[+-]?(\d+\.?\d*|\.\d+)$/.test(String(s ?? "").trim()) ? Number(s) : NaN);
const round = (x, d) => Math.round(x * 10 ** d) / 10 ** d;

export const LAYOUT_FIELDS = [
  { key: "rows", label: "Rows", hint: "1 to 25. Row 1 is the top of the project frame.", type: "number", step: 1, min: 1, max: 25 },
  { key: "cols", label: "Columns", hint: "1 to 25. Column 1 is the left.", type: "number", step: 1, min: 1, max: 25 },
  { key: "overlap", label: "Overlap (percent)", hint: "How much neighbouring panels overlap, as a percentage of the panel size. 10 to 20 is usual. 0 to 50.", type: "number", step: "any", min: 0, max: 50 },
  { key: "pw", label: "Panel width (degrees)", hint: "0.05 to 20. The field of view of the smallest rig that should be able to take a panel in one pointing, or a little less.", type: "number", step: "any", min: 0.05, max: 20 },
  { key: "ph", label: "Panel height (degrees)", hint: "0.05 to 20.", type: "number", step: "any", min: 0.05, max: 20 },
  { key: "rot", label: "Rotation (degrees east of north)", hint: "The direction the top of the grid points. 0 puts north up, east left.", type: "number", step: "any", min: -180, max: 180 },
  { key: "scale", label: "Scale (arcseconds per pixel)", hint: "0.1 to 20. Decides the pixel size of the whole mosaic: halving it quadruples the pixels.", type: "number", step: "any", min: 0.1, max: 20 },
  { key: "ra", label: "Centre right ascension", hint: "Degrees (85.2) or hours minutes seconds (05 40 59).", type: "text", inputmode: "decimal" },
  { key: "dec", label: "Centre declination", hint: "Degrees (-2.45) or degrees minutes seconds (-02 27 30).", type: "text", inputmode: "decimal" },
];

/** Form values (strings) from a layout, or the starting values of a new one. */
export function layoutToForm(layout, defaults = {}) {
  if (!layout) return { rows: "3", cols: "4", overlap: "15", pw: "1.5", ph: "1", rot: "0", scale: "2", ra: "", dec: "", ...defaults };
  return {
    rows: String(layout.rows), cols: String(layout.cols), overlap: String(layout.overlap_pct), pw: String(layout.panel_w_deg), ph: String(layout.panel_h_deg),
    rot: String(layout.rotation_deg), scale: String(layout.scale_arcsec_px), ra: String(layout.centre.ra_deg), dec: String(layout.centre.dec_deg),
  };
}

/** Parse the form. Returns { layout } when valid (ranges as the contract has them), else { errors: { field: message } } naming every bad field. */
export function readLayoutForm(v) {
  const errors = {};
  const whole = (k, lo, hi, what) => { const n = num(v[k]); if (!(Number.isInteger(n) && n >= lo && n <= hi)) errors[k] = `${what} is a whole number from ${lo} to ${hi}.`; return n; };
  const rows = whole("rows", 1, 25, "Rows"), cols = whole("cols", 1, 25, "Columns");
  const range = (k, lo, hi, what, blankIs) => {
    if (String(v[k] ?? "").trim() === "" && blankIs !== undefined) return blankIs;
    const n = num(v[k]);
    if (!(n >= lo && n <= hi)) errors[k] = `${what} is ${lo} to ${hi}.`;
    return n;
  };
  const overlap = range("overlap", 0, 50, "Overlap", 0);
  const pw = range("pw", 0.05, 20, "Panel width"), ph = range("ph", 0.05, 20, "Panel height");
  const rot = range("rot", -180, 180, "Rotation", 0);
  const scale = range("scale", 0.1, 20, "Scale");
  const ra = parseRa(v.ra), dec = parseDec(v.dec);
  if (String(v.ra ?? "").trim() === "") errors.ra = "Enter the centre's right ascension.";
  else if (!(ra >= 0 && ra < 360)) errors.ra = "Right ascension is 0 to under 360 degrees, or hours minutes seconds such as 05 40 59.";
  if (String(v.dec ?? "").trim() === "") errors.dec = "Enter the centre's declination.";
  else if (!(dec >= -90 && dec < 90)) errors.dec = "Declination is -90 up to 90 degrees, or degrees minutes seconds such as -02 27 30.";
  if (Object.keys(errors).length) return { errors };
  const layout = {
    rows, cols, overlap_pct: round(overlap, 3), panel_w_deg: round(pw, 5), panel_h_deg: round(ph, 5), rotation_deg: round(rot, 4), scale_arcsec_px: round(scale, 5),
    centre: { ra_deg: round(ra, 6), dec_deg: round(dec, 6) },
  };
  const problems = validateLayout(layout);
  if (problems.length) return { errors: { layout: problems[0].message } };
  return { layout };
}

export const sameLayout = (a, b) => !!a && !!b && JSON.stringify(canon(a)) === JSON.stringify(canon(b));
const canon = (l) => ({ rows: l.rows, cols: l.cols, o: l.overlap_pct, w: l.panel_w_deg, h: l.panel_h_deg, r: l.rotation_deg, s: l.scale_arcsec_px, a: l.centre.ra_deg, d: l.centre.dec_deg });

/** What the owner should know about the project frame a layout would give: size, cost, and warnings in plain words. level: ok | warn | error. */
export function gridNotes(layout) {
  const g = derivedGrid(layout);
  const long = Math.max(g.width_px, g.height_px);
  const px = g.width_px * g.height_px;
  const notes = [];
  let level = "ok";
  const raise = (l, text) => { notes.push({ level: l, text }); if (level !== "error") level = l; };
  if (long > MAX_GRID_EDGE) raise("error", `The project frame would be ${long.toLocaleString("en-GB")} px on its long side; the limit is ${MAX_GRID_EDGE.toLocaleString("en-GB")}. Use a coarser scale.`);
  else if (long > SINGLE_FIELD_EDGE) raise("warn", `${long.toLocaleString("en-GB")} px on the long side is larger than a single field (${SINGLE_FIELD_EDGE.toLocaleString("en-GB")}). That works, with the stack stored in tiles, but every gold run reads more.`);
  if (px / 1e9 >= 1) raise("warn", `${(px / 1e9).toFixed(1)} gigapixels: each stream's live stack keeps four planes of that, about ${((px * 16) / 1e9).toFixed(0)} GB. A coarser scale is cheaper.`);
  if (long > 60000) raise("warn", "A frame this large is slow to open and to preview. Consider fewer panels or a coarser scale.");
  return { grid: g, megapixels: px / 1e6, planeBytes: px * 4, notes, level, panelCount: layout.rows * layout.cols, tiled: long > SINGLE_FIELD_EDGE };
}

// ---- Canvas geometry (hit-testing by arithmetic) -------------------------------------------------------------------------------------------

/**
 * How the layout is laid out on a canvas of `width` x `height` CSS pixels: an equal cell per panel (the cell is the panel's share of the grid, so
 * overlapping panels do not overdraw), with a header strip above for column numbers and one on the left for row numbers.
 * { x0, y0, cw, ch, rows, cols, header, width, height }. `aspect` is the cell's width over height (step_w / step_h).
 */
export function canvasGeometry({ rows, cols, width, height, aspect = 1, header = 22 }) {
  const availW = Math.max(1, width - header - 2), availH = Math.max(1, height - header - 2);
  let cw = availW / cols, ch = cw / aspect;
  if (ch * rows > availH) { ch = availH / rows; cw = ch * aspect; }
  return { x0: header, y0: header, cw, ch, rows, cols, header, width, height, usedW: cw * cols, usedH: ch * rows };
}

/** What lies under a point of the canvas: { kind: "panel", row, col, id } | { kind: "row", row } | { kind: "col", col } | { kind: "corner" } | null. */
export function hitTest(geom, x, y) {
  const { x0, y0, cw, ch, rows, cols } = geom;
  const inX = x >= x0 && x < x0 + cw * cols, inY = y >= y0 && y < y0 + ch * rows;
  if (inX && inY) { const col = Math.floor((x - x0) / cw) + 1, row = Math.floor((y - y0) / ch) + 1; return { kind: "panel", row, col, id: panelId(row, col) }; }
  if (x >= 0 && x < x0 && y >= 0 && y < y0) return { kind: "corner" };
  if (y >= 0 && y < y0 && inX) return { kind: "col", col: Math.floor((x - x0) / cw) + 1 };
  if (x >= 0 && x < x0 && inY) return { kind: "row", row: Math.floor((y - y0) / ch) + 1 };
  return null;
}

/** The pixel rectangle of a panel's cell. */
export const cellRect = (geom, row, col) => ({ x: geom.x0 + (col - 1) * geom.cw, y: geom.y0 + (row - 1) * geom.ch, w: geom.cw, h: geom.ch });

/** Ids of the cells a dragged rectangle (two corner points, any order) covers by a fifth or more, so a drag from one cell's middle to another's takes all the cells between, corners included. */
export function panelsInDrag(geom, ax, ay, bx, by) {
  const x1 = Math.min(ax, bx), x2 = Math.max(ax, bx), y1 = Math.min(ay, by), y2 = Math.max(ay, by);
  const c0 = Math.floor((x1 - geom.x0) / geom.cw), c1 = Math.floor((x2 - geom.x0) / geom.cw);
  const r0 = Math.floor((y1 - geom.y0) / geom.ch), r1 = Math.floor((y2 - geom.y0) / geom.ch);
  const out = [];
  for (let r = Math.max(0, r0); r <= Math.min(geom.rows - 1, r1); r++) {
    for (let c = Math.max(0, c0); c <= Math.min(geom.cols - 1, c1); c++) {
      const ox = Math.min(x2, geom.x0 + (c + 1) * geom.cw) - Math.max(x1, geom.x0 + c * geom.cw);
      const oy = Math.min(y2, geom.y0 + (r + 1) * geom.ch) - Math.max(y1, geom.y0 + r * geom.ch);
      if (ox > 0 && oy > 0 && ox * oy >= 0.2 * geom.cw * geom.ch) out.push(panelId(r + 1, c + 1));
    }
  }
  // The cells under the two corners of the drag always count, so a short drag still selects what it started on.
  for (const [px, py] of [[ax, ay], [bx, by]]) { const h = hitTest(geom, px, py); if (h?.kind === "panel" && !out.includes(h.id)) out.push(h.id); }
  return out;
}

// ---- Selection ----------------------------------------------------------------------------------------------------------------------------

/**
 * A selection is a Set of panel ids; every function returns a NEW set. `mode`: "replace" (a plain click), "toggle" (ctrl or command click:
 * add what is not in, remove what is), "add" (shift: add).
 */
export function applySelection(current, ids, mode = "replace") {
  const next = mode === "replace" ? new Set() : new Set(current);
  if (mode === "toggle") { const allIn = ids.length > 0 && ids.every((id) => current.has(id)); for (const id of ids) allIn ? next.delete(id) : next.add(id); }
  else for (const id of ids) next.add(id);
  return next;
}

export const rowIds = (row, cols) => Array.from({ length: cols }, (_, i) => panelId(row, i + 1));
export const colIds = (col, rows) => Array.from({ length: rows }, (_, i) => panelId(i + 1, col));

/** Ids for a hit (a panel, a whole row, a whole column, or all via the corner). */
export function idsOfHit(hit, rows, cols) {
  if (!hit) return [];
  if (hit.kind === "panel") return [hit.id];
  if (hit.kind === "row") return rowIds(hit.row, cols);
  if (hit.kind === "col") return colIds(hit.col, rows);
  if (hit.kind === "corner") return allPanelIds(rows, cols);
  return [];
}

/** Move a selected cell with the arrow keys (for keyboard use). Returns the new cell, clamped to the layout. */
export function stepCell(cell, key, rows, cols) {
  const d = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] }[key];
  if (!d) return cell;
  return { row: Math.min(rows, Math.max(1, cell.row + d[0])), col: Math.min(cols, Math.max(1, cell.col + d[1])) };
}

// ---- Who has what -------------------------------------------------------------------------------------------------------------------------

/** "assigned" | "claimed" | "free" for a panel of MosaicState.panels. */
export function panelState(p) {
  if (p.assigned_to) return "assigned";
  return p.claimed_by && p.claimed_by.length ? "claimed" : "free";
}

/** { total, assigned, claimed, free, byMember: [{ name, assigned, claimed }] } for the text alternative to the canvas. */
export function summarise(panels) {
  const out = { total: panels.length, assigned: 0, claimed: 0, free: 0 };
  const by = new Map();
  const slot = (n) => { if (!by.has(n)) by.set(n, { name: n, assigned: 0, claimed: 0 }); return by.get(n); };
  for (const p of panels) {
    out[panelState(p)]++;
    if (p.assigned_to) slot(p.assigned_to).assigned++;
    for (const n of p.claimed_by ?? []) slot(n).claimed++;
  }
  out.byMember = [...by.values()].sort((a, b) => b.assigned + b.claimed - (a.assigned + a.claimed) || a.name.localeCompare(b.name));
  return out;
}

/** A stable colour for a member's display name (the same name is always the same colour), as hue/saturation/lightness. */
export function memberHue(name) {
  let h = 2166136261;
  for (const ch of String(name)) { h ^= ch.codePointAt(0); h = Math.imul(h, 16777619) >>> 0; }
  // Golden-angle steps spread near-identical hashes apart.
  return (h % 360 + ((h >>> 9) % 7) * 137.508) % 360;
}
export const memberColour = (name, dark = true) => `hsl(${memberHue(name).toFixed(0)} ${dark ? 58 : 62}% ${dark ? 52 : 46}%)`;

// ---- Progress, targets, heat -----------------------------------------------------------------------------------------------------------

/** The canonical filter of a stream key: "HA@3nm" -> "HA". */
export const filterOfStream = (key) => String(key).split("@")[0];

/** Hours integrated in a panel for a canonical filter ("all" sums every stream), from a panels.json document. */
export function hoursIn(panelDoc, filter) {
  if (!panelDoc) return 0;
  let s = 0;
  for (const [key, st] of Object.entries(panelDoc.streams ?? {})) if (filter === "all" || filterOfStream(key) === filter) s += st.integration_s ?? 0;
  return s / 3600;
}

/** Index of a panels.json document by panel id. */
export function indexPanels(doc) {
  const m = new Map();
  for (const p of doc?.panels ?? []) m.set(p.panel_id, p);
  return m;
}

/** The target in hours for a panel and filter: the panel's override if it has one (0 = none), else the default; null when there is none. */
export function targetFor(targets, id, filter) {
  const o = targets?.panels?.[id]?.[filter];
  const v = o !== undefined ? o : targets?.default?.[filter];
  return typeof v === "number" && v > 0 ? v : null;
}

/** Filters that have a target anywhere (the default or any override), plus the project's filters, in the project's order. */
export function targetFilters(projectFilters, targets) {
  const out = [...(projectFilters ?? [])];
  const add = (f) => { if (!out.includes(f)) out.push(f); };
  Object.keys(targets?.default ?? {}).forEach(add);
  for (const o of Object.values(targets?.panels ?? {})) Object.keys(o).forEach(add);
  return out;
}

export const HEAT_BINS = 6; // 0 none, 1 under a quarter, 2 under a half, 3 under three quarters, 4 under the target, 5 at or over it
export const HEAT_LABELS = ["no data", "under 25 %", "25 to 50 %", "50 to 75 %", "75 to 100 %", "target reached"];

/**
 * Per-panel heat for one filter: { id, hours, target, fraction, bin }. With a target, the fraction is hours over target; without one, it is
 * hours over the best panel's hours (so the map still shows where the data is), and `relative` is true.
 */
export function heatFor(ids, byId, filter, targets) {
  const hours = ids.map((id) => hoursIn(byId.get(id), filter));
  const max = Math.max(0, ...hours);
  return ids.map((id, i) => {
    const target = filter === "all" ? null : targetFor(targets, id, filter);
    const h = hours[i];
    const fraction = target ? h / target : max > 0 ? h / max : 0;
    return { id, hours: h, target, fraction, relative: !target, bin: h <= 0 ? 0 : heatBin(fraction) };
  });
}
export function heatBin(fraction) {
  if (fraction >= 1) return 5;
  if (fraction >= 0.75) return 4;
  if (fraction >= 0.5) return 3;
  if (fraction >= 0.25) return 2;
  return 1;
}

/** Panels whose hours for `filter` are under their target (panels with a target only). */
export function behindTarget(ids, byId, filter, targets) {
  return heatFor(ids, byId, filter, targets).filter((h) => h.target && h.hours < h.target).map((h) => h.id);
}

export const FILTER_MODES = [
  { id: "all", label: "Show all" },
  { id: "unassigned", label: "Unassigned" },
  { id: "unclaimed", label: "Unclaimed (no owner and no claim)" },
  { id: "behind", label: "Behind target" },
];

/** The ids a filter keeps. `panels` is MosaicState.panels. "behind" needs the progress index, the filter and the targets. */
export function filterPanels(panels, mode, { byId, filter, targets } = {}) {
  const ids = panels.map((p) => p.panel_id);
  if (mode === "unassigned") return panels.filter((p) => !p.assigned_to).map((p) => p.panel_id);
  if (mode === "unclaimed") return panels.filter((p) => panelState(p) === "free").map((p) => p.panel_id);
  if (mode === "behind") return behindTarget(ids, byId ?? new Map(), filter, targets);
  return ids;
}

// ---- Editing targets (a whole-document replace: MosaicTargets) ---------------------------------------------------------------------------

export function parseHours(text) {
  const t = String(text ?? "").trim();
  if (t === "") return null;
  const n = /^(\d+\.?\d*|\.\d+)$/.test(t) ? Number(t) : NaN;
  return n >= 0 && n <= 1000 ? n : NaN;
}

const clone = (t) => ({ default: { ...(t?.default ?? {}) }, panels: Object.fromEntries(Object.entries(t?.panels ?? {}).map(([k, v]) => [k, { ...v }])) });

/** The project default for a filter; null (or 0, which the server drops anyway) removes it. Returns a new MosaicTargets. */
export function setDefaultTarget(targets, filter, hours) {
  const t = clone(targets);
  if (hours === null || hours === 0) delete t.default[filter]; else t.default[filter] = hours;
  return tidyTargets(t);
}

/** An override for the given panels and filter; null removes the override (the panel follows the default again); 0 means no target there. */
export function setPanelTargets(targets, ids, filter, hours) {
  const t = clone(targets);
  for (const id of ids) {
    if (hours === null) { if (t.panels[id]) delete t.panels[id][filter]; }
    else (t.panels[id] ??= {})[filter] = hours;
  }
  return tidyTargets(t);
}

/** Drops empty panel entries and overrides that equal the default; with `validIds`, entries for panels that no longer exist. */
export function tidyTargets(t, validIds = null) {
  const out = { default: { ...t.default }, panels: {} };
  for (const [id, o] of Object.entries(t.panels)) {
    if (validIds && !validIds.has(id)) continue;
    const keep = Object.fromEntries(Object.entries(o).filter(([f, h]) => (out.default[f] ?? 0) !== h));
    if (Object.keys(keep).length) out.panels[id] = keep;
  }
  return out;
}

export const sameTargets = (a, b) => JSON.stringify(sortKeys(tidyTargets(clone(a)))) === JSON.stringify(sortKeys(tidyTargets(clone(b))));
function sortKeys(t) {
  const s = (o) => Object.fromEntries(Object.entries(o).sort(([a], [b]) => a.localeCompare(b)));
  return { default: s(t.default), panels: Object.fromEntries(Object.entries(t.panels).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => [k, s(v)])) };
}

/** The hours the selected panels have as a target for a filter, if they all agree (else null with `mixed`). */
export function commonTarget(targets, ids, filter) {
  const vals = new Set(ids.map((id) => targetFor(targets, id, filter)));
  return vals.size === 1 ? { value: [...vals][0], mixed: false } : { value: null, mixed: vals.size > 1 };
}

// ---- Operations (UpdateMosaicRequest.operations) --------------------------------------------------------------------------------------------

/** One operation for a set of panels, with the shortest selector. `userId` goes into the body and nowhere else. */
export function buildOperation(op, ids, rows, cols, userId) {
  const select = compressSelection(ids, rows, cols);
  return { op, ...(userId && (op === "assign" || op === "release_claims") ? { user_id: userId } : {}), select };
}

/** The request body for one edit of assignments. At most 100 operations; a single edit is one. */
export function operationsBody(operations) {
  if (operations.length > MAX_OPERATIONS) throw new RangeError(`at most ${MAX_OPERATIONS} operations in one request`);
  return { operations };
}

// ---- What a person reads about a claim error -------------------------------------------------------------------------------------------------

/** Wording for a claim attempt that the dashboard is about to send: the 40-claim cap, checked before the request. */
export function claimsAfter(current, id, claim, cap = 40) {
  const set = new Set(current);
  if (claim) set.add(id); else set.delete(id);
  const list = allPanelIdsSorted(set);
  return { list, overCap: list.length > cap };
}
function allPanelIdsSorted(set) {
  return [...set].filter(isPanelId).sort((a, b) => { const pa = parsePanelId(a), pb = parsePanelId(b); return pa.row - pb.row || pa.col - pb.col; });
}

/** { panels, derived } helper: derive a layout once for the page. */
export function deriveForPage(layout) {
  const d = derive(layout);
  return { ...d, byId: new Map(d.panels.map((p) => [p.panel_id, p])) };
}

// ---- A contributor's own panels (the dashboard) --------------------------------------------------------------------------------------------

/** The caller's panels from a MosaicState: assigned ones first, then claims they hold on panels nobody assigned to them; each { id, kind, panel }. */
export function myPanelRows(state) {
  if (!state) return [];
  const byId = new Map(state.panels.map((p) => [p.panel_id, p]));
  const out = [];
  for (const id of state.my_panels) if (byId.has(id)) out.push({ id, kind: "assigned", panel: byId.get(id) });
  for (const id of state.my_claims) if (byId.has(id) && !state.my_panels.includes(id)) out.push({ id, kind: "claimed", panel: byId.get(id) });
  return out;
}

/** Panels a member may claim: not assigned to anyone, not already theirs. Others' claims do not block it (two rigs may share a panel). */
export function claimableOptions(state) {
  if (!state) return [];
  const mine = new Set(state.my_claims);
  return state.panels.filter((p) => !p.assigned_to && !mine.has(p.panel_id)).map((p) => ({ id: p.panel_id, claimedBy: p.claimed_by, label: p.claimed_by.length ? `${p.panel_id} (also claimed by ${p.claimed_by.join(", ")})` : `${p.panel_id} (free)` }));
}

/** What the copy buttons put on the clipboard for a panel: sexagesimal, decimal degrees and the position angle. */
export function panelCopyText(panel) {
  return {
    sexagesimal: `${formatRa(panel.ra_deg)} ${formatDec(panel.dec_deg)}`.replace(/−/g, "-"),
    decimal: `${panel.ra_deg.toFixed(6)} ${panel.dec_deg.toFixed(6)}`,
    angle: panel.pa_deg.toFixed(2),
  };
}

/** The rigs worth advising for: confirmed ones with a known field of view. */
export function adviceRigs(rigs, fovOf) {
  return (rigs ?? []).filter((r) => r.confirmed && fovOf(r));
}

// ---- The public page ---------------------------------------------------------------------------------------------------------------------------

/**
 * How far a panel is towards ALL its targets: the hours it has, capped at each filter's target, over the targets it has.
 * { hours, target (hours or null), fraction (0 to 1, or null with no target) }.
 */
export function overallProgress(entry, id, targets) {
  const filters = new Set([...Object.keys(targets?.default ?? {}), ...Object.keys(targets?.panels?.[id] ?? {})]);
  let want = 0, done = 0;
  for (const f of filters) {
    const t = targetFor(targets, id, f);
    if (!t) continue;
    want += t;
    done += Math.min(hoursIn(entry, f), t);
  }
  return { hours: hoursIn(entry, "all"), target: want || null, fraction: want ? done / want : null };
}

/** Panels with data, most hours first, for the table that stands in for the picture. */
export function panelsWithData(doc) {
  return [...(doc?.panels ?? [])].sort((a, b) => b.integration_s - a.integration_s || a.panel_id.localeCompare(b.panel_id));
}

/** The stream keys of a panel that have a preview of a mode, plus the label order the page lists them in. */
export function previewStreams(entry, mode) {
  return Object.entries(entry?.streams ?? {}).filter(([, s]) => s.previews?.[mode]).map(([k]) => k).sort();
}
