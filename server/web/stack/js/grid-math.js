// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// The project frame (docs/api/openapi.yaml, Grid): a TAN (gnomonic) WCS with the reference pixel at the centre, as plan 4.2
// describes. Pure maths and form parsing, so it runs under node --test; the console draws from it, with or without a sky view.
//
// Convention (docs/api/openapi.yaml, Grid.rotation_deg, settled in open point 26; the engine's ProjectGrid::new is the implementation
// of record, and test/contract-examples.test.mjs holds this file to the corner coordinates in scripts/contract-examples.json):
//   * Pixel offsets (dx, dy) from the centre, x to the right and y UP the image.
//   * At rotation 0 north is up and east is LEFT (the usual sky orientation), so x increases towards the west.
//   * `rotation_deg` is the position angle of the image's up direction, measured from north through east. At 90 the top
//     of the image points east.
//   Tangent-plane offsets, in radians (xi east, eta north):
//       xi  = s * (-dx cos(rot) + dy sin(rot))
//       eta = s * ( dx sin(rot) + dy cos(rot))            with s = scale in radians per pixel
//   and the inverse gnomonic projection about (ra0, dec0) turns (xi, eta) into (ra, dec).

export const MIN_SIDE = 64;
export const MAX_SIDE = 12000; // plan 4.2 and 6.5: a sane maximum, 12 k x 12 k
export const SCALE_MIN = 0.1;
export const SCALE_MAX = 20;
export const ACCUMULATOR_PLANES = 4; // plan 4.3: M, M2, W (float32) and N (u32) per stream

const D2R = Math.PI / 180;
const R2D = 180 / Math.PI;
const ARCSEC = D2R / 3600;

const norm360 = (x) => ((x % 360) + 360) % 360;

/** Tangent-plane offsets (radians) of a pixel offset from the grid centre. */
export function tangentOffsets(grid, dx, dy) {
  const rot = grid.rotation_deg * D2R;
  const s = grid.scale_arcsec_px * ARCSEC;
  return { xi: s * (-dx * Math.cos(rot) + dy * Math.sin(rot)), eta: s * (dx * Math.sin(rot) + dy * Math.cos(rot)) };
}

/** Sky position { ra, dec } (degrees) of a pixel offset from the grid centre. */
export function offsetToSky(grid, dx, dy) {
  const { xi, eta } = tangentOffsets(grid, dx, dy);
  const ra0 = grid.center_ra_deg * D2R, dec0 = grid.center_dec_deg * D2R;
  const rho = Math.hypot(xi, eta);
  if (rho === 0) return { ra: norm360(grid.center_ra_deg), dec: grid.center_dec_deg };
  const c = Math.atan(rho);
  const dec = Math.asin(Math.cos(c) * Math.sin(dec0) + (eta * Math.sin(c) * Math.cos(dec0)) / rho);
  const ra = ra0 + Math.atan2(xi * Math.sin(c), rho * Math.cos(dec0) * Math.cos(c) - eta * Math.sin(dec0) * Math.sin(c));
  return { ra: norm360(ra * R2D), dec: dec * R2D };
}

/** The four corners, bottom-left, bottom-right, top-right, top-left of the image. Straight tangent-plane lines are great circles. */
export function footprint(grid) {
  const hw = grid.width_px / 2, hh = grid.height_px / 2;
  return [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]].map(([dx, dy]) => offsetToSky(grid, dx, dy));
}

/** The outline with `perEdge` extra points on each edge, closed, for a viewer that draws straight segments on the sphere. */
export function outline(grid, perEdge = 6) {
  const hw = grid.width_px / 2, hh = grid.height_px / 2;
  const c = [[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]];
  const pts = [];
  for (let i = 0; i < 4; i++) {
    const [x0, y0] = c[i], [x1, y1] = c[(i + 1) % 4];
    for (let k = 0; k < perEdge; k++) pts.push(offsetToSky(grid, x0 + ((x1 - x0) * k) / perEdge, y0 + ((y1 - y0) * k) / perEdge));
  }
  return pts;
}

/** A short segment from the centre towards the top of the image, to show which way is up. */
export function upMarker(grid) {
  return [offsetToSky(grid, 0, 0), offsetToSky(grid, 0, grid.height_px * 0.42)];
}

/** Field of view of the frame, in degrees. */
export function fieldOfView(grid) {
  const w = (grid.width_px * grid.scale_arcsec_px) / 3600, h = (grid.height_px * grid.scale_arcsec_px) / 3600;
  return { widthDeg: w, heightDeg: h, diagonalDeg: Math.hypot(w, h), areaSqDeg: w * h };
}

/** Great-circle separation of two sky positions, degrees. */
export function separation(a, b) {
  const d1 = a.dec * D2R, d2 = b.dec * D2R, dr = (a.ra - b.ra) * D2R;
  const h = Math.sin((d2 - d1) / 2) ** 2 + Math.cos(d1) * Math.cos(d2) * Math.sin(dr / 2) ** 2;
  return 2 * Math.asin(Math.min(1, Math.sqrt(h))) * R2D;
}

