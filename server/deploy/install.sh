#!/usr/bin/env bash
# Install the Starfront collaboration server on a fresh Ubuntu/Debian VPS.
#
#     sudo bash server/deploy/install.sh collab.starfront.space
#
# What it does, in order:
#   1. installs python3, a venv, and Caddy (which does HTTPS for you)
#   2. copies this checkout to /opt/starfront and makes a venv there
#   3. creates a system user and /var/lib/starfront for the database,
#      the admin token and discord.env
#   4. installs and starts the systemd service on 127.0.0.1:8800
#   5. points Caddy at it under your hostname, so https://<host>/ works
#
# Run it again after pulling a newer version: it re-copies the code and
# restarts the service, and leaves the data folder alone.
#
# A server set up under the old name (service "astrocollab", /opt/astrocollab,
# /var/lib/astrocollab) is carried over the first time this runs: the data
# folder is moved, the old service retired, and nothing is lost.
#
#     STARFRONT_LEGACY_HOST=old.example.org sudo -E bash server/deploy/install.sh collab.starfront.space
#
# keeps an older address answering alongside the new one while telescopes that
# still point at it are updated. Blank once they all have.
set -euo pipefail

HOST="${1:-}"
if [[ -z "$HOST" ]]; then
    echo "usage: sudo bash server/deploy/install.sh <hostname>" >&2
    echo "  e.g.  sudo bash server/deploy/install.sh collab.starfront.space" >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "run this with sudo" >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP=/opt/starfront
DATA=/var/lib/starfront
SERVICE=starfront
LEGACY_HOST="${STARFRONT_LEGACY_HOST-starfront-bray.duckdns.org}"

OLD_APP=/opt/astrocollab
OLD_DATA=/var/lib/astrocollab
OLD_SERVICE=astrocollab

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
CADDY_LIST=/etc/apt/sources.list.d/caddy-stable.list
if ! apt-get update -qq; then
    # A package list that has stopped answering must not stop a deploy.
    # Caddy's list on Cloudsmith began answering "402 Payment Required" in
    # October 2026; Caddy was already installed, and nothing about this
    # deploy needed its list. It is set aside and the update run again;
    # anything else wrong with apt is still fatal.
    if [[ -f "$CADDY_LIST" ]] && command -v caddy >/dev/null; then
        echo "   the Caddy package list is not answering; Caddy is installed, so the list is set aside"
        mv "$CADDY_LIST" "$CADDY_LIST.disabled"
        apt-get update -qq
    else
        exit 1
    fi
fi
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

if [[ -d "$OLD_DATA" && ! -d "$DATA" ]]; then
    echo "== carrying the server over from its old name"
    systemctl stop "$OLD_SERVICE" 2>/dev/null || true
    systemctl disable "$OLD_SERVICE" 2>/dev/null || true
    rm -f "/etc/systemd/system/$OLD_SERVICE.service"
    systemctl daemon-reload
    mv "$OLD_DATA" "$DATA"
    if [[ -f "$DATA/discord.env" ]]; then
        sed -i 's/^ASTROCOLLAB_/STARFRONT_/' "$DATA/discord.env"
    fi
    echo "   data moved to $DATA; the $OLD_SERVICE service is retired"
fi

echo "== user and folders"
id -u "$SERVICE" >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin "$SERVICE"
mkdir -p "$APP" "$DATA" "$DATA/files"
# The files folder may be a mounted volume; its contents are the service's
# already and a season's worth of subs is not worth re-owning on every deploy.
chown "$SERVICE:$SERVICE" "$DATA" "$DATA/files"
find "$DATA" -maxdepth 1 -mindepth 1 ! -name files -exec chown -R "$SERVICE:$SERVICE" {} +
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
if [[ -d "$OLD_APP" ]]; then
    rm -rf "$OLD_APP"
fi

echo "== discord.env"
if [[ ! -f "$DATA/discord.env" ]]; then
    cp "$HERE/server/deploy/discord.env.example" "$DATA/discord.env"
    chown "$SERVICE:$SERVICE" "$DATA/discord.env"
    chmod 600 "$DATA/discord.env"
    echo "   written $DATA/discord.env - fill in the Discord values, then:"
    echo "       sudo systemctl restart $SERVICE"
else
    echo "   kept $DATA/discord.env"
fi
# The public address is the hostname this was installed under, always: it is
# what Discord sends people back to, and a stale one is a sign-in that fails.
if grep -q '^STARFRONT_PUBLIC_URL=' "$DATA/discord.env"; then
    sed -i "s#^STARFRONT_PUBLIC_URL=.*#STARFRONT_PUBLIC_URL=https://$HOST#" "$DATA/discord.env"
else
    echo "STARFRONT_PUBLIC_URL=https://$HOST" >> "$DATA/discord.env"
fi

echo "== service"
install -m 644 "$HERE/server/deploy/starfront.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable --now "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

HOSTS="$HOST"
if [[ -n "$LEGACY_HOST" && "$LEGACY_HOST" != "$HOST" ]]; then
    HOSTS="$HOST, $LEGACY_HOST"
fi
echo "== caddy -> https://$HOST${LEGACY_HOST:+ (and still $LEGACY_HOST)}"
sed "s#COLLAB_HOSTS#$HOSTS#" "$HERE/server/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl enable --now caddy >/dev/null
systemctl reload caddy || systemctl restart caddy

sleep 2
echo
echo "== done"
echo "   health:  $(curl -s http://127.0.0.1:8800/api/v1/health || echo 'not answering yet')"
echo
echo "   Owner token, for scripts and emergencies (people sign in with Discord;"
echo "   owners are the Discord ids in STARFRONT_DISCORD_OWNERS):"
echo
echo "       $(sudo -u "$SERVICE" STARFRONT_DATA=$DATA $APP/venv/bin/python $APP/server/run.py --show-token)"
echo
echo "   Server URL (built into Starfront):  https://$HOST"
echo "   Discord redirect URL:               https://$HOST/auth/discord/callback"
echo "   N.I.N.A. plugin repository:         https://$HOST/nina"
echo
echo "   logs:  journalctl -u $SERVICE -f"
