// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// Mosaic geometry (docs/api/openapi.yaml `Mosaic`, README open points 76 to 79, MOSAIC_PLAN.md section 4): the panels of a rows x cols
// layout, the project grid derived from it, panel selectors, and the per-rig tiling advice a contributor reads. Pure maths, no DOM,
// so it runs under node --test; test/contract-examples.test.mjs holds `derive()` to the five layouts in scripts/contract-examples.json
// (centres, corners, position angles and the grid size), computed independently of every implementation.
//
// Convention (the contract's, restated):
//   * Panel (row r, column c) is 1-based: row 1 is the TOP of the grid as drawn (its up direction), column 1 the LEFT.
//     Its id is `r{r}c{c}`.
//   * step = panel size * (1 - overlap/100). The panel centre is dx = (c - (cols+1)/2) * step_w to the right and
//     dy = ((rows+1)/2 - r) * step_h upwards of the layout centre, in degrees of tangent plane.
//   * The layout plane is the Grid convention: xi = -dx cos t + dy sin t, eta = dx sin t + dy cos t (t = rotation), then the inverse
//     gnomonic projection about the centre. At rotation 0 north is up and east is LEFT.
//   * `pa_deg` is the position angle of the layout's up direction AT the panel centre (it differs from the rotation by the
//     convergence of the meridians: 20 degrees at 85 N in the shared examples).

const D2R = Math.PI / 180;
const R2D = 180 / Math.PI;
const norm360 = (x) => ((x % 360) + 360) % 360;
/** Wrap to (-180, 180]. */
const wrap180 = (x) => { const y = ((((x + 180) % 360) + 360) % 360) - 180; return y === -180 ? 180 : y; };

export const MAX_ROWS = 25;
export const MAX_COLS = 25;
export const MAX_GRID_EDGE = 130000; // the engine's limit for a derived grid
export const SINGLE_FIELD_EDGE = 12000; // the Grid cap for a grid the owner sets
export const MAX_SPAN_DEG = 60;
export const MARGIN = 1.04; // the grid is the panels' bounding box plus 2 % each side
export const MAX_CLAIMS = 40;
export const MAX_OPERATIONS = 100;

export const panelId = (row, col) => `r${row}c${col}`;
const ID_RE = /^r([1-9]|1[0-9]|2[0-5])c([1-9]|1[0-9]|2[0-5])$/;
/** { row, col } for a panel id, or null. */
export function parsePanelId(id) {
  const m = ID_RE.exec(String(id));
  return m ? { row: Number(m[1]), col: Number(m[2]) } : null;
}
export const isPanelId = (id) => ID_RE.test(String(id));

/** Every panel id of a rows x cols layout, row-major (row 1 first, left to right). */
export function allPanelIds(rows, cols) {
  const out = [];
  for (let r = 1; r <= rows; r++) for (let c = 1; c <= cols; c++) out.push(panelId(r, c));
  return out;
}

// ---- Layout checks ------------------------------------------------------------------------------------------------------------

const isNum = (x) => typeof x === "number" && Number.isFinite(x);

/** The step between panel centres, in degrees. */
export function stepsOf(layout) {
  const k = 1 - layout.overlap_pct / 100;
  return { stepW: layout.panel_w_deg * k, stepH: layout.panel_h_deg * k };
}

/** Size in degrees of the bounding box of the panels plus the 2 % margins: what the derived grid covers. */
export function extentOf(layout) {
  const { stepW, stepH } = stepsOf(layout);
  return { widthDeg: (layout.cols - 1) * stepW + MARGIN * layout.panel_w_deg, heightDeg: (layout.rows - 1) * stepH + MARGIN * layout.panel_h_deg };
}

/**
 * Field-by-field check of a layout against the contract's ranges. Returns [{ field, message }] (empty when valid). The derived grid
 * is checked too: more than 130 000 px on an edge, or a span over 60 degrees, is what the server refuses as `validation_failed`.
 */
