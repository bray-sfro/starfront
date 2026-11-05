#!/usr/bin/env python3
"""Set up and check a live-stacking test lab whose parts run on different machines.

    python3 tools/lab_setup.py register-hub --server http://127.0.0.1:8800 --hub-dir DIR \\
        --public-url http://omarchy.lan:8790 --xpsd-dir /mnt/xpsd \\
        --xpsd-windows '\\\\nas\\XPSD' --xpsd-macos /Volumes/XPSD --xpsd-linux /mnt/xpsd
    python3 tools/lab_setup.py bind-collab --server http://omarchy.lan:8800 --out lab-night.json \\
        --region 84,0,1,1
    python3 tools/lab_setup.py smoke --server http://omarchy.lan:8800 --via relay|hub \\
        --uploader BIN --worker BIN

Why it exists: tools/local_night.py brings the whole system up on one machine
over loopback. A home-network lab puts the server and the hub on one machine and
workers, N.I.N.A. and the uploader on others (deploy/home-lab/), so the same
set-up steps are needed one at a time, each where it belongs:

  register-hub   on the server's machine, where the hub runs: the owner signs in
                 through the stand-in Discord, registers a hub, and the hub's two
                 files are written (collab-hub.toml with a catalogue path for
                 every OS, hub-credentials.toml at 0600). Run again, it keeps the
                 registration and rewrites only collab-hub.toml.
  bind-collab    anywhere: a collaboration bound to the hub, a telescope that
                 has joined it, and the 24 h grant a telescope holds, written to
                 --out (0600) for uploading real frames by hand.
  refresh-grant  renews that grant (each lasts 24 h) without a new collaboration.
  smoke          anywhere: the gate. A throw-away collaboration, synthetic frames
                 uploaded through the server's front door (--via relay) or
                 straight to the hub (--via hub), then waits until the server
                 serves a live preview over HTTP. The workers must be using the
                 synthetic catalogue (local_catalog = "synthetic"): a real Gaia
                 catalogue can never solve the synthetic sky.

local_night.py imports the helpers here, so the two cannot drift. Local testing
only: the stand-in Discord signs anyone in as anyone. The standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OWNER = "ncedelliott"

#: The synthetic scenario's field (collab-worker synth-frames), in degrees.
SYNTH_RA, SYNTH_DEC = 85.25, -2.46

#: The keys of the hub's [xpsd_paths] table (RSC hub routes/admin.rs).
OSES = ("macos", "windows", "linux")


def say(text: str = "") -> None:
    print(text, flush=True)


def fail(text: str) -> None:
    print(f"{Path(sys.argv[0]).stem}: {text}", file=sys.stderr, flush=True)
    raise SystemExit(1)


# ----------------------------------------------------------------------- HTTP
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http(method: str, url: str, body=None, token: str = "", timeout: float = 60):
    """One request; returns (status, parsed JSON or bytes). Never raises on a
    status, only on a connection that cannot be made."""
    head = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        head["Content-Type"] = "application/json"
    if token:
        head["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=head)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, payload, kind = response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        status, payload, kind = exc.code, exc.read(), exc.headers.get("Content-Type", "")
    if "json" in kind:
        try:
            return status, json.loads(payload.decode() or "{}")
        except ValueError:
            pass
    return status, payload


def hop(url: str) -> str:
    """Fetch a URL without following redirects; returns its Location."""
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        opener.open(urllib.request.Request(url), timeout=15)
    except urllib.error.HTTPError as exc:
        return exc.headers.get("Location", "")
    return ""


def wait_until(what: str, probe, timeout: float, every: float = 1.0):
    """Call probe() until it returns something truthy; fail on timeout."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            found = probe()
        except (OSError, ValueError, urllib.error.URLError):
            found = None
        if found:
            return found
        time.sleep(every)
    fail(f"timed out after {int(timeout)} s waiting for {what}")


