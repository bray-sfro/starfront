#!/usr/bin/env bash
# Stop and remove the hub's unit on the server host (terraform destroy). With
# LAB_WIPE=1 it first backs the hub up (while it still runs, which collab-hub
# backup needs) into ~/starfront-lab/backups; server-down.sh then deletes the data.
set -euo pipefail
. "$(dirname "$0")/common.sh"
: "${LAB_SSH:?}" "${LAB_WIPE:=0}"
remote "$LAB_SSH" bash -s -- "$LAB_WIPE" <<'EOF'
set -euo pipefail
wipe="$1" lab="$HOME/starfront-lab"
if [ "$wipe" = 1 ] && [ -f "$lab/hub/collab-hub.toml" ]; then
  mkdir -p "$lab/backups"
  "$lab/bin/collab-hub" backup "$lab/backups" --config "$lab/hub/collab-hub.toml" \
    && echo "hub: backed up to $lab/backups" || echo "hub: backup failed; the data is deleted only by server-down" >&2
fi
systemctl --user disable --now collab-hub.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/collab-hub.service"
systemctl --user daemon-reload
echo "hub: stopped"
EOF