export function validateLayout(layout) {
  const errs = [];
  const bad = (field, message) => errs.push({ field, message });
  if (!layout || typeof layout !== "object" || Array.isArray(layout)) return [{ field: "layout", message: "A layout is an object." }];
  const int = (k, lo, hi) => { if (!(Number.isInteger(layout[k]) && layout[k] >= lo && layout[k] <= hi)) bad(k, `A whole number from ${lo} to ${hi}.`); };
  const num = (k, lo, hi, exHi = false) => { if (!(isNum(layout[k]) && layout[k] >= lo && (exHi ? layout[k] < hi : layout[k] <= hi))) bad(k, `A number from ${lo} to ${hi}${exHi ? " (not including " + hi + ")" : ""}.`); };
  int("rows", 1, MAX_ROWS); int("cols", 1, MAX_COLS);
  num("overlap_pct", 0, 50); num("panel_w_deg", 0.05, 20); num("panel_h_deg", 0.05, 20);
  num("rotation_deg", -180, 180); num("scale_arcsec_px", 0.1, 20);
  const c = layout.centre;
  if (!c || typeof c !== "object") bad("centre", "A centre with ra_deg and dec_deg.");
  else {
    if (!(isNum(c.ra_deg) && c.ra_deg >= 0 && c.ra_deg < 360)) bad("centre.ra_deg", "0 to under 360 degrees.");
    if (!(isNum(c.dec_deg) && c.dec_deg >= -90 && c.dec_deg < 90)) bad("centre.dec_deg", "-90 to under 90 degrees.");
  }
  if (errs.length) return errs;
  const ext = extentOf(layout);
  if (Math.max(ext.widthDeg, ext.heightDeg) > MAX_SPAN_DEG) bad("layout", `The layout spans ${Math.max(ext.widthDeg, ext.heightDeg).toFixed(1)} degrees; the limit is ${MAX_SPAN_DEG}.`);
  const g = derivedGrid(layout);
  if (Math.max(g.width_px, g.height_px) > MAX_GRID_EDGE) bad("layout", `The project frame would be ${g.width_px.toLocaleString("en-GB")} by ${g.height_px.toLocaleString("en-GB")} px; the limit is ${MAX_GRID_EDGE.toLocaleString("en-GB")} on an edge. Use a coarser scale.`);
  return errs;
}

// ---- Projection -----------------------------------------------------------------------------------------------------------------

/** Precomputed trigonometry of the layout centre, so 625 panels cost 625 projections and not 625 setups. */
function frameOf(layout) {
  const a0 = layout.centre.ra_deg * D2R, d0 = layout.centre.dec_deg * D2R, t = layout.rotation_deg * D2R;
  return { a0, d0, sa: Math.sin(a0), ca: Math.cos(a0), sd: Math.sin(d0), cd: Math.cos(d0), st: Math.sin(t), ct: Math.cos(t) };
}

/** Layout-plane offsets (dx right, dy up; degrees) to tangent-plane (xi east, eta north; radians). */
function toTangent(f, dxDeg, dyDeg) {
  const dx = dxDeg * D2R, dy = dyDeg * D2R;
  return { xi: -dx * f.ct + dy * f.st, eta: dx * f.st + dy * f.ct };
}

/** Inverse gnomonic projection about the frame's centre: { ra, dec } in degrees. */
function inverseGnomonic(f, xi, eta) {
  const rho = Math.hypot(xi, eta);
  if (rho === 0) return { ra: norm360(f.a0 * R2D), dec: f.d0 * R2D };
  const c = Math.atan(rho), sc = Math.sin(c), cc = Math.cos(c);
  const dec = Math.asin(cc * f.sd + (eta * sc * f.cd) / rho);
  const ra = f.a0 + Math.atan2(xi * sc, rho * f.cd * cc - eta * f.sd * sc);
  return { ra: norm360(ra * R2D), dec: dec * R2D };
}

const unit = (ra, dec) => [Math.cos(dec) * Math.cos(ra), Math.cos(dec) * Math.sin(ra), Math.sin(dec)];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

/** Position angle (degrees, north through east) of the layout's up direction at the panel whose tangent offsets are (xi, eta). */
function paAt(f, xi, eta, ra, dec) {
  const c = unit(f.a0, f.d0);
  const e = [-f.sa, f.ca, 0];
  const n = [-f.sd * f.ca, -f.sd * f.sa, f.cd];
  const v = [c[0] + xi * e[0] + eta * n[0], c[1] + xi * e[1] + eta * n[1], c[2] + xi * e[2] + eta * n[2]];
  const w = [f.st * e[0] + f.ct * n[0], f.st * e[1] + f.ct * n[1], f.st * e[2] + f.ct * n[2]];
  const k = dot(v, w) / dot(v, v);
  const d = [w[0] - k * v[0], w[1] - k * v[1], w[2] - k * v[2]];
  const a = ra * D2R, dd = dec * D2R;
  const ep = [-Math.sin(a), Math.cos(a), 0];
  const np = [-Math.sin(dd) * Math.cos(a), -Math.sin(dd) * Math.sin(a), Math.cos(dd)];
  const pa = Math.atan2(dot(d, ep), dot(d, np)) * R2D;
  return pa === 0 ? 0 : pa;
}

