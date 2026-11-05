#!/usr/bin/env bash
# Stop and remove the lab server's unit on the server host (terraform destroy).
# With LAB_WIPE=1 the server's data and the hub's data go too; hub-down.sh has
# already written a backup to ~/starfront-lab/backups by then. The sources and
# the venv stay, so the next apply is quick.
set -euo pipefail
. "$(dirname "$0")/common.sh"
: "${LAB_SSH:?}" "${LAB_WIPE:=0}"
remote "$LAB_SSH" bash -s -- "$LAB_WIPE" <<'EOF'
set -euo pipefail
wipe="$1" lab="$HOME/starfront-lab"
systemctl --user disable --now starfront-lab.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/starfront-lab.service"
systemctl --user daemon-reload
if [ "$wipe" = 1 ]; then
  rm -rf "$lab/data" "$lab/hub" "$lab/lab-night.json"
  echo "server: stopped; data wiped (backups kept in $lab/backups)"
else
  echo "server: stopped; data kept in $lab"
fi
EOF
