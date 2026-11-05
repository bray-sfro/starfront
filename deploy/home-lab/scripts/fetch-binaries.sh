#!/usr/bin/env bash
# Put collab-hub and collab-worker for each OS into deploy/home-lab/bin/<os>/,
# where Terraform looks for them. Rust is not cross-compiled, so each OS's
# binaries come from somewhere that builds natively:
#
#   fetch-binaries.sh --from-release            the public signed release, for all three systems, from
#                                               Rapid Stacker Collab's downloads (or its GitHub mirror):
#                                               no Collab checkout and no private access needed (no
#                                               collab-uploader, though)
#   fetch-binaries.sh --mac                     cargo build --release here (also collab-uploader,
#                                               which the smoke test and real uploads use)
#   fetch-binaries.sh --from-run RUN_ID         the archives of a Rapid Stacker Collab worker-release
#                                               run (gh run download): linux-x64 and windows-x64
#   fetch-binaries.sh --build-on user@host      build on the Linux host itself, over SSH (it needs
#                                               rustup, cmake and read access to the private
#                                               rapid-stacker repository)
#   fetch-binaries.sh --plugin [RUN_ID]         the N.I.N.A. plugin (zip + manifest) into bin/nina-plugin/,
#                                               from Starfront's plugin CI: the newest green run on
#                                               main, or the run given. The lab server's /nina feed
#                                               offers it, for install-nina-plugin.ps1
#
# The worker-release workflow is manual (Actions > worker-release > Run) and
# signs, uploads and tags nothing; its archives are unsigned, which the lab is
# fine with (nothing installs them through the desktop's signed-manifest path).
set -euo pipefail
. "$(dirname "$0")/common.sh"
bin="$(cd "$here/.." && pwd)/bin"
rsc="${RSC:-$(cd "$here/../../../.." && pwd)/Rust/rapid-stacker-collab}"
# Only the options that build need the (private) checkout; --from-release and
# the CI downloads do not, so the check is made where it is needed.
need_rsc() { [ -f "$rsc/worker/Cargo.toml" ] || die "no Rapid Stacker Collab checkout at $rsc (set RSC=...)"; }
[ $# -gt 0 ] || { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --from-release)
      # Anyone can run this: the release is public, and trust comes from the
      # signature, checked with Starfront's own verifier and embedded key, then
      # each archive's size and SHA-256 as the signed manifest gives them. The
      # archives are unpacked with the same safe unpacker the desktop uses.
      # The places are the desktop's own (volunteer.RELEASES_URLS: Collab's
      # downloads, then the GitHub mirror), tried in order; STARFRONT_RELEASES
      # names one instead.
      say "the signed release ..."
      python3 - "$(cd "$here/../../.." && pwd)" "$bin" "${STARFRONT_RELEASES:-}" <<'PY'
import hashlib, json, shutil, sys, tempfile, urllib.error, urllib.parse, urllib.request
from pathlib import Path
root, bin_dir, chosen = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(root))
from astrocontrol import volunteer
bases = [chosen] if chosen else list(volunteer.RELEASES_URLS)

