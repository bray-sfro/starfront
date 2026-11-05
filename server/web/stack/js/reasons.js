// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// Plain-language text for every reject reason, quarantine reason and flag in docs/api/records.md and openapi.yaml.
// Shared by the dashboard (tooltips and the "why" column) and checked against the docs page and the contract by
// test/docs.test.mjs, so a new enum value in the contract fails a test until it is explained here in the dashboard.

export const REJECT_REASONS = [
  { id: "malformed", label: "Unreadable or inconsistent file", fix: "Re-save the frame from your capture software; check the file is a complete FITS or XISF image.", text: "The header or the data failed a safety check (image size, bit depth, a checksum that differs from what was declared), or the file would not parse." },
  { id: "duplicate", label: "Already uploaded", fix: "Nothing to do. The first copy counts.", text: "A frame with exactly the same bytes was uploaded earlier, by you or by someone else." },
  { id: "no_pointing_hint", label: "No idea where it points", fix: "Enable RA/Dec and focal-length (or pixel scale) in your capture software's FITS header, or confirm your rig in the plugin so the project can guess.", text: "The frame has no RA/Dec, no scale, and no confirmed rig, so it could not be matched to the sky." },
  { id: "no_solution", label: "Could not be plate-solved", fix: "Check focus and that the frame has stars (narrowband subs of a faint field can be star-poor); make sure the header pointing is roughly right.", text: "No plate solution was found by any method. The stars it did find are recorded with the frame." },
  { id: "pointing_off_target", label: "Not on the project's field", fix: "Check you are imaging the right target and that the project frame covers where you point.", text: "After solving, less than 20 % of the frame overlaps the project frame." },
  { id: "fwhm_too_high", label: "Stars too soft", fix: "Check focus, seeing and tracking. The project's limit is shown on its join page.", text: "The typical star width (FWHM, in arcseconds) is above the project's limit, which is also relative to the project's median once enough frames exist." },
  { id: "eccentricity_too_high", label: "Stars too elongated", fix: "Check guiding and tracking: polar alignment, balance, cable drag and wind.", text: "The stars are stretched the same way across the frame, as guiding drift or a tracking error leaves them, beyond what the project allows (default 0.9)." },
  { id: "low_star_count", label: "Too few stars (clouds?)", fix: "Usually clouds, dew or a failed autofocus. Nothing to fix unless it happens every night.", text: "The star count is below a fraction of your own session's median, which usually means thin cloud." },
  { id: "saturated", label: "Background saturated", fix: "Shorten the exposure or check for stray light; avoid imaging into twilight or a lit sky.", text: "The sky background is saturated, so the frame carries no usable signal." },
  { id: "no_usable_stars", label: "No measurable star", fix: "Check focus. Every star was saturated, trailed, or unmeasurable.", text: "No unsaturated star with a finite width was found, so the frame's quality cannot be measured." },
  { id: "scale_out_of_bounds", label: "Pixel scale too different", fix: "Ask the owner whether your image scale suits the project, or bin to land inside the accepted range.", text: "The frame's pixel scale is outside the range the owner accepts relative to the project frame (default 0.4 to 2.5 times)." },
  { id: "calibration_required", label: "Calibration frames needed", fix: "Upload matching darks and flats (or masters) from the same session and rig.", text: "The project requires calibration and no matching masters exist for this session." },
  { id: "below_min_exposure", label: "Exposure too short", fix: "Use the project's minimum sub-exposure or longer.", text: "The exposure time is below the project's minimum." },
  { id: "osc_not_allowed", label: "Colour camera not allowed", fix: "Ask the owner to allow one-shot-colour cameras, or contribute a mono camera.", text: "The frame comes from a Bayer-pattern camera in a project that accepts mono only." },
];

export const QUARANTINE_REASONS = [
  { id: "unknown_filter", label: "Unknown filter name", fix: "Map your filter name in the plugin; the owner can also map it for the project.", text: "The filter name did not match anything the normaliser knows. The frame waits until it is mapped, then continues automatically." },
  { id: "ambiguous_filter", label: "Ambiguous filter name", fix: "Map your filter name explicitly. A single letter like G or I can mean more than one thing.", text: "The name could mean more than one filter (for example G with a project that declares Sloan filters)." },
  { id: "filter_not_in_project", label: "Filter not in this project", fix: "The owner can add the filter to the project, or you can skip it.", text: "The filter was recognised but the project does not list it." },
];

export const FLAGS = [
  { id: "poor_fit", label: "Poor registration fit", text: "The frame fitted the project frame with an error of 0.5 pixel or more. It is accepted at half weight and kept out of gold stacks by default." },
  { id: "high_sky", label: "Bright sky", text: "The background is high. The frame is accepted at reduced weight." },
  { id: "trail", label: "Possible trail", text: "More than 2 % of the frame's pixels were clipped as outliers in the live stack (a satellite or aircraft). Informational: never rejects by itself." },
  { id: "uncalibrated", label: "Not calibrated yet", text: "Accepted without calibration at half weight; it is re-processed automatically when flats arrive." },
  { id: "precalibrated", label: "Already calibrated", text: "Your file says it was calibrated (header or history), so the project skipped calibration." },
  { id: "derived", label: "Split from a colour frame", text: "A synthetic contribution made by splitting a dual-band colour frame into narrowband channels." },
  { id: "coarse", label: "Coarser than the project", text: "Your pixel scale is coarser than the project frame, so the frame is up-sampled and its weight scaled down." },
  { id: "solved_by_master", label: "Placed against the stack", text: "The catalogue solve of this frame failed its checks (a short or very narrowband exposure has few stars), so it was registered against the stars of the project's own stack instead. It is accepted; its brightness scale is estimated from the noise." },
  { id: "shared_channel_map", label: "Shared colour correction", text: "One colour channel of your colour frame had too few matched stars for its own lateral-colour correction and was placed with the luminance map. Informational." },
];

const all = new Map([...REJECT_REASONS, ...QUARANTINE_REASONS, ...FLAGS].map((r) => [r.id, r]));
export const reasonInfo = (id) => all.get(id) ?? null;
export const reasonLabel = (id) => all.get(id)?.label ?? String(id).replace(/_/g, " ");

/** Stars a frame's preparation found: `n_stars` once measured, else `stars_reached` (all a frame that never solved has). */
export function starsOf(metrics) {
  const n = metrics?.n_stars ?? metrics?.stars_reached;
  return Number.isFinite(n) ? n : null;
}

/** Below this many stars a failed solve is a sky or optics problem, not a pointing one. */
export const FEW_STARS = 25;

/**
 * A plain-language hint for a rejection whose code alone misleads: `no_solution` on a frame that found almost no stars reads
 * as a pointing problem, but the cause is nearly always focus, cloud or dew. Null when the code says enough.
 */
export function rejectHint(frame) {
  if (frame?.reject_reason !== "no_solution") return null;
  const n = starsOf(frame.metrics);
  if (n === null || n >= FEW_STARS) return null;
  return `Only ${n} ${n === 1 ? "star" : "stars"} found: usually focus, cloud or dew, not pointing.`;
}
