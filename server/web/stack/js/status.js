// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// The project status document (docs/api/status.schema.json) and the view model the pages draw from it.
//
// Where the document comes from (plan 9, docs/api/s3-layout.md):
//   1. {publicBase}/{project_id}/status.json (/stack/public/..., 60 s cache). This is all a logged-out visitor
//      needs, and it is a bare document.
//   2. GET {apiBase}/projects/{project}/status, which wraps the same document as { ok, status_doc }. It is the
//      authoritative copy, the only source for private projects (URLs inside are presigned and absolute), and
//      the fallback when the static file is missing: before the first publish S3 answers 403, which CloudFront
//      turns into the HTML 404 page, so "missing" shows up here as a non-JSON body.
//
// Only fields named in status.schema.json are read, and only by plain property access, so the test that
// replays an example document through a recording Proxy can prove this file consumes nothing the schema lacks.
// Unknown extra fields are ignored (additive changes never bump schema_version); a different schema_version is
// refused rather than guessed at.

import { config } from "./config.js";
import { streamLabel, streamShortLabel } from "./format.js";

export const SUPPORTED_SCHEMA_VERSION = 1;

export class StatusError extends Error {
  /** code: "unsupported_schema" | "malformed" | "unavailable" */
  constructor(code, message, cause) {
    super(message);
    this.name = "StatusError";
    this.code = code;
    if (cause) this.cause = cause;
  }
}

/** Accept either the bare static document or the API's { ok, status_doc } wrapper. */
export function unwrapStatus(body) {
  if (body && typeof body === "object" && body.status_doc && typeof body.status_doc === "object") return body.status_doc;
  return body;
}

/** The document names its files "/public/...", the layout of the original site; here they are served under publicBase. */
export function publicUrl(u, base = config.publicBase) {
  return typeof u === "string" && u.startsWith("/public/") ? base + u.slice("/public".length) : u;
}

function previewSet(p) {
  if (!p || typeof p !== "object") return null;
  return { full: publicUrl(p.full), thumb: publicUrl(p.thumb), updatedAt: p.updated_at };
}

/** A stream's deep-zoom pyramid (contract 1.18.0), or null when it has none or the entry is not usable. */
function deepZoomView(d) {
  if (!d || typeof d !== "object" || typeof d.url !== "string") return null;
  const ints = [d.gold_seq, d.width, d.height, d.tile_size];
  if (!ints.every((n) => Number.isInteger(n) && n > 0) || !Number.isInteger(d.overlap) || d.overlap < 0 || d.overlap >= d.tile_size) return null;
  if (!/^\/public\/.+\/image\.dzi$/.test(d.url) || d.url.includes("..")) return null;
  return { url: publicUrl(d.url), goldSeq: d.gold_seq, width: d.width, height: d.height, tileSize: d.tile_size, overlap: d.overlap, format: String(d.format) };
}

function streamView(s) {
  const by = Array.isArray(s.by_contributor) ? s.by_contributor : [];
  return {
    key: s.stream_key,
    filter: s.filter,
    label: streamLabel(s.stream_key),
    shortLabel: streamShortLabel(s.stream_key),
    sensorKind: s.sensor_kind,
    bandwidthNm: s.bandwidth_nm ?? null,
    framesAccepted: s.frames_accepted,
    framesRejected: s.frames_rejected,
    integrationS: s.integration_s,
    contributors: s.contributors,
    lastUpdate: s.last_update ?? null,
    goldSeq: s.gold_seq ?? null,
    liveN: s.live_n,
    medianFwhm: s.median_fwhm_arcsec ?? null,
    previews: {
      live: previewSet(s.preview_urls?.live),
      gold: previewSet(s.preview_urls?.gold),
    },
    deepZoom: deepZoomView(s.deep_zoom),
    byContributor: by
      .map((c) => ({ name: c.display_name, framesAccepted: c.frames_accepted, integrationS: c.integration_s }))
      .sort((a, b) => b.framesAccepted - a.framesAccepted || a.name.localeCompare(b.name)),
  };
}

