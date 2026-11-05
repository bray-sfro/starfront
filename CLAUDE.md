# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Starfront: astrophotography equipment control (mount, cameras, filter wheels, focusers, guider, flat panel) with a sequencer, planner and image pipeline, plus a collaboration server that deals sky to many telescopes, and a N.I.N.A. plugin that takes part in those collaborations. MPL-2.0. The program was formerly called AstroControl; the Python package is still `astrocontrol`, and the old `~/AstroControl` data folder and `ASTROCOLLAB_*` env vars are still honoured on purpose.

Three deliverables in one repo:

| | |
|---|---|
| `astrocontrol/` + `run.py` | the desktop program (Python 3.12+, FastAPI + uvicorn, pywebview window). Targets Windows (ASCOM is COM), but the checks run on macOS too |
| `server/` | the collaboration server at collab.starfront.space (FastAPI + SQLite, Discord sign-in) |
| `nina/` | the N.I.N.A. 3.2 plugin (.NET 8, C#/WPF), its own README and CHANGELOG |

## Commands

```bash
pip install -r requirements.txt
python run.py                 # application window
python run.py --browser       # same UI in a browser, no pywebview needed
python run.py --reload        # auto-reload for development (serves on 8765)
python run.py --server --host 0.0.0.0   # headless, reachable from another device (8765); --debug opens dev tools in the window
pythonw Starfront-debug.pyw   # the window with a stack watchdog: every 10 s each thread's stack goes to <data root>/logs/stacks.log, for a UI that freezes
python server/run.py          # collaboration server, loopback only; prints its admin token
python server/run_local.py    # the same, with a stand-in Discord (owner from --name), data under --data; local testing only
python server/run_local.py --lan   # ...listening on 0.0.0.0 so another PC on the home network can reach it (refused on a real server's data folder)
python tools/local_night.py   # the whole live-stacking system on one machine: run_local --lan, collab-hub, collab-worker (built from a Rapid Stacker Collab checkout, --rsc, or prebuilt with --bin-dir, e.g. the public release that deploy/home-lab/scripts/fetch-binaries.sh --from-release downloads); --synthetic --exit-when-stacked is its gate
```

**Three machines:** `deploy/home-lab/` (Terraform over SSH, README there) runs the server and hub on a Linux machine and a worker on each computer; its set-up steps live in `tools/lab_setup.py` (`register-hub`, `bind-collab`, `refresh-grant`, `smoke --via relay|hub`), which `local_night.py` imports. A N.I.N.A. PC installs the lab's plugin build from the lab server (`run_local.py --nina-dir`, which serves CI's build and `deploy/home-lab/install-nina-plugin.ps1`). `Running-the-servers-locally.md` is the longer how-to, including reaching the servers from N.I.N.A. on another PC; its sections after the banner are the older manual route.

**Tests are the `tools/check_*.py` scripts, with no test framework.** (The rest of `tools/` is utilities: `local_night.py`, `doctor.py` behind `Check Starfront.cmd`, `task_progress.py` for the distributed-stacking task list, the catalogue builders.) Each one is a standalone script that prints `PASS`/`FAIL` lines and a `n/m passed` count, and exits non-zero on failure. Run one with `python tools/check_<thing>.py`. To run a single case, run the script that covers it; there is no way to filter inside one. Which to run:

