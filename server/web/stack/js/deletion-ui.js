// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// The sentence a project page shows when the project is scheduled for deletion. The rules are in deletion-helpers.js.

import { html } from "./dom.js";
import { formatDate } from "./format.js";
import { byText, deletionInfo } from "./deletion-helpers.js";

/** The sentence for a project that is deleted: when, by whom, and what a Restore does. */
export function deletedSentence(p, now = Date.now()) {
  const i = deletionInfo(p, now);
  // Starfront keeps the final results after the purge (decision D004) until the owner removes them.
  if (!i.restorable && p.results_kept) return html`<strong>Deleted on ${formatDate(p.deleted_at ?? i.dueAt)}, final results kept.</strong> The raw frames have been removed; what was stacked stays here, read-only, until the owner removes it.`;
  if (!i.restorable) return html`<strong>This project is being deleted.</strong> Its data is being removed${i.byRole ? html` (asked by ${byText(i.byRole)})` : ""}; it cannot be restored.`;
  return html`<strong>This project is scheduled for deletion on ${formatDate(i.dueAt)}</strong>${i.byRole ? html`, at the request of ${byText(i.byRole)}` : ""}. Until then it is closed to joins and uploads, and hidden from everyone but its members. Then its masters, frames and activity are removed.`;
}
