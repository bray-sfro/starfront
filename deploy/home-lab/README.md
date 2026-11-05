# Home-network test lab

This folder sets up live stacking across the machines on a home network: the Starfront server, a Rapid Stacker Collab hub and workers, with N.I.N.A. on the Windows PC or on a rig of its own. `terraform apply` brings it up from the Mac, and `terraform destroy` takes it down. It is for local testing only. The server runs with the stand-in Discord (`server/run_local.py --lan`), so anyone on the network can sign in as anyone, the owner included.

| Machine | What runs there |
|---|---|
| **Linux server host** (`omarchy.lan`) | the Starfront server on port 8800 and `collab-hub` on 8790, both as `systemd --user` units; a worker |
| **This Mac** | Terraform; a worker (a LaunchAgent); `collab-uploader` for the smoke test and real uploads |
| **Windows PC** (`Eds9950X.lan`) | N.I.N.A. with the Starfront plugin, the Starfront desktop, and a worker that runs as a logon task and pauses while `NINA.exe` runs |
| **Imaging rig** (any PC that runs N.I.N.A., such as the one in the backyard) | N.I.N.A. with the Starfront plugin, installed with one line (below). No worker, no SSH, nothing for Terraform to know about |
| **NAS** (`DXP8800PLUS-76B2.lan`) | the read-only `XPSD` share (the 16 `gdr3-*.xpsd` files, about 45 GB) |

Terraform here is only the inventory, the order of the steps, change detection and teardown. Every step is a script in `scripts/` that runs over SSH from this Mac, and each one can also be run by hand. There is no cloud provider and nothing leaves the LAN. State is local and git-ignored, because the scripts pass tokens around.

The steps below assume the author's network. To set the lab up on another one, installing Terraform included, follow `docs/Starfront lab on another local network.pdf` (printed from the `.html` beside it).

## 0. Once, by hand

These steps need root or administrator rights, so Terraform does not do them.