def sign_in_as(base: str, name: str) -> str:
    """The device-code sign-in through the stand-in Discord, as `name`;
    returns the program token (what the desktop and the agent keep).

    The sign-in links point at the server's public address, so `base` must be
    an address this machine can reach that address through: loopback on the
    server's own machine, or the public address itself from another."""
    status, begun = http("POST", f"{base}/api/v1/auth/login")
    if status != 200:
        fail(f"sign-in could not start: {status} {begun}")
    to_discord = hop(begun["url"])
    if "/oauth2/authorize" not in to_discord:
        fail(f"sign-in did not lead to the stand-in Discord: {to_discord!r}")
    approve = to_discord.replace("/oauth2/authorize", "/oauth2/approve")
    approve += ("&" if "?" in approve else "?") + urllib.parse.urlencode({"name": name})
    back = hop(approve)
    if not back:
        fail("the stand-in Discord did not send the browser back")
    urllib.request.urlopen(back, timeout=15).read()
    status, polled = http("GET", f"{base}/api/v1/auth/poll?code={begun['code']}")
    if status != 200 or not isinstance(polled, dict) or not polled.get("token"):
        fail(f"sign-in did not complete: {status} {polled}")
    return polled["token"]


# --------------------------------------------------------------------- files
def toml_str(value) -> str:
    return json.dumps(str(value))


def xpsd_paths_for(xpsd: Path, share_host: str) -> dict[str, str]:
    """The [xpsd_paths] of a hub that shares its own catalogue folder over SMB
    as XPSD (the plan's §7.1 layout): this machine's path for macOS, the share
    for Windows, and the usual Linux mount point. `share_host` is a name or an
    IP address, used whole: an address cut at its first dot is no host at all."""
    return {"macos": str(xpsd), "windows": "\\\\" + share_host + "\\XPSD", "linux": "/mnt/xpsd"}


def write_hub_files(hub_dir: Path, *, hub_id: str, hub_token: str, grant_secret: str, server_local: str,
                    bind_port: int, public: str, xpsd: Path | str | None,
                    xpsd_paths: dict[str, str] | None = None, write_credentials: bool = True) -> Path:
    """collab-hub.toml and hub-credentials.toml (0600 from creation). Enrolment
    hands each worker the [xpsd_paths] entry for its OS; an OS with none gets
    no catalogue path at all, so every OS that may run a worker needs one."""
    hub_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"bind = {toml_str(f'0.0.0.0:{bind_port}')}",
             f"public_url = {toml_str(public)}",
             f"data_root = {toml_str(hub_dir / 'data')}"]
    if xpsd is not None:
        lines.append(f"xpsd_dir = {toml_str(xpsd)}")
        paths = {k: v for k, v in (xpsd_paths or {}).items() if v}
        if paths:
            lines += ["", "[xpsd_paths]"] + [f"{k} = {toml_str(paths[k])}" for k in OSES if k in paths]
    lines += ["", "[starfront]", f"url = {toml_str(server_local)}", f"hub_id = {toml_str(hub_id)}"]
    config = hub_dir / "collab-hub.toml"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if write_credentials:
        creds = hub_dir / "hub-credentials.toml"
        fd = os.open(creds, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"hub_id = {toml_str(hub_id)}\n"
                         f"hub_token = {toml_str(hub_token)}\ngrant_secret = {toml_str(grant_secret)}\n")
        os.chmod(creds, 0o600)
    (hub_dir / "data").mkdir(exist_ok=True)
    return config


def set_toml_keys(path: Path, keys: dict[str, str]) -> None:
    """Replace or add top-level keys of a flat TOML file (the worker's)."""
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.split("=", 1)[0].strip()
        if name not in keys:
            kept.append(line)
    # Top-level keys must come before any [table].
    first_table = next((i for i, l in enumerate(kept) if l.lstrip().startswith("[")), len(kept))
    added = [f"{k} = {v}" for k, v in keys.items()]
    path.write_text("\n".join(kept[:first_table] + added + kept[first_table:]) + "\n", encoding="utf-8")


def write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def run_out(argv: list[str], **kw) -> str:
    done = subprocess.run([str(a) for a in argv], capture_output=True, text=True, **kw)
    if done.returncode != 0:
        fail(f"{' '.join(str(a) for a in argv[:3])} failed ({done.returncode}): {done.stderr.strip()[-400:]}")
    return done.stdout.strip()


# -------------------------------------------------------------- the server
def register_hub(server: str, owner: str, name: str, lan_url: str) -> dict:
    status, reg = http("POST", f"{server}/api/v1/hubs", {"name": name, "lanUrl": lan_url}, token=owner)
    if status != 200 or not isinstance(reg, dict) or not reg.get("hubToken"):
        fail(f"registering the hub failed: {status} {reg}")
    return reg


