// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// The pages' one client for the Collab API (docs/api/openapi.yaml), reduced to the read-only calls the project, filter,
// combined and dashboard pages make (the dashboard adds projects?scope=mine and frames?mine=1).
//
// Rules this file enforces so pages cannot get them wrong:
//
//  * Envelope. A success body is { ok: true, ...fields } and `request()` returns it whole. A failure body is
//    { ok: false, code, message, ...extras } and becomes a thrown ApiError. Pages branch on `err.code`, never on
//    `err.message` (which is for people).
//  * Never 403 or 404 from the API itself: "no such thing" is 400 `not_found`. A route the server does not have at all
//    (an old server, or the front door not mounted yet) answers a plain 404 that is not an envelope; that is read as
//    `not_found` too, so a page shows its normal "not found" state instead of failing.
//  * Anything else that is not an envelope is `bad_response`; a network failure is `network_error`.
//  * The browser sends the `starfront_web` cookie (same origin); there is no Authorization header here.

import { config } from "./config.js";

export class ApiError extends Error {
  constructor({ code, message, status = 0, body = null, retryAfterS = null, cause }) {
    super(message || code);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.body = body;
    this.retryAfterS = retryAfterS;
    if (cause) this.cause = cause;
  }
}

function toQuery(query) {
  if (!query) return "";
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === "") continue;
    p.set(k, String(v));
  }
  const s = p.toString();
  return s ? "?" + s : "";
}

export function createClient(options = {}) {
  const base = String(options.base ?? config.apiBase).replace(/\/+$/, "");
  const getFetch = () => options.fetchImpl ?? globalThis.fetch.bind(globalThis);

  async function get(path, { query, signal } = {}) {
    const url = base + path + toQuery(query);
    let res;
    try {
      res = await getFetch()(url, { method: "GET", credentials: "same-origin", headers: { Accept: "application/json" }, signal });
    } catch (cause) {
      if (cause?.name === "AbortError") throw cause;
      throw new ApiError({ code: "network_error", message: "Network error", cause });
    }

    let text = "";
    try { text = await res.text(); } catch { /* treated as an empty body below */ }
    let parsed = null;
    try { parsed = text ? JSON.parse(text) : null; } catch { parsed = null; }

    const enveloped = parsed && typeof parsed === "object" && typeof parsed.ok === "boolean";
    if (!enveloped) {
      if (res.status === 404) throw new ApiError({ code: "not_found", message: "Not found", status: 404 });
      throw new ApiError({ code: "bad_response", message: "The server sent something that is not an API response", status: res.status });
    }
    if (parsed.ok === true && res.ok) return parsed;
    if (parsed.ok === true) {
      throw new ApiError({ code: "bad_response", message: "Unexpected status for a success body", status: res.status, body: parsed });
    }
    const h = res.headers?.get?.("Retry-After");
    const n = h ? parseInt(h, 10) : NaN;
    throw new ApiError({
      code: typeof parsed.code === "string" ? parsed.code : "unknown_error",
      message: typeof parsed.message === "string" ? parsed.message : "",
      status: res.status,
      body: parsed,
      retryAfterS: Number.isFinite(n) ? n : Number.isFinite(parsed.retry_after_s) ? parsed.retry_after_s : null,
    });
  }

  const proj = (ref) => "/projects/" + encodeURIComponent(ref);

  return {
    get,
    /** The signed-in user (cookie), or null. Never throws and never redirects: the header asks on every page. */
    async currentUser() {
      try {
        const u = (await get("/auth/me")).user;
        return u ? { ...u, name: u.display_name ?? "" } : null;
      } catch { return null; }
    },
    /** The projects the signed-in user is a member of, each with `my_membership` and `stacked_by` (the hub's last heartbeat). */
    listMyProjects: ({ limit, cursor } = {}) => get("/projects", { query: { scope: "mine", limit, cursor } }),
    /** The caller's own frames in a project (`mine=1`; contributors only ever see their own). Until the frames route exists this is `not_found`. */
    listFrames: (ref, { mine, status, filter, limit, cursor } = {}) =>
      get(proj(ref) + "/frames", { query: { mine, status, filter, limit, cursor } }),
    getProject: (ref) => get(proj(ref)).then((r) => r.project),
    /** GET /projects/{project}/status. The API wraps the document; the static file does not. Unwrapped here. */
    getStatusDoc: (ref, opts) => get(proj(ref) + "/status", opts).then((r) => r.status_doc),
    listEvents: (ref, { limit, cursor } = {}) => get(proj(ref) + "/events", { query: { limit, cursor } }),
    listMasters: (ref) => get(proj(ref) + "/masters").then((r) => r.masters),
    /** The MosaicState, or null for a project without a layout. */
    mosaicGet: (ref) => get(proj(ref) + "/mosaic").then((r) => r.mosaic),
  };
}

/** Follows `next_cursor` to the end (or `max` items). `fetchPage(cursor)` returns a page body, `pick(body)` its items. */
export async function paginate(fetchPage, pick, { max = Infinity } = {}) {
  const out = [];
  let cursor;
  do {
    const body = await fetchPage(cursor);
    out.push(...pick(body));
    cursor = body.next_cursor || undefined;
  } while (cursor && out.length < max);
  return out.slice(0, max);
}

/** Where to send somebody to sign in (Discord, via the files page's sign-in) and come back to `next`. */
export const signInUrl = (next) => "/files/signin?next=" + encodeURIComponent(next);

// ---- What to tell a person ------------------------------------------------------------------------------------
// Keyed by `code`, never parsed from `message`. Unknown codes fall back to the server's own human text.

const TEXT = {
  not_found: "That does not exist, or you cannot see it.",
  not_member: "You are not a member of this project.",
  rate_limited: "Too many attempts. Wait a moment and try again.",
  slow_down: "Too many attempts. Wait a moment and try again.",
  internal_error: "Something went wrong on our side. Try again in a minute.",
  network_error: "Could not reach the server. Check your connection and try again.",
  bad_response: "The server did not answer as expected. Try again in a minute.",
  not_authenticated: "Sign in to continue.",
  session_expired: "Your session expired. Sign in again.",
};

export function describeError(err) {
  if (!(err instanceof ApiError)) return "Something went wrong. Try again.";
  let text = TEXT[err.code] ?? (err.message && err.code !== "unknown_error" ? err.message : "Something went wrong. Try again.");
  if ((err.code === "rate_limited" || err.code === "slow_down") && err.retryAfterS) text += ` (try again in ${err.retryAfterS} s)`;
  return text;
}

export const api = createClient();
