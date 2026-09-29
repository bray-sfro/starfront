#!/usr/bin/env bash
# Install the Starfront collaboration server on a fresh Ubuntu/Debian VPS.
#
#     sudo bash server/deploy/install.sh collab.example.org
#
# What it does, in order:
#   1. installs python3, a venv, and Caddy (which does HTTPS for you)
#   2. copies this checkout to /opt/astrocollab and makes a venv there
#   3. creates a system user and /var/lib/astrocollab for the database,
#      the admin token and discord.env
#   4. installs and starts the systemd service on 127.0.0.1:8800
#   5. points Caddy at it under your hostname, so https://<host>/ works
#
# Run it again after pulling a newer version: it re-copies the code and
# restarts the service, and leaves the data folder alone.
set -euo pipefail

HOST="${1:-}"
if [[ -z "$HOST" ]]; then
    echo "usage: sudo bash server/deploy/install.sh <hostname>" >&2
    echo "  e.g.  sudo bash server/deploy/install.sh collab.example.org" >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "run this with sudo" >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP=/opt/astrocollab
DATA=/var/lib/astrocollab

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync curl \
    debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
if ! command -v caddy >/dev/null; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy >/dev/null
fi

echo "== user and folders"
id -u astrocollab >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin astrocollab
mkdir -p "$APP" "$DATA"
chown astrocollab:astrocollab "$DATA"
chmod 700 "$DATA"

echo "== code -> $APP"
# Only what the server needs: the server package and the shared domain module.
rsync -a --delete \
    --exclude '__pycache__' --exclude '*.pyc' --exclude '.check-tmp' \
    "$HERE/server" "$HERE/astrocontrol" "$HERE/requirements.txt" "$APP/"
if [[ ! -x "$APP/venv/bin/python" ]]; then
    python3 -m venv "$APP/venv"
fi
# The server needs FastAPI and uvicorn only; the desktop and Windows bits are
# left out on purpose.
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q "fastapi>=0.110" "uvicorn[standard]>=0.27" "numpy>=1.24"
chown -R root:root "$APP"
chmod -R a+rX "$APP"

echo "== discord.env"
if [[ ! -f "$DATA/discord.env" ]]; then
    sed "s#https://collab.example.org#https://$HOST#" \
        "$HERE/server/deploy/discord.env.example" > "$DATA/discord.env"
    chown astrocollab:astrocollab "$DATA/discord.env"
    chmod 600 "$DATA/discord.env"
    echo "   written $DATA/discord.env - fill in the Discord values, then:"
    echo "       sudo systemctl restart astrocollab"
else
    echo "   kept $DATA/discord.env"
fi

echo "== service"
install -m 644 "$HERE/server/deploy/astrocollab.service" /etc/systemd/system/astrocollab.service
systemctl daemon-reload
systemctl enable --now astrocollab >/dev/null
systemctl restart astrocollab

echo "== caddy -> https://$HOST"
sed "s#COLLAB_HOST#$HOST#" "$HERE/server/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl enable --now caddy >/dev/null
systemctl reload caddy || systemctl restart caddy

sleep 2
echo
echo "== done"
echo "   health:  $(curl -s http://127.0.0.1:8800/api/v1/health || echo 'not answering yet')"
echo
echo "   Owner token, for scripts and emergencies (people sign in with Discord;"
echo "   owners are the Discord ids in ASTROCOLLAB_DISCORD_OWNERS):"
echo
echo "       $(sudo -u astrocollab ASTROCOLLAB_DATA=$DATA $APP/venv/bin/python $APP/server/run.py --show-token)"
echo
echo "   Server URL (built into Starfront):  https://$HOST"
echo "   Discord redirect URL:               https://$HOST/auth/discord/callback"
echo
echo "   logs:  journalctl -u astrocollab -f"