def bind_collaboration(server: str, owner: str, hub_id: str, *, name: str, region: tuple,
                       scope_name: str, filter_mapping: list, discord_webhook: str = "") -> dict:
    """A collaboration bound to the hub, and a telescope owned by the owner that
    has joined it and joined its stacking project as a member, so it holds the
    grant a real telescope would. Returns the ids, the slug and the grant."""
    ra, dec, width, height = region
    status, made = http("POST", f"{server}/api/v1/projects", {
        "name": name, "region": {"ra": ra, "dec": dec, "width": width, "height": height},
        "requirements": {"filters": {"L": 1.0, "Ha": 1.0}},
        "stacking": {"backend": "hub", "hubId": hub_id},
        **({"discordWebhookUrl": discord_webhook} if discord_webhook else {})}, token=owner)
    if status != 200:
        fail(f"binding a collaboration to the hub failed: {status} {made}")
    sf_project = made["project"]["id"]
    _, enrolled = http("POST", f"{server}/api/v1/agents", {"name": scope_name}, token=owner)
    scope = enrolled["token"]
    http("POST", f"{server}/api/v1/agent/hello", {"protocol": 1, "profile": {
        "name": scope_name, "focalLength": 389.0, "pixelSize": 3.76, "sensorWidth": 9576,
        "sensorHeight": 6388, "binning": 1, "filters": {"Ha": 1.0}, "hoursPerNight": 6.0,
        "scale": 206.265 * 3.76 / 389.0,
        "field": [206.265 * 3.76 / 389.0 * 9576 / 3600.0, 206.265 * 3.76 / 389.0 * 6388 / 3600.0]}},
        token=scope)
    http("POST", f"{server}/api/v1/agent/projects/{sf_project}/join", {}, token=scope)
    _, browse = http("GET", f"{server}/api/v1/agent/projects", token=scope)
    block = next((p for p in browse["projects"] if p["id"] == sf_project), {}).get("stacking") or {}
    grant, pid = block.get("grant", ""), block.get("collabProjectId", "")
    if not grant or not pid:
        fail(f"the agent listing has no stacking grant for the project: {block}")
    front = f"{server}/stack/api"
    _, got = http("GET", f"{front}/projects/{pid}", token=grant)
    project = (got.get("project") or {}) if isinstance(got, dict) else {}
    # The telescope joins as a member (the grant names the project, so no code).
    status, joined = http("POST", f"{front}/projects/{pid}/join", {
        "display_name": scope_name, "accepted_terms_version": project.get("terms_version", ""),
        "filter_mapping": filter_mapping}, token=grant)
    if status != 200:
        fail(f"joining the stacking project as the telescope failed: {status} {joined}")
    return {"collaboration": sf_project, "stackingProject": pid, "slug": project.get("slug", ""),
            "grant": grant, "scopeToken": scope, "hubId": hub_id, "lanUrl": block.get("hubLanUrl", ""),
            "apiBase": block.get("apiBase", "")}


#: What is never printed: the grant and the telescope's own token.
SECRETS = ("grant", "scopeToken")


def fresh_grant(server: str, scope_token: str, collaboration: str) -> str:
    """The grant the agent listing hands the telescope now (each lasts 24 h)."""
    status, browse = http("GET", f"{server}/api/v1/agent/projects", token=scope_token)
    if status != 200 or not isinstance(browse, dict):
        fail(f"the agent listing failed: {status} {browse}")
    block = next((p for p in browse.get("projects", []) if p["id"] == collaboration), {}).get("stacking") or {}
    if not block.get("grant"):
        fail(f"the agent listing has no stacking grant for {collaboration}: {block}")
    return block["grant"]


