# Live stacking: the check inside N.I.N.A.

What has to be seen on Windows, in N.I.N.A. 3.2, before a release that touches live stacking. The build and the
upload engine's tests run anywhere (`dotnet build Starfront.sln`, `dotnet test tests/RapidStackerCollab.Core.Tests`);
none of what follows can, because it is N.I.N.A. loading the plugin, drawing it, and calling it.

**The stacking service.** The Rapid Stacker Collab stack at `collab.astro.ekeservices.com` was taken down on
2026-10-16 and is brought back by its own `deploy.sh`, which is its owner's call. Until it is up, sections A, B
and the parts of C to E that do not need a server can be done; the rest waits for it. Point the plugin at another
deployment with *Rapid Stacker Collab server* on the options page.

Tick each box only when the *Expected* line is what happened. Write the N.I.N.A. version and the plugin version at
the top of your copy.

## A. Installing, with live stacking off

- [ ] Close N.I.N.A., then `dotnet build src/Starfront.Collab/Starfront.Collab.csproj -c Release -t:DeployPlugin`.
  *Expected:* `%LOCALAPPDATA%\NINA\Plugins\3.0.0\Starfront\` holds `Starfront.Collab.dll`,
  `RapidStackerCollab.Core.dll`, `Microsoft.Data.Sqlite.dll`, three `SQLitePCLRaw.*.dll`,
  `runtimes\win-x64\native\e_sqlite3.dll`, the two PDBs, `LICENSE.txt` and `NOTICE.txt`, and no `NINA.*` file.
- [ ] Start N.I.N.A. *Expected:* no plugin error at start; Options > Plugins lists Starfront; the log has
  `Starfront plugin … initialised` and no `Starfront stacking:` line.
- [ ] Options > Plugins > Starfront. *Expected:* a *Live stacking* section below *This telescope*, its switch off and
  everything below the switch greyed out, on both the dark and the light theme.
- [ ] Look in `%LOCALAPPDATA%\NINA\PluginSettings\896B4E28-7CEB-4731-BB97-8BADE7460268\`. *Expected:* no `stacking`
  folder. In a command prompt, `tasklist /m e_sqlite3.dll`. *Expected:* no NINA.exe line.
- [ ] Run the Starfront container on a collaboration for a few frames with the simulator camera. *Expected:* the
  ledger and the report work as before (*Frames reported* fills in), and still no `Starfront stacking:` line.

## B. Turning it on, and the sign-in

- [ ] Turn on *Send my lights to live stacking*. *Expected:* the rows below come alive; the log says
  `Starfront stacking: on; not signed in to Rapid Stacker Collab yet`; the `stacking` folder appears with
  `queue.db` and `tmp\`; `tasklist /m e_sqlite3.dll` now lists NINA.exe (the bundled library loaded from the
  plugin's `runtimes` folder; no SQLite error in the log).
- [ ] The Starfront panel. *Expected:* a *Live stacking* block under Tonight's deal saying to sign in.
- [ ] *Sign in to Rapid Stacker Collab*. *Expected:* the browser opens on the approval page with the code filled in;
  the options page shows the same code with *Open the browser again* and *Cancel*.
- [ ] Against the local stack (`http://127.0.0.1:8787/api/`, through an SSH tunnel if it runs on another machine).
  *Expected:* the browser opens on `http://127.0.0.1:8787/device` with the code filled in, as with https. Against
  plain http on any other address it does not open; the line under the button gives the address to open by hand.
- [ ] Approve it. *Expected:* within a few seconds *Signed in as …* and *The sign-in is good for … days*; the log
  says `signed in to Rapid Stacker Collab as …`; `settings.json` holds `StackingToken` as unreadable text (DPAPI),
  never the `rscu_` token itself.
- [ ] Start a sign-in and press *Cancel*. Start one and press *Deny* in the browser. Start one and let it expire.
  *Expected:* each ends with its own line under the button and leaves the plugin signed out (or as it was).
- [ ] *Sign out*. *Expected:* signed out; the token no longer works on the website's token list.
- [ ] Turn the switch off while signed in. *Expected:* `Starfront stacking: off` in the log, the panel block goes,
  `tasklist` still lists the library (it stays loaded) but nothing else happens. Turn it on again: the sign-in is
  still there.

## C. Linking a collaboration and joining

The stacking project is made by the Starfront server from the collaboration (T706); nothing is pasted. For the
cloud, the coordinator first links their Rapid Stacker Collab account once in the Starfront program's Collab tab
(*Live stacking*, an API token with *Manage projects*); for a hub, they own or are listed on a registered hub.

- [ ] With the account linked and no hub, *Start a collaboration* on the panel. *Expected:* the form has no join code
  box; the collaboration's project appears among *My projects* on the Rapid Stacker Collab website with the
  collaboration's name and filters, and the card says *Live-stacked on Rapid Stacker Collab*.
