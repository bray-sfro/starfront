# Starfront 0.2.50: stability hardening

Built on 0.2.49 (tag `v0.2.49`), in the same repo, one commit per fix (`git log v0.2.49..v0.2.50`).
All testing was done on the build box with fake devices (`tools/fakes.py`: camera, mount, focuser
and filter wheel, with adjustable command delay, status delay and failure), for one telescope and
for three. Nothing was run on the telescope PCs.

Tests added (plain `tools/check_*.py`, PASS/FAIL lines):

| Test | Covers | 0.2.49 | 0.2.50 |
| --- | --- | --- | --- |
| `tools/check_hardening.py` | H1-H4, H8, single + triple, FastAPI test client | 4/20 | 20/20 |
| `tools/check_viewer.py` | H4 (page side), H5, H6, H7, H9: runs app.js's own functions under node with a fake DOM, fetch and clock | 1/10 | 11/11 |
| `tools/check_live_link.py` | real uvicorn server + `/ws` socket, slow park, slow status, single + triple | n/a (needs 0.2.50 API) | 9/9 |

## H1: the status froze while a device was busy
- **Symptom:** the whole UI stopped updating, on every telescope, during a park, a synchronous slew, a
  filter change or an image download. Then it jumped ahead.
- **Root cause:** `DeviceManager.status()` read every device's properties inline. ASCOM calls go
  through one STA thread per telescope (`_ComExecutor`), so a status read queues behind a long
  synchronous call (the default timeout is 180 s). The status socket and `/api/status` waited on
  the slowest driver of every telescope.
- **Fix:** `manager.py`. Each device's status is read on its own thread, with at most one read in
  flight per device. `status()` waits at most 0.3 s in total. A device that hasn't answered
  returns its last known state, marked `stale: true` with `statusAge` in seconds.
- **Test:** `check_hardening` H1. With a mount status read of 3 s, `/api/status` went from
  3.01 s to 0.31 s and the mount is shown as stale. In triple mode, one telescope's stuck focuser
  went from 3.01 s to 0.31 s and the other telescopes are not stale. `check_live_link`: the
  socket's longest gap is 0.71 s during the slow park and the slow status.

## H2: one broken part of the status killed all of it
- **Symptom:** the page froze completely ("live" but nothing moving) after an error in one
  subsystem.
- **Root cause:** `_snapshot()` built every section in one function. Any exception (pier camera,
  safety, collab, calibration, events …) gave a 500 on `/api/status` and an error in the WS loop.
- **Fix:** `main._snapshot` builds each section separately (`part()`). A failing section falls
  back to an empty value and is named in `sectionErrors`. The page logs it to the console.
- **Test:** H2, with the pier camera raising. Before: 500 and no socket messages. After: 200,
  `sectionErrors.piercam`, and the socket keeps sending.

## H3: the page couldn't tell fresh data from old
- **Symptom:** untrustworthy state. Nothing showed how current the screen was.
- **Root cause:** the status had no timestamp or sequence number.
- **Fix:** every status carries `serverTime` and an increasing `seq` (see H9 for the UI).
- **Test:** H3, plus `check_live_link` "seq only goes up".

## H4: buttons hung for the length of the move, and double presses queued
- **Symptom:** Park, Slew, a filter change or a focuser move kept the button spinning for
  minutes. A second press queued a second command.
- **Root cause:** the handlers called the driver synchronously, so the HTTP request lasted as long
  as the move. Nothing stopped the same command being sent twice.
- **Fix:** new `tasks.py` (`DeviceTasks`) and `main._task`.
  - Mount slew/sync/tracking/park/unpark, filter position, focuser move and rotator move run as
    tasks. A task finished within 2 s answers as before, including a quick failure as a 400.
    Otherwise it answers `{"pending": true, "task": …}` straight away.
  - The same command on the same telescope while one is running gets a 409.
  - `status.tasks` lists running tasks and recent results (done/error). Slow failures and
    completions go to the log.
  - Task keys are per telescope (`<rig>:focuser:move`); the mount is shared (`mount:park`).
  - Abort, halt and stop are still direct.
- **Test:** H4, single and triple. Park: 4.01 s before, 2.01 s pending after. Second press:
  200 (queued) before, 409 in 0.00 s after. Quick commands still answer `ok` and quick failures
  still give a 400. A slow wheel failure ends as `error` in `status.tasks`. A slow move on rig 2
  doesn't block rig 3, and a second move on rig 2 gets a 409.