# ------------------------------------------------------------------ synthetic
def upload_synthetic(uploader: Path, frames: Path, work: Path, api_base: str, grant: str,
                     pid: str, log: Path, list_base: str = "") -> None:
    """Upload each synthetic contributor's folder with collab-uploader to
    `api_base` (the server's front door or the hub), signed in with the
    telescope's grant (COLLAB_TOKEN). The uploader learns its projects from a
    listing, which a hub does not serve (the hub profile leaves projectsList
    out), so `list_base` names where to list them: the server's front door,
    while the rig and the frames go to `api_base`."""
    manifest = json.loads((frames / "manifest.json").read_text())
    # The frames were written a moment ago, and the uploader holds back a file
    # until it has stopped changing; these never change, so no settling time.
    env = dict(os.environ, COLLAB_TOKEN=grant, COLLAB_SETTLE_SECONDS="0")
    for who in manifest["contributors"]:
        folder = frames / who["slug"]
        conf = work / "uploader" / who["slug"]
        conf.mkdir(parents=True, exist_ok=True)
        base = [uploader, "--api-base", api_base, "--config-dir", conf]
        lister = [uploader, "--api-base", list_base or api_base, "--config-dir", conf]
        lights = sorted(folder.glob("light_*.fits"))
        if not lights:
            continue
        steps = [
            lister + ["projects"],
            base + ["rig", "confirm", "--from", lights[0], "--yes"],
            base + ["upload", folder, "--project", pid, "--include-calibration", "--now"],
        ]
        for step in steps:
            say(f"  uploader[{who['slug']}] {' '.join(str(a) for a in step[len(base):len(base) + 2] if not str(a).startswith('/'))}")
            done = subprocess.run([str(a) for a in step], env=env, capture_output=True, text=True)
            with open(log, "a") as handle:
                handle.write(f"$ {' '.join(str(a) for a in step)}\n{done.stdout}{done.stderr}\n")
            # Exit 2 is "finished, some files failed": the synthetic set carries bad
            # files on purpose (a truncated one, an off-field one), so it is fine.
            if done.returncode not in (0, 2) or (done.returncode == 2 and step[len(base)] != "upload"):
                fail(f"collab-uploader {step[len(base)]} for {who['slug']} failed ({done.returncode}); see {log}\n"
                     + (done.stderr or done.stdout)[-600:])
            if step[len(base)] == "upload":
                say("    " + (done.stdout.strip().splitlines() or [""])[-1])


def preview_published(public: Path) -> str:
    """What the hub's push has put in the public tree: '' until status.json
    parses AND a stream's live preview (<stream>/live/latest.png) is there."""
    status = public / "status.json"
    if not status.exists():
        return ""
    try:
        json.loads(status.read_text())
    except ValueError:
        return ""
    images = sorted(public.glob("*/live/latest.png"))
    return str(images[0].relative_to(public)) if images else ""


def _live_urls(doc) -> list[str]:
    """Every live-preview image named anywhere in a status document."""
    found = []
    if isinstance(doc, dict):
        for value in doc.values():
            found += _live_urls(value)
    elif isinstance(doc, list):
        for value in doc:
            found += _live_urls(value)
    elif isinstance(doc, str) and "/live/" in doc and doc.split("?", 1)[0].endswith(".png"):
        found.append(doc)   # e.g. /public/<project>/L/live/latest.png?v=<publish time>
    return found


def preview_served(server: str, pid: str) -> str:
    """The same as preview_published, over HTTP from any machine: the server's
    status.json for the project names a live preview, and that image is served.
    The document names its files "/public/...", served under /stack/public/."""
    status, doc = http("GET", f"{server}/stack/public/{pid}/status.json", timeout=10)
    if status != 200 or not isinstance(doc, dict):
        return ""
    for url in _live_urls(doc):
        path = "/stack" + url if url.startswith("/public/") else url
        full = path if path.startswith("http") else server + path
        if http("GET", full, timeout=20)[0] == 200:
            return full
    return ""


# ---------------------------------------------------------------- commands
def _region(text: str, default=(84.0, 0.0, 1.0, 1.0)) -> tuple:
    if not text:
        return default
    try:
        parts = [float(x) for x in text.split(",")]
        if len(parts) not in (2, 4):
            raise ValueError
    except ValueError:
        fail("--region is RA,DEC or RA,DEC,WIDTH,HEIGHT in degrees")
    return tuple(parts) if len(parts) == 4 else (parts[0], parts[1], 1.0, 1.0)


