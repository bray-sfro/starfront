#!/usr/bin/env bash
# Put a collab-worker on one machine and install it as that machine's per-user
# service: this Mac (LAB_OS=macos, run locally), the server host (linux) or the
# Windows PC (windows), both over SSH. A worker already enrolled keeps its
# token; only a new one is given an enrol code, minted on the server host.
# Run by Terraform once per entry of var.workers.
set -euo pipefail
. "$(dirname "$0")/common.sh"
: "${LAB_ID:?}" "${LAB_OS:?}" "${LAB_SERVER_SSH:?}" "${LAB_HUB_URL:?}" "${LAB_BIN:?}"
: "${LAB_TARGET:=}" "${LAB_SCRATCH_GB:=40}" "${LAB_RAM_GB:=0}" "${LAB_MODE:=synthetic}" "${LAB_XPSD:=}"
[ -f "$LAB_BIN" ] || die "no $LAB_BIN; run scripts/fetch-binaries.sh first"

mint_code() {
  remote "$LAB_SERVER_SSH" "starfront-lab/bin/collab-hub enrol-code --config starfront-lab/hub/collab-hub.toml --note $LAB_ID"
}

case "$LAB_OS" in
  macos)
    lab="$HOME/starfront-lab-worker"
    mkdir -p "$lab/bin"
    code=-
    [ -f "$lab/worker/collab-worker.toml" ] || code=$(mint_code)
    [ -x "$lab/bin/collab-worker" ] && "$lab/bin/collab-worker" service stop --dir "$lab/worker" >/dev/null 2>&1 || true
    cp -f "$LAB_BIN" "$lab/bin/collab-worker.new" && mv -f "$lab/bin/collab-worker.new" "$lab/bin/collab-worker"
    bash "$here/remote/worker.sh" "$LAB_ID" "$LAB_HUB_URL" "$lab" "$code" "$LAB_MODE" "$LAB_SCRATCH_GB" "$LAB_XPSD" 0
    ;;
  linux)
    : "${LAB_TARGET:?user@address of the server host}"
    code=-
    remote "$LAB_TARGET" 'test -f starfront-lab/worker/collab-worker.toml' || code=$(mint_code)
    remote "$LAB_TARGET" 'mkdir -p starfront-lab/bin; [ -x starfront-lab/bin/collab-worker ] && starfront-lab/bin/collab-worker service stop --dir starfront-lab/worker >/dev/null 2>&1; true'
    copy "$LAB_BIN" "$LAB_TARGET:starfront-lab/bin/collab-worker.new"
    remote "$LAB_TARGET" 'chmod +x starfront-lab/bin/collab-worker.new && mv -f starfront-lab/bin/collab-worker.new starfront-lab/bin/collab-worker'
    remote "$LAB_TARGET" bash -s -- "$LAB_ID" "$LAB_HUB_URL" '$HOME/starfront-lab' "$code" "$LAB_MODE" "$LAB_SCRATCH_GB" "$LAB_XPSD" 0 \
      < "$here/remote/worker.sh"
    ;;
  windows)
    : "${LAB_TARGET:?user@address of the Windows PC}"
    code=-
    enrolled=$(remote "$LAB_TARGET" 'if (Test-Path starfront-lab\worker\collab-worker.toml) { "yes" } else { "no" }' | tr -d '\r')
    [ "$enrolled" = yes ] || code=$(mint_code)
    # A running .exe cannot be replaced, so the worker stops first. Ending the
    # task ends only service-run, not the `run` child it supervises (Collab
    # follow-up), so any process started from this .exe is ended as well.
    remote "$LAB_TARGET" 'New-Item -ItemType Directory -Force starfront-lab\bin | Out-Null; if (Test-Path starfront-lab\bin\collab-worker.exe) { & starfront-lab\bin\collab-worker.exe service stop --dir starfront-lab\worker *> $null; Get-Process collab-worker -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq (Resolve-Path starfront-lab\bin\collab-worker.exe).Path } | Stop-Process -Force }; exit 0'
    copy "$LAB_BIN" "$LAB_TARGET:starfront-lab/bin/collab-worker.exe"
    copy "$here/remote/worker-windows.ps1" "$LAB_TARGET:starfront-lab/worker-windows.ps1"
    [ "$code" != - ] || code=keep
    remote "$LAB_TARGET" "powershell -NoProfile -ExecutionPolicy Bypass -File starfront-lab\\worker-windows.ps1 -Id $LAB_ID -Hub $LAB_HUB_URL -Code $code -Mode $LAB_MODE -ScratchGb $LAB_SCRATCH_GB -RamGb $LAB_RAM_GB -Xpsd '$LAB_XPSD'"
    ;;
  *) die "LAB_OS is macos, linux or windows, not $LAB_OS" ;;
esac
