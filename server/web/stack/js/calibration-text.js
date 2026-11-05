// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// Which masters a light was calibrated with, in a few words (contract 1.11.0, Frame.calibration). Pure, so it is unit-tested;
// the owner console's frame panel and the contributor dashboard both use it. Nothing here says where anything was shot: the
// nights are the nights of the calibration frames.

const nightText = (night) => {
  const d = typeof night === "string" ? new Date(night + "T00:00:00Z") : null;
  if (!d || Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
};

// "20 Oct 06:10 UTC": a flat is chosen by its time, not its night (contract 1.22.0, taken_from); "" when absent or unparsable.
const timeText = (iso) => {
  const d = typeof iso === "string" ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return "";
  const day = d.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });
  return day + " " + d.toISOString().slice(11, 16) + " UTC";
};

const KINDS = [["bias", "bias"], ["dark", "dark"], ["flat", "flat"]];

/**
 * @param {object|undefined|null} c Frame.calibration
 * @returns {{text: string, detail: string}|null} null when the frame has no recorded calibration (not prepared yet, or a
 *   calibration frame). `text` is the compact line ("bias · dark 300 s · no flat"); `detail` the long form for a tooltip.
 */
export function describeCalibration(c) {
  if (!c || typeof c !== "object") return null;
  if (c.precalibrated) return { text: "pre-calibrated", detail: "The light was already calibrated; no master was applied." };
  const short = [];
  const long = [];
  const lacking = [];
  for (const [key, name] of KINDS) {
    const m = c[key];
    if (!m) { lacking.push(name); continue; }
    let s = name;
    if (key === "dark" && Number.isFinite(m.exposure_s)) s += " " + trim(m.exposure_s) + " s";
    if (key === "flat" && m.filter) s += " " + m.filter;
    if (m.library === true) s += " (library)"; // from the rig's calibration library, not the project's own frames (contract 1.23.0)
    const night = nightText(m.night);
    const taken = key === "flat" ? timeText(m.taken_from) : "";
    if (taken) s += " · " + taken;
    else if (key === "flat" && night) s += " (" + night + ")";
    short.push(s);
    const bits = [];
    if (m.source === "uploaded") bits.push("uploaded by the contributor");
    else if (Number.isFinite(m.frames)) bits.push("integrated from " + m.frames + (m.frames === 1 ? " frame" : " frames"));
    if (night && key !== "flat") bits.push("newest night " + night);
    long.push(s + (bits.length ? " (" + bits.join(", ") + ")" : ""));
  }
  if (!short.length) return { text: "no calibration", detail: "No master matched this light." };
  const text = [...short, ...lacking.map((n) => "no " + n)].join(" · ");
  const detail = long.join("; ") + (lacking.length ? "; missing: " + lacking.join(", ") + " (completed when one arrives)" : "") + (Number.isFinite(c.dark_scale) && c.dark && c.dark_scale !== 1 ? "; dark scaled by " + c.dark_scale.toFixed(2) : "");
  return { text, detail };
}

function trim(n) {
  return Number.isInteger(n) ? String(n) : String(Math.round(n * 10) / 10);
}
