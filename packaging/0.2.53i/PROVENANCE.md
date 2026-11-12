# 0.2.53i — flat panel cover status and open-on-start

Patch on **0.2.53h** (`packaging/eagle-0.2.53h/Starfront-0.2.53h-source.zip`, commit `07590abef3d175b38161fba4e0fffb01b01ac213`).

Public `main` is 0.2.43. This branch does not merge the application tree onto `main`. It carries the 0.2.53i source zip and a patch that applies to a clean 0.2.53h tree.

`__version__` is `0.2.53i`. Nothing was deployed. No roof command is sent from the new cover code.

## Apply

On a clean 0.2.53h tree:

```
patch -p1 < packaging/0.2.53i/Starfront-0.2.53i-from-0.2.53h.patch
```

`git apply` accepts the same patch. The source zip is already 0.2.53i.

```
3742f53a45971993a33f4de1a1c06434826d5ec2f1c4c6cc9cb588642f50c6d9  Starfront-0.2.53i-source.zip
55e241411871fe7ca70fb81937e6d874285c2fa4797285d649dc41d83275d6d7  Starfront-0.2.53i-from-0.2.53h.patch
```

The same zip and patch are in the agent handoff store.

## What was going wrong

Twin FSQ on EAGLE4PRO0258, Starfront 0.2.53, `ASCOM.OptecAlnitak.CoverCalibrator`, `autoCover` true, `coverTimeoutSeconds` 180. The lid was left closed after an L-flat that afternoon. The sequence at 7:30 PM CST unparked, homed, slewed, took 5 s L centring frames (median ~503 ADU, no ASTAP solution) and autofocused on 6 s R frames (~503 ADU). There was no "Opening the cover" line. At 7:36 the panel and camera reconnected. Only then, on the next light frame, did it log "Opening the cover for a light frame" and "Cover open". The following frame was still ~513 ADU.

Two bugs, both still in 0.2.53h:

1. `has_cover` was latched once in `connect()`. Unknown, and a failed `CoverState` read (`_get(..., 0)` which is NotPresent), were stored as "no cover" for the rest of the night. `open_light_path` then skipped Unknown and NotPresent and only ran for a light frame, after centring and autofocus. A failure or a timeout was swallowed and the frame was taken anyway.
2. The sequence-start `_open_cover` used that same latch, and it ran only when some plan entry was not calibration. It did not run before centring or autofocus, and a sky-flat-only run never reached it.

The 0.2.53c Hakos patch (`origin/cursor/hakos-roof-cover-6a72`) already treated Unknown as "ask OpenCover" and a failed read as `unread`, not NotPresent. That logic is reused here as `coverprep`. The roof-sensor work from that patch is not included.

## What 0.2.53i does

1. **Badge.** `coverstatus.py` turns the cached cover and calibrator reading into one line next to the roof badge: `Cover open · light off`, `Cover closed · light on 80/100`, and the same for Moving, Unknown, Error, and Not present. Each rig in the status payload has the same `cover` object.
2. **Status reads do not block.** `/api/status` kicks a probe thread and returns the cache. It never calls CoverState, CalibratorState, or Brightness on the request thread. A driver that hangs stays on the probe. The badge stays Unknown until a reading arrives.
3. **Buttons.** Open cover, Close cover, Light on (full brightness when no level is given), Light off. Each asks for confirmation. The API is the existing `/api/flatpanel/cover` and `/api/flatpanel/light`.
4. **Open is refused with HTTP 409** while `roofguard` is held (darks, bias, panel flats, including autocal), and while an exposure, a slew, or autofocus is running. Those three are refused because the sequence opens the cover *before* them, and nothing in the existing code shows that moving the lid during an exposure, a slew, or a focus sweep is safe. Close and the light are still allowed. No shutter command is sent.
5. **Before any sky frame.** Lights, centring, plate-solve frames, autofocus, and sky flats re-read CoverState. Unknown on a connected CoverCalibrator is opened and must reach Open within the timeout (`calibration.coverTimeoutSeconds`, 180 on EAGLE, unless the sequencer timeout has been turned down). NotPresent is the only "no cover" answer. A failed read is not treated as no cover and is not treated as open. The calibrator light is turned off. If the cover does not report Open, the run is held and nothing is homed, slewed, centred, focused, or exposed.
6. **The frame is checked.** A sky frame whose median is near the bias (the Twin FSQ's ~503 ADU) and whose spread is only a few ADU holds the run. The driver having said Open is not enough.
7. **Reconnect.** A camera reconnect, or a new cover connection, drops the cached reading. The next sky frame re-reads the driver and opens again if it is not Open. Darks, bias, panel flats, and autocal never take that path. Panel flats still close the cover and turn the light on. The roof is not opened for them, and Open Cover is refused while they run.
