# Merge notes — triple-RASA work, 2026-10-07

This tree (`C:\Users\Bray\Starfront-local`) is the working copy for the
triple-telescope / ZWO work done on the observatory PC on 7 October 2026.
It is kept outside Dropbox on purpose: a second session on another PC is
editing `Dropbox\astrocontrol` at the same time, and whole-file writes from
either side were silently undoing the other's edits all day.

The snapshot here was taken at 20:59 from the Dropbox tree after the second
restoration, so it already contains the other PC's *Python* live-stack work
up to 20:41 (`capture.py`, `pipeline.py`, `rigs.py`, `config.py`,
`calibration.py`, `livestack.py`, `web/livestack.js`,
`tools/check_imagestack.py`).  It does **not** contain that session's page
wiring for the live stack in `index.html`, `app.js` and `style.css` — those
three files were restored from this side and the live-stack ids
(`btnLiveStack`, `btnStackReset`, `stackInfo`, `stackMaxPixels`,
`stackPick`) are missing, which `tools\check_controls.py` reports.

## Files changed on this side (what to carry into the other tree)

| File | What |
| --- | --- |
| `astrocontrol/devices/zwo.py` | Aliases/serials for ZWO bodies; serial cache no longer trusted by device number (`_settle_duplicates`, `_forget`, `_held`); connect opens, verifies the serial and retries after a USB reorder (`_open`, `_expected_serial`); every SDK call under one process lock (`_Serialized`, `_LockedCall`); `status()` reports `canCool`, `gainRange`, `offsetRange`, `maxBin`, `sensor`, `pixelSizeUm`. |
| `astrocontrol/ninaimport.py` | Set up every telescope from the N.I.N.A. profiles in one step. |
| `astrocontrol/identify.py` | New: "which focuser is which" check (`PairingCheck`). |
| `astrocontrol/main.py` | `/api/nina/setup`, `/api/equipment/identify`, `/api/telescopes/filters` (GET/POST), `all=true` on `/api/camera/expose`, `/abort`, `/loop`, `/cooler`; cooler by hand cancels a warm-down; `sequencer.extra_aborts.append(pairing.abort)`. |
| `astrocontrol/sequencer.py` | `extra_aborts` list, called from `_abort_cameras`. |
| `astrocontrol/web/index.html` | Equipment page rebuilt around the all-telescopes overview (`rigOverview`), N.I.N.A. setup dialog, focuser-check dialog, `filtersDialog`. Script tag `app.js?v=64`. |
| `astrocontrol/web/app.js` | Overview cards (`renderRigOverview`, alias-first names), N.I.N.A. setup, focuser check, `askInstalledFilters`, capture/loop/abort/cooler on all telescopes (`cameraPath`, `coolerPath`, `anyCapture`), cooler buttons disabled only when `canCool === false`. |
| `astrocontrol/web/style.css` | Equipment dialog 920px wide; rig cards; setup rows; identify log; filters dialog rows. |
| `astrocontrol/web/plan.js` | Run sequence / Run on loop ask `app.askInstalledFilters()` first. |
| `astrocontrol/web/wizard.js` | First-light steps for N.I.N.A. setup and connecting every telescope. |
| `tools/check_triple.py` | New: 53 checks covering all of the above. |
| `astrocontrol/__init__.py` | Version 0.2.44. |
| `sequencer.py` (`_prepare_camera`), `main.py` (`CameraSettingsStoreRequest.coolAllAtStart`, `/api/settings/camera` routes it to the shared config), `web/index.html`, `web/app.js` | `camera.coolAllAtStart` (shared, default on): every camera that can cool is cooled at sequence start whatever each telescope's own `coolAtStart` says (rasa2 spent 2026-10-08 uncooled because its imported per-rig box was off). Checkbox at the top of Equipment > Camera, saved to the shared settings from whichever telescope's page it is ticked on. 2 checks in `check_night`. |
| `sequencer.py` (`_guiding_rough`, `_check_guiding`), `devices/phd2.py` (`start_guiding(fresh_star=True)` → `deselect_star` + `_find_a_star`), `config.py`, `main.py`, `web/index.html`, `web/app.js` | Rough-guiding recovery: guiding over `recovery.roughGuidingArcsec` (default 3") for `roughGuidingFrames` (2) frames in a row is treated like a lost star with no grace: stop, drop the star, auto-select a fresh one, restart (same attempts/budget as lost-star recovery); not again within `roughGuidingRetryMinutes` (10). 0" turns it off. Controls under Equipment > Recovery > The guide star > Rough guiding. 6 checks in `check_recovery` (Guider stub now reports `rmsTotal`/`samples`). Script tag `app.js?v=70`. |
| `capture.py`, `rigs.py`, `config.py`, `main.py`, `web/index.html`, `web/app.js` (recovery) | Optional disconnect-reconnect for a camera that fails a frame outside the sequencer's own retry: `CaptureService._reconnect_and_retry` (hook `reconnect_camera` set per telescope in `RigSet.sync` → `connect_remembered(rig, ("camera",))`) wraps Capture/Loop (`_run`) and `capture_blocking` (focus, centring, focuser check); settings `recovery.reconnectManualCaptures` (default on) and `recovery.hungSeconds` (default 120: an exposure not reported done this long after it should have counts as failed, replacing the fixed 300 s wait). Equipment > Recovery has the two new controls. 3 new checks in `check_triple`. Script tag `app.js?v=69`. |
| `sequencer.py` (Stop) | A Stop pressed by a person no longer parks the mount or stops tracking even with park-at-end on (`_stopped_by_hand`, set in `stop()`, cleared in `start()`); the end-of-run block logs that the mount is left tracking. Park-at-end still applies to a night that ends on its own, to weather shutdowns and to Abort & park. |
| `capture.py`, `focusing.py`, `identify.py`, `solving.py`, `sequencer.py`, `warnings.py` | Frames silently unsaved: focus sweeps, centring frames and the focuser check each remembered and restored `save_enabled`; two overlapping (a stopped run's sweep still finishing as the next run's began) restored each other's answer and left rasa1's lights unsaved for a whole target (2026-10-08 20:52–21:04). Now `CaptureService.suspend_saving()/resume_saving()` count borrowers and restore once; `set_output(save=)` during a suspension records the choice for afterwards; every target start forces `save=True`; the "Frames are not being saved" warning names the telescope(s). 5 new checks in `check_triple`. |
| `web/app.js` (d-pad), `main.py` (`/api/mount/jog/stop`), `devices/ascom.py` (`stop_jog(direction)`) | Hand control rebuilt to match N.I.N.A. (AscomTelescope.MoveAxis / TelescopeVM.Move+StopMove): press-and-hold via pointer events with `setPointerCapture` (the old mousedown/mouseup/mouseleave handlers stopped most presses after ~0.3 s in WebView2), release sends MoveAxis(axis, 0) for the pressed axis only, per-axis jog bookkeeping (`_jog_axes`), moves and stops logged with the rate. Verified end to end on the ASCOM Telescope Simulator: a 6.2 s press with the pointer leaving the button mid-way ran start to stop. Script tag `app.js?v=68`. |
| `web/app.js` (Equipment rows) | A slot's driver dropdown no longer locks while the device is connected; choosing "— none —" disconnects and forgets the slot immediately (`slotChanged` → DELETE `/api/equipment/telescopes/{rig}/devices/{kind}`), so a safety monitor can be dropped in one move. Script tag `app.js?v=67`. |
| `astrocontrol/main.py` (`/api/mount/jog`) | A jog on a parked mount unparks it first and says so in the log; the d-pad is no longer greyed while parked. A person at the controls outranks the safety shutdown's park. |
| `astrocontrol/devices/ascom.py` | COM executor has two lanes (`PriorityQueue`): `urgent=True` calls (jog, stop, abort, park, unpark, tracking) jump ahead of status polling, which on a serial-linked OnStep had queued a hand-controller press ~10 s. Mount `status()` serves AtPark/AtHome/Tracking/SideOfPier/SiderealTime from a 2–5 s cache (`_get_cached`), halving serial traffic per tick; commands clear the cache. `AxisRates` read at connect and jog rates clamped to an advertised range (`_allowed_rate`), as N.I.N.A. does. Verified against the ASCOM Telescope Simulator. |
| `web/app.js` (mount panel) | The N/S/E/W buttons are disabled with a tooltip while the mount is parked (the driver refuses MoveAxis on a parked mount, and the refusal only showed as an error after the press); a refused jog no longer leaves an unhandled rejection. Script tag `app.js?v=66`. Note for the Mount: homing (FindHome) does not work on this OnStep and must never be used as a park fallback. |
| `astrocontrol/sequencer.py` (park) | End of an ordinary run now uses the same verified park as the shutdown (`_make_safe` → `_park_and_check`), and `_park_and_check` retries a refused Park every 3 s until `parkTimeoutSeconds` after a 2 s grace once guiding stops (OnStep answers "Park failed: Mount in motion" for a moment after the last guide pulse; one refusal used to leave the mount tracking into the morning). A failed end-of-run park now also sends a failure notification. |
| `web/index.html`, `web/app.js`, `web/style.css` (viewer) | "All scopes" mode in the image viewer: `#multiView` grid of one tile per telescope (`allScopes`, `setAllScopes`, `updateAllScopes`), each following its own camera's latest frame via `render.png`, centre crosshair (shares `chkCrosshair`), `#allScopesZoom` centre crop (½, ¼, ⅛ via `region=`), remembered in localStorage. Script tag `app.js?v=65`. |
| `astrocontrol/plans.py` | `fitted_filters(rigs)` and `allocation_for(..., fitted)`: a follower telescope with a drawer filter shoots the master's frames through its own filter (rows relabelled and folded per exposure). Callers updated in `sequencer.py` (`_deal_key`, `_shoot`) and `main.py` (plan payload `allocations`). |
| `web/index.html`, `web/app.js`, `web/style.css` (Filters page) | Equipment → Filters now opens with "What is in each telescope": one row per telescope with a box to type the filter into, saved on leaving the box via `/api/telescopes/filters` (`renderScopeFilters`). The old per-telescope "In the light path now" select (`camFixedFilter`, `fillFixedFilter`) is gone; the wheel-slot controls remain below for telescopes with a wheel. |
| `astrocontrol/collabclient.py` | `_carried_filters()`: the collaboration profile lists every telescope's filters — wheel, typed slots, or the drawer filter (`camera.fixedFilter`) — merged across the mount, so a triple RASA with L/Ha/OIII in three drawers is no longer refused as "lists no filters". |
| `astrocontrol/collab.py` | The "lists no filters" refusal now says where to declare a drawer filter. `compatibility()` passes the filters check when the rig carries **any** of the project's filters (it used to fail a rig for the ones it lacked), naming what it can and cannot shoot. Shared with the server: redeploy the server so joins are judged the same way. |
| `server/app.py` | When dealing a night's filter, only filters the rig's profile carries are considered (a rig that declares none is offered all, as before). **The collaboration server must be redeployed for this to take effect** (`Deploy the Starfront server.cmd`). |
| `astrocontrol/main.py` (filters) | Saving the Run-time filter answers re-sends the profile to the collaboration server at once (`collab_client.hello()` on a thread). |
| `astrocontrol/sequencer.py` (filters) | The "plan asks for X but Y is fitted" warning is said once per telescope and filter per run (`_said_fitted`), not every frame. |
| `astrocontrol/sequencer.py` (slews, 0.2.45) | `_slew(mount, ra, dec, label)`: waits out a reported guide pulse (`mount.pulse_guiding`), half a second of grace, then retries a refused slew every 3 s until `slewRetrySeconds` (60). Used by `_goto` and `_flip`. 2026-10-19 20:47: OnStep answered "SlewError: Already in motion" 38 ms after PHD2's last pulse, and that one refusal parked the mount and ended the night. |
| `astrocontrol/devices/base.py`, `ascom.py`, `alpaca.py` | Mount `pulse_guiding` property (`IsPulseGuiding`); False where the driver cannot say. |
| `astrocontrol/sequencer.py` (flip, 0.2.45) | `_flip` returns whether the side of pier changed; a flip slew that left the mount on the same side is retried every `flipRetryMinutes` (5) up to `FLIP_RETRIES` (4) times while the flip is still due, between frames. OnStep keeps tracking past the meridian inside its own limit and only flips a goto beyond it; two minutes past was not beyond it on 2026-10-19 20:23. |
| `astrocontrol/sequencer.py` (`_wait_for_guiding`, 0.2.45) | PHD2's "Stopped" only counts as a failed start after 20 s of grace: read one second after `guide`, it had a panel shot "unguided" while PHD2 was in fact guiding. |
| `astrocontrol/rigs.py`, `sequencer.py`, `main.py` (cooler, 0.2.45) | `Rig.note_cooler` / `Rig.cooler_wanted` remember what the cooler was last asked for (sequence cool-down, warm-down, Cooler button); `RigSet.reconnect_camera(rig)` reconnects and then `Rig.restore_camera()` puts gain, offset, setpoint and cooler back. Used by `CaptureService.reconnect_camera` (hand/focus frames) and `_reconnect_cameras` (sequence frames). 2026-10-19 20:38: three cameras reconnected after a USB dropout and shot the rest of the night at +18 C. |
| `astrocontrol/web/plan.js`, `style.css`, `index.html` (0.2.45) | Per-telescope exposure/frames boxes: live totals on every keystroke but the save waits for the value to be committed (change / Enter / Tab / spinner), empty or partial values never reach the server; after a save lands the plan is redrawn once nothing is mid-edit (`renderWhenIdle`, `planEditing`, `focusout` on the list), and the running-sequence redraw also waits for that; "Follow the master again" button on a follower's tab (DELETE `/api/plan/entries/{id}/filters?rig=`). Script tag `plan.js?v=63`, `style.css?v=64`. |
| `astrocontrol/devices/pegasus.py` (new), `devices/manager.py`, `devices/base.py`, `ascom.py`, `alpaca.py` (0.2.46) | New `pegasus` backend: Pegasus Astro power boxes through Unity Platform's local web service (`http://127.0.0.1:32000`, routes from Unity's own OpenAPI document at `/docs/peg/api.json`). `list_devices("switch")` lists every box Unity has connected (driver id `UPBv3/<serial>`, optional `@host:port`); `PegasusUnitySwitch` connects (Connected list → DriverUniqueKey → OPTIONS Start → GET Report), exposes the 12 V outputs, USB ports, dew heaters, relay and adjustable outputs as named channels (names as typed in Unity), switches them with Unity's PUT routes (`PWM/{n}/On|Off`, `USB/{n}/On|Off`, `Dew/{n}/{level}`…), caches the report 2 s, reports the box's volts/amps/temperature as `readings`. UPBv2/PPBA/PPBMicro/Saddle routes are mapped but untested. `SwitchBank.describe_channels()` (names without values, no driver calls) on every switch backend. |
| `astrocontrol/rigs.py`, `config.py`, `sequencer.py`, `main.py` (0.2.46) | Automatic camera power cycling. `camera.powerChannels` (per telescope, read from the telescope's own overrides so a follower never inherits the master's outlets; empty = outlets whose Unity name equals the telescope name, e.g. ports labelled `rasa1`/`RASA1`). `recovery.powerCycleCameras` (on), `powerCycleAfterReconnects` (1), `powerCycleOffSeconds` (10), `powerCycleBootSeconds` (25). `RigSet.reconnect_camera(rig, stop, progress)`: plain reconnect first; a repeat reconnect of the same camera within 15 min (`POWER_CYCLE_WINDOW_SECONDS`) or a plain reconnect that fails goes to `_power_cycle` (abort if busy → disconnect → outlets off → wait → outlets back to what they were → boot wait → reconnect + `restore_camera`), never twice within 90 s, outlets always restored on Stop or error. `RigSet.power_cycle_camera` (hand), `power_mapping` (UI), `status()` adds `power` per telescope. Sequencer passes `stop`/`progress`. `POST /api/camera/powercycle?rig=` (refused while a sequence runs or the camera is exposing; runs on a thread). Models: `powerChannels`, the four recovery keys. `tools/check_power.py` (48 checks); `check_recovery`'s `RigsWithRestore` stub takes `stop`/`progress`. |
| `web/index.html`, `web/app.js`, `web/style.css` (0.2.46) | Equipment › Camera gets a "Power" section: one tick box per outlet on the box (`camPowerOutlets`, drawn from the master's `devices.switch.channels`, ticked from the telescope's own `powerChannels` or the by-name match), a note saying what recovery would switch and why, and "Power-cycle this camera now" (`btnPowerCycle`, confirm dialog). Equipment › Recovery › "The power box" (4 controls). Observatory panel shows the box's meters (`switchReadings`). `BACKEND_LABELS.pegasus`. Script tag `app.js?v=71`, `style.css?v=65`. |
| `astrocontrol/config.py`, `rigs.py`, `main.py` (0.2.47) | Any device tied to outlets. New shared section `power.outlets` = `{outlet name: device key}`; a device key is `"<rig id>:<kind>"` for the gear on one telescope and the bare kind for the master-only slots (`mount`, `guider`, `piercam`, `safetymonitor`, `dome`). `Rig.device_key(kind)`, `outlet_table()`, `assigned_outlets(kind)`; `Rig.power_channels(switch, kind="camera")` resolves assigned → (camera only) 0.2.46 `camera.powerChannels` → (camera only) by telescope name, skipping outlets assigned elsewhere; module `_match_outlets` (exact, loose, `index:N`). `RigSet.power_cycle_device(rig, kind)` (camera wrapper kept), `_power_cycle(..., kind)` keyed by `"<rig id>:<kind>"` in `_cycled_at`/`_cycling`, disconnects that kind and reconnects it (`restore_camera` for the camera). `power_mapping` adds `devices[kind] = {source, channels, lastCycled}` with the camera still spread at the top. `POST /api/settings/power` (`PowerSettingsRequest.outlets`, validated against the live telescopes and kinds, whole table replaced), `POST /api/power/cycle?kind=&rig=` (`/api/camera/powercycle` now calls it). `check_power.py` 67 checks. |
| `web/index.html`, `web/app.js`, `web/style.css` (0.2.47) | New Equipment › Power page: one row per outlet with a picker of every device (`powerOutlets`, options `rig:kind` grouped per telescope + Observatory; unassigned rows show the by-name hint), saved by the dialog's Save through `/api/settings/power` only while a box is connected (`readPowerOutlets`); a Gear list (`powerGear`) with the outlets each device would cycle and a Power-cycle button per device (`powerCycleDevice`). The Camera page's tick boxes are gone: note + "Power-cycle this camera now" + "Assign outlets…" (`btnPowerPage`). Observatory switch rows carry what each outlet feeds (`.switch-feeds`, from `outletFeeds()` over every rig's `power.devices`). `syncPowerUi` replaces `syncCameraOutlets`. Script tag `app.js?v=72`, `style.css?v=66`. |

## How to merge without losing either side

1. Stop one session before touching the other tree.
2. Take the other PC's `index.html`, `app.js`, `style.css` live-stack additions
   and apply them on top of this tree's versions (not the other way round:
   this tree's versions of those three files are the complete ones for the
   triple-telescope work).