## H5: a button could stay disabled for ever
- **Symptom:** buttons stuck spinning, especially after a network drop or server restart.
- **Root cause:** `apiRaw` used `fetch` with no timeout. A request that was never answered never
  released its button.
- **Fix:** `app.js`.
  - Requests are abandoned after 45 s (POST), 22 s (GET), or 240 s for connect, solve, import,
    calibration and live stack. The message says "no answer after Ns — it may still be running;
    check the status".
  - A network failure says "could not reach Starfront".
  - A `pending` answer keeps the button busy and the toast as "…running" until `status.tasks`
    reports done (✓) or the error.
- **Test:** `check_viewer` H5 ×2 and H4-page ×2.

## H6: the viewer stopped showing the newest frame
- **Symptom:** after the second sub the main viewer kept showing an older frame. With several
  telescopes this happened almost at once.
- **Root cause:** `refreshImages()` always ended with `updateFramePosition()`, which set
  `state.browsing = index > 0`. As soon as a newer frame arrived, the frame on view was no longer
  index 0, so the page decided the operator was browsing and stopped following. With three
  telescopes the list interleaves them, so any other telescope's frame did the same.
- **Fix:** `browsing` is only set by what the operator does, in `selectImage(id, keepView,
  browse)`. Newest and the follow path pass `false`. A click or a step infers it from whether the
  frame is the newest of the telescope that took it. `updateFramePosition` no longer changes it.
- **Test:** `check_viewer` H6 ×3, including the triple-telescope case.

## H7: out-of-order answers showed old data
- **Symptom:** the image list occasionally lacked the frame just taken, or the viewer flicked back
  to the previous frame.
- **Root cause:** overlapping requests had no ordering. An older `/api/images`, `/stats` or render
  answering last overwrote the newer one.
- **Fix:** request counters in `refreshImages`, `selectImage` and `loadPreview`. Only the newest
  answer is drawn.
- **Test:** `check_viewer` H7 ×2.

## H8: two quick Capture presses started two exposures
- **Symptom:** occasional doubled frames or camera errors after a double click.
- **Root cause:** a check-then-act race in `CaptureService.start`. `busy` only turns true once the
  thread runs, and the slow `set_settings` driver call sits between the check and the thread
  start.
- **Fix:** a per-telescope start lock in `start` and `capture_blocking`. A second press gets
  "a capture is already starting".
- **Test:** H8. Before: `['started','started']`, 2 exposures. After: 1 exposure.

## H9: "live" while nothing was arriving (usability)
- **Symptom:** the badge said live with a dead or half-open socket (after sleep or a Wi-Fi
  change).
- **Root cause:** the badge only reflected `onopen`/`onclose`, and there was no watchdog.
- **Fix:**
  - The badge shows `live`, `no data Ns` (after 3 s) or `offline`, and its tooltip gives the time
    of the last status.
  - A banner at the top says "Starfront is not sending updates / Connection lost — reconnecting.
    What you see is from HH:MM:SS (Ns ago)."
  - A socket that has been silent for 10 s is closed and dialled again.
  - Devices reported `stale` are shown as dashed or amber pills, with a tooltip giving the age.
- **Test:** `check_viewer` H9 ×2.

## Existing checks
Full run with `runchecks.sh` (isolated data, HOME and LOCALAPPDATA per check). The suite includes
the triple-telescope checks (`check_triple`, `check_fsq_port`, `check_hardening`,
`check_live_link`) and the single-telescope ones. The only failures are the ones that already
fail on plain 0.2.47 (`check_controls` 2, `check_imagestack` livestack KeyError, `check_livestack`
1, `check_night_rollover` 2, `check_reference` 2). 36 checks, 6 min. `check_night_rollover` passed on this run (it fails on 0.2.47 and 0.2.49 at some times of day; that is a clock dependence in the test, not something this release changed). See `checks050.txt`.

## Not done / for Bray
- Not yet shown in a browser on real hardware. The page-side changes are tested under node with a
  fake DOM, and the server side over a real socket.
- The sequencer still drives devices directly, which is correct. During a sequence the manual
  buttons can still send a slew or a filter change: same behaviour as before.
- A `pending` command that fails late shows its error as a toast and in the log. The panels don't
  yet list running tasks; `status.tasks` is there for that.
- The status cadence is about 0.7 s instead of 0.4 s while a device is stale, because of the
  0.3 s status budget.
