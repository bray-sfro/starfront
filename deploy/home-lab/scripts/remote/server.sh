# Runs ON the server host (piped over SSH by server-up.sh): a venv with the
# server's packages and a systemd user unit for run_local.py --lan.
#   bash -s -- PUBLIC_HOST PORT
set -euo pipefail
public_host="$1" port="$2"
lab="$HOME/starfront-lab"
units="$HOME/.config/systemd/user"
mkdir -p "$lab/data" "$lab/nina" "$units"

command -v python3 >/dev/null || { echo "server host: python3 is missing; run bootstrap-linux.sh" >&2; exit 1; }
[ -x "$lab/venv/bin/python" ] || python3 -m venv "$lab/venv"
# The server's packages, without pywebview: the desktop window is not needed
# here, and it wants GTK or Qt on Linux.
grep -viE '^\s*pywebview' "$lab/src/requirements.txt" > "$lab/requirements-server.txt"
"$lab/venv/bin/pip" install -q --disable-pip-version-check -r "$lab/requirements-server.txt"

cat > "$units/starfront-lab.service" <<EOF
[Unit]
Description=Starfront lab server (stand-in Discord: anyone on the LAN can sign in as anyone; local testing only)
After=network-online.target

[Service]
WorkingDirectory=%h/starfront-lab/src
ExecStart=%h/starfront-lab/venv/bin/python server/run_local.py --lan --public-host $public_host --port $port --data %h/starfront-lab/data --nina-dir %h/starfront-lab/nina
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable starfront-lab.service >/dev/null 2>&1
systemctl --user restart starfront-lab.service

for _ in $(seq 60); do
  if curl -fsS "http://127.0.0.1:$port/api/v1/health" >/dev/null 2>&1; then
    echo "server: up at http://$public_host:$port"
    exit 0
  fi
  sleep 1
done
echo "server: no health answer on port $port after 60 s; its log:" >&2
journalctl --user -u starfront-lab -n 40 --no-pager >&2 || true
exit 1
