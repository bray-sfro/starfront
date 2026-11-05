// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// /stack/dashboard: my projects (what is stacking each) and my frames (with CSV). Session (cookie) only.
// Everything shown is the signed-in user's own. The frames table is requested with mine=1.

import { api, describeError, paginate, signInUrl } from "../api.js";
import { html, setHtml } from "../dom.js";
import { whoami } from "../session.js";
import { projectUrl } from "../route.js";
import { statusBadge, filterChips } from "../components/project-card.js";
import { formatHours, formatInt, formatArcsec, formatDateTime, relativeTime, toCsv, pluralise, fileNameParts } from "../format.js";
import { reasonInfo, reasonLabel, rejectHint, starsOf } from "../reasons.js";
import { FRAME_COLUMNS } from "../frames-csv.js";
import { describeCalibration } from "../calibration-text.js";

const $ = (id) => document.getElementById(id);
const time = (iso) => (iso ? html`<time datetime="${iso}" title="${formatDateTime(iso)}">${relativeTime(iso)}</time>` : "—");
const memberProjects = [];

// ---- Projects -----------------------------------------------------------------------------------------------------------

/** "Stacked by <hub>, 2 workers, last seen 3 minutes ago", or what is known of it. */
function hubLine(s) {
  if (!s) return html`<p class="dash-hub">Not stacked on a hub yet.</p>`;
  const workers = Number.isFinite(s.workers) ? `, ${pluralise(s.workers, "worker")}` : "";
  return html`<p class="dash-hub">Stacked by ${s.hub_name}${workers}, ${s.last_seen ? html`last seen ${time(s.last_seen)}` : "not heard from yet"}. <span class="badge ${s.online ? "badge--ok" : "badge--warn"}">${s.online ? "online" : "offline"}</span></p>`;
}

function projectPanel(p) {
  const m = p.my_membership;
  const id = p.project_id;
  return html`<article class="panel" aria-labelledby="pp-${id}">
    <div class="row row--between"><h3 id="pp-${id}" class="dash-title"><a href="${projectUrl(p.slug)}">${p.name}</a></h3><span class="row">${statusBadge(p.status)}<span class="badge badge--accent">${m.role}</span>${m.status === "pending" ? html`<span class="badge badge--warn">awaiting approval</span>` : ""}</span></div>
    <p class="muted dash-sub">${p.target_name || ""} ${filterChips(p.filters)}</p>
    <div class="grid dash-stats">
      <div><div class="stat-tile__label">Your frames</div><div class="num dash-stat">${formatInt(m.stats.frames_accepted)} <small>accepted, ${formatInt(m.stats.frames_rejected)} rejected</small></div></div>
      <div><div class="stat-tile__label">Your integration</div><div class="num dash-stat">${formatHours(m.stats.integration_s)}</div></div>
    </div>
    ${hubLine(p.stacked_by)}
  </article>`;
}

async function loadProjects() {
  const out = $("projects-out");
  try {
    const list = await paginate((c) => api.listMyProjects({ limit: 50, cursor: c }), (b) => b.projects, { max: 100 });
    memberProjects.push(...list.filter((p) => p.my_membership && p.my_membership.status !== "removed"));
    if (!memberProjects.length) {
      setHtml(out, html`<div class="empty"><p>You are not in any stacking project yet.</p><p>A project owner's join code, entered in Starfront, adds your telescope to one.</p></div>`);
      fillFramesProjects();
      return;
    }
    setHtml(out, html`${memberProjects.map(projectPanel)}`);
    fillFramesProjects();
  } catch (e) { setHtml(out, html`<p class="form__msg is-err" role="alert">${describeError(e)}</p>`); }
}

// ---- Frames -------------------------------------------------------------------------------------------------------------

const STATUS_CLASS = { accepted: "badge--ok", accumulated: "badge--ok", in_gold: "badge--ok", rejected: "badge--err", failed: "badge--err", quarantined: "badge--warn", uploading: "badge--info", uploaded: "badge--info", preparing: "badge--info", withdrawn: "badge" };
const frameState = { slug: null, cursor: null, rows: [] };

function fillFramesProjects() {
  const sel = $("fr-project");
  setHtml(sel, html`${memberProjects.filter((p) => p.my_membership.status === "active").map((p) => html`<option value="${p.slug}">${p.name}</option>`)}`);
  if (sel.options.length) { frameState.slug = sel.value; loadFrames(true); }
  else setHtml($("frames-out"), html`<div class="empty"><p>Join a project to see how your frames are judged.</p></div>`);
}

function whyCell(f) {
  const id = f.reject_reason || f.quarantine_reason;
  if (id) {
    const hint = rejectHint(f);
    return html`<span title="${reasonInfo(id)?.text ?? ""}">${reasonLabel(id)}</span>${hint ? html`<div class="dash-small">${hint}</div>` : ""}${reasonInfo(id)?.fix ? html`<div class="muted dash-tiny">${reasonInfo(id).fix}</div>` : ""}${f.reject_detail ? html`<div class="muted dash-tiny">${f.reject_detail}</div>` : ""}`;
  }
  if (f.flags?.length) return html`${f.flags.map((fl) => html`<span class="badge" title="${reasonInfo(fl)?.text ?? ""}">${reasonLabel(fl)}</span> `)}`;
  return "";
}

