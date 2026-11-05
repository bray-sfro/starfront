// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// Safe HTML building without a framework. Contributor display names, project names and filter strings are
// user-supplied, so every interpolated value is escaped unless it was built by this module (Raw).
//
//   setHtml(el, html`<li class="${cls}">${name}</li>`)
//
// setHtml refuses a plain string, so "innerHTML = someUserText" cannot slip in by accident.

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
}

export class Raw {
  constructor(value) { this.value = String(value); }
  toString() { return this.value; }
}

/** Mark a string as already-safe HTML. Use only on text this code built itself. */
export const raw = (s) => new Raw(s);

function part(v) {
  if (v === null || v === undefined || v === false || v === true) return "";
  if (v instanceof Raw) return v.value;
  if (Array.isArray(v)) return v.map(part).join("");
  return esc(v);
}

/** Tagged template: escapes every interpolation, concatenates arrays, drops null/undefined/booleans. */
export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) out += part(values[i]) + strings[i + 1];
  return new Raw(out);
}

/** Allow only site-relative paths and http(s) URLs in href/src attributes. Anything else becomes "#". */
export function safeUrl(u) {
  const s = String(u ?? "").trim();
  if (/^\/(?![/\\])/.test(s)) return s;
  if (/^https?:\/\//i.test(s)) return s;
  return "#";
}

export function setHtml(el, value) {
  if (!(value instanceof Raw)) throw new TypeError("setHtml needs html`...` output, not a plain string");
  el.innerHTML = value.value;
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Shorthand for a status message paragraph (.form__msg convention). */
export function setMsg(el, text, kind) {
  if (!el) return;
  el.textContent = text || "";
  el.className = "form__msg" + (kind ? " is-" + kind : "");
}
