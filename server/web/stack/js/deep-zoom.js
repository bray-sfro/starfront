// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// Deep zoom (MOSAIC_PLAN.md phase 6, contract 1.18.0, docs/api README open points 84 to 90): the pure parts. Which streams have a
// pyramid, the Deep Zoom (DZI) arithmetic the worker and OpenSeadragon agree on, the tile source handed to the viewer, the sentences
// the page says about a pyramid, and the lazy loader of the vendored OpenSeadragon. No DOM beyond the loader's script element, so
// this runs under node --test; the viewer component is components/deep-zoom.js.
//
// The arithmetic mirrors worker/crates/engine/src/pipeline/deepzoom.rs (`max_level`, `level_size`, `tile_counts`, `tile_rect`,
// `dzi_xml`); test/deep-zoom.test.mjs pins the same vectors as the Rust tests, so the two cannot drift unnoticed.

import { sortStreams } from "./status.js";
import { formatInt, streamLabel } from "./format.js";

/** The vendored OpenSeadragon (js/vendor/openseadragon/NOTICE.txt). Same origin, so the CSP's `script-src 'self'` covers it. */
export const OSD_URL = "/stack/js/vendor/openseadragon/openseadragon.min.js";
export const OSD_VERSION = "4.1.1";

// ---- The Deep Zoom arithmetic ----------------------------------------------------------------------------------------------

/** The highest level: ceil(log2(max(w, h))); level 0 is one pixel. */
export function maxLevel(w, h) {
  const m = Math.max(w, h, 1);
  return m <= 1 ? 0 : Math.ceil(Math.log2(m));
}

/** Size of `level` of a w x h image: ceil(w / 2^(max - level)), at least 1. */
export function levelSize(w, h, level) {
  const div = 2 ** Math.max(0, maxLevel(w, h) - level);
  return [Math.max(1, Math.ceil(w / div)), Math.max(1, Math.ceil(h / div))];
}

/** Tiles across and down at a level of lw x lh pixels. */
export function tileCounts(lw, lh, tile) {
  return [Math.ceil(lw / tile), Math.ceil(lh / tile)];
}

/** The pixel rectangle {x, y, w, h} of tile (col, row), overlap included: no overlap at the image's edge. */
export function tileRect(lw, lh, tile, overlap, col, row) {
  const span = (i, size) => {
    const a = i === 0 ? 0 : i * tile - overlap;
    const b = Math.min((i + 1) * tile + overlap, size);
    return [a, b - a];
  };
  const [x, w] = span(col, lw);
  const [y, h] = span(row, lh);
  return { x, y, w, h };
}

/** The descriptor the worker publishes as image.dzi. */
export function dziXml(w, h, tile, overlap, format = "jpg") {
  return `<?xml version="1.0" encoding="UTF-8"?>\n<Image xmlns="http://schemas.microsoft.com/deepzoom/2008" Format="${format}" Overlap="${overlap}" TileSize="${tile}"><Size Width="${w}" Height="${h}"/></Image>\n`;
}

/** Tiles in a fully covered pyramid (an upper bound: tiles with no data are not written). */
export function fullTileCount(w, h, tile) {
  let n = 0;
  for (let l = 0; l <= maxLevel(w, h); l++) {
    const [c, r] = tileCounts(...levelSize(w, h, l), tile);
    n += c * r;
  }
  return n;
}

// ---- Which streams, what to open ------------------------------------------------------------------------------------------------

/** The streams of a status view that have a pyramid, in the page's filter order. */
export function deepZoomStreams(view) {
  return sortStreams((view?.streams ?? []).filter((s) => s.deepZoom));
}

/** The stream to open: `preferred` if it has a pyramid, else the first that does, else null. */
export function pickStream(streams, preferred) {
  return streams.find((s) => s.key === preferred) ?? streams[0] ?? null;
}

/** The list's key for the combined preview (no stream key is lower-case). */
export const PREVIEW_KEY = "preview";

/**
 * The combined (colour) preview as a choice beside the pyramids: the auto-stretched picture of the project page's "Combined preview", opened
 * as one image in the same viewer. Gold once it has published, else live; null when the project has no palette or nothing is published yet.
 */
export function previewChoice(view) {
  const c = view?.combined;
  if (!c || c.palette === "none") return null;
  const mode = c.gold?.full ? "gold" : c.live?.full ? "live" : null;
  if (!mode) return null;
  return { key: PREVIEW_KEY, preview: { url: c[mode].full, mode, palette: c.palette, goldSeq: mode === "gold" ? view.totals?.goldSeqMax ?? null : null } };
}

/** What the viewer's list offers: every pyramid, in the page's filter order, then the combined preview as "Preview". */
export function viewerChoices(view) {
  const p = previewChoice(view);
  return p ? [...deepZoomStreams(view), p] : deepZoomStreams(view);
}

