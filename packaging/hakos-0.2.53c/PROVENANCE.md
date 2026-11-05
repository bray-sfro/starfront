# Starfront 0.2.53c — Hakos roof status and Alnitak cover prep

This directory is a patch against the installed Hakos baseline. It is not a
change to public `main` (0.2.43). Applying it on `main` will not work.

## Baseline

| | |
|---|---|
| Tree | `hotfix/0.2.53b-roofopen` |
| Commit | `d9d7cdac99d0cb3334f74475350da5be254f8955` |
| Version before | `0.2.53b` (`astrocontrol.__version__`) |
| Version after | `0.2.53c` |
| Installed exe SHA256 | `8520fa1d35a7fc27b6e70507d078a0f294f6406adb628484e09789b84573f439` |
| Installed console SHA256 | `5534a27ed6304a83824f6a5789b145aa52c2e94e7a5966be756b1f1411dbce5f` |
| Matching zip SHA256 | `932c0c5a293fc768fd370aa4572c3d3f46584dc7feb96088dc59cc1ca43353bb` |

The exe and console hashes match the 0.2.53b single-windows zip built from
that commit. Public GitHub `main` does not contain this tree (about 157
paths differ). Do not replace `main` with it.

## Apply

From a clean checkout of `d9d7cdac99d0cb3334f74475350da5be254f8955`:

```text
git apply --check 0001-roof-status-and-cover-prepare.patch
git apply 0001-roof-status-and-cover-prepare.patch
```

## Checks (ran on the patched tree, no hardware)

- `python tools/check_cover.py` — 15 of 15
- `python tools/check_roof_status.py` — 17 of 17
- `python tools/check_roof_open.py` — 50 of 50

## Build

On the patched tree, with the same Python used for a release:

```text
python -m pip install pyinstaller pillow
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

`packaging/build.ps1` writes `packaging/dist/Starfront-0.2.53c-windows.zip`
and `VERSION.txt` containing `Starfront 0.2.53c`. The installed folder name
used a `-single-windows` suffix; the script itself does not add `single`.

## Rollback

Put back the 0.2.53b single-windows zip whose `Starfront.exe` SHA256 is
`8520fa1d35a7fc27b6e70507d078a0f294f6406adb628484e09789b84573f439`.
No process was restarted and nothing was deployed from this change.

## What the patch does

Roof display and API: driver shutter words are labelled unverified. A missing
or stale read is unknown, not open. Accepting OpenShutter or CloseShutter does
not report a verified position. CloseShutter still sends one close; the waiter
no longer fails on the first still-open read (that failure was immediate in
the Nov 4 log while the driver later changed state).

Cover: ASCOM `CoverStatus` 0 stays NotPresent and is not commanded. 4
(Unknown) is not treated as no cover. A failed `CoverState` read is `unread`,
not NotPresent, and OpenCover is not sent on that failure. A successful
Unknown, Closed, Moving, or Error asks OpenCover once during light-path prep.
The device name is not used to invent a cover.

## What it does not do

The roof's physical open is not fixed and is not claimed. `OpenShutter` is
still the only open command, and the driver word is still not a roof sensor.
Dark frames are not treated as a closed roof. The flat-panel cap's physical
position is not known from `coverState: unknown`. This patch does not run
against a live panel or roof.
