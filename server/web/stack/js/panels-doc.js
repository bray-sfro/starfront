// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// public/{project_id}/panels.json (docs/api/panels.schema.json): the per-panel progress of a mosaic project. Sparse (only panels with data),
// display names only. status.json's `panels` index carries its URL (a presigned one for a private project).

export const SUPPORTED_PANELS_SCHEMA = 1;

/** The document at `url`, or null while it is missing, unreadable or of a version this page does not understand. Never throws. */
export async function fetchPanelsDoc(url, fetchImpl = globalThis.fetch?.bind(globalThis)) {
  if (!url || !fetchImpl) return null;
  try {
    const res = await fetchImpl(url, { headers: { Accept: "application/json" } });
    if (!res.ok) return null;
    return normalisePanelsDoc(JSON.parse(await res.text()));
  } catch { return null; }
}

/** Validates the parts the pages read; returns the document or null. */
export function normalisePanelsDoc(doc) {
  if (!doc || typeof doc !== "object" || doc.schema_version !== SUPPORTED_PANELS_SCHEMA || !Array.isArray(doc.panels)) return null;
  return {
    generated_at: doc.generated_at ?? null,
    layout: doc.layout ?? null,
    targets: doc.targets && typeof doc.targets === "object" ? { default: doc.targets.default ?? {}, panels: doc.targets.panels ?? {} } : { default: {}, panels: {} },
    totals: doc.totals ?? { panels_with_data: doc.panels.length, frames_accepted: 0, integration_s: 0 },
    panels: doc.panels.filter((p) => p && typeof p.panel_id === "string"),
  };
}
