#!/usr/bin/env bash
# Copy collab-hub to the server host, register it with the lab server (once),
# write its configuration, check it, and (re)start it as a systemd user unit.
# Then make sure the "Lab night" collaboration exists. Run by Terraform.
set -euo pipefail
. "$(dirname "$0")/common.sh"
: "${LAB_SSH:?}" "${LAB_BIN:?}" "${LAB_HUB_URL:?}" "${LAB_PORT:=8800}" "${LAB_HUB_PORT:=8790}" "${LAB_MODE:=synthetic}"
: "${LAB_XPSD_LINUX:=/mnt/xpsd}" "${LAB_XPSD_MACOS:=}" "${LAB_XPSD_WINDOWS:=}" "${LAB_REGION:=84,0,1,1}"
[ -f "$LAB_BIN" ] || die "no $LAB_BIN; run scripts/fetch-binaries.sh first"

say "hub: copying collab-hub to $LAB_SSH:starfront-lab/bin/"
remote "$LAB_SSH" 'mkdir -p starfront-lab/bin && systemctl --user stop collab-hub.service 2>/dev/null || true'
# Copied beside, then renamed: a running binary cannot be overwritten in place.
copy "$LAB_BIN" "$LAB_SSH:starfront-lab/bin/collab-hub.new"
remote "$LAB_SSH" 'chmod +x starfront-lab/bin/collab-hub.new && mv -f starfront-lab/bin/collab-hub.new starfront-lab/bin/collab-hub'
# ssh joins its arguments into one command line that the remote shell parses
# again, so each one is quoted here first. Unquoted, the Windows share
# \\NAS\XPSD lost its backslashes on the way (\NASXPSD), and every Windows
# worker the hub enrolled was handed an xpsd_dir that does not exist; an empty
# argument would vanish and shift the ones after it.
args=$(printf ' %q' "$LAB_PORT" "$LAB_HUB_PORT" "$LAB_HUB_URL" "$LAB_MODE" \
  "$LAB_XPSD_LINUX" "$LAB_XPSD_MACOS" "$LAB_XPSD_WINDOWS" "$LAB_REGION")
remote "$LAB_SSH" "bash -s --$args" < "$here/remote/hub.sh"