- [ ] Unlink the account in the Collab tab, then start another. *Expected:* not stacked (no line on its card). Link
  it again, but stop the Collab service (or point the link at a dead address in a test setup) and start one more.
  *Expected:* the owner's card says *…could not be set up yet:* with the reason; another telescope's card says *the
  owner's Starfront Collab tab says why*; the log says `… is not ready on Rapid Stacker Collab yet (failed…)` once,
  not at every check-in. Bring the service back: within the retry wait the card goes to the normal line.
- [ ] *Edit* an old collaboration that was given a pasted code before T706. *Expected:* the code shows read-only as
  `XXXX-XXXX-XXXX` and still works; a different code is refused with the message under the form.
- [ ] As the owner of a registered hub, start a collaboration. *Expected:* the card says *Live-stacked on the hub
  <its name>*, with no Rapid Stacker Collab sign-in needed.
- [ ] On a second telescope that has not joined a cloud collaboration, open its card. *Expected:* *Live-stacked on
  Rapid Stacker Collab* but no code anywhere; join it and refresh: the code is now given.
- [ ] With the switch on and signed in, wait for a check-in (or sign in again, which checks in). *Expected:* the log
  has `Starfront stacking: filters for …` and `… -> … (joined): Joined …`; *Linked collaborations* shows it with the
  filter mapping; `stacking\links.json` has the link; the card says *Joined …*.
- [ ] A telescope whose camera has never been connected. *Expected:* *Not joined: connect your camera first…*;
  connect it and press *Retry now*: joined.
- [ ] A wheel with none of the project's filters. *Expected:* *Not joined: none of this wheel's filters…*.
- [ ] A project that approves members by hand. *Expected:* *waiting for its owner to approve*; approve on the
  website and wait up to half an hour (or restart N.I.N.A.): *Joined*.
- [ ] Bump the service's terms version while a telescope is joined. *Expected:* its frames keep going (the upload
  route does not check terms, and neither does the plugin). The *Accept* path needs the terms to change between the
  join's resolve and its join; with a test server that answers `terms_outdated` to the join, *Expected:* *The terms
  of … changed; accept them to carry on*, *Read the new terms* opens them, *Accept them and carry on* joins.

## D. Sending frames

- [ ] Run the container for a few lights. *Expected:* for each light the log has `Starfront stacking: <file> queued
  for …`, then `sent …` once the camera is idle; the panel counts *waiting*, then *sent*; the file in flight shows
  with a percentage.
- [ ] Open the frame on the stacking project's website, or download it. *Expected:* no `SITELAT`, `SITELONG`,
  `SITEELEV`, `OBSERVER`, `CENTALT`, `CENTAZ`, `OBJCTALT`, `OBJCTAZ` or `AIRMASS` in its header; `stacking\tmp\` is
  empty afterwards; the original file in your image folder is byte for byte as N.I.N.A. wrote it.
- [ ] A light shot outside the Starfront container (a plain Deep Sky Object). *Expected:* not queued; nothing logged
  for it above debug level.
- [ ] Save as XISF for one run. *Expected:* sent like FITS, with the location properties gone.
- [ ] Take flats the next morning (or the evening before the lights). *Expected:* each flat is logged `flat …: lights of
  the same filter and night went to …` and queued for that project; an evening flat is counted on the panel as *waiting
  for its night's lights* until a light of its filter is queued. A flat of a filter no light used that night waits
  until the next midday, then goes by the fallbacks or is logged `not sending flat …`.
- [ ] Shoot a few lights of a target that is no collaboration's, then flats of their filter. *Expected:* the flats are
  logged `not sending flat …: this night's lights of that filter were of another target` once their wait is over.