/** A contributor's rig (contract 1.26.0): named by camera and telescope only. */
function rigView(r) {
  const streams = Array.isArray(r.streams) ? r.streams : [];
  const reasons = r.rejected_by_reason && typeof r.rejected_by_reason === "object" ? r.rejected_by_reason : {};
  return {
    label: String(r.label),
    framesAccepted: r.frames_accepted,
    framesRejected: r.frames_rejected,
    integrationS: r.integration_s,
    medianFwhm: r.median_fwhm_arcsec ?? null,
    rejectedByReason: Object.entries(reasons).map(([reason, count]) => ({ reason, count })).sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason)),
    streams: streams.map((s) => ({
      key: s.stream_key,
      framesAccepted: s.frames_accepted,
      framesRejected: s.frames_rejected,
      integrationS: s.integration_s,
      medianFwhm: s.median_fwhm_arcsec ?? null,
      weightShare: s.weight_share ?? null,
    })),
  };
}

/** Validate the version, then build the view model. Throws StatusError. */
export function normalizeStatus(input) {
  return buildView(unwrapStatus(input));
}

/** The view model from a bare document. Split out so a test can replay a document through a recording Proxy. */
export function buildView(doc) {
  if (!doc || typeof doc !== "object") throw new StatusError("malformed", "Status document is not an object");
  if (doc.schema_version !== SUPPORTED_SCHEMA_VERSION) {
    throw new StatusError("unsupported_schema", `Status document schema_version ${JSON.stringify(doc.schema_version)} is not supported (expected ${SUPPORTED_SCHEMA_VERSION})`);
  }
  if (!doc.totals || !Array.isArray(doc.streams) || !doc.project) throw new StatusError("malformed", "Status document lacks project, totals or streams");

  const p = doc.project;
  const t = doc.totals;
  const streams = doc.streams.map(streamView);
  const contributors = (Array.isArray(doc.contributors) ? doc.contributors : []).map((c) => ({
    rank: c.rank,
    name: c.display_name,
    country: c.country ?? null,
    integrationS: c.integration_s,
    framesAccepted: c.frames_accepted,
    streams: Array.isArray(c.streams) ? c.streams : [],
    rigs: Array.isArray(c.rigs) ? c.rigs.map(rigView) : [],
  }));
  const timeline = (Array.isArray(doc.timeline) ? doc.timeline : []).map((e) => ({
    ts: e.ts,
    date: e.date,
    kind: e.kind,
    text: e.text,
    streamKey: e.stream_key ?? null,
    goldSeq: e.gold_seq ?? null,
    framesAccepted: e.frames_accepted,
    integrationS: e.integration_s,
    previews: e.previews && typeof e.previews === "object" ? { ...e.previews } : null,
  }));
  // The Progress time-lapse: the stack after each capture night (contract 1.12.0). Absent before the first gold.
  const nights = (Array.isArray(doc.nights) ? doc.nights : [])
    .filter((n) => n && typeof n.night === "string")
    .map((n) => ({
      night: n.night,
      integrationS: n.integration_s ?? 0,
      framesAccepted: n.frames_accepted ?? 0,
      contributors: n.contributors ?? 0,
      sessions: n.sessions ?? 0,
      streams: Object.fromEntries(Object.entries(n.streams && typeof n.streams === "object" ? n.streams : {}).map(([k, v]) => [k, {
        full: v.full, thumb: v.thumb, integrationS: v.integration_s ?? 0, framesAccepted: v.frames_accepted ?? 0, updatedAt: v.updated_at ?? null,
      }])),
    }))
    .sort((a, b) => a.night.localeCompare(b.night));
  const reasons = t.rejected_by_reason && typeof t.rejected_by_reason === "object" ? t.rejected_by_reason : {};

  const view = {
    schemaVersion: doc.schema_version,
    generatedAt: doc.generated_at,
    access: doc.access,
    project: {
      id: p.project_id,
      slug: p.slug,
      name: p.name,
      targetName: p.target_name,
      status: p.status,
      visibility: p.visibility,
      licence: p.licence,
      palette: p.palette,
      filters: Array.isArray(p.filters) ? p.filters : [],
      grid: p.grid
        ? {
            centerRaDeg: p.grid.center_ra_deg,
            centerDecDeg: p.grid.center_dec_deg,
            rotationDeg: p.grid.rotation_deg,
            scaleArcsecPx: p.grid.scale_arcsec_px,
            widthPx: p.grid.width_px,
            heightPx: p.grid.height_px,
          }
        : null,
    },
    totals: {
      framesAccepted: t.frames_accepted,
      framesRejected: t.frames_rejected,
      integrationS: t.integration_s,
      streams: t.streams,
      sessions: t.sessions,
      contributors: t.contributors,
      memberCount: t.member_count,
      lastUpdate: t.last_update ?? null,
      goldSeqMax: t.gold_seq_max ?? null,
    },
    rejections: Object.entries(reasons)
      .map(([reason, count]) => ({ reason, count }))
      .filter((r) => r.count > 0)
      .sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason)),
    streams,
    combined: {
      palette: doc.combined?.palette ?? "none",
      live: previewSet(doc.combined?.live),
      gold: previewSet(doc.combined?.gold),
      // Another palette whose channels are all stacked (contract 1.30.0): an SHO project that also has L, R, G and B shows LRGB too.
      alternates: (Array.isArray(doc.combined?.alternates) ? doc.combined.alternates : [])
        .filter((a) => a && typeof a.palette === "string")
        .map((a) => ({ palette: a.palette, live: previewSet(a.live), gold: previewSet(a.gold) })),
    },
    coverage: doc.coverage ? { url: publicUrl(doc.coverage.url), updatedAt: doc.coverage.updated_at, maxN: doc.coverage.max_n } : null,
    // The index of panels.json, for a project with a mosaic layout (contract 1.17.0); null for a single field.
    panels: doc.panels ? { url: publicUrl(doc.panels.url), updatedAt: doc.panels.updated_at, withData: doc.panels.with_data, total: doc.panels.total } : null,
    contributors,
    timeline,
    nights,
  };
  view.hasLive = streams.some((s) => s.previews.live) || !!view.combined.live;
  view.hasGold = streams.some((s) => s.previews.gold) || !!view.combined.gold;
  view.updatedAt = view.totals.lastUpdate || view.generatedAt;
  return view;
}

