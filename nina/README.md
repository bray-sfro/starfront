# Starfront for N.I.N.A.

The N.I.N.A. side of a Starfront collaboration: several telescopes on one
target, a single field or a mosaic. A plugin for N.I.N.A. 3.2 or later on .NET 8, built the way the
[N.I.N.A. plugin template](https://github.com/isbeorn/nina.plugin.template)
builds plugins, so N.I.N.A.'s own plugin manager installs and updates it.

## For people using it

1. **Add the Starfront plugin repository.** In N.I.N.A., Options > General >
   Plugin Repositories, press `+` and enter

   ```
   https://collab.starfront.space/nina
   ```

   Then open the Plugins tab, *Available*, and install **Starfront**. Restart
   N.I.N.A. Updates arrive the same way. You have to be in the Starfront
   Discord server to take part: https://discord.com/invite/3ut6a7pfsY

2. **Sign in.** Options > Plugins > Starfront > *Sign in with
   Discord*. A browser window opens; you need to be a member of the Starfront
   Discord server. Coming back, the page says who you are and that this
   telescope is enrolled under your account.

3. **Your exposures.** On the same page, the filters of your own wheel are
   listed by their own names; fill in the seconds per sub you normally shoot
   each at. The server works out which of its filters yours are. Your share
   of every collaboration is dealt at these lengths, so every light
   calibrates against your own darks. Gain, offset and binning are there
   too; -1 means the camera's default.

4. **Join a collaboration.** On the Imaging tab, open the **Starfront** panel
   (it is a dock panel like Image History). Each open collaboration says what
   it wants, how far it has got, who is on it and whether your telescope
   qualifies. Press *Join*. If a project wants longer or shorter subs than
   your table says, it tells you which filter to change.

5. **Put it in your sequence.** In the Advanced Sequencer, where a target
   container would go, add **Starfront collaboration** (category *Starfront*).
   Keep your usual triggers around it - autofocus, meridian flip, dither,
   centre after drift - they fire around every frame exactly as they would
   around a target. Leave the container's own conditions empty; it runs the
   night's deal once and stops.

When the sequence reaches the container it asks the server what this
telescope was dealt tonight - once; a night's deal holds for the whole night -
and builds a target for it: the field of a single-field collaboration, or one
per dealt panel of a mosaic. Each goes *is it up?*, slew and centre (and
rotate, with a rotator), switch filter, take the frames. A field below the
project's altitude floor, below your horizon or too near a Moon that is up is
skipped for now and tried again once the others are done. Every light is
credited to the sky it covered with its star size and guiding error, kept on
disk, and sent to the server on the next check-in. Frames shot while the
server was down go the next time it answers.

The target name in your file pattern is the project's name, with `panel <n>`
added on a mosaic, so your files sort by project and panel, and each FITS
header carries `STARFRNT` (the project) and `SFPANEL`.

6. **Live stacking (optional, off by default).** A collaboration's lights can
   be stacked as they land. The Starfront server makes the stacking project
   from the collaboration itself, on its coordinator's hub or on
   [Rapid Stacker Collab](https://collab.astro.ekeservices.com) once the
   coordinator has linked their Collab account in the Starfront program's
   Collab tab. Nobody makes a second project or pastes a join code, and each
   card says where the collaboration is stacked (and, while the project is
   still being set up or could not be, says that). Turn on *Live stacking*
   under Options > Plugins > Starfront and sign in to Rapid Stacker Collab
   there (a code to approve in the browser, as with Discord). At the next
   check-in the telescope joins the stacking project of every stacked
   collaboration it is on, and from then on each light the container
   saves for it is sent there as it lands: a copy with your location taken
   out, never your file itself. The Starfront panel shows what is waiting
   and going. A light pointed well away from the stacking project's field is
   not sent. Flats go to the projects that got the night's lights of their
   filter (an evening flat waits for them), and a night spent on another
   target keeps its flats here. Darks, bias and dark flats go to the
   telescope's calibration library, which serves every project; a folder of
   them can be kept in step with it under *Calibration library*. With the
   switch off the plugin connects to nothing new and behaves exactly as before.

   A collaboration whose stacking project is on a Rapid Stacker Collab *hub*
   needs no sign-in to Rapid Stacker Collab: the check-in carries the
   telescope's grant for it, and *Linked collaborations* shows the link as
   `(Hub: name)`. When the hub is on your home network the lights go to it
   directly; when it does not answer they go through the Starfront server, and
   a light waiting for a fresh grant is kept until one arrives.

## For people building it

Windows, the .NET 8 SDK (8.0.100 or later) and N.I.N.A. 3.2. PowerShell 7 is
not required; the scripts run on Windows PowerShell as well.

```powershell
cd nina
dotnet build src/Starfront.Collab/Starfront.Collab.csproj -c Release
dotnet test tests/RapidStackerCollab.Core.Tests     # the live-stacking upload engine; runs on macOS too
dotnet test tests/Starfront.Collab.Tests            # live stacking's join codes, filter mapping, flat routing, field check and library sync
```

`Starfront.sln` holds the plugin, the upload engine it ships
(`src/RapidStackerCollab.Core`) and that engine's tests. The whole solution,
XAML included, also builds on macOS (`dotnet build Starfront.sln`); only
N.I.N.A. itself needs Windows. `MANUAL_CHECKLIST.md` lists what has to be
checked inside N.I.N.A. before a release that touches live stacking.

Close N.I.N.A., then put the build where N.I.N.A. loads plugins from
(`%LOCALAPPDATA%\NINA\Plugins\3.0.0\Starfront`):

```powershell
dotnet build src/Starfront.Collab/Starfront.Collab.csproj -c Release -t:DeployPlugin
```

A release is a zip and a manifest, made from the same `Plugin.props` the DLL's
attributes come from:

```powershell
./build/Build-Release.ps1 -Tag v1.0.0.0 -Repository bray-sfro/starfront -InstallerBase https://collab.starfront.space/nina/download
```

That leaves `Starfront.Collab.1.0.0.0.zip` and its manifest in
`artifacts/release`. Copy both into `server/nina_plugin/` and deploy the
server (`Deploy the Starfront server.cmd`); the server serves the manifests at
`/nina/plugins/manifests` and the zips at `/nina/download/<name>`, which is
the plugin-repository shape N.I.N.A. expects. The checksum in the manifest is
of the zip's bytes, so the two always travel together.

Without `-InstallerBase` the release points at a GitHub release asset,
`https://github.com/<repo>/releases/download/<tag>/<zip>`, which the
`.github/workflows/release.yml` workflow publishes when a `v*` tag is pushed.
That is the path to the public listing: submit the manifest to
[nina.plugin.manifests](https://github.com/isbeorn/nina.plugin.manifests)
once the test is over. The central repository accepts open-source plugins from
public repositories only, and asks for an accountable human maintainer.

### How it is put together

| | |
|---|---|
| `Plugin.cs` | the manifest N.I.N.A. loads; data context of the options page |
| `Services/Runtime.cs` | the one set of services, shared across N.I.N.A.'s separate MEF containers |
| `Settings/` | the settings file (credentials protected for the Windows account) and the per-profile table |
| `Server/` | the server's API and the shapes it speaks |
| `Services/StarfrontSession.cs` | the check-in every few minutes; sign-in, enrol, join, leave, report |
| `Services/FrameReporter.cs` | credits every saved light to its panel by the target name in its metadata |
| `Ledger/` | what was shot, on disk, until the server has taken it |
| `Sequencer/` | the *Starfront collaboration* container, its panels, the altitude gate, the once-through strategy |
| `Ui/` | the options page, the exposure table, the Imaging-tab panel |
| `Services/Stacking/` | live stacking: the Rapid Stacker Collab sign-in, the link to each collaboration's stacking project and the join, the queue and the uploads, the flat router (`StackingFlats`), the field check (`StackingTargets`), the calibration library's folder sync (`StackingLibrary`), and the switch that turns all of it on and off |
| `Services/Stacking/RoutingApiClient.cs`, `ClientCache.cs` | sends each call to the cloud or to the hub of its own collaboration, with one cached client per base |
| `Services/Stacking/HubJoiner.cs`, `HubBackend.cs`, `GrantTokenStore.cs` | hub collaborations: the join from the check-in's grant, one rig id per hub, and the grant kept for each link (a refused grant holds that link only) |
| `Services/Stacking/LanProbe.cs` | the LAN short-cut to a hub on the home network, falling back to the Starfront server when the hub does not answer |
| `Services/Stacking/StackingLink.cs` | one link between a collaboration and its stacking project, cloud or hub |
| `../RapidStackerCollab.Core/` | the upload engine (resumable multipart upload, the SQLite queue, location stripping), copied from Rapid Stacker Collab's own N.I.N.A. plugin; see its `SOURCE.md` |

Everything the plugin owns is in the zip; nothing of N.I.N.A.'s is.
`build/Plugin.targets` lists exactly what ships: the plugin, the upload
engine, the SQLite it keeps its queue in (with the native
`runtimes/win-x64/native/e_sqlite3.dll`, which the plugin loads by full path
because N.I.N.A. adds a plugin's folders to the search path only after it has
started), `LICENSE.txt` and `NOTICE.txt`.

Licensed under the Mozilla Public License 2.0, like N.I.N.A. itself.
`src/RapidStackerCollab.Core` is © EKE Services LLC, also MPL-2.0, and keeps
its own file headers and build rules; `NOTICE.txt` lists the third-party
components that ship.