- [ ] Point the container's target somewhere far from the stacking project's field (or save a light with another
  target's coordinates). *Expected:* `not sending …: it shows another target …: pointed N° from the field of …`.
- [ ] With the rig known (after the first join), take a few darks. *Expected:* `… queued for this telescope's
  calibration library`; the panel shows *Calibration library: …*. Turn *Send new darks there* off and *Also send
  darks, bias* on: the next darks go to the project of the last light sent. Both off: darks are not sent.
- [ ] Under *Calibration library*, choose a folder of darks and bias (a master among them) and press *Sync the library
  now*. *Expected:* *Synced: N file(s) queued*, the master rather than its raw frames; a second press says *up to
  date* and queues nothing; *Prefer raw frames* queues the raw frames.
- [ ] *Pause uploads*: frames queue and wait (*Uploads are paused*); *Resume uploads*: they go. Choose *Only inside a
  daily time window* outside the window: they wait with the reason; *Upload now* sends them.
- [ ] Set a speed limit of 200 KB/s during an upload. *Expected:* the upload slows at once.
- [ ] Unplug the network during an upload, then plug it back. *Expected:* it is tried again and completes, without
  starting the whole file over.
- [ ] Close N.I.N.A. during an upload, then start it again. *Expected:* `… interrupted last time will resume`, and
  the file completes.
- [ ] Revoke the token on the website mid-night. *Expected:* uploads stop, the panel and the options page say to sign
  in again; the night's sequence carries on untouched.

## E. Verdicts and the rest

- [ ] After a frame is sent, wait a minute or two. *Expected:* a rejected or quarantined frame is logged with its
  reason.
- [ ] Turn the switch off in the middle of an upload. *Expected:* it stops within a few seconds, N.I.N.A. stays
  responsive, and the upload resumes when the switch is turned back on.
- [ ] Close N.I.N.A. with live stacking on. *Expected:* it closes as quickly as before.

## F. Hub projects

A hub project is a collaboration whose stacking project lives on a Rapid Stacker Collab hub, not in the cloud. Its
steps need the hub, so they are run against the home-network topology, not the cloud stack.

Local topology these steps assume:

- **Mac:** the Starfront server, `python server/run_local.py --lan --data ~/starfront-local` (stand-in Discord). Its
  LAN address is `http://<mac>.local:8800`.
- **Mac:** `collab-hub run`, registered with that server from the desktop Collab tab. The hub binary is built in the
  Rapid Stacker Collab repository, not this one. Its LAN address is `http://<mac>.local:8790`.
- **Windows PC:** N.I.N.A. with the plugin. `ServerUrl` in the plugin's settings file (in the `PluginSettings` folder
  named in section A) is `http://<mac>.local:8800`.

Before the first box, note the hub's version and the plugin's version at the top of your copy.

- [ ] Bind a collaboration to the hub from the desktop Collab tab (backend *hub*), then open its listing on the Starfront
  site. *Expected:* the listing shows the hub binding (the hub's name and its address); the server has a stacking
  project for the collaboration.
- [ ] With the switch on and no Rapid Stacker Collab sign-in, open Options > Plugins > Starfront. *Expected:* the page
  says *Hub projects need no Rapid Stacker Collab sign-in.*; the log's `Starfront stacking: on` line has no
  *not signed in* suffix once a hub link exists; the *Linked collaborations* list shows the hub link as
  *Collaboration → Project (Hub: name)*.
- [ ] Wait for a check-in. *Expected:* the log has `Starfront stacking: this telescope is rig <id> on <hub>` and
  `Starfront stacking: <collaboration> -> <project> (joined, hub): Joined …` (or `already a member`).
- [ ] With the hub stopped or off the LAN, shoot a few lights of the hub collaboration. *Expected:* each is logged
  `queued for …`, then `sent …` once the camera is idle; the server's log shows the part PUTs to `/stack/s3/…`; the
  server's `stack_frames` table has a row for each file with status `uploaded`.
- [ ] With the hub running on the same LAN (the default: `StackingHubTrustLan` is true in the settings file), shoot a
  few lights of the hub collaboration. *Expected:* the log has, once, `Starfront stacking: <collaboration> now uploads
  over the LAN (http://<mac>.local:8790/…)`; the hub's `objects/raw` receives the files; the server's log shows no
  `/stack/s3/` part PUTs for them.
- [ ] Wait for the hub to push the verdicts. *Expected:* within one poll of the hub pushing a verdict, the Starfront
  dock shows it, and the log has `Starfront stacking: <file> <status>`.
- [ ] Stop the hub while lights are queued for the LAN path. *Expected:* at the next check-in the log has `Starfront
  stacking: <collaboration> now uploads through the Starfront server` once; the queued frames go to the server and
  arrive as in the first step.
- [ ] Remove the telescope from the collaboration on the website. *Expected:* within one check-in no new light for it
  is queued (the log says `… is not linked to a stacking project`); frames already queued stay in the queue and are
  not sent.
- [ ] Make the hub refuse this telescope's grant (rotate the grant secret on the server, then the next check-in
  carries no valid grant). *Expected:* the log has `Starfront stacking: <collaboration>: the hub refused this
  telescope's grant; its uploads wait for the next check-in`; the link shows *Waiting for a new grant from Starfront
  for …; frames are kept and sent when it arrives*; lights shot meanwhile are queued, not dropped; a cloud
  collaboration on the same night keeps uploading; Rapid Stacker Collab's sign-in is not asked for again.
- [ ] Restore the grant (the next check-in brings it). *Expected:* the held and new frames are sent without a restart,
  and the waiting line goes.
- [ ] Night with one cloud collaboration and one hub collaboration linked. *Expected:* each light goes to the backend
  of its own collaboration only (the cloud ones to Rapid Stacker Collab, the hub ones to the server or the hub); the
  log shows a rig id for each backend, and the two ids differ (one library per backend, D001). Restart N.I.N.A.: each
  rig id is unchanged.
- [ ] Repeat *The SQLite preload* checks from section B with a hub link present. *Expected:* unchanged: `tasklist /m
  e_sqlite3.dll` lists NINA.exe once the switch is on, and no SQLite error in the log.
