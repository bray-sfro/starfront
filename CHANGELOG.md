# Changelog

## 0.2.54 (consolidated, 2026-11-11)

One tree for every change made since 0.2.53b, in single-scope and multiscope
builds. Not installed anywhere.

- 0.2.53g and 0.2.53h (PR #10): everything above.
- 0.2.53c (PR #5): the roof driver's ShutterStatus is never shown as a verified
  open or closed; a close that does not change the reading warns; Alnitak cover
  prep before panel flats. Merged under 0.2.53g's roof guard: the guard still
  refuses an open first.
- 0.2.53d (PR #6): a timed-out camera call quarantines that camera until the
  call returns; no automatic outlet power-cycling on reconnect; three failed
  reconnects stop.
- 0.2.53e/f (PR #7): centring never syncs the mount; it slews by the measured
  offset, rejects flipped or jumping solves, and fails the target when
  attempts run out.
- Coordinator code (astrocontrol/collab.py, filters.py) is the live collab
  server's, plus the per-camera `instrument` on session reports.
- Multiscope build: the 0.2.53b-r2 per-camera collab agents and filter
  identification, on top of all of the above.

## 0.2.53i

- Flat panel cover and calibrator light in the top bar, next to the roof badge: Open, Closed, Moving, Unknown, Error, Not present, plus light on/off and brightness. The status request reads a cache. It does not call the cover driver, so a hung CoverState cannot stall `/api/status`.
- Open cover, Close cover, Light on (full brightness) and Light off in the top bar, with a confirmation. Open is refused (HTTP 409) during darks, bias and panel flats, and while an exposure, slew or autofocus is running.
- Sequence start re-reads CoverState and opens the cover before the first slew, centring frame, autofocus frame, sky flat or light. Unknown on a connected CoverCalibrator is opened; a `has_cover` flag latched at connect is not trusted. The run is held if the cover does not report Open in time, or if the first sky frame still looks like a bias (the Twin FSQ's ~503 ADU lid). A camera or cover reconnect re-checks before the next sky frame. Darks, bias, panel flats and autocal keep the cover closed and never open the roof.

## 0.2.53h

- Auto-upload backfill uses the same capture root as capture. A blank `capture.rootDirectory` is `data_root()/captures`, not "scan nothing".
- The backfill log names that folder and the counts: scanned, matched, queued, and skipped by reason.
- A capture folder that matches more than one collaboration target is not given the first name. It is kept only when the header or the current task names exactly one stamp; otherwise it is skipped and logged. On EAGLE, `m42_horse_test_mosaic` is project `97fcae827a9f`, task `481536614589`, not the declined task `b9412d46f576`.

## 0.2.53g

Single-scope Namibia (EAGLE4PRO) build, on 0.2.53b.

- Roof badge in the main window: opening and closing show elapsed time, then open or closed, or the error, timeout or technician-lock text. Display only.
- Darks, bias and panel flats, including a calibration recipe run from the plan, keep the roof shut. They never send OpenShutter, they refuse an open while they run, and they hold or abort if the roof is not closed. The cover stays closed where there is one. Sky flats and lights are unchanged.
- A project storage quota (HTTP 507) pauses that project only. A full disk still pauses every upload.
- Warn when the mount clock is more than a minute off this PC. The clock is not set and the mount is not synced.
- Auto-upload treats 0, missing or an invalid cap as 10 Mbit/s. Daytime-only is unchanged. The upload thread logs the queue, each result, pauses and the daytime wait.
- Lights captured for a collaboration task but missing PROJID are queued for the master telescope on the next daytime window, without rewriting the FITS file. The follower (Rokinon 135mm / ASI6200MC) does not upload, and its frames are not included in the collaboration session report that counts toward project hours. A report that is sent names the camera (INSTRUME) and that camera's focal length.
- A light is not taken while the filter wheel has not settled on the requested slot. A blank FILTER is not written and a filter is not guessed. A colour camera with no wheel is tagged OSC-RGB.
