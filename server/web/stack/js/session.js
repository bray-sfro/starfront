// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// Who is signed in, asked once per page load and shared between the header and the page.
//
// The `starfront_web` cookie is the only credential and it is HttpOnly, so the page never holds one. Only a display name
// is cached in sessionStorage (for the first paint of the header). The cache is a hint, never authority: the API is
// what enforces access. Storage may be blocked; everything works without it.

import { api } from "./api.js";

const KEY = "sf_user";
const TTL_MS = 5 * 60 * 1000;

export function getCachedUser() {
  try {
    const raw = JSON.parse(sessionStorage.getItem(KEY) || "null");
    if (raw && Date.now() - raw.t < TTL_MS && typeof raw.name === "string") return { name: raw.name, role: raw.role };
  } catch { /* storage unavailable or corrupt */ }
  return null;
}

function cacheUser(user) {
  try {
    if (user) sessionStorage.setItem(KEY, JSON.stringify({ t: Date.now(), name: user.name, role: user.role }));
    else sessionStorage.removeItem(KEY);
  } catch { /* ignore */ }
}

let pending = null;

/** Resolves to the signed-in user (full object from /auth/me) or null. Never rejects, never redirects. */
export function whoami() {
  if (!pending) {
    pending = api.currentUser().catch(() => null).then((u) => { cacheUser(u); return u; });
  }
  return pending;
}
