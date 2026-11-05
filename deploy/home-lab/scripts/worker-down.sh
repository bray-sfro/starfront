#!/usr/bin/env bash
# Stop a lab worker and remove its service (terraform destroy). It drains first,
# except on Windows, where stopping ends it at once. Its config folder and token
# stay, so the next apply re-installs it without a new enrol code.
set -euo pipefail
. "$(dirname "$0")/common.sh"
: "${LAB_ID:?}" "${LAB_OS:?}" "${LAB_TARGET:=}"
case "$LAB_OS" in
  macos)
    lab="$HOME/starfront-lab-worker"
    [ -x "$lab/bin/collab-worker" ] && "$lab/bin/collab-worker" service uninstall --dir "$lab/worker" || true ;;
  linux)
    remote "$LAB_TARGET" '[ -x starfront-lab/bin/collab-worker ] && starfront-lab/bin/collab-worker service uninstall --dir starfront-lab/worker; true' ;;
  windows)
    # Uninstalling ends service-run but not its `run` child, so that goes too.
    remote "$LAB_TARGET" 'if (Test-Path starfront-lab\bin\collab-worker.exe) { & starfront-lab\bin\collab-worker.exe service uninstall --dir starfront-lab\worker; Get-Process collab-worker -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq (Resolve-Path starfront-lab\bin\collab-worker.exe).Path } | Stop-Process -Force }; exit 0' ;;
esac
say "worker $LAB_ID: service removed (its config and token are kept)"
