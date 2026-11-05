// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// One place for settings that differ between deployments. Overridden by window.COLLAB_CONFIG (js/site-config.js).

export const defaults = Object.freeze({
  // Where the Collab API lives. Must be same-origin for cookie calls.
  apiBase: "/stack/api",
  // Where the hub's pushed artefacts are served: status.json, previews, deep-zoom tiles (docs/api/s3-layout.md).
  publicBase: "/stack/public",
  // The name used in titles and the header.
  siteName: "Starfront",
  // How often the project page re-reads status.json while it is open and visible, in seconds. 0 disables.
  // The server caches it for 60 s, so anything below 60 buys nothing.
  refreshSeconds: 90,
});

export function resolveConfig(overrides) {
  const out = { ...defaults };
  if (overrides && typeof overrides === "object") {
    for (const k of Object.keys(defaults)) {
      if (k in overrides && overrides[k] !== undefined) out[k] = overrides[k];
    }
  }
  out.apiBase = String(out.apiBase).replace(/\/+$/, "");
  out.publicBase = String(out.publicBase).replace(/\/+$/, "");
  return Object.freeze(out);
}

export const config = resolveConfig(globalThis.COLLAB_CONFIG);
