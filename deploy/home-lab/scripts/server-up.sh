#!/usr/bin/env bash
# Copy this Mac's Starfront sources to the server host and (re)start the lab
# server there as a systemd user unit. Run by Terraform, or by hand with the
# same LAB_* variables. Uncommitted changes are copied too: that is the point.
#   LAB_SSH=user@omarchy LAB_SRC=~/Develop/EKEServices/Astro/starfront \
#   LAB_PUBLIC_HOST=omarchy.lan LAB_PORT=8800 bash server-up.sh
set -euo pipefail
. "$(dirname "$0")/common.sh"
: "${LAB_SSH:?}" "${LAB_SRC:?}" "${LAB_PUBLIC_HOST:?}" "${LAB_PORT:=8800}"

say "server: copying server/, astrocontrol/, tools/ and requirements.txt to $LAB_SSH:starfront-lab/src/"
remote "$LAB_SSH" 'mkdir -p starfront-lab/src'
rsync -az --delete --exclude __pycache__ --exclude '*.pyc' --exclude .DS_Store -e "$(rsh)" \
  "$LAB_SRC/server" "$LAB_SRC/astrocontrol" "$LAB_SRC/tools" "$LAB_SRC/requirements.txt" \
  "$LAB_SSH:starfront-lab/src/"

# The N.I.N.A. plugin build (fetch-binaries.sh --plugin) and its install script,
# for the PC that runs N.I.N.A.: the server's /nina feed offers the build and
# run_local.py serves the script (run_local.py --nina-dir). CI's manifest names
# a placeholder download address; it is pointed at this server, so N.I.N.A.'s
# own plugin manager could take the feed as well.
stage="$(mktemp -d)"
cp "$here/../install-nina-plugin.ps1" "$stage/"
for manifest in "$here/../bin/nina-plugin"/*.manifest.json; do
  [ -f "$manifest" ] || continue
  cp "$(dirname "$manifest")"/*.zip "$stage/"
  python3 - "$manifest" "$stage/$(basename "$manifest")" "http://$LAB_PUBLIC_HOST:$LAB_PORT/nina/download" <<'PY'
import json, sys
src, dst, base = sys.argv[1:]
with open(src, encoding="utf-8-sig") as f:
    data = json.load(f)
data["Installer"]["URL"] = base + "/" + data["Installer"]["URL"].rsplit("/", 1)[-1]
with open(dst, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
PY
  say "server: the N.I.N.A. plugin build $(basename "$manifest" .manifest.json) for the /nina feed"
done
rsync -az --delete -e "$(rsh)" "$stage/" "$LAB_SSH:starfront-lab/nina/"
rm -rf "$stage"

remote "$LAB_SSH" bash -s -- "$LAB_PUBLIC_HOST" "$LAB_PORT" < "$here/remote/server.sh"
