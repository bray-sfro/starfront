# Mount clock check

Patch on **0.2.53f**. The clean tree is `Starfront-0.2.53f-source.zip` from the 0.2.53f centering work, hash `436e0f1697bc2856cce32f6c8b8619f57ecb6bac8520d36ef49a1b9443948962`. That is the same tree the quota-507 patch applies to.

This is not a new installer. `__version__` stays `0.2.53f`. Nothing here was packaged for Windows, installed, or started on a rig. TheSky's clock is not set, and the mount is not synced.

## What changed

On FSQ106, TheSkyX's own clock was wrong while the PC's timezone and the site coordinates were right. Centring then saw a right-ascension offset of about the same amount. This reads the clock the mount driver already exposes and warns when it is more than a minute from this PC.

The warnings watchdog runs `mount_clock` with its other checks. The first pass is a few seconds after startup, then every 15 seconds. The first time a mount is connected, `reported_clock()` asks the driver once. Later passes repeat that verdict, so the warning stays up without another driver call. Disconnecting drops it. The next connection is read again.

Compared, mount minus PC:

- UTC, from ASCOM `UTCDate` or Alpaca `utcdate`, against this PC's UTC.
- Local sidereal time, from ASCOM `SiderealTime` or Alpaca `siderealtime` (the property the mount status already reads), against sidereal time computed here from the PC clock at the site longitude. The PC's site is used. The mount's own longitude is used only when the site has not been set. The difference is taken the short way around 24 hours.

If either absolute difference is more than 60 seconds, the warnings board gets `mount.clock` (a warning, so the run is not stopped and nobody is paged) and the session log gets the sentence. `status.mountClock` carries the same verdict, including `clock check skipped` when no mount is connected or the driver did not report a time. Reading that field does not talk to the driver.

Nothing writes the clock, sends a sync, slews, or changes tracking.

## Files

```
05f51d796d3088c1f5e1a4300f3f99176f565dbaf5a9ee946086967c8d617ce3  Starfront-mount-clock-from-0.2.53f.patch
336160892d84797a44f21e6c0e5f5ca73d0fc528e6fcfd8f436639276d94b553  Starfront-0.2.53f-mount-clock-source.zip
```

The source zip is already patched. To apply onto a clean 0.2.53f tree: `patch -p1 < Starfront-mount-clock-from-0.2.53f.patch`.

The quota-507 patch touches different files. Both apply to the same 0.2.53f tree, in either order.

`python3 tools/check_mount_clock.py`: 36/36 passed. `python3 tools/check_warnings.py`: 53/53 passed. See `TEST-SUMMARY.txt`.
