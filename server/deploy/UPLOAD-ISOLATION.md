# Uploads isolated from the control path

Branch `perf/upload-isolation` (on top of b05f94e, live as of 2026-11-03).

## Why

Bray: opening and closing collabs was slow while a contributor web-uploaded
hundreds of 52 MB calibration frames (~18 MB/s, `PUT /api/v1/projects/<id>/files/<name>`).

Findings (live server, read-only, 2026-11-03 19:25-19:35 CST):

* One uvicorn process serves everything on a 1-vCPU droplet, and the upload
  handler is `async def`. Its file writes (`handle.write` per ~64 KB chunk),
  the FITS header read, `os.replace`, every SQLite call (behind the store's
  single process-wide RLock) and the re-judging of the uploader's set all ran
  on the event loop. Every control call (`PUT /api/v1/projects/{id}`,
  `agent/hello`, `agent/task`, `agent/projects`, `presence`, ...) waits in the
  same loop and for the same CPU.
* The droplet's CPU is ~6x slower than the box (same Python microbenchmark:
  4.9 s vs 0.8 s). At 18 MB/s python is ~25% and Caddy ~15-18% of the one
  vCPU; CPU pressure (PSI some avg60) was 32%.
* Each handler is individually cheap: on loopback, health 5-25 ms,
  `/api/v1/projects/{id}` 20-40 ms, judge_all for the uploader's 552 files
  1.3 ms, depth maps cached. No pathological scans of the files dir.
* What Starfront does on close/reopen: `PUT /api/v1/projects/{id}` then
  `collab_client.poll()` (hello, agent/task, agent/projects, presence), then
  `_collab_state` (auth, auth/me) and the tab's reload (health, agents,
  projects): ~10 sequential calls, each on a fresh HTTPS connection (urllib).
  Any per-request delay is multiplied by ten.
* SQLite was already WAL; busy timeout was Python's default 5 s; no
  `synchronous` setting (FULL).

## Changes

1. `STARFRONT_ROLE` (server/app.py): `all` (default), `control` (answers
   everything but uploads; uploads get 503 + Retry-After) or `uploads` (only
   uploads and `/api/v1/health`). `is_upload()` = PUT of
   `/api/v1/projects/<id>/files/<name>` or anything under `/api/v1/agent/uploads`
   (whose throttles live in memory, so all of it stays in one process).
   Health reports `role`.
2. `server/deploy/starfront-uploads.service`: the same program on
   127.0.0.1:8801 with `STARFRONT_ROLE=uploads`, `Nice=10`, `CPUWeight=20`,
   `IOWeight=20`. `starfront.service` gets `STARFRONT_ROLE=control`.
3. `server/deploy/Caddyfile`: those two path sets proxied to 8801, the rest
   to 8800. `install.sh` installs and starts both units.
4. Upload handlers (server/files.py web PUT; server/agentuploads.py chunk and
   finish): all checks, SQLite, disk writes (coalesced into 1 MiB writes),
   header read, rename and re-judging run in worker threads
   (`asyncio.to_thread`); the event loop only receives bytes.
5. server/store.py: `busy_timeout` 10 s (connect timeout and PRAGMA), since
   two processes now write the file; `synchronous=NORMAL` (safe with WAL).
6. tests/test_upload_isolation.py: path classification and role behaviour.

In-memory state checked before splitting: the rate limiter (uploads are not
rate-limited), `_figures_cache`/`_depth_cache` (keyed on contributions, which
uploads never touch) stay in the control process; the agent-upload throttles
(`_busy_agents`, `_streams`) move entirely to the uploads process. No
background threads or startup tasks exist. Not suggested: 2 uvicorn workers
for everything - the caches and agent-upload throttles are per process.

## Tests

* `pytest tests`: 86 passed, 29 skipped (no astropy, same as the server);
  with astropy installed 113 passed, same as before the change.
* `tools/check_server.py` (from the Starfront tree): 159/162, the same three
  Discord-role checks fail on the unchanged code too.

## Repro (box, /workspace/collab/perf)

