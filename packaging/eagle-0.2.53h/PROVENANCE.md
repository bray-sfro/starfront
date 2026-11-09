# 0.2.53h — backfill uses the capture root

Patch on **0.2.53g** (`packaging/eagle-0.2.53g/Starfront-0.2.53g-source.zip`).

EAGLE's `capture.rootDirectory` is blank. Capture writes to `data_root()/captures` (`C:\Users\primalucelab\Starfront\captures`). 0.2.53g's backfill treated a blank setting as "no folder", scanned nothing, and logged nothing.

0.2.53h calls `capture.capture_root`, the same function `CaptureService.root_dir` uses.

A capture folder that matches more than one collaboration target is not given the first name. A header `COLTASK`/`PROJID` that names one stamp wins. Otherwise the folder must equal exactly one stamp (target name, project name, project id, or task id, as written and as `clean_target`). If several stamps share the folder, the task this PC is on is used when it is exactly one of them. Anything still shared is skipped and logged.

On EAGLE the folder `m42_horse_test_mosaic` is project `97fcae827a9f`, task `481536614589` (accepted), not the declined task `b9412d46f576`.

The log line names the root and the counts: scanned, matched, queued, and skipped by reason.

## Apply

On a clean 0.2.53g tree:

```
patch -p1 < packaging/eagle-0.2.53h/Starfront-0.2.53h-from-0.2.53g.patch
```

The source zip is already 0.2.53h. `__version__` is `0.2.53h`.

```
9025eeea1b42f9ade53c4b30114e075a14ee785d95477dccd8b113d13a76b802  Starfront-0.2.53h-source.zip
1e65b4dfbf87c7be66d79ec1dd483ac631762e1d50cf7d701d7536578f56df4c  Starfront-0.2.53h-from-0.2.53g.patch
```

The same zip is in the agent store at `handoff/Starfront-0.2.53h-source.zip`.

No Windows installer. Nothing was installed on a rig.