def _hub_id(args, owner: str) -> str:
    """The hub to bind to: --hub-id, else the one in --hub-config, else the
    owner's only hub. `owner` is the token already held: a second sign-in as
    the same person replaces the first one's token."""
    if args.hub_id:
        return args.hub_id
    if args.hub_config:
        doc = tomllib.loads(Path(args.hub_config).expanduser().read_text())
        return (doc.get("starfront") or {}).get("hub_id", "")
    _, mine = http("GET", f"{args.server}/api/v1/hubs", token=owner)
    hubs = (mine or {}).get("hubs", []) if isinstance(mine, dict) else []
    if len(hubs) != 1:
        fail(f"the owner has {len(hubs)} hubs; pass --hub-id or --hub-config")
    return hubs[0].get("id") or hubs[0].get("hubId", "")


def cmd_register_hub(args) -> int:
    hub_dir = Path(args.hub_dir).expanduser().resolve()
    paths = {"macos": args.xpsd_macos, "windows": args.xpsd_windows, "linux": args.xpsd_linux}
    creds_file = hub_dir / "hub-credentials.toml"
    owner = sign_in_as(args.server, args.name)
    known = None
    if creds_file.exists() and not args.force:
        creds = tomllib.loads(creds_file.read_text())
        _, mine = http("GET", f"{args.server}/api/v1/hubs", token=owner)
        ids = {h.get("id") or h.get("hubId") for h in (mine or {}).get("hubs", [])} if isinstance(mine, dict) else set()
        if creds.get("hub_id") in ids:
            known = creds
            say(f"the hub {creds['hub_id']} is already registered; rewriting collab-hub.toml only")
    reg = ({"hubId": known["hub_id"], "hubToken": known["hub_token"], "grantSecret": known["grant_secret"]}
           if known else register_hub(args.server, owner, args.hub_name, args.public_url))
    config = write_hub_files(hub_dir, hub_id=reg["hubId"], hub_token=reg["hubToken"],
                             grant_secret=reg["grantSecret"], server_local=args.server,
                             bind_port=args.bind_port, public=args.public_url,
                             xpsd=args.xpsd_dir or None, xpsd_paths=paths, write_credentials=known is None)
    say(json.dumps({"hubId": reg["hubId"], "config": str(config), "registered": known is None}))
    return 0


def cmd_bind_collab(args) -> int:
    owner = sign_in_as(args.server, args.name)
    hub_id = _hub_id(args, owner)
    if not hub_id:
        fail("no hub id; pass --hub-id or --hub-config")
    bound = bind_collaboration(args.server, owner, hub_id, name=args.collab_name, region=_region(args.region),
                               scope_name=args.scope_name, filter_mapping=[],
                               discord_webhook=args.discord_webhook)
    bound["grantMadeAt"] = int(time.time())
    write_secret(Path(args.out).expanduser(), json.dumps(bound, indent=2) + "\n")
    say(json.dumps({k: v for k, v in bound.items() if k not in SECRETS}))
    say(f"the grant (24 h) is in {args.out}, mode 0600")
    return 0


def cmd_refresh_grant(args) -> int:
    path = Path(args.state).expanduser()
    bound = json.loads(path.read_text())
    bound["grant"] = fresh_grant(args.server, bound["scopeToken"], bound["collaboration"])
    bound["grantMadeAt"] = int(time.time())
    write_secret(path, json.dumps(bound, indent=2) + "\n")
    say(json.dumps({k: v for k, v in bound.items() if k not in SECRETS}))
    if args.print_grant:
        say(bound["grant"])
    return 0


