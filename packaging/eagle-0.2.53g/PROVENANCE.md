# 0.2.53g — Namibia single-scope (EAGLE4PRO)

Patch on **0.2.53b** single-scope, the build EAGLE runs now.

Base zip SHA256:

```
d88de93243f9d9eb0d93f0f9e0b236c4aeb7afcc2b46f24a5aac7c8413f88413
```

This PR does not merge application code onto public `main` (0.2.43). It carries the patched source zip and a patch. Windows packaging is done separately. Nothing here was installed on a rig, and no hardware was commanded.

`__version__` is `0.2.53g`.

## Apply

On a clean 0.2.53b tree:

```
patch -p1 < packaging/eagle-0.2.53g/Starfront-0.2.53g-from-0.2.53b.patch
```

The source zip is already 0.2.53g.

```
986571d87b532bf96042abda7bfc5cbf81a6db35dab7c8c64c174b51947d33fd  Starfront-0.2.53g-source.zip
9f13053e23b7056dfc2c448410a3412e74aa5863bd0dd91133ec3ac880070285  Starfront-0.2.53g-from-0.2.53b.patch
```

The same zip is in the agent store at `handoff/Starfront-0.2.53g-source.zip`.

## Ported

- PR #8 (`cursor/autoupload-quota-507-3fef`): a quota HTTP 507 pauses that project only. A disk 507 still pauses every upload. Pauses are kept in `pauses.json`. Applied cleanly onto 0.2.53b.
- PR #9 (`cursor/mount-clock-skew-1633`): warn when the mount clock is more than a minute off this PC. Read-only. The version check now expects `0.2.53g`. Applied with line offsets.

## What 0.2.53g changes

1. **Roof badge.** `astrocontrol/roofstatus.py` turns the shutter state the status snapshot already read, plus the sequencer's technician-lock text, into one line: `Roof opening… 12s`, `Roof open`, `Roof closing… 8s`, `Roof closed`, `Roof error`, or `Roof locked: …`. The main window shows it in `#roofBadge`. Open, close, and the safety rules are unchanged.

2. **Roof stays shut for darks, bias, and panel flats.** Sky flats and lights may still open the roof, and only through the existing technician-lock and safety-monitor path. See TEST-SUMMARY for the functions and what the mock dome recorded.

3. **Auto-upload.** `autoUploadMbit` of 0, missing, or invalid is 10 Mbit/s. `autoUploadDaytimeOnly` is unchanged. The worker logs queue size, each result, pauses and their reason, and when it is waiting for daytime.

4. **Backfill.** Lights under a collaboration target that have no `PROJID` are queued with that target's project and task. The FITS file is not rewritten. The upload request already sends `project` and `task`. Only the master camera is queued. The 14 FSQ85 frames from 2026-11-05 with a blank `PROJID` (the 5 s lights in `m42_horse_test_mosaic`) are the ones this picks up. Their blank `FILTER` is left blank. The 27 Rokinon frames are not queued.

5. **Follower exclusion.** The Rokinon 135mm / ASI6200MC does not upload and is not included in the collaboration session report. Identity is rig role, camera name, and Bayer pattern, not the folder. `camera.uploadCollab` defaults off for a follower and on for the master. Nothing already on the server is deleted.

6. **Session report.** Follower subs are stored on their own slice and are not POSTed to `/api/v1/agent/report`, so they do not count toward project hours. A report that is sent includes `instrument` (FITS `INSTRUME`) and that camera's `focalLength`.

7. **Filters.** A light is not taken until the wheel reports the requested slot. A driver that says moving but then returns one stable slot is trusted, and that is logged. All reads of -1 hold the run and alert. `FILTER` is never blank and never guessed. A colour camera with no wheel is tagged `OSC-RGB` on new frames only. Existing files are not rewritten, and the 131 blank-filter FSQ85 lights are not assigned a filter.
