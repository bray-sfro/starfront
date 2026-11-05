# The collaboration server

Where imaging tasks come from.

An observatory runs Starfront and asks this, every few minutes, "what should
I shoot?". It answers with a task — a rectangle of sky, some filters, and how
deep to go — and takes back what was actually captured, judging it against the
rules the coordinator set.

## Running it

```
python server/run.py
```

Or double-click `Start the Starfront server.cmd`. That is the whole thing: it
makes up a coordinator token on the first run, keeps it beside the database and
prints it, so there is nothing to configure and nothing to remember. Paste it
into Starfront under **Collab → Running a collaboration**.

It listens on this machine only. A telescope on another PC needs
`--host 0.0.0.0`; nothing else does, and a service that quietly listened to the
network on somebody's behalf would be a poor default.

The token lives in `admin-token.txt` beside the database, **not** in the program
folder — that one is synchronised to Dropbox, and a credential that can rewrite
every project has no business in a synchronised folder or in a launcher script
somebody might share.

| | |
|---|---|
| `STARFRONT_DATA` | where the database and the token live (default: the `collab` folder beside your settings) |
| `STARFRONT_ADMIN_TOKEN` | overrides the token file, for a service definition or a password manager |
| `--show-token` | print the token and exit |

The settings were called `ASTROCOLLAB_…` before the server was named; those
names are still read, so nothing written under them has to change.

### Trying it out locally, without Discord

```
python server/run_local.py
```

People sign in through Discord, and a Discord application (client secret,
guild, a redirect Discord accepts) is a lot to set up just to try the server,
the program and the N.I.N.A. plugin on one machine. `run_local.py` runs the
same server with a stand-in Discord of its own, served at `/_local/discord` on
the server's own address. Signing in from the program or the plugin opens a
page that asks only for a name: **@ncedelliott** owns the server unless
`--name` says otherwise, and any other name typed there signs in as an
ordinary member, for seeing what somebody else's telescope sees. The same name
is the same person every run, so its projects are still there after a restart.

| | |
|---|---|
| `--name NAME` | the owner's Discord name (default `ncedelliott`) |
| `--port N` | listen port (default 8800) |
| `--data DIR` | the database and token (default `~/starfront-local`) |

The stand-in signs in whoever asks, as the owner if they like, so the launcher
is fenced off from anything real. It listens on `127.0.0.1` with no `--host`
(another PC reaches it through an SSH tunnel to 8800, which carries the
sign-in page too). It refuses a data folder holding a `discord.env` and the
folder a real server uses by default. It replaces any Discord settings already
in the environment rather than mixing them in. And it only ever sends the
browser back to its own callback. `tools/check_local_server.py` covers all of
that, with a full sign-in over HTTP.

## Putting it on a VPS, with Discord sign-in

A server other people use wants to be always on and reachable over HTTPS —
Discord will only send people back to an `https://` address. A small Linux
VPS (Ubuntu or Debian, the cheapest tier) and a hostname pointed at it is all
it takes; `server/deploy/install.sh` does the rest.

1. **Point a hostname at the VPS.** An `A` record, e.g. `collab.starfront.space`
   → the VPS's IP. Caddy fetches the certificate itself once that resolves.

2. **Create the Discord application** at
   <https://discord.com/developers/applications> → *New Application* →
   *OAuth2*. Note the **Client ID**, generate a **Client Secret**, and add
   the redirect `https://collab.starfront.space/auth/discord/callback`. No bot is
   needed and no scopes beyond `identify` and `guilds.members.read`, which
   the server asks for by itself. Get your **server (guild) id** from Server
   Settings → Widget, and, if only some people should be able to *start*
   collaborations, the **role id** of that role.

3. **Install.** On the VPS, as a user with sudo, with this repository copied
   or cloned there:

   ```bash
   sudo bash server/deploy/install.sh collab.starfront.space
   ```

   It installs Python and Caddy, copies the server to `/opt/starfront`,
   makes a venv, creates the `starfront` system user with its data in
   `/var/lib/starfront`, installs and starts the systemd service on
   loopback, and puts Caddy in front of it on your hostname. It prints the
   owner token at the end. Run it again after pulling a newer version; it
   leaves the data alone. A server first set up under the old name
   (`astrocollab`) is carried over the first time it runs. While telescopes
   still point at an older address, `STARFRONT_LEGACY_HOST=old.example.org`
   in front of the command keeps that address answering as well.

