# Starfront 0.2.53d — camera call quarantine

Patch against the 0.2.53c tree, which itself patches
`d9d7cdac99d0cb3334f74475350da5be254f8955` (installed 0.2.53b).
Do not merge this onto public main (0.2.43).

## Apply

On 0.2.53c:

```text
git apply --check 0001-camera-call-quarantine.patch
git apply 0001-camera-call-quarantine.patch
```

From the installed baseline, apply the 0.2.53c patch first, then this one.
Resulting `astrocontrol.__version__` is `0.2.53d`.

## Build

`packaging/build.ps1` writes `Starfront-0.2.53d-windows.zip` and
`VERSION.txt` from `__version__`. README and `packaging/READ ME FIRST.txt`
are already in the tree.

## Rollback

0.2.53c source zip, or the installed 0.2.53b zip whose Starfront.exe SHA256 is
`8520fa1d35a7fc27b6e70507d078a0f294f6406adb628484e09789b84573f439`.
This change was not deployed.

## Behavior

A timed-out or cancelled camera call is not treated as cancelled inside the
driver. Until that call returns, Starfront does not start another exposure,
download, abort, disconnect, or reconnect on that camera, and does not save
or upload a frame. Automatic outlet power-cycling is no longer part of
camera reconnect. The 0-to-0 exposure-limit guard still blocks StartExposure.
Cooler, gain, and offset are restored only after a reconnect that is allowed
to run. Three failed reconnects stop. A hand power-cycle button remains.
