# Runs ON the server host (piped over SSH by hub-up.sh).
#   bash -s -- PORT HUB_PORT HUB_URL MODE XPSD_LINUX XPSD_MACOS XPSD_WINDOWS REGION
set -euo pipefail
port="$1" hub_port="$2" hub_url="$3" mode="$4" xpsd_linux="$5" xpsd_macos="$6" xpsd_windows="$7" region="$8"
lab="$HOME/starfront-lab"
py="$lab/venv/bin/python" hub="$lab/bin/collab-hub" conf="$lab/hub/collab-hub.toml"
units="$HOME/.config/systemd/user"
server="http://127.0.0.1:$port"

# In synthetic mode the hub gets no catalogue at all (the workers use the
# synthetic sky); in real mode it reads the NAS share mounted by bootstrap-linux.sh.
xpsd=""
if [ "$mode" = real ]; then
  xpsd="$xpsd_linux"
  ls "$xpsd"/gdr3-*.xpsd >/dev/null 2>&1 || {
    echo "hub: no gdr3-*.xpsd in $xpsd. Is the NAS share mounted? (bootstrap-linux.sh, then: ls $xpsd)" >&2; exit 1; }
fi

"$py" "$lab/src/tools/lab_setup.py" register-hub --server "$server" --hub-dir "$lab/hub" \
  --hub-name "Lab hub" --public-url "$hub_url" --bind-port "$hub_port" --xpsd-dir "$xpsd" \
  --xpsd-linux "$xpsd_linux" --xpsd-macos "$xpsd_macos" --xpsd-windows "$xpsd_windows"
"$hub" check --config "$conf"

cat > "$units/collab-hub.service" <<EOF
[Unit]
Description=Rapid Stacker Collab hub for the Starfront lab
After=network-online.target starfront-lab.service

[Service]
ExecStart=%h/starfront-lab/bin/collab-hub run --config %h/starfront-lab/hub/collab-hub.toml
Environment=RUST_LOG=info
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable collab-hub.service >/dev/null 2>&1
systemctl --user restart collab-hub.service

up=0
for _ in $(seq 60); do
  if curl -fsS "http://127.0.0.1:$hub_port/api/hub/v1/health" >/dev/null 2>&1; then up=1; break; fi
  sleep 1
done
if [ "$up" != 1 ]; then
  echo "hub: no health answer on port $hub_port after 60 s; its log:" >&2
  journalctl --user -u collab-hub -n 40 --no-pager >&2 || true
  exit 1
fi
echo "hub: up at $hub_url"

# An admin token for `lab.sh status`, minted once and kept owner-only.
if [ ! -s "$lab/hub/admin-token" ]; then
  (umask 077 && "$hub" token --admin --config "$conf" > "$lab/hub/admin-token")
fi

# The long-lived collaboration for real frames; made again only if the hub was
# registered anew (its hub id changed).
hub_id=$(sed -n 's/^hub_id = "\(.*\)"$/\1/p' "$conf")
if [ ! -s "$lab/lab-night.json" ] || ! grep -q "\"hubId\": \"$hub_id\"" "$lab/lab-night.json"; then
  "$py" "$lab/src/tools/lab_setup.py" bind-collab --server "$server" --hub-id "$hub_id" \
    --out "$lab/lab-night.json" --region "$region"
else
  echo "hub: the Lab night collaboration is already bound to $hub_id"
fi
