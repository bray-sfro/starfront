# "Start new stack" button

- Page: owners (collab owner or server owner; not rig/agent tokens) see a "Start new stack" button under the release preview.
  Disabled with "Stack queued" (Cancel offered) or "Stacking…" while a request is active or JOB.json is running and not stale.
- API (control service, :8800 by Caddy's default route): `GET|POST|DELETE /api/v1/releases/<p>/stack`.
  POST writes `<releases>/<p>/STACK_REQUEST.json` {id, state: queued, requested, by, byName}; 409 while one is
  queued/claimed/running (expires after 6 h untouched) or a non-stale job is running. DELETE cancels a queued one.
- Box: `stack_poller.sh` reads the file over ssh, claims it (claimed -> running -> done|failed, same id only),
  starts the progress bar with jobstatus.py, runs the per-project command from stack_projects.conf.
  Publishing stays with the command / the existing watcher.
