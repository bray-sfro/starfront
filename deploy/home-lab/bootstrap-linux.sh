#!/usr/bin/env bash
# One-time root set-up of the lab's server host (Omarchy / Arch Linux). Run it
# AT that machine; Terraform cannot, because until this runs nothing on it
# accepts a connection, and every step here needs root.
#
#   sudo bash bootstrap-linux.sh [--subnet 192.168.1.0/24] \
#        [--nas DXP8800PLUS-76B2.lan --share XPSD --nas-user USER] [--no-nas]
#
# It:
#   1. installs openssh, rsync, python, curl and cifs-utils, and starts sshd;
#   2. opens TCP 22 (SSH, for Terraform), 8800 (Starfront server) and 8790
#      (collab-hub) to the home subnet and its IPv6 prefix only, in ufw (Omarchy
#      denies all incoming by default); nothing is forwarded on the router, the
#      hub only calls out;
#   3. lets your user's systemd services run while you are logged out (linger);
#   4. mounts the NAS's XPSD share read-only at /mnt/xpsd on first use (fstab,
#      x-systemd.automount), with its password in a root-only file.
#
# Then, from the Mac:  ssh-copy-id <you>@omarchy.lan   and   nc -z omarchy.lan 22
# (8800 and 8790 then answer "connection refused" until Terraform starts them).
set -euo pipefail
subnet="192.168.1.0/24" nas="DXP8800PLUS-76B2.lan" share="XPSD" nas_user="" with_nas=1
while [ $# -gt 0 ]; do
  case "$1" in
    --subnet) subnet="$2"; shift 2 ;;
    --nas) nas="$2"; shift 2 ;;
    --share) share="$2"; shift 2 ;;
    --nas-user) nas_user="$2"; shift 2 ;;
    --no-nas) with_nas=0; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run it with sudo" >&2; exit 1; }
user="${SUDO_USER:-}"
[ -n "$user" ] && [ "$user" != root ] || { echo "run it with sudo from your own account, not as root" >&2; exit 1; }

echo "== packages and sshd"
pacman -S --needed --noconfirm openssh rsync python curl cifs-utils
systemctl enable --now sshd

echo "== firewall: 22, 8800, 8790 from $subnet and the LAN's IPv6 prefix only"
if command -v ufw >/dev/null; then
  for port in 22 8800 8790; do
    ufw allow from "$subnet" to any port "$port" proto tcp comment "starfront lab"
  done
  # The LAN's IPv6 prefixes too. The router's DNS answers omarchy.lan with IPv6
  # addresses as well, and most clients try those first; ufw drops what it does
  # not allow, so each connection would wait out an IPv6 timeout before falling
  # back to IPv4. These are the /64s the router advertises on this machine's
  # links; run the script again if the ISP changes the prefix.
  for prefix in $(ip -6 route show proto ra | awk '$1 ~ /::\/64$/ {print $1}' | sort -u); do
    for port in 22 8800 8790; do
      ufw allow from "$prefix" to any port "$port" proto tcp comment "starfront lab"
    done
  done
  ufw status numbered
else
  echo "ufw is not installed; if another firewall is active, open TCP 22, 8800 and 8790 from $subnet yourself"
fi

echo "== linger for $user (user services run while logged out)"
loginctl enable-linger "$user"

if [ "$with_nas" = 1 ]; then
  echo "== the catalogue share //$nas/$share at /mnt/xpsd (read-only)"
  getent hosts "$nas" >/dev/null || { echo "$nas does not resolve; check the NAS name (--nas), or pass --no-nas" >&2; exit 1; }
  [ -n "$nas_user" ] || read -r -p "NAS user that can read $share: " nas_user
  read -r -s -p "password for $nas_user on $nas: " nas_pass; echo
  install -d -m 700 /etc/starfront-lab
  umask 077
  printf 'username=%s\npassword=%s\n' "$nas_user" "$nas_pass" > /etc/starfront-lab/nas-credentials
  unset nas_pass
  mkdir -p /mnt/xpsd
  uid=$(id -u "$user") gid=$(id -g "$user")
  line="//$nas/$share /mnt/xpsd cifs ro,credentials=/etc/starfront-lab/nas-credentials,uid=$uid,gid=$gid,iocharset=utf8,vers=3.0,_netdev,nofail,x-systemd.automount 0 0"
  grep -v ' /mnt/xpsd cifs ' /etc/fstab > /etc/fstab.lab-tmp || true
  printf '%s\n' "$line" >> /etc/fstab.lab-tmp
  cat /etc/fstab.lab-tmp > /etc/fstab && rm -f /etc/fstab.lab-tmp
  systemctl daemon-reload
  systemctl restart remote-fs.target || true
  if ls /mnt/xpsd/gdr3-*.xpsd >/dev/null 2>&1; then
    echo "   $(ls /mnt/xpsd/gdr3-*.xpsd | wc -l) gdr3-*.xpsd files visible (16 expected)"
  else
    echo "   /mnt/xpsd shows no gdr3-*.xpsd yet: check the share name, user and password (sudo mount /mnt/xpsd)"
  fi
fi

echo "== done. From the Mac: ssh-copy-id $user@$(hostname).lan"