/** The list's text for a choice. */
export const choiceLabel = (choice) => (choice.preview ? "Preview" : streamLabel(choice.key));

/** What OpenSeadragon opens for a choice: a pyramid's tile source, or the preview as a single image (it builds its own levels from it). */
export function sourceFor(choice) {
  return choice.preview ? { type: "image", url: choice.preview.url } : tileSource(choice.deepZoom);
}

/**
 * The tile source OpenSeadragon takes for a pyramid, built from the status entry instead of fetching and parsing image.dzi: the
 * same fields the descriptor carries, one request fewer, and nothing to go wrong with the descriptor's content type. The
 * descriptor is still published for any other viewer.
 */
export function tileSource(dz) {
  return {
    Image: {
      xmlns: "http://schemas.microsoft.com/deepzoom/2008",
      Url: dz.url.replace(/image\.dzi$/, "image_files/"),
      Format: dz.format,
      Overlap: String(dz.overlap),
      TileSize: String(dz.tileSize),
      Size: { Width: String(dz.width), Height: String(dz.height) },
    },
  };
}

/** A key that changes exactly when the viewer has to open another pyramid (or another preview picture). */
export const sourceKey = (stream) => (stream?.preview ? `${PREVIEW_KEY}|${stream.preview.url}` : stream?.deepZoom ? `${stream.key}|${stream.deepZoom.url}` : "");

/** "11 240 × 8 408 px, gold stack #3" and, when a newer gold exists, a sentence about it; for the preview, which picture it is. */
export function describe(stream) {
  if (stream.preview) {
    const p = stream.preview;
    const which = p.mode === "gold" ? `gold stack${p.goldSeq ? ` #${p.goldSeq}` : ""}` : "live stack";
    return { size: "", text: `Preview: the ${p.palette} combined preview of the ${which}, auto-stretched, at its published size.`, behind: "" };
  }
  const dz = stream.deepZoom;
  const size = `${formatInt(dz.width)} × ${formatInt(dz.height)} px`;
  const behind = stream.goldSeq && stream.goldSeq > dz.goldSeq;
  return {
    size,
    text: `${streamLabel(stream.key)}: ${size}, gold stack #${dz.goldSeq}.`,
    behind: behind ? `Gold stack #${stream.goldSeq} is newer; its full-resolution view is still being prepared.` : "",
  };
}

// ---- The loader ------------------------------------------------------------------------------------------------------------------

export class OsdUnavailable extends Error {
  /** reason: "load_failed" | "timeout" | "no_api" */
  constructor(reason, message) {
    super(message);
    this.name = "OsdUnavailable";
    this.reason = reason;
  }
}

export const UNAVAILABLE_TEXT = {
  load_failed: "The zoom viewer could not be loaded. The 2048 px preview above shows the whole stack.",
  timeout: "The zoom viewer took too long to load. The 2048 px preview above shows the whole stack.",
  no_api: "The zoom viewer loaded but did not start in this browser. The 2048 px preview above shows the whole stack.",
};

let pending = null;

/**
 * Resolves to OpenSeadragon's global once the vendored script is in. Lazy (nothing is requested before a visitor opens the
 * section) and once per page; a failure is remembered for the page, not retried on every click. `opts` are for tests:
 * { url, timeoutMs, doc, win, fresh }.
 */
export function loadOpenSeadragon(opts = {}) {
  const url = opts.url ?? OSD_URL;
  const doc = opts.doc ?? globalThis.document;
  const win = opts.win ?? globalThis.window ?? globalThis;
  if (pending && !opts.fresh) return pending;
  pending = new Promise((resolve, reject) => {
    if (typeof win.OpenSeadragon === "function" && win.OpenSeadragon.Viewer) { resolve(win.OpenSeadragon); return; }
    const el = doc.createElement("script");
    el.src = url;
    el.async = true;
    el.referrerPolicy = "no-referrer";
    const timer = setTimeout(() => { el.remove?.(); reject(new OsdUnavailable("timeout", UNAVAILABLE_TEXT.timeout)); }, opts.timeoutMs ?? 20000);
    el.onload = () => {
      clearTimeout(timer);
      const O = win.OpenSeadragon;
      if (typeof O !== "function" || !O.Viewer) reject(new OsdUnavailable("no_api", UNAVAILABLE_TEXT.no_api));
      else resolve(O);
    };
    el.onerror = () => { clearTimeout(timer); reject(new OsdUnavailable("load_failed", UNAVAILABLE_TEXT.load_failed)); };
    doc.head.appendChild(el);
  });
  pending.catch(() => {}); // remembered, never an unhandled rejection
  return pending;
}