/** Every palette image of the project, its own palette first, then any alternate; [] when the palette is "none". Each is { palette, live, gold }. */
export function combinedPalettes(view) {
  const c = view?.combined;
  if (!c || c.palette === "none") return [];
  return [{ palette: c.palette, live: c.live, gold: c.gold }, ...(c.alternates || [])];
}

/** The palette choice to show: a palette the project has, "both" when it has two, else its own palette. */
export function resolvePalette(choice, palettes) {
  const names = palettes.map((p) => p.palette);
  if (choice === "both" && names.length > 1) return "both";
  return names.includes(choice) ? choice : names[0] ?? "none";
}

/** "live" | "gold". Anything else is treated as "live" (the safe default: it always exists first). */
export function normaliseMode(m) {
  return m === "gold" ? "gold" : "live";
}

/** Preview set of a stream (or the combined block) for a mode; null if that mode has not published yet. */
export function previewFor(streamOrCombined, mode) {
  return streamOrCombined?.previews?.[normaliseMode(mode)] ?? streamOrCombined?.[normaliseMode(mode)] ?? null;
}

/** What each mode means, in one sentence each; shown beside every toggle. Plan 13 Q4 requires both be labelled. */
export const MODE_TEXT = {
  live: "Live: the running stack, updated after every batch of accepted frames. Fast, but cosmetic rejection is gentle and the order frames arrived in can show.",
  gold: "Gold: a full re-integration of every accepted frame with proper outlier rejection, published nightly, when enough new frames arrive, or when the owner asks.",
};

/** Short label for a preview: "Live" or "Gold #3". */
export function modeLabel(mode, stream) {
  if (normaliseMode(mode) === "gold") return stream?.goldSeq ? `Gold #${stream.goldSeq}` : "Gold";
  return "Live";
}

