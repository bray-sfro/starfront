// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// Reads which project (and which filter) a page is showing from location.pathname. Pure, so it is unit-tested.
// The server (server/app.py) serves the same static page for every slug; the URL is the only
// place the slug lives.

/** Where the pages live on the Starfront server. */
export const BASE = "/stack";
export const homeUrl = () => `${BASE}/`;

const SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

/** "/stack/p/horsehead-sho" -> { slug, filter: null, view: "project" } ("combined" view too); ".../filter/HA%40osc" -> filter "HA@osc". */
export function parseProjectPath(pathname) {
  const m = /^\/stack\/p\/([^/]+)(?:\/(filter)\/([^/]+)|\/(combined))?\/?$/.exec(pathname || "");
  if (!m) return null;
  let slug = m[1];
  try { slug = decodeURIComponent(slug); } catch { return null; }
  if (!SLUG.test(slug) || slug.length < 3 || slug.length > 40) return null;
  if (m[2]) {
    let filter = m[3];
    try { filter = decodeURIComponent(filter); } catch { /* keep as is */ }
    return { slug, filter, view: "filter" };
  }
  return { slug, filter: null, view: m[4] || "project" };
}

export const projectUrl = (slug) => `${BASE}/p/${encodeURIComponent(slug)}`;
/** The combined (colour) preview's own page, as the filter pages are the streams'. */
export const combinedUrl = (slug) => `${projectUrl(slug)}/combined`;
// '@' is legal in a path segment and appears in stream keys (HA@osc); keep it readable in the address bar.
export const filterUrl = (slug, streamKey) => `${BASE}/p/${encodeURIComponent(slug)}/filter/${encodeURIComponent(streamKey).replace(/%40/g, "@")}`;