/** The project grid (`Grid`) derived from a layout: centred on it, same rotation and the owner's scale, the panels' bounding box plus 2 %. */
export function derivedGrid(layout) {
  const ext = extentOf(layout);
  const side = (deg) => 2 * Math.ceil((deg * 3600) / layout.scale_arcsec_px / 2 - 1e-9);
  return {
    center_ra_deg: layout.centre.ra_deg, center_dec_deg: layout.centre.dec_deg, rotation_deg: layout.rotation_deg,
    scale_arcsec_px: layout.scale_arcsec_px, width_px: side(ext.widthDeg), height_px: side(ext.heightDeg),
  };
}

/**
 * Every panel of a layout and the project grid: { grid, panels }, panels row-major. A panel is
 * { panel_id, row, col, dx, dy (layout-plane offsets, degrees), ra_deg, dec_deg, pa_deg, corners: [{ra_deg, dec_deg} x 4] }
 * with corners bottom-left, bottom-right, top-right, top-left as drawn.
 */
export function derive(layout) {
  const f = frameOf(layout);
  const { stepW, stepH } = stepsOf(layout);
  const hw = layout.panel_w_deg / 2, hh = layout.panel_h_deg / 2;
  const panels = [];
  for (let row = 1; row <= layout.rows; row++) {
    for (let col = 1; col <= layout.cols; col++) {
      const dx = (col - (layout.cols + 1) / 2) * stepW;
      const dy = ((layout.rows + 1) / 2 - row) * stepH;
      const { xi, eta } = toTangent(f, dx, dy);
      const centre = inverseGnomonic(f, xi, eta);
      const corners = [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]].map(([ox, oy]) => {
        const t = toTangent(f, dx + ox, dy + oy);
        const p = inverseGnomonic(f, t.xi, t.eta);
        return { ra_deg: p.ra, dec_deg: p.dec };
      });
      panels.push({ panel_id: panelId(row, col), row, col, dx, dy, ra_deg: centre.ra, dec_deg: centre.dec, pa_deg: paAt(f, xi, eta, centre.ra, centre.dec), corners });
    }
  }
  return { grid: derivedGrid(layout), panels };
}

/** Sky position of a point given in a panel's own frame (dx right, dy up, degrees), the frame rotated by the panel's position angle. */
export function panelOffsetToSky(panel, dxDeg, dyDeg, paDeg = panel.pa_deg) {
  const f = { a0: panel.ra_deg * D2R, d0: panel.dec_deg * D2R, sd: Math.sin(panel.dec_deg * D2R), cd: Math.cos(panel.dec_deg * D2R), st: Math.sin(paDeg * D2R), ct: Math.cos(paDeg * D2R) };
  const t = toTangent(f, dxDeg, dyDeg);
  return inverseGnomonic(f, t.xi, t.eta);
}

// ---- Selectors (PanelSelector) -----------------------------------------------------------------------------------------------------

/**
 * The panel ids a PanelSelector names: the union of `panels`, `rows`, `cols` and inclusive `rects`, row-major, no duplicates.
 * Throws a RangeError for a row or column outside the layout, as the server answers `validation_failed`.
 */
export function expandSelector(sel, rows, cols) {
  const set = new Set();
  const inR = (r) => Number.isInteger(r) && r >= 1 && r <= rows;
  const inC = (c) => Number.isInteger(c) && c >= 1 && c <= cols;
  for (const id of sel?.panels ?? []) {
    const p = parsePanelId(id);
    if (!p || !inR(p.row) || !inC(p.col)) throw new RangeError(`panel ${id} is outside the ${rows} x ${cols} layout`);
    set.add(id);
  }
  for (const r of sel?.rows ?? []) {
    if (!inR(r)) throw new RangeError(`row ${r} is outside the layout`);
    for (let c = 1; c <= cols; c++) set.add(panelId(r, c));
  }
  for (const c of sel?.cols ?? []) {
    if (!inC(c)) throw new RangeError(`column ${c} is outside the layout`);
    for (let r = 1; r <= rows; r++) set.add(panelId(r, c));
  }
  for (const q of sel?.rects ?? []) {
    const { row_from: r0, row_to: r1, col_from: c0, col_to: c1 } = q;
    if (![r0, r1].every(inR) || ![c0, c1].every(inC)) throw new RangeError("a rectangle reaches outside the layout");
    for (let r = Math.min(r0, r1); r <= Math.max(r0, r1); r++) for (let c = Math.min(c0, c1); c <= Math.max(c0, c1); c++) set.add(panelId(r, c));
  }
  return allPanelIds(rows, cols).filter((id) => set.has(id));
}