4. **Fill in Discord.** Edit `/var/lib/starfront/discord.env` — the values
   from step 2, plus your own Discord user id under
   `STARFRONT_DISCORD_OWNERS` so that you are an owner when you sign in —
   and `sudo systemctl restart starfront`. The startup line says
   `discord   sign-in on, guild …` when it has taken.

5. **In Starfront**, everybody presses *Join with Discord*. The server's
   address is built into the program (`collab.DEFAULT_SERVER`); a build for a
   different server changes that one line. The owner token the script
   printed is only needed for scripts and emergencies — an owner signs in
   like everybody else.

| | |
|---|---|
| `journalctl -u starfront -f` | the server's log |
| `sudo systemctl restart starfront` | after changing `discord.env` |
| `/var/lib/starfront/collab.sqlite` | the database — back it up by copying it |
| `curl https://collab.starfront.space/api/v1/health` | `"discord": true` when sign-in is on |
| `https://collab.starfront.space/nina` | the N.I.N.A. plugin repository people add in N.I.N.A. |

**How sign-in works.** The device-code flow, because Starfront is a desktop
program: it asks the server for a login code (`POST /api/v1/auth/login`) and
opens the browser on `/auth/discord/start?code=…`; the server sends the
browser to Discord; Discord sends it back to `/auth/discord/callback`; the
server checks the person is in the guild, notes their roles, mints a user
token and ties it to the code; Starfront, polling `/api/v1/auth/poll`, is
handed the token once. The client secret never leaves the server. One token
per person: signing in on a second machine signs the first out.

**Who may do what.** A *telescope* (agent token) fetches work and reports
frames — nothing else, ever. A *member* (user token) enrols their own
telescopes, and, holding the role if one is required, starts collaborations
and changes or closes the ones they started. The *owner* (admin token) may do
anything. Membership of the Discord server is the account; somebody who leaves
it cannot sign in again, and the server never lets a stranger in.

## The shape of it

**Pull, not push.** The agent asks; the server never reaches into an
observatory. That is not only simpler — no delivery guarantees, no queue,
nothing to get stuck — it is the only arrangement where a rig behind a domestic
router at a dark site works at all, and where the server going down means "no
new tasks" rather than "the night stops".

**Two kinds of caller, and they cannot do each other's jobs.** An *agent* is a
telescope, holding a token that lives in a settings file on an observatory PC.
A *person* is somebody signed in with Discord, or the server's owner on the
admin token. A telescope's credential must never be able to rewrite a project,
and a person's login must never be able to drive a mount, so they are separate
dependencies rather than roles on one credential.

**The unit of work is an area of sky, not a panel.** Panels are an artifact of
one particular camera on one particular telescope; a project several rigs
contribute to cannot be defined in them. A mosaic project is a rectangle; a rig
that joins is handed the whole of it tiled with its own camera, and each night
a list of which of those cells to shoot and how many frames to put on each. A
`single` project is one object: every rig is handed one cell, its own frame
centred on it, and nobody tiles the framer's field however narrow their camera.

**Depth is integration time at a point on the sky** — the only definition that
means the same thing to a 300 mm refractor and a 2000 mm reflector.

**The night is dealt from the depth map.** Every contribution carries the
footprint of the panel it was shot over, so each rig's cells can be credited
with what has really landed on them — by everybody, per filter, and by that rig
alone. `collab.assign` picks a rig's panels for a night in that order of pull:
not where somebody else is tonight; where this rig has been least; where the
field is thinnest. The night is cut into as many visits as it holds, never
shorter than the project's `minFramesPerVisit` in any filter, because a rig has
to stack its own frames. Finished cells are skipped. A list holds for the
night the rig says it is in (`GET /api/v1/agent/task?night=...`) and is remade
the first time it asks in the next, so the whole thing moves on by default.

**One row per agent, task, night, filter and panel.** A panel reported again
with more frames replaces the row at the larger figure; a coordinator's
overruling of a verdict stands through that.

## The agent's protocol — three calls

| | |
|---|---|
| `POST /api/v1/agent/hello` | here is what I am and what I can do |
| `GET /api/v1/agent/task` | what should I shoot? |
| `POST /api/v1/agent/report` | here is what I captured |

A task arrives **offered** and stays that way until somebody accepts it. An
assignment that silently rewrote what a mount did tonight would be the software
going rogue, however well meant.

## Judging

`astrocontrol/collab.py` holds the rules, and **both sides import it** — the
server to judge what arrives, the program to warn an operator before they waste
a night. Two implementations would drift, and the first symptom of drift is a
ledger reporting hours nobody can use.