3. Run `python tools\check_controls.py`, `python tools\check_triple.py`,
   `python tools\check_recovery.py`, `python tools\check_night.py`.
4. Put the result under git before the next session starts.  Git is not
   installed on the observatory PC yet.

Builds on this PC: PyInstaller is in the Claude scratchpad venv (Python
3.13), not in the project venv.  `packaging\build.ps1` fails inside Dropbox
because the sync client locks `packaging\dist`; build from this tree instead.

## 0.2.49 — unified build (2026-11-01)

One tree for every telescope PC: RASA multi-scope 0.2.47 (`Starfront-local`)
as the base, plus the calibration Dropbox layout (0.2.48), plus the nine FSQ106
commits (single-scope fork, 0.2.43) ported per telescope. Full notes,
conflicts and test results: `MERGE-0.2.49.md` next to the release zip.

| Change | Where |
| --- | --- |
| Calibration library in `<Dropbox>\RASA Rig\Calibration\RASA#\{Bias,Darks,Flats,Masters}\<date>` | `calibration.py`, `calibrating.py`, `capture.py` (`set_output(exact=True)`), `tools/check_calib_layout.py` |
| Image tab opens on the newest night's subs (per telescope, own subfolder) | `capture.recall_recent`, `main._recall_last_night`, `app.js` |
| Sticky warnings clear on success; Dismiss button (`POST /api/warnings/{key}/dismiss`) | `sequencer._unwarn`, `main.py`, `app.js` |
| Guiding: a lost star never recalibrates; rough restart N recalibrates only on a well-seen star; a calibration > 10° off square is refused | `sequencer.py`, `phd2.calibration_data`, `config.py` |
| Meridian flip: `sequencer.flipLimitMinutes` (default 0 = old behaviour); kept RASA's OnStep flip retries | `sequencer._flip_room/_flip_due/_flip`, `config.py`, `main.py`, UI |
| Desktop server keeps its port (`<data>\server.port`) | `desktop.py` |
| Start-of-target focus runs through each telescope's first filter | `sequencer._select_first_filters` |
| Focus/centring frames always come into view | `app.js` |
| N.I.N.A. Starfront Pier Watch plugin source (`nina/`) | C# only; `nina/build/PLUGIN-TARGETS-CHANGE.md` |
| Press feedback (busy button + toast), unsaved frames kept as previews, WS sends slow sections only on change | `app.js`, `style.css`, `capture.py`, `main.py` |
| `stackView` stub in `app.js` | live-stack page wiring is still missing (see below) |

