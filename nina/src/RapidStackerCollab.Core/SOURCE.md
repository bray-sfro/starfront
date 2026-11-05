# Where this folder comes from

`RapidStackerCollab.Core` is the platform-independent core of the Rapid Stacker Collab N.I.N.A. plugin
(EKE Services LLC, MPL-2.0), copied here so the Starfront plugin can upload each saved light to a Rapid
Stacker Collab project for live stacking. It has no N.I.N.A., WPF or Windows dependency.

| | |
|---|---|
| Upstream repository | `github.com/EKE-Services/rapid-stacker-collab` (private; the plugin folder is to be mirrored publicly) |
| Upstream path | `nina-plugin/src/RapidStackerCollab.Core/` and `nina-plugin/tests/RapidStackerCollab.Core.Tests/` |
| Core copied from | commit `956fe161163dc3454fa882ef8bce24d6b6d4dbdb` (`master`, 2026-10-21: the upstream plugin 0.2.2.0) |
| Tests copied from | the same commit |

## Rules

- **No local edits** to the copied `.cs` files, `aliases.json` or the csproj files. A change Starfront needs goes upstream first and is
  copied back; a change that cannot wait is recorded below under "Local differences" with the reason, so a re-sync carries it.
- The copy is built to upstream's rules through the `Directory.Build.props` and `.editorconfig` beside it (nullable on, warnings
  as errors, MPL header on every file), not Starfront's.
- Tests that read the upstream repository's contract (`docs/api/openapi.yaml`, `docs/api/examples/strip-vectors.json`,
  `scripts/contract-examples.json`, `worker/crates/filters/`) are skipped here with a reason (`Support/RepoPaths.cs`). Vendoring
  those files under `nina/docs/api/` and `nina/scripts/` would make them run with no code change.

## Re-syncing

```bash
RSC=/path/to/rapid-stacker-collab; REF=master
git -C "$RSC" archive "$REF" nina-plugin/src/RapidStackerCollab.Core | tar -x -C src --strip-components=2
git -C "$RSC" archive "$REF" nina-plugin/tests/RapidStackerCollab.Core.Tests | tar -x -C tests --strip-components=2
```

Then restore this file, the two `Directory.Build.props` and the two `.editorconfig` files (`git checkout -- ...`), update the
commits in the table, build, run the tests, and diff the ported files below against their upstream sources.

## Local differences

None. The copied `.cs` files and the copied tests match commit `956fe16` exactly (checked by diff on 2026-10-22); the only
files here that upstream does not have are the ones the rules above name: the two `Directory.Build.props`, the two
`.editorconfig` files and this file.

## Candidates for upstream

Not local differences, so nothing below is patched here. These are changes Starfront would like upstream first, so a
later re-sync can drop a local workaround:

- `ApiClientOptions` taking a `Func<Uri>`, so one client could switch its base between the cloud and a hub's LAN address.
  Starfront keeps one client per base and chooses between them in `RoutingApiClient`, using `LanProbe`.
- A per-item base field on the upload queue, so that a resume after a base change stays on the base the item started on.
  Starfront pins an item's base in memory only (T603), so a resume after a base change can still move to the other base.
- A hook that tells a project's 401 from a global one. T607 works around it in the router: a hub 401 is isolated in
  `RoutingApiClient` and never reaches `UploadRunner.OnAuthRequired`, so the cloud sign-in is not marked as needed.
- `prefer_relative_part_urls` (plan section 6.4): a hint that lets a hub return relative part URLs, which a client can
  resolve against the base it used.

## Starfront's hub files (no upstream counterpart)

These sit on top of the Core's `ApiClient`, `UploadRunner` and upload queue. They are Starfront's own, but a re-sync must
diff them against the Core surface they call (the `ApiClient` members, `UploadRunner.OnAuthRequired`, the queue's
types) and fix any signature that moved:

- `Services/Stacking/ClientCache.cs`
- `Services/Stacking/GrantTokenStore.cs`
- `Services/Stacking/RoutingApiClient.cs`
- `Services/Stacking/LanProbe.cs`
- `Services/Stacking/HubJoiner.cs`
- `Services/Stacking/HubBackend.cs`
- `Services/Stacking/StackingLink.cs`

## Ported by hand, not copied

The Starfront plugin does not use upstream's Shell or Plugin projects (their settings, logging and MVVM worlds duplicate
Starfront's own). These upstream files were ported into `src/Starfront.Collab/Services/Stacking/` in Starfront's style and must
be diffed against upstream on a re-sync:

| Starfront file | Upstream source |
|---|---|
| `StackingUploader.cs` | `RapidStackerCollab.Shell/Hosting/CollabServices.cs` (the object graph and lifetime) |
| `StackingIntake.cs` | `RapidStackerCollab.Shell/Intake/FrameIntake.cs` (without attribution: the ledger names the collaboration; with the other-target rule, flats to the router, darks to the library) |
| `StackingFlats.cs` | `RapidStackerCollab.Shell/Intake/FlatRouter.cs` (the last light's project stands in for the active project, the links for the project catalog) |
| `StackingLibrary.cs` | `RapidStackerCollab.Shell/Library/LibrarySyncService.cs` (without its MVVM base; the options page wraps it) |
| `StackingTargets.cs` | `RapidStackerCollab.Shell/Intake/FrameIntake.cs` (`OtherTargetText`) over the Core's `ProjectAttribution.IsOtherTarget` |
| `StackingUploader.cs` (`FlatsLoopAsync`, the library loop) | `RapidStackerCollab.Shell/Hosting/CollabServices.cs` (`FlatsLoopAsync`, `LibrarySync.RunAsync`) |
| `StackingMapping.cs` | `RapidStackerCollab.Shell/Filters/FilterMappingGrid.cs` (the prefill rules) |
| `JoinCodes.cs` | `RapidStackerCollab.Shell/Projects/JoinFlow.cs` (the code normaliser) |
| `ImagingState.cs` | `RapidStackerCollab.Shell/Imaging/ImagingStateTracker.cs`, `RapidStackerCollab.Plugin/Services/NinaAdapters.cs` (`CameraStateConsumer`) |
| `VerdictPoller.cs` | `RapidStackerCollab.Shell/Dock/VerdictPoller.cs` |
| `TempCleaner.cs` | `RapidStackerCollab.Shell/Hosting/TempCleaner.cs` |
| `NativeSqlite.cs` | `RapidStackerCollab.Plugin/Services/NinaAdapters.cs` (`NativeSqlite.Preload`) |

`StackingJoiner.cs`, `StackingLinks.cs`, `StackingSignIn.cs` and the rest of that folder are Starfront's own, with no upstream counterpart to follow.
