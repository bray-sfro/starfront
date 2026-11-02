#!/usr/bin/env bash
# Put the collaboration's files on a DigitalOcean block-storage volume.
#
#     sudo bash server/deploy/attach-files-volume.sh <volume name>
#     e.g.  sudo bash server/deploy/attach-files-volume.sh starfront-files
#
# Before running it: in the DigitalOcean panel, Volumes -> Create Volume,
# the size you want, the same region as the droplet, attached to the
# droplet, "Manually Format & Mount". The name you give it is the argument.
#
# What it does: formats the volume if it is blank (never if it already has a
# file system), stops the server, mounts the volume at the files folder,
# carries over anything already in that folder, adds it to /etc/fstab so it
# comes back after a reboot, and starts the server again. Safe to run twice.
#
#     sudo bash server/deploy/attach-files-volume.sh <volume name> --grow
#
# after making the volume bigger in the panel grows the file system to fill
# it, with the server running.
set -euo pipefail

NAME="${1:-}"
MODE="${2:-}"
if [[ -z "$NAME" ]]; then
    echo "usage: sudo bash server/deploy/attach-files-volume.sh <volume name> [--grow]" >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "run this with sudo" >&2
    exit 1
fi

DEV="/dev/disk/by-id/scsi-0DO_Volume_$NAME"
DATA=/var/lib/starfront
FILES=$DATA/files
SERVICE=starfront

if [[ ! -e "$DEV" ]]; then
    echo "no volume called '$NAME' is attached to this droplet ($DEV)." >&2
    echo "attached volumes:" >&2
    ls /dev/disk/by-id/ 2>/dev/null | grep DO_Volume | sed 's/^/   /' >&2 || echo "   (none)" >&2
    exit 1
fi

if [[ "$MODE" == "--grow" ]]; then
    echo "== growing the file system on $DEV"
    resize2fs "$DEV"
    df -h "$FILES"
    exit 0
fi

if mountpoint -q "$FILES"; then
    echo "== $FILES is already on a volume"
    df -h "$FILES"
    exit 0
fi

if ! blkid "$DEV" >/dev/null 2>&1; then
    echo "== formatting $DEV (it has no file system yet)"
    mkfs.ext4 -F -q "$DEV"
else
    echo "== $DEV already has a file system; keeping it"
fi

echo "== stopping the server"
systemctl stop "$SERVICE"

mkdir -p "$FILES"
KEPT=""
if [[ -n "$(ls -A "$FILES" 2>/dev/null)" ]]; then
    KEPT="$FILES.before-volume"
    echo "== keeping what is already in $FILES to copy onto the volume"
    mv "$FILES" "$KEPT"
    mkdir -p "$FILES"
fi

if ! grep -q "$DEV" /etc/fstab; then
    echo "$DEV $FILES ext4 defaults,nofail,discard 0 2" >> /etc/fstab
fi
echo "== mounting"
mount "$FILES"
chown starfront:starfront "$FILES"
chmod 700 "$FILES"

if [[ -n "$KEPT" ]]; then
    echo "== copying the files that were already here"
    rsync -a "$KEPT/" "$FILES/"
    chown -R starfront:starfront "$FILES"
    rm -rf "$KEPT"
fi

echo "== starting the server"
systemctl start "$SERVICE"
sleep 2
echo
df -h "$FILES"
echo
echo "health: $(curl -s http://127.0.0.1:8800/api/v1/health || echo 'not answering yet')"
