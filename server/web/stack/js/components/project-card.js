// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// Project cards for the directory and the dashboard. Pure: ProjectSummary in, Raw HTML out (escaped by html``).
import { html, safeUrl } from "../dom.js";
import { formatHours, relativeTime, pluralise, filterChipClass } from "../format.js";
import { projectUrl } from "../route.js";

export const STATUS_BADGE = {
  draft: ["badge--warn", "Draft"], open: ["badge--ok", "Open"], paused: ["badge--warn", "Paused"],
  closed: ["badge", "Closed"], archived: ["badge", "Archived"], deleted: ["badge--err", "Scheduled for deletion"],
};
export const VISIBILITY_TEXT = { public: "Public", unlisted: "Unlisted", private: "Private" };

export function statusBadge(status) {
  const [cls, label] = STATUS_BADGE[status] || ["badge", status];
  return html`<span class="badge ${cls}">${label}</span>`;
}

export function filterChips(filters) {
  return html`<span class="chips">${(filters || []).map((f) => html`<span class="chip chip--${filterChipClass(f)}">${f}</span>`)}</span>`;
}

/** `p` is a ProjectSummary (docs/api/openapi.yaml). Links to the project page by slug. */
export function projectCard(p, { badge } = {}) {
  const c = p.counts || {};
  return html`<a class="card project-card" href="${projectUrl(p.slug)}">
    ${p.combined_preview_url
      ? html`<figure class="preview"><img data-card-preview src="${safeUrl(p.combined_preview_url)}" alt="Latest combined preview of ${p.name}" width="160" height="120" loading="lazy"></figure>`
      : html`<div class="preview"><div class="preview__ph" style="aspect-ratio:4/3">No preview yet</div></div>`}
    <div class="project-card__body">
      <div class="row row--between"><h3 class="project-card__title">${p.name}</h3>${badge ?? statusBadge(p.status)}</div>
      <div class="muted">${p.target_name || "Target not set"} · ${VISIBILITY_TEXT[p.visibility] || p.visibility}</div>
      ${filterChips(p.filters)}
      <div class="project-card__meta">
        <span><strong>${formatHours(c.integration_s ?? 0)}</strong> integration</span>
        <span><strong>${pluralise(c.member_count ?? 0, "member")}</strong></span>
        <span>updated ${relativeTime(c.last_update)}</span>
      </div>
    </div>
  </a>`;
}

/**
 * A preview URL can fail: a private project's list entry may carry a path that is not public, a presigned URL may
 * have expired, or the first publish may not have happened. Swap a failed image for the same placeholder a card
 * without a preview gets. `error` does not bubble, so listen in the capture phase on a container.
 */
export function wirePreviewFallbacks(container) {
  container.addEventListener("error", (ev) => {
    const img = ev.target;
    if (!(img instanceof HTMLImageElement) || !img.hasAttribute("data-card-preview")) return;
    const ph = document.createElement("div");
    ph.className = "preview__ph";
    ph.style.aspectRatio = "4 / 3";
    ph.textContent = "No preview yet";
    img.replaceWith(ph);
  }, true);
}