def get(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()

for product in ("collab-worker", "collab-hub"):
    # A place that is down or does not carry the release yet is skipped; one
    # whose signature does not verify stops everything, as a tampered mirror.
    for base in bases:
        url = f"{base}/{product}/latest.json"
        try:
            body, sig = get(url), get(url + ".sig").decode()
        except (urllib.error.URLError, OSError) as exc:
            print(f"  {url}: {exc}; trying the next place")
            continue
        break
    else:
        sys.exit(f"{product}: no release at any of {', '.join(bases)}")
    if not volunteer.verify_signature(volunteer.MANIFEST_PUBLIC_KEY, body, sig):
        sys.exit(f"{url}: the signature does not match Starfront's release key; nothing was installed")
    manifest = json.loads(body)
    for os_key, asset in manifest["assets"].items():
        a = asset["primary"]
        data = get(urllib.parse.urljoin(url, a["url"]))
        if len(data) != a["size"] or hashlib.sha256(data).hexdigest() != a["sha256"]:
            sys.exit(f"{a['filename']}: size or SHA-256 differs from the signed manifest; nothing more was installed")
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / a["filename"]
            archive.write_bytes(data)
            volunteer.safe_extract(archive, Path(tmp) / "x", "zip" if a["filename"].endswith(".zip") else "tar.gz")
            name = product + (".exe" if os_key.startswith("windows") else "")
            found = next((Path(tmp) / "x").rglob(name), None)
            if found is None:
                sys.exit(f"{a['filename']} holds no {name}")
            (bin_dir / os_key).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(found, bin_dir / os_key / name)
            (bin_dir / os_key / name).chmod(0o755)
        print(f"  {product} {manifest['version']} {os_key}: verified, from {base}")
PY
      shift ;;
    --mac)
      need_rsc
      say "building the macOS binaries in $rsc/worker ..."
      (cd "$rsc/worker" && CARGO_NET_GIT_FETCH_WITH_CLI=true cargo build --release \
         -p collab-hub -p collab-worker -p collab-uploader)
      mkdir -p "$bin/macos-arm64"
      for b in collab-hub collab-worker collab-uploader; do cp -f "$rsc/worker/target/release/$b" "$bin/macos-arm64/"; done
      shift ;;
    --from-run)
      [ $# -ge 2 ] || die "--from-run needs a run id"
      tmp="$(mktemp -d)"
      say "downloading the archives of run $2 ..."
      gh run download "$2" -R EKE-Services/rapid-stacker-collab -D "$tmp"
      for archive in $(find "$tmp" -name 'collab-*-*-*.zip' -o -name 'collab-*-*-*.tar.gz'); do
        case "$archive" in
          *-linux-x64.tar.gz) os=linux-x64 ;; *-windows-x64.zip) os=windows-x64 ;;
          *-macos-arm64.zip) os=macos-arm64 ;; *) continue ;;
        esac
        out="$tmp/x-$os-$(basename "$archive")"; mkdir -p "$out" "$bin/$os"
        case "$archive" in *.zip) unzip -q -o "$archive" -d "$out" ;; *) tar -xzf "$archive" -C "$out" ;; esac
        find "$out" -type f \( -name collab-hub -o -name collab-worker -o -name 'collab-hub.exe' -o -name 'collab-worker.exe' \) \
          -exec cp -f {} "$bin/$os/" \;
      done
      rm -rf "$tmp"
      shift 2 ;;
    --build-on)
      [ $# -ge 2 ] || die "--build-on needs user@host"
      need_rsc
      say "building on $2 (the source is copied from $rsc) ..."
      remote "$2" 'mkdir -p starfront-lab/build'
      rsync -az --delete --exclude target --exclude node_modules --exclude .git -e "$(rsh)" "$rsc/" "$2:starfront-lab/build/rsc/"
      remote "$2" 'cd starfront-lab/build/rsc/worker && CARGO_NET_GIT_FETCH_WITH_CLI=true cargo build --release -p collab-hub -p collab-worker'
      mkdir -p "$bin/linux-x64"
      copy "$2:starfront-lab/build/rsc/worker/target/release/collab-hub" "$2:starfront-lab/build/rsc/worker/target/release/collab-worker" "$bin/linux-x64/"
      shift 2 ;;
    --plugin)
      run=""
      case "${2:-}" in [0-9]*) run="$2"; shift ;; esac
      [ -n "$run" ] || run=$(gh run list -R EKEServices/starfront -w ci.yml -b main -s success -L 1 --json databaseId -q '.[0].databaseId')
      [ -n "$run" ] || die "no green plugin CI run on Starfront's main"
      say "the plugin from Starfront CI run $run ($(gh run view "$run" -R EKEServices/starfront --json headSha,displayTitle -q '.headSha[0:7] + " " + .displayTitle'))"
      tmp="$(mktemp -d)"
      gh run download "$run" -R EKEServices/starfront -n plugin-validation -D "$tmp"
      ls "$tmp"/*.zip "$tmp"/*.manifest.json >/dev/null 2>&1 || die "run $run's plugin-validation artifact has no zip and manifest"
      rm -rf "$bin/nina-plugin" && mkdir -p "$bin/nina-plugin"
      cp -f "$tmp"/*.zip "$tmp"/*.manifest.json "$bin/nina-plugin/"
      rm -rf "$tmp"
      shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
say "binaries now in $bin:"
ls -l "$bin"/*/ 2>/dev/null