def cmd_smoke(args) -> int:
    uploader, worker = Path(args.uploader), Path(args.worker)
    for binary in (uploader, worker):
        if not binary.exists():
            fail(f"no such binary: {binary}")
    status, _ = http("GET", f"{args.server}/api/v1/health", timeout=5)
    if status != 200:
        fail(f"the server at {args.server} does not answer its health check ({status})")
    owner = sign_in_as(args.server, args.name)
    hub_id = _hub_id(args, owner)
    bound = bind_collaboration(args.server, owner, hub_id, name=f"Lab smoke {time.strftime('%H:%M:%S')}",
                               region=(SYNTH_RA, SYNTH_DEC, 1.0, 1.0), scope_name="Lab smoke scope",
                               filter_mapping=[{"raw": "Narrowband A", "canonical": "OIII"}])
    pid = bound["stackingProject"]
    if args.via == "hub":
        lan = (args.hub or bound["lanUrl"]).rstrip("/")
        if not lan:
            fail("the collaboration names no hub LAN address; pass --hub http://HOST:8790")
        api_base = f"{lan}/api"
    else:
        api_base = f"{args.server}/stack/api"
    work = Path(tempfile.mkdtemp(prefix="lab-smoke-"))
    try:
        frames = work / "frames"
        say("generating synthetic frames (collab-worker synth-frames)...")
        run_out([worker, "synth-frames", "--out", frames])
        say(f"uploading to {api_base} (--via {args.via}) ...")
        upload_synthetic(uploader, frames, work, api_base, bound["grant"], pid, work / "uploader.log",
                         list_base=f"{args.server}/stack/api")
        say("uploaded. Waiting for the workers to stack and the hub to push a live preview...")
        begun = time.time()
        found = wait_until("a live preview from the server", lambda: preview_served(args.server, pid),
                           args.timeout, every=3)
        say(f"live preview served: {found} after {int(time.time() - begun)} s")
        say(f"project page: {args.server}/stack/p/{bound['slug']}")
        return 0
    finally:
        if args.keep:
            say(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set up and check a multi-machine live-stacking lab")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--server", required=True, help="the Starfront server, e.g. http://omarchy.lan:8800")
        p.add_argument("--name", default=OWNER, help=f"the owner's stand-in Discord name (default {OWNER})")

    reg = sub.add_parser("register-hub", help="register a hub and write its two files (run where the hub runs)")
    common(reg)
    reg.add_argument("--hub-dir", required=True, help="where collab-hub.toml and the credentials go")
    reg.add_argument("--hub-name", default="Lab hub")
    reg.add_argument("--public-url", required=True, help="what other machines reach the hub at")
    reg.add_argument("--bind-port", type=int, default=8790)
    reg.add_argument("--xpsd-dir", default="", help="the hub's own catalogue folder (blank: none)")
    for key in OSES:
        reg.add_argument(f"--xpsd-{key}", default="", help=f"the catalogue path a {key} worker uses")
    reg.add_argument("--force", action="store_true", help="register a new hub even if one is already here")
    reg.set_defaults(run=cmd_register_hub)

    bind = sub.add_parser("bind-collab", help="a collaboration bound to the hub, and a telescope's grant")
    common(bind)
    bind.add_argument("--out", required=True, help="JSON file for the ids and the grant (written 0600)")
    bind.add_argument("--hub-id", default="")
    bind.add_argument("--hub-config", default="", help="read the hub id from this collab-hub.toml")
    bind.add_argument("--collab-name", default="Lab night")
    bind.add_argument("--scope-name", default="Lab scope")
    bind.add_argument("--region", default="", help="RA,DEC[,WIDTH,HEIGHT] in degrees (default 84,0,1,1)")
    bind.add_argument("--discord-webhook", default="")
    bind.set_defaults(run=cmd_bind_collab)

    grant = sub.add_parser("refresh-grant", help="renew the 24 h grant in a bind-collab state file")
    common(grant)
    grant.add_argument("--state", required=True, help="the --out file of bind-collab")
    grant.add_argument("--print-grant", action="store_true", help="also print the grant (last line)")
    grant.set_defaults(run=cmd_refresh_grant)

    smoke = sub.add_parser("smoke", help="synthetic frames in, a live preview out (the multi-machine gate)")
    common(smoke)
    smoke.add_argument("--via", choices=("relay", "hub"), default="relay",
                       help="upload through the server's front door (relay) or straight to the hub")
    smoke.add_argument("--hub", default="", help="the hub's LAN URL for --via hub (default: what the server says)")
    smoke.add_argument("--hub-id", default="")
    smoke.add_argument("--hub-config", default="")
    smoke.add_argument("--uploader", required=True, help="the collab-uploader binary")
    smoke.add_argument("--worker", required=True, help="a collab-worker binary (for synth-frames)")
    smoke.add_argument("--timeout", type=float, default=1500)
    smoke.add_argument("--keep", action="store_true", help="keep the synthetic frames and logs")
    smoke.set_defaults(run=cmd_smoke)

    args = parser.parse_args(argv)
    args.server = args.server.rstrip("/")
    if not re.match(r"^https?://", args.server):
        parser.error("--server is an http:// or https:// address")
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