/** Group streams by canonical filter, keeping the plan's display order for known filters. */
const ORDER = ["L", "R", "G", "B", "HA", "OIII", "SII", "HB", "NII", "CLEAR", "DUO", "OSC"];
export function sortStreams(streams) {
  const rank = (s) => { const i = ORDER.indexOf(s.filter); return i < 0 ? ORDER.length : i; };
  return [...streams].sort((a, b) => rank(a) - rank(b) || a.key.localeCompare(b.key));
}

/** Find the stream(s) a URL segment names: an exact stream key first, else every stream of that canonical filter. */
export function resolveStreams(view, segment) {
  let want = String(segment ?? "");
  try { want = decodeURIComponent(want); } catch { /* keep the raw segment */ }
  const exact = view.streams.find((s) => s.key === want);
  if (exact) return [exact];
  const up = want.toUpperCase();
  const exactCi = view.streams.find((s) => s.key.toUpperCase() === up);
  if (exactCi) return [exactCi];
  return sortStreams(view.streams.filter((s) => s.filter === up));
}

/** Capture nights on which `streamKey` has an image, oldest first. Each is the night's entry with `image` (that stream's
 *  `{ full, thumb, integrationS, framesAccepted, updatedAt }`) added; the project totals stay on the entry. */
export function nightsFor(view, streamKey) {
  return view.nights
    .filter((n) => n.streams[streamKey])
    .map((n) => ({ ...n, image: n.streams[streamKey] }));
}

/** Streams that have an image on at least one capture night. */
export function nightStreamKeys(view) {
  const keys = new Set();
  for (const n of view.nights) for (const k of Object.keys(n.streams)) keys.add(k);
  return sortStreams(view.streams.filter((s) => keys.has(s.key))).map((s) => s.key);
}

/** Cumulative project integration (hours) by capture night, for timelineChart(). The x axis is the night itself, at noon UTC. */
export function nightsSeries(view) {
  return { points: view.nights.map((n) => ({ t: `${n.night}T12:00:00Z`, v: n.integrationS / 3600, label: n.night })) };
}

/** The timeline as an activity feed, newest first, without the daily snapshots older workers wrote. */
export function timelineToFeed(view) {
  return view.timeline
    .filter((e) => e.kind !== "daily_snapshot")
    .map((e) => ({ ts: e.ts, text: e.text, type: e.kind }))
    .sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts));
}

/** Leaderboard rows with a 0-1 share of the leader's time for the meter. */
export function leaderboard(view) {
  const top = Math.max(...view.contributors.map((c) => c.integrationS), 0);
  return view.contributors.map((c) => ({ ...c, share: top > 0 ? c.integrationS / top : 0 }));
}

/** Whether the document splits any contributor by rig (contract 1.26.0; older documents do not). */
export function hasRigs(view) {
  return view.contributors.some((c) => c.rigs.length > 0);
}

/**
 * Every rig that reached each stream, for the "rigs compared" tables: one entry per stream (in sortStreams order)
 * with its rigs ranked by their share of the stack's weight, then by hours.
 */
export function rigsByStream(view) {
  const per = new Map();
  for (const c of view.contributors) {
    for (const r of c.rigs) {
      for (const s of r.streams) {
        if (!per.has(s.key)) per.set(s.key, []);
        per.get(s.key).push({ contributor: c.name, rig: r.label, ...s });
      }
    }
  }
  const order = sortStreams(view.streams).map((s) => s.key);
  const keys = [...per.keys()].sort((a, b) => {
    const ia = order.indexOf(a), ib = order.indexOf(b);
    return (ia < 0 ? Infinity : ia) - (ib < 0 ? Infinity : ib) || a.localeCompare(b);
  });
  return keys.map((key) => ({
    key,
    label: streamLabel(key),
    shortLabel: streamShortLabel(key),
    rigs: per.get(key).sort((a, b) => (b.weightShare ?? -1) - (a.weightShare ?? -1) || b.integrationS - a.integrationS || a.contributor.localeCompare(b.contributor) || a.rig.localeCompare(b.rig)),
  }));
}

/**
 * Hours per filter split by contributor and rig, for stackedBarChart(): integrationByContributor()'s shape, with a
 * series per (contributor, rig). Used when the document carries rigs.
 */