Still missing (unchanged from 0.2.45/0.2.47): the live-stack page wiring and
its HTTP routes (`/api/livestack/*`, `/api/settings/livestack`). The pipeline
runs per telescope; nothing on the page shows it. `check_controls` reports it.

## 0.2.50 — stability hardening (2026-11-01)

Status that tells the truth, buttons that come back, a viewer that follows
the camera. Each item has a reproduction under `tools/`; details, root
causes and before/after results: `HARDENING-0.2.50.md` next to the zip.

| Change | Where |
| --- | --- |
| H1 Device status reads run on their own thread with a 0.3 s budget; a slow driver shows its last state with `stale`/`statusAge` instead of freezing every telescope's status | `devices/manager.py` |
| H2 Each status section is guarded; a failing one is named in `sectionErrors` and the rest still goes out (HTTP and socket) | `main._snapshot` |
| H3 Every status carries `serverTime` and `seq` | `main._snapshot` |
| H4 Slow device commands (mount slew/sync/tracking/park/unpark, filter, focuser, rotator) answer within 2 s, as `pending` if still going; a second press of the same command on the same telescope is a 409; `status.tasks` reports running/done/error | `tasks.py`, `main._task` |
| H5 Page requests time out (45 s, 240 s for connect/solve/import) with a message; a network failure says so; pending commands keep the button busy until `status.tasks` settles them | `app.js` |
| H6 The viewer stays on the newest frame: "browsing" is only set by the operator, never inferred when a newer sub (or another telescope's frame) arrives | `app.js` |
| H7 Out-of-order answers (image list, frame stats, renders) are dropped | `app.js` |
| H8 One capture start at a time per telescope (two quick presses started two exposures) | `capture.py` |
| H9 Live badge shows "no data Ns" when the socket is quiet, a banner when the link is stale or lost, and a silent socket is reopened after 10 s; stale devices are dashed in the pills | `app.js`, `style.css` |

Tests: `tools/check_hardening.py`, `tools/check_viewer.py` (needs node),
`tools/check_live_link.py` (needs `websockets`).

## 0.2.51 — sequence recovery (2026-11-01)

From the RASA PC logs of 2026-09-11 .. 2026-11-01 (and the FSQ106 night
copies): one camera off the USB bus ended the night for all three
telescopes, a hung download left the program "running" for 12.5 hours, and
nothing resumed after a restart. Details, incident timeline, root causes and
before/after results: `RECOVERY-0.2.51.md` next to the zip.

| Change | Where |
| --- | --- |
| R1 Per-telescope recovery state machine: a camera still failing after the quick retries is benched and recovered in the background (abort -> free capture -> reconnect / power box -> verify -> rejoin at the missed frame); the master camera no longer ends the run; the others keep imaging | `recovery.py`, `sequencer._shoot/_bench/_recover_rig` |
| R2 Bounded retries with an exponential backoff (`rigRecoveryAttempts` 6, 20 s doubling to 300 s); a telescope out of attempts sits out the target, every telescope out of attempts ends the night with a close-out | `sequencer`, `config.recovery` |
| R3 One camera reconnect at a time across telescopes, plus one retry after "invalid id" | `rigs.reconnect_camera` |
| R4 Timeouts on every device step: camera download (`downloadTimeoutSeconds`), per-telescope frame deadline in `_parallel`, cooling, filter, every recovery step; a stuck capture thread is released | `capture.py`, `sequencer._parallel` |
| R5 Watchdog: no progress for `watchdogMinutes` -> thread stacks to `logs/sequence-stacks-*.txt`, captures aborted; still stuck -> the run is given up, the night closed out (park), status says why; same for a run still going past dawn | `sequencer._watch/_abandon` |
| R6 Close-out on failure (`closeOutOnFailure` park/stopTracking/none) whatever parkAtEnd says; a park that fails stops tracking | `sequencer._run`, `_make_safe` |
| R7 Progress journal `sequence-progress.json` after every frame; on start-up the same night's interrupted run resumes at the exact entry, panel and frame (`resumeAfterRestart`) | `sequencer`, `main._resume_after_restart` |
| R8 Cooling stall: no 0.5 C progress in `coolStallSeconds` -> cooler re-asserted, then released, instead of 20 min with every telescope waiting | `sequencer._await_cooling` |
| R9 Guiding given up on is tried again after `guideRetryMinutes` (doubling) | `sequencer._retry_guiding` |
| R10 A dropped mount is reconnected (`mountReconnectAttempts`), a refused slew retried after a reconnect; tracking that stopped is switched back on between frames | `sequencer._mount_ready/_slew/_check_tracking` |
| R11 Status `rigRecovery` (state, attempt n/m, reason, step), `watchdog`, `resumed`; the recovery bar lists each telescope, the Observatory cards get a badge | `sequencer.status`, `app.js`, `style.css` |

Tests: `tools/check_recovery_real.py` (11 cases, one and three telescopes,
real sequencer and server with fake devices): 1/11 on 0.2.50 (only
`mount_dropped`, already handled there); 11/11 on 0.2.51.

## 0.2.52 — fleet dashboard (2026-11-01)

A page for every Starfront PC at once, run on any one PC on the observatory
LAN (reached over the VPN), personal use. How to run and set up:
`FLEET-0.2.52.md` next to the zip.

| Change | Where |
| --- | --- |
| F1 Opt-in LAN access: `server.lanAccess` (off by default), `server.fleetToken` (generated when LAN access is turned on), `server.lanPort` 8765. With it on Starfront binds 0.0.0.0 and every request not from the PC itself needs `X-Fleet-Token` (websocket: header or `?fleetToken=`); with it off LAN requests are refused unless started with an explicit `--host` (old tablet mode). The token is masked in /api/settings | `config.py`, `main._lan_guard`, `desktop.lan_bind`, `run.py` |
| F2 `GET/POST /api/settings/server`, `GET /api/fleet/node` (version, host name, telescopes) | `main.py` |
| F3 The fleet dashboard: `python -m astrocontrol.fleet`, `Starfront.exe --fleet`, `Starfront Console.exe --fleet`; `fleet.json` in the data folder (an example is written the first time). One poll thread per PC with a timeout; cards rebuilt from each PC's last answer (live / stale Ns / offline); mount, run, frame n of m, filter, focuser, camera temperature and cooler, guiding RMS, last frame thumbnail, recovery, warnings; summary bar; alerts (offline > N min, stalled, recovery failed, recovering too long, critical warnings) with optional browser notifications; per-PC run start/pause/resume/stop, per-telescope abort, park/unpark/slew (unpark and slew confirm on the page and on the server), each command followed pending -> done/failed through the PC's background-command status; optional `dashboardToken` for opening the page from another PC | `astrocontrol/fleet/`, `astrocontrol/web/fleet/` |
| F4 `tools/fleet_demo.py` (canned PCs, `--screenshot` through headless Chrome) and `tools/fleet_node.py` (a real Starfront on fake devices, for the check) | `tools/` |

Tests: `tools/check_fleet.py` (48 checks: token, aggregation, freshness,
isolation of a hanging PC, routing to the right PC and telescope, confirm,
alerts, dashboard token, and the page in jsdom).