1. On the main router, reserve addresses for the server host, the Windows PC and the NAS, by each one's built-in network card. On the author's network that is the GFiber router (https://192.168.1.1), not the eero: the eero is a second router behind it with its own 192.168.4.x Wi-Fi network, so it refuses 192.168.1.x reservations. The scripts use the `.lan` names, but the names follow the leases.
2. **On the Mac:** run `ssh-keygen -t ed25519` if `~/.ssh/id_ed25519` does not exist yet.
3. **On the Linux server host**, at its own keyboard:
   ```bash
   sudo bash bootstrap-linux.sh --nas DXP8800PLUS-76B2.lan --share XPSD --nas-user <nas user>
   ```
   - It installs sshd, rsync, python, curl and cifs-utils.
   - It opens TCP 22, 8800 and 8790 to `192.168.1.0/24` and the network's IPv6 prefix only, in ufw. The IPv6 rules matter: the router's DNS also gives the server's IPv6 addresses, clients try those first, and a blocked one costs every request a timeout.
   - It enables linger, so the units run while you are logged out.
   - It mounts the share read-only at `/mnt/xpsd`.

   Then, from the Mac, `ssh-copy-id <you>@omarchy.lan`.
4. **On the Windows PC**, in an elevated PowerShell, signed in as the account N.I.N.A. uses:
   ```powershell
   powershell -ExecutionPolicy Bypass -File bootstrap-windows.ps1 -MacPublicKey "<contents of ~/.ssh/id_ed25519.pub>" -NasUser <nas user>
   ```
   - It installs OpenSSH Server, with PowerShell as its shell.
   - It sets the network profile to Private, and allows SSH from the subnet on that profile only.
   - It authorises the Mac's key.
   - It stores the NAS credential for the worker's logon task.
5. **On the NAS:** create the `XPSD` share with a read-only user, and copy the `gdr3-*.xpsd` files from `~/Astronomy/XPSD`. The `gdr3sp-*` and `apass_*` files are not needed.
6. **On the Mac,** for its worker in real mode: Finder > Go > Connect to Server, `smb://DXP8800PLUS-76B2.lan/XPSD`. It mounts at `/Volumes/XPSD`.

To check the network the way Terraform will see it:

```bash
echo '{"server":"omarchy.lan","windows":"Eds9950X.lan","nas":"DXP8800PLUS-76B2.lan","ssh":"server,windows"}' \
  | python3 scripts/discover.py
```

## 1. Binaries

Rust is not cross-compiled. Each OS's `collab-hub` and `collab-worker` are built on that OS:

```bash
scripts/fetch-binaries.sh --from-release                # all three systems, from the public signed release (no private access)
scripts/fetch-binaries.sh --mac                         # this Mac: also collab-uploader
scripts/fetch-binaries.sh --from-run <worker-release run id>   # Linux and Windows, from CI
scripts/fetch-binaries.sh --build-on <you>@omarchy.lan  # or Linux natively (needs rustup, cmake, rapid-stacker access)
scripts/fetch-binaries.sh --plugin                      # the N.I.N.A. plugin, from Starfront's newest green plugin CI run on main
```

The plugin build lands in `bin/nina-plugin/`, and the next `apply` gives it to the lab server. The server's `/nina` feed then offers it, in place of the released plugin in `server/nina_plugin/`. CI stamps it `1.0.0.0`, which is how N.I.N.A. lists it.

`worker-release` is the Rapid Stacker Collab workflow that you run by hand (Actions > worker-release > Run workflow). It builds the archives as workflow artifacts and signs, uploads and tags nothing. Its unsigned archives are fine here, because nothing installs them through the desktop's signed-manifest path.

`--from-release` needs neither that workflow nor any private access: it downloads the public signed release from the desktop's own places in order (Rapid Stacker Collab's downloads, then the GitHub releases on Starfront's repository, tags `collab-worker` and `collab-hub`; `STARFRONT_RELEASES=<base>` names one instead), checks each manifest's signature with Starfront's embedded key and each archive's SHA-256, and unpacks all three systems' binaries. That release has no `collab-uploader`, which only `lab.sh smoke` and `lab.sh upload` use; build it with `--mac` for those. `tools/local_night.py --bin-dir bin/macos-arm64` runs the same downloaded binaries on one machine.

## 2. Up

```bash
cp terraform.tfvars.example terraform.tfvars   # set server_user and windows_user
terraform init
terraform apply                                # catalogue_mode = "synthetic" by default
scripts/lab.sh status                          # three workers, the Starfront link
scripts/lab.sh smoke relay                     # synthetic frames through the server's front door
scripts/lab.sh smoke hub                       # ...and straight to the hub over the LAN
```

Once both smoke runs publish a live preview, switch to the real catalogue and re-apply. The hub then reads `/mnt/xpsd`, and the workers are given each OS's path to the share:

```bash
terraform apply -var catalogue_mode=real -var lab_region="<RA>,<DEC>,1,1"
scripts/lab.sh night                           # the "Lab night" collaboration and its project page
```

Point N.I.N.A. and the desktop at `terraform output -raw server_url`:
- N.I.N.A., on the Windows PC or the imaging rig: see the next section.
- The desktop: set `collab.serverUrl`.

### N.I.N.A. on any PC: one line

On the PC that runs N.I.N.A., close N.I.N.A., then in an ordinary PowerShell window (not an administrator one):

```powershell
irm http://omarchy.lan:8800/_local/install-nina-plugin.ps1 | iex
```

It installs the plugin build the lab server offers, after checking it against its manifest's SHA-256, in place of any Starfront plugin already there. It sets the plugin's `ServerUrl` to the lab server and changes nothing else in the settings. If the plugin was signed in to another server, it says so: sign in again, because a sign-in belongs to one server. Run it again whenever the lab server has a newer build. The script is `install-nina-plugin.ps1` here; `run_local.py --nina-dir` serves it with the server's address filled in.

The PC needs nothing else: no SSH, and no Terraform entry. It reaches `omarchy.lan` on 8800 and 8790 from the main network or from the eero's Wi-Fi. The eero hands its Wi-Fi clients only a private IPv6 address, so they go straight to IPv4. To go back to the real server, set `ServerUrl` to `""` in the settings file the script names, or install the released plugin from N.I.N.A.'s plugin list.

`scripts/lab.sh upload <folder>` sends real frames from the Mac, as the Lab scope telescope.

A change to the Starfront sources on this Mac, committed or not, changes the source hash, so the next `apply` copies them across and restarts the server. A new binary in `bin/` re-installs what runs it. Workers keep their tokens across re-installs.

## After a reboot

| Machine | What comes back by itself |
|---|---|
| Linux server host | everything: the server, the hub and its worker start at boot, with nobody signed in (linger) |
| Mac | the worker, once you sign in (a LaunchAgent). `/Volumes/XPSD` does not remount by itself; add it to System Settings > General > Login Items, or reconnect it in Finder |
| Windows PC | the worker, once the account signs in (a logon task). The share needs no drive mapping; the credential `cmdkey` stored survives reboots |
| Imaging rig | the plugin, whenever N.I.N.A. starts |

Then `scripts/lab.sh status`: three workers, each with its catalogue `ready` in real mode. A worker whose share is not mounted reports `missing` until it is.

## 3. Down

`terraform destroy` stops and removes every unit, LaunchAgent and logon task. The data stays on the server host in `~/starfront-lab`. With `-var wipe_data=true`, the hub is first backed up to `~/starfront-lab/backups`, and then the database, frames and stacks are deleted.

## The test campaign

The manual tasks in `TASKS.DISTRIBUTED_STACKING.md` (T208, T310, T417, T507, T606 and T705) assume a Mac hub. Here the hub is the Linux host, so read "the Mac" in them as `omarchy.lan`. `STARFRONT_DISTRIBUTED_STACKING_CHECKLIST.md` gives the order to run them in. T507's install from the Volunteer page can be run now: the release signing key and the signed release exist (since 2026-10-28). Its other half, the worker pausing while N.I.N.A. is open, can be tested with the worker installed here.
