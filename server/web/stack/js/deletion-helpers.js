// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// Pure helpers for deleting a project (contract 1.16.0, docs/api/README.md open points 70 to 75): what a deleted project says about
// itself, and the validation of the confirmation form. No DOM, so these run under node --test; the dialog and the banners are
// in deletion-ui.js.

import { formatDate } from "./format.js";

export const GRACE_DAYS = 7;
const DAY_MS = 86400000;

/** Is this project (any view: Project, ProjectSummary, AdminProject) scheduled for deletion? */
export const isDeleted = (p) => !!p && p.status === "deleted";

/**
 * What a deleted project looks like to the person reading it. `restorable` follows the server's rule: before `delete_after`
 * (and before the purge has started, which the server alone knows: a Restore after that answers 409 `purge_started`).
 */
export function deletionInfo(p, now = Date.now()) {
  if (!isDeleted(p)) return { deleted: false };
  const due = Date.parse(p.delete_after);
  const known = !Number.isNaN(due);
  const restorable = known && due > now;
  return {
    deleted: true,
    dueAt: p.delete_after ?? null,
    byRole: p.deleted_by_role ?? null,
    restorable,
    immediate: !restorable,
    daysLeft: known ? Math.max(0, Math.ceil((due - now) / DAY_MS)) : null,
  };
}

/** "scheduled for deletion on 20 Oct 2026" or, once the date has passed, "being deleted". */
export function deletionSentence(p, now = Date.now()) {
  const i = deletionInfo(p, now);
  if (!i.deleted) return "";
  return i.restorable ? `scheduled for deletion on ${formatDate(i.dueAt)}` : "being deleted";
}

/** Who asked, in words (a role; the API never says more). */
export function byText(role) {
  return role === "admin" ? "a site administrator" : role === "owner" ? "the project owner" : "someone";
}

/** The two choices of the delete dialog. */
export const DELETE_MODES = Object.freeze([
  { value: "delayed", label: `in ${GRACE_DAYS} days (can be restored)` },
  { value: "immediate", label: "immediately (cannot be undone)" },
]);

/**
 * Checks what the dialog collected ({ confirm_slug, when }) against the project. -> { error } or { confirmSlug, immediate }.
 * The slug is compared exactly, as the server does; a trailing space the person did not mean is forgiven (it is trimmed).
 */
export function readDeleteForm(project, values) {
  const typed = String(values?.confirm_slug ?? "").trim();
  if (!typed) return { error: `Type the project's address, ${project.slug}, to confirm.` };
  if (typed !== project.slug) return { error: `That is not the project's address. Type ${project.slug} exactly.` };
  const when = values?.when ?? "delayed";
  if (!DELETE_MODES.some((m) => m.value === when)) return { error: "Choose when the project is deleted." };
  return { confirmSlug: typed, immediate: when === "immediate" };
}

/** The line under a project in a list or a table: the date, and who asked. Empty for a project that is not deleted. */
export function deletionLine(p, now = Date.now()) {
  const i = deletionInfo(p, now);
  if (!i.deleted) return "";
  const who = i.byRole ? ` by ${byText(i.byRole)}` : "";
  return i.restorable ? `Deletes on ${formatDate(i.dueAt)}${who}` : `Being deleted${who}`;
}
