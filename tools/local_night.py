#!/usr/bin/env python3
"""Bring up the whole live-stacking system on this Mac, for a home-network test.

    python3 tools/local_night.py                      # start everything, print the URLs, tail the logs
    python3 tools/local_night.py --synthetic          # ...and feed it synthetic frames, as a telescope would
    python3 tools/local_night.py --synthetic --exit-when-stacked   # the gate: stack, then tear down, exit 0

Why it exists: live stacking is five programs on two or three machines (the
Starfront server, a hub, a worker, an uploader, N.I.N.A.), and starting them
by hand means a stand-in Discord, a hub registration, two TOML files, an enrol
code and a collaboration bound to the hub, in the right order. This is the one
command that does all of it, so a reviewer can see a stack appear without
cloud accounts and the author can point the Windows PC and N.I.N.A. at it.

What it starts, all under one --data folder (default ~/starfront-local):

  1. server/run_local.py --lan   the Starfront server with its stand-in Discord
                                 (owner ncedelliott), port 8800
  2. collab-hub run              registered to that server (a hub-credentials.toml
                                 at 0600 and a collab-hub.toml written here), port 8790
  3. collab-worker run           enrolled to the hub, idle policy "always"

It also signs in as the owner, binds a throw-away collaboration to the hub
(the project page URL is printed), and prints what the Windows PC needs: the
worker enrol command, the ServerUrl for the N.I.N.A. plugin and the three URLs.

--synthetic generates frames with `collab-worker synth-frames` and uploads them
with `collab-uploader` through the Starfront front door (/stack/api), using the
grant a telescope gets from the agent listing. The synthetic sky is fake, so the
hub is given no XPSD folder and the worker is told to use the synthetic
catalogue (COLLAB_LOCAL_CATALOG=synthetic, the same knob scripts/local-e2e.sh
uses); a real Gaia catalogue could never solve those stars. Without --synthetic
the hub uses ~/Astronomy/XPSD when it exists.

--exit-when-stacked waits until the hub has pushed a live preview to
STARFRONT_DATA/stack/public/<project_id>/status.json, then stops everything and
exits 0 (non-zero on timeout or if any program dies).

What it does not do: build or publish anything for release (it runs the three
binaries from a Rapid Stacker Collab checkout, building them with cargo unless
--no-build; or, with --bin-dir, ones already there, such as the public signed
release that `deploy/home-lab/scripts/fetch-binaries.sh --from-release` puts in
deploy/home-lab/bin/macos-arm64, which needs no access to Collab's private
source but has no collab-uploader, so --synthetic then wants one), touch the cloud (the server is a local stand-in anyone on the
network can sign in to as anyone: local testing only), install the worker as a
service, or edit your Starfront desktop settings. Like run_local.py it refuses a
real server's data folder. The standard library only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

# The set-up steps are shared with the multi-machine lab (deploy/home-lab/), so
# the two cannot drift: they live in lab_setup.py.
from lab_setup import (OWNER, SYNTH_DEC, SYNTH_RA, bind_collaboration, fail, http, preview_published,  # noqa: E402
                       register_hub, run_out, say, set_toml_keys, sign_in_as, toml_str, upload_synthetic,
                       write_hub_files, xpsd_paths_for)

DEFAULT_RSC = ROOT.parent / "Rust" / "rapid-stacker-collab"
# The default above is relative to the main checkout (Astro/starfront); a
# worktree sits one level deeper, so fall back to the well-known location.
FALLBACK_RSC = Path.home() / "Develop" / "EKEServices" / "Astro" / "Rust" / "rapid-stacker-collab"

BINARIES = ("collab-hub", "collab-worker", "collab-uploader")

_passed = True


# ------------------------------------------------------------------ processes
class Fleet:
    """The child programs, started in their own process groups so a stop takes
    their children (the worker's prepare helpers) with them."""

    def __init__(self, logs: Path):
        self.logs = logs
        self.procs: dict[str, subprocess.Popen] = {}
        logs.mkdir(parents=True, exist_ok=True)

    def start(self, name: str, argv: list[str], env: dict[str, str] | None = None,
              cwd: Path | None = None) -> subprocess.Popen:
        log = open(self.logs / f"{name}.log", "ab", buffering=0)
        merged = dict(os.environ)
        merged.update(env or {})
        proc = subprocess.Popen([str(a) for a in argv], stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=merged, cwd=cwd,
                                start_new_session=True)
        self.procs[name] = proc
        return proc

    def dead(self) -> str:
        """The name of a program that has exited, or ''."""
        for name, proc in self.procs.items():
            if proc.poll() is not None:
                return f"{name} exited with {proc.returncode} (see {self.logs / (name + '.log')})"
        return ""

    def stop(self) -> None:
        for name in reversed(list(self.procs)):
            proc = self.procs[name]
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
        deadline = time.time() + 15
        for proc in self.procs.values():
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        self.procs.clear()


def tail(logs: Path, names: list[str], stop: threading.Event) -> None:
    """Print new lines of each log, prefixed with its name, until stopped."""
    files = {}
    while not stop.is_set():
        for name in names:
            path = logs / f"{name}.log"
            if name not in files and path.exists():
                handle = open(path, "r", errors="replace")
                handle.seek(0, os.SEEK_END)
                files[name] = handle
            handle = files.get(name)
            while handle is not None:
                line = handle.readline()
                if not line:
                    break
                print(f"[{name}] {line.rstrip()}", flush=True)
        stop.wait(0.5)


def wait_for(what: str, probe, fleet: Fleet, timeout: float, every: float = 0.5):
    """Call probe() until it returns something truthy; fail if a program dies."""
    end = time.time() + timeout
    while time.time() < end:
        gone = fleet.dead()
        if gone:
            fail(f"while waiting for {what}: {gone}")
        try:
            found = probe()
        except (OSError, ValueError, urllib.error.URLError):
            found = None
        if found:
            return found
        time.sleep(every)
    fail(f"timed out after {int(timeout)} s waiting for {what}")


# --------------------------------------------------------------------- set up
def find_rsc(arg: str | None) -> Path:
    for candidate in ([Path(arg)] if arg else [DEFAULT_RSC, FALLBACK_RSC]):
        if (candidate / "worker" / "Cargo.toml").exists():
            return candidate.resolve()
    fail("no Rapid Stacker Collab checkout found; pass --rsc DIR")


def find_binaries(rsc: Path, build: bool) -> dict[str, Path]:
    target = rsc / "worker" / "target" / "release"
    if build:
        say("building collab-hub, collab-worker and collab-uploader (cargo build --release)...")
        env = dict(os.environ, CARGO_NET_GIT_FETCH_WITH_CLI="true")
        code = subprocess.call(["cargo", "build", "--release", "-p", "collab-hub", "-p", "collab-worker",
                                "-p", "collab-uploader"], cwd=rsc / "worker", env=env)
        if code != 0:
            fail("cargo build failed")
    found = {name: target / name for name in BINARIES}
    missing = [n for n, p in found.items() if not p.exists()]
    if missing:
        fail(f"missing {', '.join(missing)} in {target}; run without --no-build")
    return found


def binaries_in(folder: Path, need_uploader: bool) -> dict[str, Path]:
    """Prebuilt binaries in `folder` (--bin-dir). The public release carries the
    hub and the worker but not collab-uploader, which only --synthetic needs."""
    found = {name: folder / name for name in BINARIES if (folder / name).exists()}
    missing = [n for n in ("collab-hub", "collab-worker") + (("collab-uploader",) if need_uploader else ())
               if n not in found]
    if missing:
        fail(f"missing {', '.join(missing)} in {folder}"
             + ("; collab-uploader is not in the public release, so --synthetic needs one built from "
                "Rapid Stacker Collab" if "collab-uploader" in missing else ""))
    return found


def refuse_real_data(data: Path) -> None:
    """The same refusals as server/run_local.py, before anything is written."""
    from server import auth
    from astrocontrol.config import data_root
    real = (data_root() / "collab").resolve()
    if (data / auth.ENV_FILE).exists():
        fail(f"{data} has a {auth.ENV_FILE}, so it is a real server's folder. Give --data a folder of its own.")
    if data == real or real in data.parents:
        fail(f"{data} is where a real server keeps its database. Give --data a folder of its own.")


def port_free(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


# ------------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the whole live-stacking system on this Mac")
    parser.add_argument("--rsc", help="Rapid Stacker Collab checkout (default: the sibling Rust/rapid-stacker-collab)")
    parser.add_argument("--no-build", action="store_true", help="use the binaries already built in RSC")
    parser.add_argument("--bin-dir", help="use prebuilt binaries from this folder instead of a Collab checkout "
                                          "(e.g. deploy/home-lab/bin/macos-arm64 after fetch-binaries.sh "
                                          "--from-release)")
    parser.add_argument("--data", default=str(Path.home() / "starfront-local"),
                        help="everything lives here (default ~/starfront-local)")
    parser.add_argument("--port", type=int, default=8800, help="server port (default 8800)")
    parser.add_argument("--hub-port", type=int, default=8790, help="hub port (default 8790)")
    parser.add_argument("--name", default=OWNER, help=f"the owner's stand-in Discord name (default {OWNER})")
    parser.add_argument("--synthetic", action="store_true", help="upload synthetic frames as a telescope would")
    parser.add_argument("--exit-when-stacked", action="store_true",
                        help="wait for a published live preview, tear down, exit 0")
    parser.add_argument("--timeout", type=float, default=1500, help="seconds to wait for the stack (default 1500)")
    parser.add_argument("--region", default="",
                        help="the collaboration's sky region for real frames, RA,DEC[,WIDTH,HEIGHT] in degrees "
                             "(default 84,0,1,1; ignored with --synthetic). Real frames solve only against "
                             "the catalogue cone of this region, so set it to your target")
    parser.add_argument("--discord-webhook", default="",
                        help="a Discord webhook URL for the collaboration (gold published, member joined); "
                             "the desktop has no field for it yet")
    args = parser.parse_args(argv)
    from server.run_local import missing_requirements
    missing = missing_requirements()
    if missing:
        parser.error(f"this Python ({sys.executable}) lacks {', '.join(missing)}, which the server needs: "
                     f"run {sys.executable} -m pip install -r requirements.txt")
    region = (84.0, 0.0, 1.0, 1.0)
    if args.region:
        try:
            parts = [float(x) for x in args.region.split(",")]
            if len(parts) not in (2, 4):
                raise ValueError
        except ValueError:
            parser.error("--region is RA,DEC or RA,DEC,WIDTH,HEIGHT in degrees")
        region = tuple(parts) if len(parts) == 4 else (parts[0], parts[1], 1.0, 1.0)
    if args.exit_when_stacked and not args.synthetic:
        parser.error("--exit-when-stacked needs --synthetic (nothing else makes frames)")
    if args.bin_dir and (args.rsc or args.no_build):
        parser.error("--bin-dir takes the binaries as they are; it goes with neither --rsc nor --no-build")

    from server.run_local import lan_addresses, local_name
    data = Path(args.data).expanduser().resolve()
    refuse_real_data(data)
    for port in (args.port, args.hub_port):
        if not port_free(port):
            fail(f"port {port} is already in use (an earlier run still going?)")
    if args.bin_dir:
        bins = binaries_in(Path(args.bin_dir).expanduser().resolve(), args.synthetic)
    else:
        bins = find_binaries(find_rsc(args.rsc), not args.no_build)

    data.mkdir(parents=True, exist_ok=True)
    logs = data / "logs"
    fleet = Fleet(logs)
    ips = lan_addresses()
    host = ips[0] if ips else local_name()
    server_public = f"http://{host}:{args.port}"
    server_local = f"http://127.0.0.1:{args.port}"
    hub_public = f"http://{host}:{args.hub_port}"
    hub_local = f"http://127.0.0.1:{args.hub_port}"
    stop_tail = threading.Event()
    result = 1

    def shutdown(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    try:
        # 1. the server with its stand-in Discord
        say(f"starting the Starfront server (stand-in Discord, owner {args.name}) on port {args.port}...")
        fleet.start("server", [sys.executable, ROOT / "server" / "run_local.py", "--lan", "--data", data,
                               "--port", args.port, "--name", args.name], cwd=ROOT)
        wait_for("the server's health", lambda: http("GET", f"{server_local}/api/v1/health", timeout=3)[0] == 200,
                 fleet, 60)

        # 2. the owner signs in and registers a hub
        owner = sign_in_as(server_local, args.name)
        reg = register_hub(server_local, owner, "Local hub", hub_public)
        xpsd = Path.home() / "Astronomy" / "XPSD"
        use_xpsd = xpsd if (xpsd.is_dir() and not args.synthetic) else None
        hub_dir = data / "hub"
        # The Windows PC reaches this Mac's folder as the XPSD share, by the
        # whole address (an IP cut at its first dot named no host at all).
        config = write_hub_files(hub_dir, hub_id=reg["hubId"], hub_token=reg["hubToken"],
                                 grant_secret=reg["grantSecret"], server_local=server_local,
                                 bind_port=args.hub_port, public=hub_public, xpsd=use_xpsd,
                                 xpsd_paths=xpsd_paths_for(use_xpsd, host) if use_xpsd else None)
        say(f"starting collab-hub on port {args.hub_port} ({'XPSD ' + str(xpsd) if use_xpsd else 'no XPSD folder'})...")
        fleet.start("hub", [bins["collab-hub"], "run", "--config", config])
        wait_for("the hub's health", lambda: http("GET", f"{hub_local}/api/hub/v1/health", timeout=3)[0] == 200,
                 fleet, 60)

        # 3. a throw-away collaboration bound to the hub, and a telescope that has
        # joined it as a member. With --synthetic, the three synthetic contributors
        # share its one membership, so their filter names are mapped here, once;
        # real frames uploaded with collab-uploader (the command is printed below)
        # use the same membership with no mapping.
        bound = bind_collaboration(
            server_local, owner, reg["hubId"], name="Local night", scope_name="Local scope",
            region=(SYNTH_RA, SYNTH_DEC, 1.0, 1.0) if args.synthetic else region,
            filter_mapping=[{"raw": "Narrowband A", "canonical": "OIII"}] if args.synthetic else [],
            discord_webhook=args.discord_webhook)
        sf_project, pid, slug, grant = (bound["collaboration"], bound["stackingProject"],
                                        bound["slug"], bound["grant"])
        # The grant, for uploading real frames by hand (it lasts 24 h): owner-only.
        grant_file = data / "telescope-grant"
        grant_file.write_text(grant + "\n")
        grant_file.chmod(0o600)

        # 4. a local worker
        code = run_out([bins["collab-hub"], "enrol-code", "--config", config, "--note", "Windows PC"])
        local_code = run_out([bins["collab-hub"], "enrol-code", "--config", config, "--note", "this Mac"])
        wdir = data / "worker"
        # The synthetic sky has no XPSD file, but the worker's configuration check still
        # wants a catalogue folder to exist: an empty one, never read (the synthetic
        # catalogue wins), set in the environment so it is not written to the file.
        wenv = {}
        if args.synthetic:
            unused = data / "worker" / "no-xpsd"
            unused.mkdir(parents=True, exist_ok=True)
            wenv = {"COLLAB_LOCAL_CATALOG": "synthetic", "COLLAB_XPSD_DIR": str(unused)}
        run_out([bins["collab-worker"], "enrol", hub_local, "--code", local_code, "--worker-id", "mac-worker",
                 "--dir", wdir], env=dict(os.environ, **wenv))
        scratch = wdir / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        set_toml_keys(wdir / "collab-worker.toml", {"idle_policy": '"always"', "scratch_dir": toml_str(scratch)})
        say("starting collab-worker (idle policy always)...")
        fleet.start("worker", [bins["collab-worker"], "run", "--config", wdir / "collab-worker.toml"],
                    env=dict(wenv, RUST_LOG=os.environ.get("RUST_LOG", "info")))
        time.sleep(2)

        page = f"{server_public}/stack/p/{slug}"
        say("")
        say("=" * 72)
        say("  LOCAL NIGHT IS UP   (anyone on this network can sign in as anyone)")
        say("=" * 72)
        say(f"  server        {server_public}")
        say(f"  hub           {hub_public}")
        say(f"  project page  {page}")
        say(f"  collaboration {sf_project}   stacking project {pid}   slug {slug}")
        say(f"  data & logs   {data}   ({logs})")
        say("")
        say("  Windows PC, a second worker:")
        say(f"    collab-worker enrol {hub_public} --code {code}")
        say("    collab-worker service install")
        say("  N.I.N.A. plugin settings file:  ServerUrl = " + server_public)
        say("  Starfront desktop (local mode): collab.serverUrl = " + server_public)
        say(f"  (the enrol code is single-use and works for 1 h; mint more with: "
            f"{bins['collab-hub']} enrol-code --config {config})")
        if not args.synthetic and "collab-uploader" not in bins:
            say("")
            say("  Real frames: send them from N.I.N.A. with the Starfront plugin (collab-uploader is not in")
            say("  the public release, so there is no command-line uploader here).")
        if not args.synthetic and "collab-uploader" in bins:
            up = (f"COLLAB_TOKEN=$(cat {grant_file}) {bins['collab-uploader']} "
                  f"--api-base {server_public}/stack/api --config-dir {data / 'uploader' / 'real'}")
            say("")
            say("  Real frames from this Mac, as the telescope 'Local scope' (grant valid 24 h):")
            say(f"    {up} projects")
            say(f"    {up} rig confirm --from <one light> --yes")
            say(f"    {up} upload <folder> --project {pid} --include-calibration --now")
        say("")

        # 5. synthetic frames
        public_tree = data / "stack" / "public" / pid
        if args.synthetic:
            frames = data / "synthetic-frames"
            shutil.rmtree(frames, ignore_errors=True)
            say("generating synthetic frames (collab-worker synth-frames)...")
            run_out([bins["collab-worker"], "synth-frames", "--out", frames])
            say(f"uploading through the front door {server_public}/stack/api ...")
            upload_synthetic(bins["collab-uploader"], frames, data, f"{server_public}/stack/api", grant, pid,
                             logs / "uploader.log")
            say("uploaded. Waiting for the hub to relay, the worker to stack, and the hub to push a live preview...")

        if args.exit_when_stacked:
            begun = time.time()
            shown = 0.0
            while True:
                gone = fleet.dead()
                if gone:
                    fail(gone)
                found = preview_published(public_tree)
                if found:
                    say(f"live preview published: {public_tree / found} after {int(time.time() - begun)} s")
                    result = 0
                    break
                if time.time() - begun > args.timeout:
                    fail(f"no live preview in {public_tree} after {int(args.timeout)} s; see the logs in {logs}")
                if time.time() - shown > 30:
                    shown = time.time()
                    say(f"  ... {int(time.time() - begun)} s")
                time.sleep(2)
        else:
            say("Running. Ctrl-C stops everything. Logs follow.")
            threading.Thread(target=tail, args=(logs, ["server", "hub", "worker"], stop_tail), daemon=True).start()
            while True:
                gone = fleet.dead()
                if gone:
                    fail(gone)
                time.sleep(1)
    except KeyboardInterrupt:
        say("\nstopping...")
        result = 0
    finally:
        stop_tail.set()
        fleet.stop()
        say("everything stopped.")
    return result


if __name__ == "__main__":
    sys.exit(main())
