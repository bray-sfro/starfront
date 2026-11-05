// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// Formatting and small pure helpers shared by pages. No DOM, so it runs under node --test.

const nf = new Intl.NumberFormat("en-GB");

export function formatInt(n) {
  return Number.isFinite(n) ? nf.format(Math.round(n)) : "—";
}

/** Integration time. 330600 -> "91 h 50 min"; 2700 -> "45 min"; 40 -> "40 s". */
export function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const totalMin = Math.round(seconds / 60);
  if (totalMin < 60) return `${totalMin} min`;
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return m ? `${nf.format(h)} h ${String(m).padStart(2, "0")} min` : `${nf.format(h)} h`;
}

/** Hours to one decimal: 330600 -> "91.8 h". */
export function formatHours(seconds, digits = 1) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  return `${(seconds / 3600).toFixed(digits)} h`;
}

export function formatBytes(n) {
  if (!Number.isFinite(n) || n < 0) return "—";
  const units = ["B", "kB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return `${i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

export function formatArcsec(x) {
  return Number.isFinite(x) ? `${x.toFixed(2)}″` : "—";
}

/** "2026-10-13" from an ISO timestamp (UTC). */
export function formatDate(ts) {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "—" : d.toISOString().slice(0, 10);
}

export function formatDateTime(ts) {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "—" : d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

/** "5 min ago", "3 h ago", "2 d ago"; beyond 30 days, the date. `now` is injectable for tests. */
export function relativeTime(ts, now = Date.now()) {
  if (!ts) return "never";
  const t = new Date(ts).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.round((now - t) / 1000);
  if (s < -60) return formatDate(ts);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 30 * 86400) return `${Math.round(s / 86400)} d ago`;
  return formatDate(ts);
}

/** Regional-indicator flag for an ISO 3166 alpha-2 code. Returns "" for anything else. */
export function countryFlag(cc) {
  if (typeof cc !== "string" || !/^[A-Z]{2}$/.test(cc)) return "";
  return String.fromCodePoint(...[...cc].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
}

export function countryName(cc) {
  if (typeof cc !== "string" || !/^[A-Z]{2}$/.test(cc)) return "";
  try { return new Intl.DisplayNames(["en"], { type: "region" }).of(cc) || cc; } catch { return cc; }
}

// ---- Filter colours ------------------------------------------------------------------------------------

const FILTER_CLASS = { L: "lum", R: "red", G: "green", B: "blue", HA: "ha", OIII: "oiii", SII: "sii" };
const FILTER_VAR = { L: "--f-lum", R: "--f-red", G: "--f-green", B: "--f-blue", HA: "--f-ha", OIII: "--f-oiii", SII: "--f-sii" };

/** Canonical filter ("HA") or stream key ("HA@osc") to the chip modifier ("ha"). */
export function filterChipClass(filterOrStreamKey) {
  const f = String(filterOrStreamKey || "").split("@")[0];
  return FILTER_CLASS[f] || "other";
}

export function filterColorVar(filterOrStreamKey) {
  const f = String(filterOrStreamKey || "").split("@")[0];
  return `var(${FILTER_VAR[f] || "--f-other"})`;
}

/** Human label for a stream key: "HA@osc" -> "HA (OSC)", "HA@3nm" -> "HA 3 nm", "HA@unk" -> "HA (bandwidth unknown)". */
export function streamLabel(key) {
  const [f, q] = String(key || "").split("@");
  if (!q) return f;
  if (q === "osc") return `${f} (OSC)`;
  if (q === "unk") return `${f} (bandwidth unknown)`;
  const m = /^([0-9.]+)nm$/.exec(q);
  return m ? `${f} ${m[1]} nm` : `${f} ${q}`;
}

/** The same, short enough for a chip or a chart axis: only "@unk" changes, "HA@unk" -> "HA ?" (the full label goes in a
 *  title / aria-label beside it). */
export function streamShortLabel(key) {
  const [f, q] = String(key || "").split("@");
  return q === "unk" ? `${f} ?` : streamLabel(key);
}

/** True when any of these stream keys has an unknown bandwidth (a chart or table that shows "?" then explains it). */
export const anyUnknownBandwidth = (keys) => (keys || []).some((k) => String(k).endsWith("@unk"));

// ---- Codes typed by people -----------------------------------------------------------------------------

const CODE_ALPHABET = /^[2-9A-HJKMNP-Z]+$/; // 2-9 and A-Z without I, L, O

/** Device user code: 8 characters, hyphen and case optional. -> { valid, value: "K7QM3XWD", display: "K7QM-3XWD" } */
export function normaliseUserCode(input) {
  const value = String(input ?? "").toUpperCase().replace(/[\s-]/g, "");
  const valid = value.length === 8 && CODE_ALPHABET.test(value);
  return { valid, value, display: value.length > 4 ? `${value.slice(0, 4)}-${value.slice(4)}` : value };
}

/** Join code: 12 characters, shown XXXX-XXXX-XXXX. */
export function normaliseJoinCode(input) {
  const value = String(input ?? "").toUpperCase().replace(/[\s-]/g, "");
  const valid = value.length === 12 && CODE_ALPHABET.test(value);
  const display = value.replace(/(.{4})(?=.)/g, "$1-");
  return { valid, value, display };
}

// ---- CSV -----------------------------------------------------------------------------------------------

/**
 * One CSV cell. Strings that a spreadsheet would read as a formula (leading = + - @ tab or CR) get a leading
 * apostrophe, so a hostile filename or filter string cannot run in the contributor's Excel. Numbers are left
 * alone: a negative number is not a formula.
 */
export function csvCell(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : "";
  if (typeof v === "boolean") return v ? "true" : "false";
  let s = String(v);
  if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCsv(columns, rows) {
  const head = columns.map((c) => csvCell(c.label)).join(",");
  const body = rows.map((r) => columns.map((c) => csvCell(c.get(r))).join(","));
  return [head, ...body].join("\r\n") + "\r\n";
}

// ---- Query / path ------------------------------------------------------------------------------------------

export function pluralise(n, one, many = one + "s") {
  return `${formatInt(n)} ${n === 1 ? one : many}`;
}

/** A file name cut after each `_`, `-` and `.`, so a table cell can wrap it at those points instead of letter by letter. */
export function fileNameParts(name) {
  return String(name ?? "").split(/(?<=[_.-])/).filter(Boolean);
}