/**
 * The most compact PanelSelector for a set of panel ids: whole rows and whole columns as `rows` / `cols`, the rest as rectangles
 * (runs of columns shared by adjacent rows), and single ids only when more than 50 rectangles would be needed. 625 selected panels
 * are `rows: [1..25]`, never 625 ids. expandSelector(compressSelection(s)) is s.
 */
export function compressSelection(ids, rows, cols) {
  const chosen = new Set();
  for (const id of ids) { const p = parsePanelId(id); if (p && p.row <= rows && p.col <= cols) chosen.add(id); }
  if (!chosen.size) return {};
  const fullRows = [], fullCols = [];
  for (let r = 1; r <= rows; r++) { let all = true; for (let c = 1; c <= cols && all; c++) all = chosen.has(panelId(r, c)); if (all) fullRows.push(r); }
  for (let c = 1; c <= cols; c++) { let all = true; for (let r = 1; r <= rows && all; r++) all = chosen.has(panelId(r, c)); if (all) fullCols.push(c); }
  const rowSet = new Set(fullRows), colSet = new Set(fullCols);
  const out = {};
  if (chosen.size === rows * cols) return { rows: fullRows }; // everything: one list, whatever else is full
  const rest = [];
  for (let r = 1; r <= rows; r++) {
    if (rowSet.has(r)) continue;
    // runs of columns not already covered by a whole column
    let c = 1;
    while (c <= cols) {
      if (!chosen.has(panelId(r, c)) || colSet.has(c)) { c++; continue; }
      let e = c;
      while (e + 1 <= cols && chosen.has(panelId(r, e + 1)) && !colSet.has(e + 1)) e++;
      rest.push({ r, c0: c, c1: e });
      c = e + 1;
    }
  }
  // Merge runs that repeat in consecutive rows into rectangles.
  const rects = [];
  const open = new Map(); // "c0:c1" -> rect still growing
  const byRow = new Map();
  for (const run of rest) { if (!byRow.has(run.r)) byRow.set(run.r, []); byRow.get(run.r).push(run); }
  for (let r = 1; r <= rows; r++) {
    const here = new Map((byRow.get(r) ?? []).map((x) => [`${x.c0}:${x.c1}`, x]));
    for (const [key, rect] of [...open]) if (!here.has(key)) { rects.push(rect); open.delete(key); }
    for (const [key, x] of here) {
      if (open.has(key)) open.get(key).row_to = r;
      else open.set(key, { row_from: r, col_from: x.c0, row_to: r, col_to: x.c1 });
    }
  }
  rects.push(...open.values());
  if (fullRows.length) out.rows = fullRows;
  if (fullCols.length) out.cols = fullCols;
  if (rects.length <= 50) {
    if (rects.length) out.rects = rects.sort((a, b) => a.row_from - b.row_from || a.col_from - b.col_from);
  } else {
    // Too many pieces for the rect list: name the leftover panels one by one (at most 625).
    const covered = new Set(expandSelector({ rows: fullRows, cols: fullCols }, rows, cols));
    out.panels = [...chosen].filter((id) => !covered.has(id));
  }
  return out;
}

// ---- Looking things up on the sky ---------------------------------------------------------------------------------------------------

/** The panel whose centre is nearest a point of the layout plane (dx right, dy up; degrees), or null when it is outside the bounding box of the panels. */
export function panelAtOffset(layout, dx, dy) {
  const { stepW, stepH } = stepsOf(layout);
  const sw = stepW || layout.panel_w_deg, sh = stepH || layout.panel_h_deg;
  const halfW = ((layout.cols - 1) * sw + layout.panel_w_deg) / 2, halfH = ((layout.rows - 1) * sh + layout.panel_h_deg) / 2;
  if (Math.abs(dx) > halfW || Math.abs(dy) > halfH) return null;
  const col = Math.min(layout.cols, Math.max(1, Math.round(dx / sw + (layout.cols + 1) / 2)));
  const row = Math.min(layout.rows, Math.max(1, Math.round((layout.rows + 1) / 2 - dy / sh)));
  return { row, col, panel_id: panelId(row, col) };
}

