// This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
// If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
// Ported from Rapid Stacker Collab (EKE Services LLC).

// The columns of the "my frames" CSV export (dashboard). Pure so it is unit-tested. Every cell goes through
// format.js csvCell, which neutralises spreadsheet formulas in strings (a hostile file name must not run in Excel).
// Deliberately absent: anything about the observer or the site (the API never returns it) and other members.
import { toCsv } from "./format.js";
import { describeCalibration } from "./calibration-text.js";

export const FRAME_COLUMNS = [
  { label: "frame_id", get: (f) => f.frame_id }, { label: "file", get: (f) => f.filename }, { label: "filter", get: (f) => f.filter },
  { label: "filter_as_written", get: (f) => f.filter_raw }, { label: "status", get: (f) => f.status },
  { label: "reject_reason", get: (f) => f.reject_reason }, { label: "reject_detail", get: (f) => f.reject_detail },
  { label: "quarantine_reason", get: (f) => f.quarantine_reason }, { label: "flags", get: (f) => (f.flags || []).join(";") },
  { label: "calibration", get: (f) => describeCalibration(f.calibration)?.text ?? "" },
  { label: "exposure_s", get: (f) => f.exposure_s }, { label: "fwhm_arcsec", get: (f) => f.metrics?.fwhm_arcsec },
  { label: "eccentricity", get: (f) => f.metrics?.eccentricity }, { label: "stars", get: (f) => f.metrics?.n_stars ?? f.metrics?.stars_reached },
  { label: "weight", get: (f) => f.metrics?.weight }, { label: "gold_seq", get: (f) => f.gold_seq }, { label: "uploaded_at", get: (f) => f.uploaded_at },
];

export const framesToCsv = (rows) => toCsv(FRAME_COLUMNS, rows);
