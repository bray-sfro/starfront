# Starfront 0.2.53e — FSQ106 centering (target-offset)

Patch carrier only. Do not merge this branch onto public `main` (0.2.43) and run that tree. The installed FSQ106 build is plain **0.2.53 single**, commit `fee06568ffcb14997087aca1a37d11159bdae41c` (`release/0.2.53`, tag `v0.2.53`). This package is that tree plus the centering fix. It is not Hakos 0.2.53b / 0.2.53c / 0.2.53d.

## Baseline match

- Installed `Starfront.exe` SHA256 `7431F83C97206CFD1E0F749BBF5570EE51D706B7813576F8C80C24D81BD4624F` (11,302,092 bytes)
- Adjacent install zip SHA256 `7FB47A8189887AFB39E06CD2516DB658C6543780BA0B53452EDA3A1AA8434556`
- Source used: `starfront-0.2.53-single-fee0656-src.tar.gz` SHA256 `464c34fa1a6a549de6fedea7af4bf5726e5e0899f3ff47e926b5299e12e1fc39`
- `VERSION.txt` on the rig: `Starfront 0.2.53`

## Config-only

No. `solver` has radius, tolerance, attempts, exposure, and ASTAP options. There is no offset-sync switch. Centring on fee0656 always called `mount.sync_to` (`SyncToCoordinates`) first, and only nudged after that call threw.

## What 0.2.53e does

Resulting version: **0.2.53e** (`astrocontrol/__init__.py`).

Centring never calls `SyncToCoordinates`. After each accepted solve the pointing offset is solved minus target, and the slew is target minus that offset (`aim = target + (target − solved)`), always from the target. The operator Sync button (`mode="sync"`) still calls `sync_to`; the centring loop does not.

- A solve whose rotation is about 180° from the camera angle or the last accepted solve, and which is not on that field, is rejected. The mount is not moved.
- A solve that jumps much further than the slew just commanded is rejected.
- A large error that is the right way up is kept, so a model that is wrong by more than the search radius can still be corrected.
- If the hinted radius finds nothing, one blind solve (no position hint, radius 180, field size kept) runs, then attempts go back to the configured radius.
- When attempts run out, the sequencer fails that target. It does not autofocus or image. The next target still runs. The night is not closed out for this alone.

## Windows package

Unpack `Starfront-0.2.53e-source.zip` and, in `Starfront-0.2.53e`:

```
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

`packaging\build.ps1` reads `__version__` and writes `packaging\dist\Starfront-0.2.53e-windows.zip` with `VERSION.txt` containing `Starfront 0.2.53e`.

Or, on a clean fee0656 tree:

```
patch -p1 < Starfront-0.2.53e-from-fee0656.patch
```

Not deployed. No mount, camera, or solver was commanded.