Everything is in units that mean the same thing on every rig. **Star size is in
arcseconds, never pixels**: 2.5 px is superb at 0.5″/px and unusable at 3″/px,
and a ledger that compared them directly would be worse than no ledger.

Verdicts are **advisory**. A coordinator can overrule one, and should be able
to: seeing varies, and a night the numbers reject may be the only data anybody
has on that patch of sky.

## Collecting the lights

A collaboration started with **Collect the lights on the server** ticked (the
Collab tab's form in Starfront, or `collectFiles` on the API) takes every
contributor's subs onto this server, under the files folder, one directory
per collaboration and one per person inside it:

```
/var/lib/starfront/files/<project id>/<discord user id>/lights/M31_Ha_001.fits
                                                        /flats/
                                                        /darks/
                                                        /bias/
                                                        /darkflats/
```

People send them from the **files page**, `https://collab.starfront.space/files`:
sign in with Discord there, pick the collaboration, drop the night's folder
on it. Only FITS and XISF files are taken; each is filed as a light, flat,
dark, bias or dark flat by the word N.I.N.A. put in its name (or by the kind
the uploader chose for files that carry none); a file already there at the
same size is skipped (so the same folder can be dropped again the next day),
and a cut-off upload is picked up where it stopped by dropping the folder
again.
The sign-in on that page is its own credential, a cookie, so it does not sign
anybody's N.I.N.A. or Starfront out. The same `PUT` takes a telescope's
token, which is how the programs will send each light as it is saved.

| | |
|---|---|
| `PUT /api/v1/projects/{id}/files/{name}` | one file, the body streamed to disk; `?kind=light|flat|dark|bias|darkflat` (blank: read from the name), `?telescope=` labels it |
| `GET /api/v1/projects/{id}/files` | what has arrived: everything to the coordinator, your own to you |
| `GET /api/v1/projects/{id}/files/{user}/{kind}/{name}` | one file back out, to the coordinator or its uploader |
| `DELETE /api/v1/projects/{id}/files/{name}?kind=` | your own file; the coordinator's `?user=` anybody's |
| `GET /api/v1/health` → `files` | how many, how many bytes, how much room is left |

The coordinator gets the whole set by copying the directory off the VPS
(`rsync -a root@…:/var/lib/starfront/files/<project id>/ .`), or file by file
from the page. **Disk is the limit.** A 61-megapixel sub is 120 MB and a
night from one telescope is several gigabytes, so the cheapest droplet's
disk fills in a week of a busy collaboration. Attach a block-storage volume
and mount it at `/var/lib/starfront/files`:

```bash
sudo bash server/deploy/attach-files-volume.sh starfront-files        # the volume's name in the panel
sudo bash server/deploy/attach-files-volume.sh starfront-files --grow # after making it bigger
```

The script formats a blank volume, mounts it at the files folder, carries
over anything already there, and makes the mount survive a reboot. (A volume
mounted anywhere else needs `STARFRONT_FILES` in `discord.env` *and* that
path in `ReadWritePaths` in the service file.) The server always leaves two
gigabytes free and refuses uploads beyond that, telling the person to tell
the coordinator.

## Live stacking: where a collaboration is stacked

A collaboration's lights can be stacked as they land, on the owner's
**hub** or on [Rapid Stacker Collab](https://collab.astro.ekeservices.com)'s
**cloud**. Either way this server makes the stacking project from the
collaboration and keeps it in step (T706): nobody makes a second project or
pastes a join code. The next section covers the hub; the cloud is at the end of
it. A new collaboration that says nothing about stacking is stacked on the
owner's hub when they have one, else on the cloud when their Collab account is
linked, else not at all.

`GET /api/v1/agent/projects` tells each telescope where a collaboration is
stacked in its `stacking` object (`backend`, the hub's name and address,
`managed` for a cloud project this server made), and whether it can be
joined yet in `stacking.state`:

- `ready`: a telescope on it can join (the hub grant, or the cloud join code, is
  in the listing);
- `pending`: the cloud project is still being made;
- `failed`: making or updating it failed and is being retried (an expired Collab
  link, say). The owner's own telescopes also get the reason in
  `stacking.problem`, so an owner who uses only N.I.N.A. hears of it too;
  nobody else does, as it can name the owner's Collab account;
- blank: not stacked.

The cloud join code and the hub grant are credentials, so they go only to
telescopes on the collaboration and to the owner's own. A pasted code is no
longer taken (400). A collaboration from before T706 that carries one keeps it,
read-only, and reads as an old cloud binding (`managed: false`). For 1.0.0.33
plugins the listing still carries `stackingJoinCode` and `stackingLinked`
(yes when there is a cloud code), which are unchanged. `tools/check_server.py`
has the cases.

## Live stacking: hubs and the front door

A collaboration can be stacked on a **hub**: a `collab-hub` program on someone's
home network (Rapid Stacker Collab repository, `worker/crates/hub`). This server
is then the durable mailbox and the public face; the hub does the stacking. The
design is `STARFRONT_DISTRIBUTED_STACKING_PLAN.md`; the operator's guide is
`docs/runbooks/starfront-hub.md` in Rapid Stacker Collab; the desktop's README
section "Live stacking at home" explains the decisions. `STARFRONT_PUBLIC_URL`
must be set to the address telescopes and hubs reach (`run_local.py --lan` sets
it to the LAN address).

### The hub API: `/api/v1/hubs/*` (`server/hubs.py`, `server/stack/`)

Owner calls use a signed-in person or the admin token; hub calls use the hub's
own token (`sfh_`, only its SHA-256 is kept).

| Route | Who | What |
|---|---|---|
| `POST /api/v1/hubs` `{name, lanUrl?}` | a person who may start collaborations | registers a hub; returns `hubId`, `hubToken`, `grantSecret` once |
| `GET /api/v1/hubs`, `DELETE /api/v1/hubs/{id}`, `PUT .../coordinators` | the hub's owner | list, revoke, set the coordinators allowed to bind to it (decision D002) |
| `POST .../heartbeat` | the hub | its status document; its `workers` entry feeds the dashboard's "stacked by" |
| `GET .../pull?since=N&wait=0..20` | the hub | long-polls the change feed: `{changes:[{seq, kind, key, at}], seq}`, at most 500 a call. Kinds: `project`, `member`, `rig`, `frame`, `library` |
| `GET .../frames?state=uploaded&limit=N` (add `kind=library` for the calibration library), `GET .../frames/{id}/bytes`, `POST .../frames/{id}/ack` | the hub | the relay: list what is waiting, fetch with `Range` (206), acknowledge with the SHA-256 (409 on a mismatch, idempotent). A frame the relay no longer holds is 404 |
| `GET .../records/member/{project}/{user}`, `GET .../records/rig/{user}/{rig}`, `POST .../records/batch` | the hub | full member and rig records for the feed's keys (at most 200 a batch); a removed member keeps the real name and carries `removed_at`, `removed_by`, `frames_disposition` |
| `POST .../push` | the hub | multipart: a JSON `records` part (frame verdicts, projects' grids, stream stats, events) plus file parts `{project}/{path}` and `deepzoom.tar`. Everything is validated before anything is written; it is idempotent (stale verdicts, lower `grid_rev` or `gold_seq` are ignored); a new gold run triggers the Discord notice; over `STARFRONT_STACK_HUB_BUDGET_GB` (50) is 429 |
| `PUT .../push/{project}/masters/{name}` | the hub | a final master (`.xisf`, `.fits`, `.fz`) streamed to disk: `Content-Length` required (411), `X-Content-SHA256` required (422 on mismatch), the same name, size and hash again is a 200 `{"written": false}`. Masters are members-only |

Errors on the relay and push routes are plain `{"detail": ...}`, not the Collab
envelope. A hub never sees a frame, project or master that belongs to another
hub (404 or 409).

### The front door: `/stack/api`

A sub-application (`server/stack/front.py`) that speaks Rapid Stacker Collab's
contributor API, so the N.I.N.A. plugin's upload engine and `collab-uploader` use
it unchanged. Every answer, including an unknown path or a bad body, is the Collab
envelope `{ok, code, message}` and never 403 or 404.

- **Auth**: `Authorization: Bearer sfg_...`, the grant the agent listing hands a
  telescope (valid a day, names one person, telescope and project); a bad grant
  is 401 `token_invalid`, a grant for another project or hub 401 `token_scope`.
  A cookie from the browser sign-in is read-only (writes are 401
  `not_authenticated`). Joining needs no code when the grant names the project.
- **Routes**: `GET /auth/me`; `GET /projects` (`?scope=mine` for the dashboard,
  each with `stacked_by`), `POST /projects/resolve-join-code`, `GET /projects/{p}`
  (includes `deleted_at`, `delete_after`, `results_kept`), `POST .../join`,
  `GET|PUT .../membership`; `GET|POST /rigs`, `PATCH /rigs/{id}`; frames
  `POST /projects/{p}/frames/init`, `.../{id}/complete`, `.../{id}/withdraw`,
  `GET .../frames` (`?mine=1`) and `.../{id}`; the calibration library
  `GET /rigs/{id}/library` and `POST .../library/frames/init|complete|withdraw`;
  results `GET /projects/{p}/status|events|mosaic|masters` and
  `.../masters/{name}` (members only).
- **Uploads**: `init` returns **absolute** part URLs (`{public url}/stack/s3/{upload_id}/{n}`);
  the 64-hex upload id in the URL is the credential, valid an hour. Parts are
  stored in the relay (`STARFRONT_DATA/stack/relay`) with a quoted-MD5 `ETag`.
  The `stack-sweep` thread aborts uploads left `uploading` for three days and
  retries due cloud calls. Below the free-space floor `init` answers 429
  `quota_exceeded`. A `private` project's status and previews are never put in
  the public tree.
- **Retention**: a relayed frame is deleted `STARFRONT_STACK_RELAY_KEEP_DAYS`
  (7; 0 = at once) after the hub acknowledged it; a withdrawn one at its
  `raw_delete_after`.
- No route creates, edits, deletes or re-keys a project: the Starfront
  collaboration is the only place one is made, and the stacking project (and a
  cloud project, through `server/stack/cloud.py`) is derived from it.

### Pages

Read-only progress pages (see "Driving it"): `/stack/p/{slug}`, `.../filter/{F}`,
`.../combined`, the cookie dashboard `/stack/dashboard` (also `/stack/`), and the
hub's pushed files at `/stack/public/{project}/...`. Masters are not under
`/public`; they are served through `/stack/api/projects/{p}/masters/{name}` to
members.

### Deleting, and the cloud link

`DELETE /api/v1/projects/{id}` on a hub-backed collaboration (its owner or the
server owner) files a seven-day deletion: the stacking project becomes `deleted`
with `deleted_at`, `delete_after` and `deleted_by`, the collaboration is closed,
joins and uploads answer 409 `project_deleted`, and the hub hears of it through
the feed. `POST /api/v1/projects/{id}/restore` undoes it until `delete_after`.
After the window the sweep deletes the relay bytes and keeps the rows, the
final masters and `status.json`; `DELETE /api/v1/projects/{id}/stack/results`
(owner, after the window) removes what is left under `public/{project}/`.

A collaboration can instead be stacked on Rapid Stacker Collab's cloud (T706):
the owner links a Collab API token with `projects:manage` once
(`GET /api/v1/stacking` shows the account link, the hubs and the default), the
server creates and updates the Collab project from the collaboration, and keeps
the token sealed with AES-GCM under `STARFRONT_CREDENTIAL_KEY` (else the admin
token). A failed call is recorded on the collaboration and retried with
back-off. Collab's delete and restore need a browser session, so a cloud
project is paused instead. `tools/check_server.py` and
`tools/check_stack_relay.py` have the cases; `tools/local_night.py` runs the
real programs together.

## What is deliberately not here yet

- **A drawn depth map.** The depth map exists at the resolution of each rig's
  cells and drives the dealing; nothing rasterises it for a picture yet.

## Driving it

The server serves **read-only progress pages** under `/stack/`: a collaboration's
project page (`/stack/p/<slug>`), each filter (`/stack/p/<slug>/filter/<F>`) and
the combined colour preview (`/stack/p/<slug>/combined`). They are static files
in `server/web/stack/` (ported from Rapid Stacker Collab, MPL-2.0, see its
`NOTICE.txt`), read what a hub has pushed to `STARFRONT_DATA/stack/public/<project
id>/` through `GET /stack/public/...`, and run under a strict Content-Security-Policy
(`script-src 'self'`, no inline script or handler; `tools/check_csp.py` checks it).
The cookie dashboard ("My projects") is `/stack/dashboard`. The rest of the server
(`/download`, `/files`, `/sky`) is unchanged and outside that policy.

What is still **not** here is any page that can write or administer. The
**admin token never reaches a browser**, and the coordinator's and owner's tools
stay in the **Collab** tab in Starfront, which holds both credentials and makes
these calls on the operator's behalf - so a token that can rewrite every project
on the server is never in a page, in devtools, or in any extension that asks for
it.

`python tools/check_server.py` exercises the whole protocol over real HTTP
against a real server on a real port. `python tools/check_coordinator.py`
covers the tiling arithmetic and tries each credential against the other's
endpoints. `python tools/check_csp.py` checks the pages for inline script.