Copy of the live DB (tokens replaced), server and Caddy (TLS, same version
2.11.4) pinned to one core with five busy loops on it (~1/6 core, close to the
droplet), uploads of 52 MB FITS-headed darks as the real uploader, and a
control loop doing Starfront's close/reopen sequence (10 calls, fresh TLS
connection each, toggling a closed test project open/closed in the copy).

| scenario | close/reopen sequence p50 / p95 / max (ms) | `PUT /projects/{id}` p95 | uploads |
|---|---|---|---|
| before, no uploads | 332 / 410 / 1256* | 107 | - |
| before, 18 MB/s | 435 / 560 / 812 | 81 | 19 MB/s |
| before, unthrottled | 1404 / 1565 / 1719 | 389 | 88 MB/s |
| threads only, 18 MB/s | 396 / 516 / 643 | 48 | 19 MB/s |
| threads only, unthrottled | 1144 / 1269 / 1380 | 164 | 88 MB/s |
| split + Nice 10, no uploads | 280 / 331 / 1213* | 43 | - |
| split + Nice 10, 18 MB/s | 315 / 371 / 515 | 42 | 11 MB/s** |
| split + Nice 10, unthrottled | 318 / 355 / 449 | 42 | 10 MB/s** |
| split, Nice 0, 18 MB/s | 348 / 393 / 536 | 39 | 19 MB/s |
| split, Nice 0, unthrottled | 444 / 521 / 566 | 59 | 98 MB/s |

\* first `agent/projects` after start builds its cache.
\** The busy loops that emulate the slow CPU run at nice 0 and outrank the
niced uploads; on the droplet uploads only yield when the control process
actually wants the CPU (it is ~60% idle at 18 MB/s), so expect close to full
speed there. If uploads should never be slowed at all, use Nice=0 /
CPUWeight=100: control still stays within ~10-25% of idle.

## Deploy (not done; needs Bray's OK)

A restart drops in-flight uploads. Telescope auto-uploads resume from their
offset; a files-page upload in flight is lost, and the contributor re-runs the
folder (same name and size are skipped). So deploy in a gap:

1. Gap check (repeat until all zero, and nobody is mid-close in Starfront):
   `journalctl -u starfront --since -3min | grep -c 'PUT /api/v1/projects/[^ ]*/files/'`;
   `find /var/lib/starfront/files -name '*.part' -mmin -2 | wc -l`;
   `ss -tn state established '( dport = :8800 )' | wc -l` (only the poll traffic).
2. Backups: `sqlite3 /var/lib/starfront/collab.sqlite ".backup /var/lib/starfront/collab-pre-deploy-$(date +%Y%m%d-%H%M).sqlite"`;
   `tar czf /root/starfront-config-$(date +%Y%m%d-%H%M).tgz /etc/caddy/Caddyfile /etc/systemd/system/starfront*.service*`;
   `cd /opt/starfront && git tag pre-deploy-$(date +%Y%m%d-%H%M)`.
3. Code: fetch the branch bundle, `git checkout perf/upload-isolation` (or
   fast-forward master), `venv/bin/python -m pytest -q tests`, then
   `git tag deploy-$(date +%Y%m%d-%H%M)`.
4. Uploads unit: `install -m644 server/deploy/starfront-uploads.service /etc/systemd/system/`;
   `systemctl daemon-reload && systemctl enable --now starfront-uploads`;
   `curl -s 127.0.0.1:8801/api/v1/health` shows `"role":"uploads"`.
5. Caddy: write the new Caddyfile with the live host line
   (`collab.starfront.space, starfront-bray.duckdns.org`),
   `caddy validate --config /etc/caddy/Caddyfile`, `systemctl reload caddy`.
   New uploads now go to 8801; uploads already streaming into 8800 finish there.
6. Control: drop-in `/etc/systemd/system/starfront.service.d/role.conf` with
   `[Service]\nEnvironment=STARFRONT_ROLE=control` (leaves the live unit file
   alone), `systemctl daemon-reload`, re-check the gap (step 1), `systemctl restart starfront`.
7. Health: `curl -s 127.0.0.1:8800/api/v1/health` -> `"role":"control"`;
   public health; an unauthenticated `curl -X PUT https://collab.starfront.space/api/v1/projects/x/files/a.fits -d x`
   -> 401 and the line appears in `journalctl -u starfront-uploads`;
   time the read-only control GETs; watch both journals for 5 minutes.