function calibrationCell(f) {
  const d = describeCalibration(f.calibration);
  return d ? html`<span title="${d.detail}">${d.text}</span>` : "";
}

function frameRow(f) {
  const m = f.metrics || {};
  return html`<tr>
    <td class="dash-file">${fileNameParts(f.filename).map((p) => html`${p}<wbr>`)}</td>
    <td>${f.filter}${f.filter_raw && f.filter_raw !== f.filter ? html`<div class="muted dash-tiny">${f.filter_raw}</div>` : ""}</td>
    <td><span class="badge ${STATUS_CLASS[f.status] || ""}">${String(f.status).replace("_", " ")}</span></td>
    <td class="dash-why">${whyCell(f)}</td>
    <td>${calibrationCell(f)}</td>
    <td class="num">${f.exposure_s ?? "—"}</td>
    <td class="num">${formatArcsec(m.fwhm_arcsec)}</td>
    <td class="num">${m.eccentricity ?? "—"}</td>
    <td class="num">${starsOf(m) ?? "—"}</td>
    <td>${time(f.uploaded_at || f.created_at)}</td>
  </tr>`;
}

function frameQuery() {
  return { mine: "1", status: $("fr-status").value || undefined, filter: $("fr-filter").value || undefined };
}

async function loadFrames(reset) {
  const out = $("frames-out");
  if (reset) { frameState.cursor = null; frameState.rows = []; setHtml(out, html`<p class="loading" role="status">Loading frames</p>`); }
  try {
    const r = await api.listFrames(frameState.slug, { ...frameQuery(), limit: 50, cursor: frameState.cursor });
    frameState.rows.push(...r.frames);
    frameState.cursor = r.next_cursor;
    if (!frameState.rows.length) { setHtml(out, html`<div class="empty"><p>No frames match.</p></div>`); $("fr-more").hidden = true; $("fr-csv").disabled = true; return; }
    setHtml(out, html`<div class="table-scroll"><table class="data-table">
      <caption class="visually-hidden">Your frames in this project, newest first</caption>
      <thead><tr><th scope="col">File</th><th scope="col">Filter</th><th scope="col">Status</th><th scope="col">Why</th><th scope="col">Calibration</th><th scope="col" class="num">Exp (s)</th><th scope="col" class="num">FWHM</th><th scope="col" class="num">Ecc.</th><th scope="col" class="num">Stars</th><th scope="col">Uploaded</th></tr></thead>
      <tbody>${frameState.rows.map(frameRow)}</tbody></table></div>
      <p class="muted dash-small">${pluralise(frameState.rows.length, "frame")} shown${frameState.cursor ? ", more available" : ""}.</p>`);
    $("fr-more").hidden = !frameState.cursor;
    $("fr-csv").disabled = false;
  } catch (e) {
    $("fr-more").hidden = true;
    $("fr-csv").disabled = true;
    // The frames list arrives with the relay; until then (or when the hub has pushed nothing) the table is simply empty.
    if (e.code === "not_found") setHtml(out, html`<div class="empty"><p>The frame list is not available yet.</p></div>`);
    else setHtml(out, html`<p class="form__msg is-err" role="alert">${describeError(e)}</p>`);
  }
}

function wireFrames() {
  $("fr-project").addEventListener("change", () => { frameState.slug = $("fr-project").value; loadFrames(true); });
  $("fr-status").addEventListener("change", () => loadFrames(true));
  $("fr-filter").addEventListener("change", () => loadFrames(true));
  $("fr-more").addEventListener("click", async () => { $("fr-more").disabled = true; await loadFrames(false); $("fr-more").disabled = false; });
  $("fr-csv").addEventListener("click", async () => {
    const btn = $("fr-csv");
    btn.disabled = true;
    const note = $("fr-csv-note");
    note.textContent = "Collecting every frame…";
    try {
      const rows = await paginate((c) => api.listFrames(frameState.slug, { ...frameQuery(), limit: 100, cursor: c }), (b) => b.frames, { max: 5000 });
      const blob = new Blob([toCsv(FRAME_COLUMNS, rows)], { type: "text/csv;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${frameState.slug}-my-frames-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      note.textContent = `${pluralise(rows.length, "frame")} exported.`;
    } catch (e) { note.textContent = describeError(e); }
    btn.disabled = false;
  });
}

// ---- Start --------------------------------------------------------------------------------------------------------------

const user = await whoami();
if (!user) {
  $("signin").hidden = false;
  $("signin-link").href = signInUrl("/stack/dashboard");
} else {
  $("who").textContent = user.name || "astrophotographer";
  $("dash").hidden = false;
  loadProjects();
  wireFrames();
}