export function integrationByRig(view, { top = 7 } = {}) {
  const totals = new Map();
  const labels = new Map();
  const hours = new Map();
  for (const c of view.contributors) {
    for (const r of c.rigs) {
      const key = `${c.name}\u0000${r.label}`;
      labels.set(key, `${c.name}: ${r.label}`);
      for (const s of r.streams) {
        totals.set(key, (totals.get(key) || 0) + s.integrationS);
        if (!hours.has(s.key)) hours.set(s.key, []);
        if (s.integrationS > 0) hours.get(s.key).push({ key, value: s.integrationS / 3600 });
      }
    }
  }
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([k]) => k);
  const named = new Set(ranked.slice(0, top));
  const series = ranked.slice(0, top).map((k) => ({ key: k, label: labels.get(k) }));
  if (ranked.length > top) series.push({ key: "\u0000others", label: "Other rigs" });
  const rows = sortStreams(view.streams).map((s) => {
    const segments = [];
    let others = 0;
    for (const seg of hours.get(s.key) || []) {
      if (named.has(seg.key)) segments.push(seg);
      else others += seg.value;
    }
    if (others > 0) segments.push({ key: "\u0000others", value: others });
    return { key: s.key, label: s.shortLabel, fullLabel: s.label, segments };
  });
  return { series, rows };
}

export const LICENCE_LABEL = {
  members_only: "Members only",
  cc_by: "CC BY 4.0",
  cc_by_nc: "CC BY-NC 4.0",
};

// ---- Fetching ----------------------------------------------------------------------------------------------------

/**
 * Load a project's status. Tries the static public file first (unless the project is private, or `preferApi`),
 * then the API. Resolves { view, source: "public" | "api" }; rejects StatusError("unavailable") if neither has it.
 *
 * `api` is the client from api.js (or a stub in tests); `fetchImpl` and `publicBase` are injectable.
 */
export async function loadStatus({ project, api, fetchImpl, publicBase = config.publicBase, preferApi = false }) {
  const f = fetchImpl ?? globalThis.fetch?.bind(globalThis);
  const tryStatic = !preferApi && project.visibility !== "private" && f;
  let staticError = null;

  if (tryStatic) {
    try {
      const res = await f(`${publicBase}/${encodeURIComponent(project.project_id)}/status.json`, { headers: { Accept: "application/json" } });
      if (res.ok) {
        const text = await res.text();
        return { view: normalizeStatus(JSON.parse(text)), source: "public" };
      }
      staticError = new Error(`static status.json answered ${res.status}`);
    } catch (e) {
      // An unsupported schema_version must not be papered over by a second source of the same version.
      if (e instanceof StatusError && e.code === "unsupported_schema") throw e;
      staticError = e;
    }
  }

  try {
    const doc = await api.getStatusDoc(project.project_id);
    return { view: normalizeStatus(doc), source: "api" };
  } catch (e) {
    if (e instanceof StatusError) throw e;
    throw new StatusError("unavailable", "No status has been published for this project yet", e ?? staticError);
  }
}

/**
 * Hours per filter, split by contributor, for stackedBarChart(). The `top` biggest contributors (by total time) get a
 * series each; everyone else is folded into "Others" so the legend stays readable.
 */
export function integrationByContributor(view, { top = 7 } = {}) {
  const totals = new Map();
  for (const s of view.streams) for (const c of s.byContributor) totals.set(c.name, (totals.get(c.name) || 0) + c.integrationS);
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([n]) => n);
  const named = new Set(ranked.slice(0, top));
  const series = ranked.slice(0, top).map((n) => ({ key: n, label: n }));
  const hasOthers = ranked.length > top;
  if (hasOthers) series.push({ key: "\u0000others", label: "Others" });
  const rows = sortStreams(view.streams).map((s) => {
    const segments = [];
    let others = 0;
    for (const c of s.byContributor) {
      if (named.has(c.name)) segments.push({ key: c.name, value: c.integrationS / 3600 });
      else others += c.integrationS / 3600;
    }
    if (others > 0) segments.push({ key: "\u0000others", value: others });
    return { key: s.key, label: s.shortLabel, fullLabel: s.label, segments };
  });
  return { series, rows };
}
