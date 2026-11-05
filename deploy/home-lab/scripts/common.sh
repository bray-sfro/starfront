# Sourced by the lab's scripts: how this Mac reaches the other machines.
# Every connection is non-interactive (BatchMode), so a missing key fails at
# once instead of waiting at a password prompt Terraform cannot answer.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAB_KEY="${LAB_KEY:-$HOME/.ssh/id_ed25519}"
SSH_OPTS=(-i "$LAB_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

say() { printf '%s\n' "$*"; }
die() { printf '%s: %s\n' "$(basename "$0")" "$*" >&2; exit 1; }

# remote TARGET COMMAND...   run a command over SSH
remote() { local target="$1"; shift; ssh "${SSH_OPTS[@]}" "$target" "$@"; }

# copy SRC TARGET:PATH       scp, same options
copy() { scp -q "${SSH_OPTS[@]}" "$@"; }

# rsh                         the -e argument for rsync
rsh() { printf 'ssh'; printf ' %q' "${SSH_OPTS[@]}"; }