/**
 * Where each panel sits on the project grid's image, as fractions of its width and height (0 at the left / top), for drawing the outlines over the
 * coverage map: { panel_id, x0, y0, x1, y1 }. The image is the grid (the layout plus its margin), so the maths is the layout plane scaled to the grid.
 */
export function panelRectsOnGrid(layout, grid = derivedGrid(layout)) {
  const { stepW, stepH } = stepsOf(layout);
  const wDeg = (grid.width_px * grid.scale_arcsec_px) / 3600, hDeg = (grid.height_px * grid.scale_arcsec_px) / 3600;
  const out = [];
  for (let row = 1; row <= layout.rows; row++) {
    for (let col = 1; col <= layout.cols; col++) {
      const dx = (col - (layout.cols + 1) / 2) * stepW, dy = ((layout.rows + 1) / 2 - row) * stepH;
      out.push({
        panel_id: panelId(row, col), row, col,
        x0: 0.5 + (dx - layout.panel_w_deg / 2) / wDeg, x1: 0.5 + (dx + layout.panel_w_deg / 2) / wDeg,
        y0: 0.5 - (dy + layout.panel_h_deg / 2) / hDeg, y1: 0.5 - (dy - layout.panel_h_deg / 2) / hDeg,
      });
    }
  }
  return out;
}

// ---- Per-rig tiling advice -----------------------------------------------------------------------------------------------------------

/** Arcseconds per pixel of a rig (the stored value, else focal length, pixel size and binning), or null. */
export function rigScale(rig) {
  if (isNum(rig?.pixel_scale_arcsec) && rig.pixel_scale_arcsec > 0) return rig.pixel_scale_arcsec;
  if (isNum(rig?.focal_length_mm) && rig.focal_length_mm > 0 && isNum(rig?.pixel_size_um) && rig.pixel_size_um > 0) return (206.2648 * rig.pixel_size_um * (rig.binning || 1)) / rig.focal_length_mm;
  return null;
}

/** Field of view of a rig in degrees ({ widthDeg, heightDeg, scale }), or null while the sensor size is unknown. Sensor size is the frame size as captured (binned). */
export function rigFov(rig) {
  const scale = rigScale(rig);
  if (scale === null || !(rig?.width_px > 0) || !(rig?.height_px > 0)) return null;
  return { widthDeg: (rig.width_px * scale) / 3600, heightDeg: (rig.height_px * scale) / 3600, scale };
}

/** Pointings (count and offsets, degrees from the centre, evenly spread, first and last flush with the edges) that cover `span` with a frame `fov` wide. */
function axisPlan(span, fov, overlapPct) {
  if (fov >= span - 1e-9) return { n: 1, offsets: [0], step: 0 };
  const maxStep = fov * (1 - overlapPct / 100);
  const n = 1 + Math.ceil((span - fov) / maxStep - 1e-9);
  const step = (span - fov) / (n - 1);
  return { n, offsets: Array.from({ length: n }, (_, i) => (i - (n - 1) / 2) * step), step };
}

function planFor(spanW, spanH, fovW, fovH, overlapPct) {
  const x = axisPlan(spanW, fovW, overlapPct), y = axisPlan(spanH, fovH, overlapPct);
  const pts = [];
  // Top row first, left to right: the order a person shoots a mosaic in.
  for (let j = y.n - 1; j >= 0; j--) for (let i = 0; i < x.n; i++) pts.push({ col: i + 1, row: y.n - j, dx: x.offsets[i], dy: y.offsets[j] });
  return { cols: x.n, rows: y.n, count: pts.length, pts, overlapX: x.n > 1 ? 1 - x.step / fovW : null, overlapY: y.n > 1 ? 1 - y.step / fovH : null };
}

