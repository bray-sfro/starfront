# Quota 507 pauses one project

Patch on **0.2.53f** (the client in `packaging/fsq106-0.2.53f/Starfront-0.2.53f-source.zip`, hash `436e0f1697bc2856cce32f6c8b8619f57ecb6bac8520d36ef49a1b9443948962`).

This is not a new installer. `__version__` stays `0.2.53f`. Nothing here was packaged for Windows, installed, or started on a rig.

## What changed

A project's storage quota answers HTTP 507. 0.2.53f treated every 507 as a rig-wide pause (`paused_until`, about six hours) and `step()` then uploaded nothing. One full project (m42 horse test mosaic, 40 GiB) held every other project's queue, including M31.

A quota 507 now pauses that project only, the same way a 409 already does. Other projects keep uploading. A disk-full or infrastructure 507 still pauses the whole rig. The pause, and which kind it is, is written to `pauses.json` next to `queue.json` and is still there after a restart. The Collab tab line names each paused project and the queue still waiting for the others. It says "Auto-upload paused" only when the pause is the whole rig.

## How a 507 is classified

`pause_scope_for_507` in `astrocontrol/autoupload.py`, first match wins:

1. JSON `scope`: `project` (also `collaboration`) or `server` (also `all`, `global`). On the body, or inside `detail` when `detail` is an object.
2. JSON `reason`: `quota` (also `project_quota`, `storage_quota`) or `disk` (also `disk_full`, `infrastructure`, `filesystem`). Same places.
3. Header `X-Starfront-Pause-Scope: project` or `server`.
4. Detail text. Quota words (`quota`, `project storage`, `storage quota`, `storage limit`, `project is full`, `this project is full`) pause that project. Disk words (`disk`, `no room`, `keeping its last`, `free space`, `filesystem`, `inode`, `infrastructure`, `no space left`) pause every project.
5. Anything else stays global, so an unfamiliar disk-full sentence still holds the rig.

The sentence already used for the host disk, "the server is keeping its last 25 GB free; uploads are paused", stays global. "this project's storage quota is full (40 GiB)" is one project, even with no `scope` field.

## What the collab server should return

Please send this on the agent upload API (`/api/v1/agent/uploads`). The client already honours it; the text rule above is only the fallback.

Project quota:

```json
{"detail": "this project's storage quota is full (40 GiB)",
 "scope": "project", "reason": "quota"}
```

Host disk:

```json
{"detail": "the server is keeping its last 25 GB free; uploads are paused",
 "scope": "server", "reason": "disk"}
```

`Retry-After` is still how long the pause lasts (six hours when the header is absent). Optional header, same choice: `X-Starfront-Pause-Scope: project` or `server`. JSON `scope` wins if they disagree.

No collab-server change is in this patch.

## Files

```
caed063d9abe762d214a80f3d9df1d69973cabadac5adc54c955e860f43a5912  Starfront-quota-507-from-0.2.53f.patch
b8b4a80e358505fe2bb3b5270abb0715f21929b52dcd226b2c950f60ebe35cd0  Starfront-0.2.53f-quota-507-source.zip
```

The source zip is already patched. To apply onto a clean 0.2.53f tree: `patch -p1 < Starfront-quota-507-from-0.2.53f.patch`.

`python3 tools/check_autoupload.py`: 46/46 passed. See `TEST-SUMMARY.txt`.