- `check_server.py`: the server and the agent protocol, over real HTTP with a real SQLite file
- `check_night.py`, `check_recovery.py`: the sequencer
- `check_controls.py`: run it whenever `astrocontrol/web/` changes. It statically cross-checks every `$('id')` in the JS against the HTML and the API routes
- live stacking: `check_stack_relay.py` (a fake hub against the real server, upload to relay to push to pages), `check_hub.py` and `check_volunteer.py` (the desktop's hub supervisor and worker installer, against fakes of the Rust binaries), `check_csp.py` whenever `server/web/stack/` changes, `check_local_server.py` for `run_local.py`
- the others are named for what they cover (`check_coordinator`, `check_filters`, `check_wcs`, …)

Checks isolate themselves by setting `ASTRO_DATA_DIR` to a temp dir before importing `astrocontrol`, and fake the hardware, Discord, the server and the Rust hub and worker. Follow that pattern in new checks. When fixing a fault, add a case that fails on the old code (each check's docstring tells the story of the faults it covers).

No linter or formatter is configured.

N.I.N.A. plugin (Windows, from `nina/`; copy `local.props.example` to the git-ignored `local.props` to set `NinaExecutable`, `PluginDeployDirectory` or `PluginPowerShell` for this machine):
```powershell
dotnet build src/Starfront.Collab/Starfront.Collab.csproj -c Release
dotnet build src/Starfront.Collab/Starfront.Collab.csproj -c Release -t:DeployPlugin   # into N.I.N.A.'s plugin folder; close N.I.N.A. first
```
These also run on macOS (anything but launching N.I.N.A.):
```bash
cd nina && dotnet build Starfront.sln -c Release   # the plugin (XAML included), the upload engine, the tests
dotnet test tests/RapidStackerCollab.Core.Tests    # the live-stacking upload engine (15 contract tests skip here by design)
dotnet test tests/Starfront.Collab.Tests           # live stacking's join codes, filter mapping, flat routing, field check, library sync, and hub routing, grants and LAN probe
```
**CI runs from `.github/workflows/` at the repository root** (since 2026-10-22, task T703): `ci.yml` builds and tests the plugin on pushes touching `nina/**`, `release.yml` fires only on `v*` tags (releases stay the author's), and `python-checks.yml` runs every `tools/check_*.py` on ubuntu and windows (`check_com_executor` on windows only, as it needs pywin32). Three checks are allowed to fail there, each with its reason in the workflow (`check_coordinator`'s four colour-camera cases, `check_night_rollover`'s timezone dependence, two `check_reference` angle cases). The `check_coordinator` four (133/137 locally on 2026-10-28) are: an RGB project taking a colour camera with no filters typed in, a narrowband-only project refusing it, the same night under a dark sky counting, and the colour Moon rule leaving a mono camera alone. All four fail on how one-shot-colour data is matched to a project's filters; clear an entry when its check is fixed. The local `dotnet build`/`dotnet test` and the `tools/` checks are still the gate before a commit.

**Live stacking** (since plugin 1.0.0.32, off by default; the current release, 1.0.0.33, follows Rapid Stacker Collab plugin 0.2.2.0, whose Core was copied from its commit `956fe16`): a collaboration can carry a Rapid Stacker Collab join code (`stackingJoinCode`, handed only to telescopes on it or owned by its owner), and the plugin sends each light the container saves for it to that service, unless it was pointed well away from the stacking project's field (`StackingTargets`). Unreleased since 2026-10-22 (`nina/CHANGELOG.md`, *Unreleased*): the stacking project flows from the collaboration, so the start form no longer takes a pasted join code (an old one keeps working), and a collaboration can be stacked on a LAN hub instead of the cloud, reached directly on the home network with the Starfront server as the fallback (`HubBackend`, `RoutingApiClient`, `LanProbe`, `GrantTokenStore`). Flats follow the night's lights of their filter to their projects (`StackingFlats`; a night spent on another target keeps its flats), and darks, bias and dark flats go to the rig's calibration library, which a folder can be kept in step with (`StackingLibrary`). Those three, and the hub files above, are written without N.I.N.A. or WPF so `Starfront.Collab.Tests` compiles them in (its `.csproj` lists each one); keep them that way. `nina/src/RapidStackerCollab.Core/` is a **verbatim copy** of that project's upload engine (MPL-2.0, EKE Services LLC) with its own nested build props and MPL-header rule: don't edit it here, re-sync it (`SOURCE.md`). Starfront's side is `nina/src/Starfront.Collab/Services/Stacking/`; with the setting off it must open, hook and connect nothing. `nina/MANUAL_CHECKLIST.md` is the in-N.I.N.A. check, not yet run.

**Line endings are mixed, file by file.** These are CRLF (checked 2026-10-24 with `git ls-files --eol`): `README.md`, `requirements.txt`, `nina/Starfront.sln`, in `astrocontrol/` `__init__.py`, `capture.py`, `livestack.py`, `main.py`, `pipeline.py`, `devices/base.py`, `devices/manager.py`, `imaging/wcs.py` and `web/` `app.js`, `index.html`, `style.css`, and `tools/check_livestack.py`, `check_plan_order.py`, `check_register.py`; `docs/README.txt` and `server/nina_plugin/README.txt` are mixed. Everything else tracked is LF. An edit that rewrites line endings turns a ten-line change into a whole-file diff.

Installer: `powershell -ExecutionPolicy Bypass -File packaging\build.ps1` (PyInstaller + Inno Setup). Its version is `astrocontrol.__version__`, and each version gets an entry in `CHANGELOG.md` (the desktop's; the plugin keeps `nina/CHANGELOG.md`). The installer is copied to `server/downloads/`.

## Releasing and deploying

- **Server deploy:** `Deploy the Starfront server.cmd` scp's `server/`, `astrocontrol/` and `requirements.txt` to the VPS and runs `server/deploy/install.sh` (systemd + Caddy). The server imports `astrocontrol`, so a change to the shared modules ships with the server.
- **Plugin release:** build the zip and manifest, then copy both into `server/nina_plugin/` (replacing the previous `Starfront.Collab.<ver>.manifest.json`; the manifest's checksum is of the zip's bytes, so never replace one without the other). Add the entry to both `nina/CHANGELOG.md` and `server/nina_plugin/CHANGELOG.md`, then deploy the server, which serves the feed at `/nina`. Commits are titled `N.I.N.A. plugin 1.0.0.N: …`.
  Releases are built with the `PackagePlugin` target, not `build/Build-Release.ps1` (that script writes a GitHub URL into the manifest's `Repository` and refuses to finish otherwise; the live manifests carry the Discord invite from `nina/Plugin.props`). From `nina/`, after `dotnet build src/Starfront.Collab/Starfront.Collab.csproj -c Release -p:Version=1.0.0.N`:
  ```bash
  dotnet msbuild src/Starfront.Collab/Starfront.Collab.csproj -t:PackagePlugin -p:Configuration=Release -p:Version=1.0.0.N \
    -p:PluginInstallerUrl=https://collab.starfront.space/nina/download/Starfront.Collab.1.0.0.N.zip -p:PluginChannel=Release -p:PluginPowerShell=pwsh
  pwsh build/Verify-ReleaseArtifacts.ps1 -Directory artifacts/release -Tag v1.0.0.N -Repository EKEServices/starfront -InstallerBase https://collab.starfront.space/nina/download
  ```
  The output lands in `nina/artifacts/release/` (empty it of older versions first: the check expects exactly one zip and one manifest). The packaging scripts need PowerShell; on macOS that is the .NET global tool (`dotnet tool install --global PowerShell`, then `~/.dotnet/tools` on `PATH`), since Homebrew has no stable PowerShell cask. `*.zip` is git-ignored: the zip is copied into `server/nina_plugin/` but never committed, and only the manifest is.
- **Docs:** `docs/*.html` is the source and the PDFs are printed from it with headless Edge (command in `docs/README.txt`). Copies of the plugin guides also live in `server/nina_plugin/` and must be refreshed with them.

## Architecture: the parts that span files

- **`astrocontrol/collab.py` is imported by both the program and the server.** It defines projects, tasks and contributions, and how they are judged and dealt (`collab.assign`). The server uses it to judge what arrives and the program uses it to warn an operator first. Change the rules there, never in one side only. `collabclient.py` is the telescope's half (ask, hold, report) and `collabadmin.py` is the coordinator's half.
- **The server protocol is pull-only.** An agent calls `POST /api/v1/agent/hello`, `GET /api/v1/agent/task?night=…` and `POST /api/v1/agent/report`; the server never reaches into an observatory. Agent tokens (telescopes) and user tokens (people, via Discord device-code sign-in) are separate dependencies. Neither can do the other's job, and that separation must be kept. A task arrives *offered* and is never silently applied.
- **Distributed live stacking (built 2026-10-22, all but the six manual home-network tasks; design in `STARFRONT_DISTRIBUTED_STACKING_PLAN.md`, progress in `TASKS.DISTRIBUTED_STACKING.md`, the six manual tasks step by step in `STARFRONT_DISTRIBUTED_STACKING_CHECKLIST.md`, a 2026-10-23 snapshot of open follow-ups in `..._FOLLOWUPS.md`).** The stacking itself is done by `collab-hub` and `collab-worker`, Rust binaries from the Rapid Stacker Collab repo (proprietary; only the integration lives here). The desktop supervises a hub (`astrocontrol/hub.py`, `/api/hub/*`) and installs a worker (`astrocontrol/volunteer.py`, `/api/volunteer/*`), which never runs a binary whose signed manifest and hash have not checked out; `MANIFEST_PUBLIC_KEY` is the real release key since 2026-10-28 (the same public key as the worker's `RELEASE_PUBLIC_KEY`; change both together), and the public release is tried last, after the server's `server/downloads/collab-*/`, from `RELEASES_URLS` in order: Rapid Stacker Collab's own downloads (`https://collab.astro.ekeservices.com/downloads/<product>/`, the canonical home since 2026-10-30, published by Collab's `publish-downloads.sh`), then the GitHub releases on this repository tagged `collab-worker` and `collab-hub`, which carried the first signed release (worker and hub 0.1.0) and stay as the mirror. The manifests name archives by relative path, so one signed set serves from every place; the programs are proprietary, not under the MPL. A new release means new files in both, not a new tag. On the server, `server/hubs.py` is a third credential beside agent and user tokens: a hub token (`sfh_`, kept only as its SHA-256) plus a per-hub secret that signs short-lived `sfg_` grants the hub verifies offline. It is still pull-only: the hub registers, heartbeats, pulls, fetches relayed frames (`server/stack/relay.py`) and pushes results (`push.py`). `/stack/api` (`server/stack/front.py`) is a sub-app speaking Rapid Stacker Collab's error envelope for uploaders, and `server/web/stack/` holds the progress pages under a strict CSP. `livestack.py` stays as the single-rig local preview only.
- **Units are honest everywhere:** star sizes in arcseconds, never pixels. Right ascension is in **degrees on the server** and **hours in the program** (`/api/mount/slew` takes RA in hours). Depth is integration time at a point on the sky. Where a person chose a time, it is stored as a *time of night*.
- **The program is a local HTTP + WebSocket API (`main.py`) with a no-build, no-framework UI (`astrocontrol/web/`).** The UI is only a client: `/ws` pushes the same state as `GET /api/status`. Per-telescope routes take `?rig=<id>` and default to the master telescope, which is looked up on every use, never cached, because it can change at runtime. `rigs.py` holds the live telescopes and `equipment.py` the saved ones.
- **Threading is deliberate.** Each telescope has its own COM apartment thread for ASCOM calls (`devices/ascom.py`), captures run on their own thread, and PHD2 has its own reader thread demultiplexing its socket (`devices/phd2.py`). HTTP handlers queue work and read state; they never block on hardware.
- **Device backends** implement the abstract classes in `devices/base.py` (ASCOM COM, Alpaca REST, PHD2, ZWO). `devices/manager.py` handles discovery and connection state.
- **There are no image or FITS library dependencies.** `imaging/` has its own FITS/XISF/PNG I/O, star detection (blobs against a local background, not peaks), WCS and reprojection. Keep it that way.
- **The sequencer (`sequencer.py`) runs the plan and recovers when it goes wrong.** `schedule.py` works out the night, windows and running order; `autoplan.py` picks the filters and sub counts; `overheads.py` holds the measured between-exposure costs.
- **Data on disk:** `config.data_root()` is `ASTRO_DATA_DIR`, else `~/AstroControl` if it exists, else `~/Starfront`. Settings, targets, plans, logs and frames live there and are git-ignored. `config.DEFAULTS` lists every settings key, and unknown keys are dropped on save, so a new setting must be added there. The server keeps its database and `admin-token.txt` under `STARFRONT_DATA`, never in the repo folder.

## Conventions

- `README.md` (very long) records the reason for every design decision, usually a night that was lost without it. Read the section for the area you are changing before changing it, and update it when behaviour changes.
- Commit messages say what went wrong and why the change is the fix, not only what changed. Comments and docstrings in the code are written the same way.
- Never commit settings, targets, plans, logs, frames, databases, `discord.env` or any token or secret (see `.gitignore`).