/**
 * How a contributor frames one panel. `panel` is a derived panel (ra_deg, dec_deg, pa_deg) of a layout whose panels are `panelW` x `panelH` degrees; `overlapPct`
 * is the PROJECT'S overlap, applied between neighbouring pointings of the rig's own field of view; `rig` is a Rig (focal length, pixel size, binning, frame size).
 *
 *   canRotate true (default): the camera is turned to the panel's position angle (or a quarter turn from it, whichever needs fewer
 *     pointings), so the pointings form a cols x rows grid in the panel's own frame and every pointing centre lies inside the panel.
 *   canRotate false: the camera stays at `fixedPaDeg` (the angle its top points, default 0 = north up). The panel is then turned by the difference to the camera's
 *     frame, and the advice covers that rotated rectangle's bounding box, a conservative cover that never needs fewer pointings than the aligned one.
 *
 * Returns { ok: true, fov, orientation, camera_pa_deg, cols, rows, count, pointings: [{ n, name, col, row, ra_deg, dec_deg }], overlap, notes }
 * or { ok: false, reason } while the rig cannot be sized.
 */
export function tilingAdvice({ panel, panelW, panelH, overlapPct, rig, canRotate = true, fixedPaDeg = 0 }) {
  const fov = rigFov(rig);
  if (!fov) return { ok: false, reason: "The rig's focal length, pixel size or frame size is not known yet, so its field of view cannot be worked out. Upload a frame with it, or confirm the rig in the plugin." };
  if (!(panelW > 0) || !(panelH > 0)) return { ok: false, reason: "The panel has no size." };
  const notes = [];
  let orientation, cameraPa, plan, frame;
  if (canRotate) {
    const a = planFor(panelW, panelH, fov.widthDeg, fov.heightDeg, overlapPct);
    const b = planFor(panelW, panelH, fov.heightDeg, fov.widthDeg, overlapPct);
    // Fewer pointings wins; a tie keeps the camera upright.
    if (b.count < a.count) { plan = b; orientation = "quarter-turn"; cameraPa = wrap180(panel.pa_deg + 90); }
    else { plan = a; orientation = "aligned"; cameraPa = wrap180(panel.pa_deg); }
    frame = panel.pa_deg;
    notes.push(orientation === "aligned" ? `Rotate the camera to ${cameraPa.toFixed(1)}°, the panel's position angle.` : `Rotate the camera to ${cameraPa.toFixed(1)}° (a quarter turn from the panel's angle): it needs fewer pointings that way.`);
  } else {
    const delta = (panel.pa_deg - fixedPaDeg) * D2R;
    const c = Math.abs(Math.cos(delta)), s = Math.abs(Math.sin(delta));
    const bw = panelW * c + panelH * s, bh = panelW * s + panelH * c;
    plan = planFor(bw, bh, fov.widthDeg, fov.heightDeg, overlapPct);
    orientation = "fixed"; cameraPa = wrap180(fixedPaDeg); frame = fixedPaDeg;
    const off = Math.abs(wrap180(panel.pa_deg - fixedPaDeg));
    notes.push(off < 0.5 ? "The camera angle already matches the panel's." : `The camera cannot rotate: the panel is ${off.toFixed(1)}° off the camera's angle, so the pointings cover its bounding box and some overlap the neighbours more than needed.`);
  }
  if (plan.count === 1) notes.push(fov.widthDeg >= panelW && fov.heightDeg >= panelH ? "One pointing frames the whole panel with room to spare." : "One pointing covers the panel.");
  const pointings = plan.pts.map((p, i) => {
    // Offsets are in the frame the plan was laid out in: the panel's own (a rotatable camera) or the fixed camera's.
    const sky = panelOffsetToSky(panel, p.dx, p.dy, frame);
    return { n: i + 1, name: `${panel.panel_id}-${i + 1}`, col: p.col, row: p.row, ra_deg: sky.ra, dec_deg: sky.dec };
  });
  return {
    ok: true, fov, orientation, camera_pa_deg: cameraPa, cols: plan.cols, rows: plan.rows, count: plan.count, pointings,
    overlap: { x: plan.overlapX, y: plan.overlapY, wanted: overlapPct / 100 }, notes,
  };
}

/** The pointings as CSV (name, decimal and sexagesimal coordinates, camera angle) for a spreadsheet or a sequence importer. */
export function pointingsCsv(advice, fmt) {
  const q = (s) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);
  const head = "name,ra_deg,dec_deg,ra_hms,dec_dms,position_angle_deg";
  if (!advice?.ok) return head + "\n";
  const rows = advice.pointings.map((p) => [p.name, p.ra_deg.toFixed(6), p.dec_deg.toFixed(6), fmt.ra(p.ra_deg), fmt.dec(p.dec_deg), advice.camera_pa_deg.toFixed(2)].map(String).map(q).join(","));
  return [head, ...rows].join("\n") + "\n";
}