/**
 * What the owner should know about a frame before saving it. `policy` (optional) adds the scale range contributors will be
 * accepted in. level: ok | warn | error (error: the contract would refuse it).
 */
export function sanity(grid, policy) {
  const fov = fieldOfView(grid);
  const px = grid.width_px * grid.height_px;
  const planeBytes = px * 4;
  const longSide = Math.max(grid.width_px, grid.height_px);
  const notes = [];
  let level = "ok";
  const raise = (l, text) => { notes.push({ level: l, text }); if (l === "error") level = "error"; else if (level === "ok") level = l; };
  if (longSide > MAX_SIDE) raise("error", `The longest side is ${longSide.toLocaleString("en-GB")} px; the limit is ${MAX_SIDE.toLocaleString("en-GB")}.`);
  else if (longSide > MAX_SIDE * 0.8) raise("warn", "This is close to the size limit. Every stream keeps several full-size planes and every gold run reads them all.");
  if (px / 1e6 > 100 && longSide <= MAX_SIDE) raise("warn", `${(px / 1e6).toFixed(0)} megapixels is large: a gold run will be slow and storage heavy. A coarser scale or a tighter frame is usually enough.`);
  if (fov.diagonalDeg > 6) raise("warn", `A ${fov.diagonalDeg.toFixed(1)}° diagonal is wide for one tangent-plane frame: stars at the corners are noticeably distorted. Consider a mosaic of projects.`);
  if (grid.scale_arcsec_px < SCALE_MIN || grid.scale_arcsec_px > SCALE_MAX) raise("error", `Scale must be ${SCALE_MIN} to ${SCALE_MAX} arcseconds per pixel.`);
  const out = {
    fov, megapixels: px / 1e6, planeBytes, accumulatorBytes: planeBytes * ACCUMULATOR_PLANES, longSide,
    capFraction: longSide / MAX_SIDE, level, notes,
  };
  if (policy && Number.isFinite(policy.accept_scale_min) && Number.isFinite(policy.accept_scale_max)) {
    out.acceptRange = { min: grid.scale_arcsec_px * policy.accept_scale_min, max: grid.scale_arcsec_px * policy.accept_scale_max };
  }
  return out;
}

// ---- Parsing and formatting coordinates -------------------------------------------------------------------------------------

const num = (s) => (/^[+-]?(\d+\.?\d*|\.\d+)$/.test(String(s).trim()) ? Number(s) : NaN);