8. Rollback: `git checkout pre-deploy-<ts>`, remove the drop-in, restore the
   Caddyfile from the tar and reload Caddy, `systemctl disable --now starfront-uploads`,
   `systemctl restart starfront`; DB restore from the .backup only if needed.

## Follow-up worth doing in Starfront (client)

`collabadmin`/`collabclient` open a new HTTPS connection per call (urllib), so
a close is ~10 TLS handshakes from the observatory. A keep-alive session
(`http.client` connection reuse or `requests.Session`) and skipping the
duplicate state reload would cut a close/reopen to a few round trips. Needs a
Starfront release; not in this branch.

## Moving uploads off the control host (proposal)

Today: one $6 droplet (1 vCPU/1 GB) + a 300 GiB block volume ($0.10/GiB-mo
= $30/mo) holding ~119 GiB, growing ~13 GiB a night while M31 collects.

### Option A - second small droplet for ingest

An "ingest" droplet in NYC1 owns the files volume and runs the uploads role,
the files page downloads, releases and Caddy for an `uploads.` host name;
the control droplet keeps SQLite and every control route.

* Cost: +$6/mo (1 GB) or +$12/mo (2 GB); volume unchanged ($30/mo now,
  $0.10/GiB as it grows: 1 TiB = ~$100/mo).
* Catch: a block volume attaches to one droplet, and the upload path writes
  file rows into SQLite. Either the ingest box calls a small authenticated
  internal API on the control box over the VPC to register each file (and to
  read project/quota/rules), or the files tables move to their own database
  on the ingest box. DO Network File Storage ($0.15/GiB-mo) could share one
  filesystem instead, at 1.5x the volume price.
* Migration: create the droplet in the same VPC, add `uploads.collab...` DNS,
  detach/attach the volume in a gap, deploy the uploads role there with the
  internal API, point Caddy's upload routes at it (or the clients at the new
  host).
* Clients: none required if the control Caddy proxies upload paths to the
  ingest box over the VPC (bytes still cross the control box's Caddy, which
  is 15-18% CPU at 18 MB/s). Full isolation needs Starfront, the NINA plugin
  and the files page to send uploads to the `uploads.` host.

### Option B - direct to DO Spaces with presigned URLs (recommended long term)

Bytes never touch either droplet. The client asks the control server for
permission, PUTs straight to a Spaces bucket in NYC3, then tells the server
it is done.

* New endpoints: `POST /api/v1/projects/{id}/files/{name}/presign`
  (auth, quota, collecting checks; returns a presigned PUT URL, or a multipart
  upload id + presigned part URLs for resumable telescope uploads) and
  `POST .../complete` (server HEADs the object for size/ETag, ranged-GETs the
  first few KB for the FITS/XISF header facts, adds the file row, re-judges).
  Stale multipart uploads expire by a bucket lifecycle rule.
* Cost: Spaces $5/mo includes 250 GiB stored + 1 TiB egress; then
  $0.02/GiB-mo stored and $0.01/GiB egress. 120 GiB = $5; 500 GiB = $10;
  1 TiB = ~$20 (vs ~$100 on block storage). Inbound is free, and NYC3 ->
  NYC1 droplet transfer is free. Old subs could go to cold storage
  ($0.007/GiB-mo). The 300 GiB volume could shrink or go once migrated.
* Server changes: boto3 (S3 API) and Spaces keys in the server's env;
  downloads become presigned GETs; the stacking pipeline and Dropbox copy
  read from Spaces (rclone/s3) instead of the volume; release masters can
  stay on the volume or move too.
* Clients: files page uses presign/PUT/complete (bucket CORS for the site
  origin); Starfront auto-upload maps its chunk/finish protocol onto
  multipart parts (sha256 kept client-side, checked against a server-side
  hash on complete or via S3 checksum headers); the NINA plugin (C#, source
  location still unknown) needs the same. Keep the current PUT endpoints for
  older clients during the changeover.
* Migration: create the bucket + keys, ship server support behind a flag,
  `rclone copy` the existing files to the bucket with the same keys
  (`<project>/<user>/<kind>/<name>`), flip reads to Spaces, release the
  clients, then retire the old upload path.
