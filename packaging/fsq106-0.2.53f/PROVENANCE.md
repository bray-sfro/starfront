# Starfront 0.2.53f — trusted field offsets back to the target

Patch on **0.2.53e** (FSQ106 centering). 0.2.53e is fee06568ffcb14997087aca1a37d11159bdae41c plus the 0.2.53e centering change. This is not Hakos 0.2.53b/c/d. Not a Windows build. Not installed.

## What changed

0.2.53e spent its only blind solve before the flip check, so later exposures stayed on the stale 15° mount hint.

0.2.53f:

- Each fresh exposure may blind-solve once if the hint misses. Rejecting that result does not block a later exposure from searching the whole sky again.
- A blind field is re-solved at its own coordinates with search radius `-r 1.5`. Image FOV stays `-fov` from the optics. Those are not the same number.
- `_centring_outlier` is unchanged. One flipped solve still does not move the mount.
- A field is trusted when that confirmed solve matches the optics scale and image FOV, or when two independent confirmed solves agree. There is no 30° give-up.
- A trusted field is slewed with the usual pointing offset back to the plan target: aim = target − (solved − target). The mount is not synced. The flipped angle is not written into the camera angle. The next exposure is an ordinary centring solve.
- For the offline field RA 22.7206h Dec +41.366° and target RA 0.71167h Dec +41.2667°, that aim is RA 2.7027h Dec +41.1674°. That is the command that lands on the target if the measured place is real. It is not a slew to 22.72h. An untrusted flip still does not command it.
- Attempts still end in a hard fail. No autofocus and no imaging.

The operator Sync button still calls `sync_to`. Centring does not.

## Files

```
e8961b5bff10771897f1a0f8dacff35363618646afd50aac0d3cb9132234446b  Starfront-0.2.53f-from-0.2.53e.patch
436e0f1697bc2856cce32f6c8b8619f57ecb6bac8520d36ef49a1b9443948962  Starfront-0.2.53f-source.zip
```

Apply on the 0.2.53e tree: `patch -p1 < Starfront-0.2.53f-from-0.2.53e.patch`.

Version in the source zip is `0.2.53f`. Windows packaging is done separately from that zip; this tree was not packaged here.

`python3 tools/check_centring.py`: 21/21 passed.