/** Split "05h40m59.1s", "05 40 59", "5:40:59", "-02d27m30s" into [a, b, c]; NaN if it is not in that shape. */
function parts(text) {
  const t = String(text ?? "").trim().replace(/[hHdD°]/g, " ").replace(/[mM′']/g, " ").replace(/[sS″"]/g, " ").replace(/:/g, " ").trim();
  if (!t) return [NaN];
  const p = t.split(/\s+/);
  if (p.length > 3) return [NaN];
  return p.map(num);
}

/**
 * Right ascension to degrees. A plain number is DEGREES ("85.2"); anything with separators or h/m/s is HOURS, minutes,
 * seconds ("05 40 59", "5h40m59s"). Returns NaN for anything else.
 */
export function parseRa(text) {
  const t = String(text ?? "").trim();
  if (/^[+-]?(\d+\.?\d*|\.\d+)$/.test(t)) return Number(t);
  const p = parts(t);
  if (p.some(Number.isNaN)) return NaN;
  const [h, m = 0, s = 0] = p;
  if (h < 0 || h >= 24 || m < 0 || m >= 60 || s < 0 || s >= 60) return NaN;
  return (h + m / 60 + s / 3600) * 15;
}

/** Declination to degrees: "-2.45", "-02 27 30", "-2d27m30s", "+41:16:09". */
export function parseDec(text) {
  const t = String(text ?? "").trim();
  if (/^[+-]?(\d+\.?\d*|\.\d+)$/.test(t)) return Number(t);
  const neg = /^\s*-/.test(t);
  const p = parts(t.replace(/^\s*[+-]/, ""));
  if (p.some(Number.isNaN)) return NaN;
  const [d, m = 0, s = 0] = p;
  if (d < 0 || m < 0 || m >= 60 || s < 0 || s >= 60) return NaN;
  const v = d + m / 60 + s / 3600;
  return neg ? -v : v;
}

export function formatRa(deg) {
  if (!Number.isFinite(deg)) return "—";
  const totalS = Math.round((norm360(deg) / 15) * 3600 * 10) / 10;
  const h = Math.floor(totalS / 3600) % 24, m = Math.floor((totalS % 3600) / 60), s = totalS - h * 3600 - m * 60;
  return `${String(h).padStart(2, "0")}h ${String(m).padStart(2, "0")}m ${s.toFixed(1).padStart(4, "0")}s`;
}

export function formatDec(deg) {
  if (!Number.isFinite(deg)) return "—";
  const sign = deg < 0 ? "−" : "+";
  const totalS = Math.round(Math.abs(deg) * 3600 * 10) / 10;
  const d = Math.floor(totalS / 3600), m = Math.floor((totalS % 3600) / 60), s = totalS - d * 3600 - m * 60;
  return `${sign}${String(d).padStart(2, "0")}° ${String(m).padStart(2, "0")}′ ${s.toFixed(1).padStart(4, "0")}″`;
}

// ---- The form ------------------------------------------------------------------------------------------------------------------

const wholeNumber = (x) => { const n = num(x); return Number.isInteger(n) ? n : NaN; };

/**
 * Parse the frame form. `v` holds the raw strings: ra, dec, rot, scale, w, h. Returns { grid } when all six are valid,
 * otherwise { errors: { field: message } } naming every bad field (the form marks each one). `rot` empty means 0.
 */
export function readGridForm(v) {
  const errors = {};
  const ra = parseRa(v.ra), dec = parseDec(v.dec);
  const rot = String(v.rot ?? "").trim() === "" ? 0 : num(v.rot);
  const scale = num(v.scale), w = wholeNumber(v.w), h = wholeNumber(v.h);
  if (String(v.ra ?? "").trim() === "") errors.ra = "Enter the centre's right ascension.";
  else if (!(ra >= 0 && ra < 360)) errors.ra = "Right ascension is 0 to under 360 degrees, or hours minutes seconds such as 05 40 59.";
  if (String(v.dec ?? "").trim() === "") errors.dec = "Enter the centre's declination.";
  else if (!(dec >= -90 && dec <= 90)) errors.dec = "Declination is -90 to 90 degrees, or degrees minutes seconds such as -02 27 30.";
  if (!(rot >= -180 && rot <= 180)) errors.rot = "Rotation is -180 to 180 degrees.";
  if (String(v.scale ?? "").trim() === "") errors.scale = "Enter the scale in arcseconds per pixel.";
  else if (!(scale >= SCALE_MIN && scale <= SCALE_MAX)) errors.scale = `Scale is ${SCALE_MIN} to ${SCALE_MAX} arcseconds per pixel.`;
  if (!(w >= MIN_SIDE && w <= MAX_SIDE)) errors.w = `Width is a whole number from ${MIN_SIDE} to ${MAX_SIDE.toLocaleString("en-GB")} pixels.`;
  if (!(h >= MIN_SIDE && h <= MAX_SIDE)) errors.h = `Height is a whole number from ${MIN_SIDE} to ${MAX_SIDE.toLocaleString("en-GB")} pixels.`;
  if (Object.keys(errors).length) return { errors };
  return { grid: { center_ra_deg: round(ra, 6), center_dec_deg: round(dec, 6), rotation_deg: round(rot, 4), scale_arcsec_px: round(scale, 5), width_px: w, height_px: h } };
}

const round = (x, d) => Math.round(x * 10 ** d) / 10 ** d;

/** Form values from a grid (degrees, as plain numbers). */
export function gridToForm(g) {
  return g ? { ra: String(g.center_ra_deg), dec: String(g.center_dec_deg), rot: String(g.rotation_deg), scale: String(g.scale_arcsec_px), w: String(g.width_px), h: String(g.height_px) } : { ra: "", dec: "", rot: "0", scale: "", w: "", h: "" };
}

/** A position angle folded into (-90, 90]: a frame after a meridian flip reads 180 degrees apart and frames the same field. */
export function foldRotation(pa) {
  let r = ((pa % 180) + 180) % 180;
  if (r > 90) r -= 180;
  return round(r, 2);
}

/**
 * The parts of a project frame a rig gives ("use my camera", contract 1.21.0, open point 94), as form strings: `rot` from
 * the rig's last solved orientation (absent when none was solved yet), `scale` from that solve or else the rig's optics,
 * `w`/`h` from its sensor. Null for a rig that gives nothing. The centre is the target's, never the rig's.
 */
export function rigFrame(rig) {
  if (!rig) return null;
  const o = rig.orientation;
  const optics = rig.focal_length_mm > 0 && rig.pixel_size_um > 0 ? (206.265 * rig.pixel_size_um * (rig.binning || 1)) / rig.focal_length_mm : NaN;
  const scale = [o?.scale_arcsec_px, rig.pixel_scale_arcsec, optics].find((x) => Number.isFinite(x) && x > 0);
  const v = {};
  if (o && Number.isFinite(o.position_angle_deg)) v.rot = String(foldRotation(o.position_angle_deg));
  if (scale) v.scale = String(round(scale, 4));
  if (Number.isInteger(rig.width_px) && Number.isInteger(rig.height_px)) { v.w = String(rig.width_px); v.h = String(rig.height_px); }
  return Object.keys(v).length ? v : null;
}

export const sameGrid = (a, b) => !!a && !!b && ["center_ra_deg", "center_dec_deg", "rotation_deg", "scale_arcsec_px", "width_px", "height_px"].every((k) => a[k] === b[k]);
