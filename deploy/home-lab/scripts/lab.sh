#!/usr/bin/env bash
# Day-to-day lab commands, run from this Mac once `terraform apply` is done.
#
#   lab.sh status              the hub's status: workers, catalogue, Starfront link
#   lab.sh night               the "Lab night" collaboration: ids, project page; renews its grant
#   lab.sh smoke relay|hub     synthetic frames through the server's front door, or straight to the hub
#   lab.sh upload FOLDER       real frames, as the Lab scope telescope, through the front door
#   lab.sh logs server|hub|WORKER_ID   follow a service's log
#
# It reads the server's address and SSH target from `terraform output`.
set -euo pipefail
. "$(dirname "$0")/common.sh"
lab_dir="$(cd "$here/.." && pwd)"
starfront="$(cd "$lab_dir/../.." && pwd)"
out() { terraform -chdir="$lab_dir" output -raw "$1"; }
server_url=$(out server_url) server_ssh=$(out server_ssh)
mac_bin="$lab_dir/bin/macos-arm64"
py="${PYTHON:-python3}"

case "${1:-}" in
  status)
    remote "$server_ssh" 'COLLAB_HUB_ADMIN_TOKEN=$(cat starfront-lab/hub/admin-token) starfront-lab/bin/collab-hub status --config starfront-lab/hub/collab-hub.toml' ;;
  night)
    remote "$server_ssh" "starfront-lab/venv/bin/python starfront-lab/src/tools/lab_setup.py refresh-grant --server http://127.0.0.1:${server_url##*:} --state starfront-lab/lab-night.json" \
      | head -1 | "$py" -c 'import json,sys; d=json.load(sys.stdin); [print(f"{k:16} {v}") for k,v in d.items()]'
    slug=$(remote "$server_ssh" "sed -n 's/.*\"slug\": \"\\(.*\\)\".*/\\1/p' starfront-lab/lab-night.json")
    say "project page     $server_url/stack/p/$slug"
    say "N.I.N.A. ServerUrl / desktop collab.serverUrl: $server_url"
    say "N.I.N.A. on any PC (close it first):  irm $server_url/_local/install-nina-plugin.ps1 | iex" ;;
  smoke)
    via="${2:-relay}"
    "$py" "$starfront/tools/lab_setup.py" smoke --server "$server_url" --via "$via" \
      --uploader "$mac_bin/collab-uploader" --worker "$mac_bin/collab-worker" ;;
  upload)
    folder="${2:?lab.sh upload FOLDER}"
    state=$(remote "$server_ssh" "starfront-lab/venv/bin/python starfront-lab/src/tools/lab_setup.py refresh-grant --server http://127.0.0.1:${server_url##*:} --state starfront-lab/lab-night.json --print-grant")
    grant=$(printf '%s\n' "$state" | tail -1)
    pid=$(printf '%s\n' "$state" | head -1 | "$py" -c 'import json,sys; print(json.load(sys.stdin)["stackingProject"])')
    conf="$HOME/.config/starfront-lab-uploader"
    up=("$mac_bin/collab-uploader" --api-base "$server_url/stack/api" --config-dir "$conf")
    first=$(find "$folder" -maxdepth 2 -iname '*.fit*' | head -1)
    [ -n "$first" ] || die "no FITS files in $folder"
    COLLAB_TOKEN="$grant" "${up[@]}" projects
    COLLAB_TOKEN="$grant" "${up[@]}" rig confirm --from "$first" --yes
    COLLAB_TOKEN="$grant" "${up[@]}" upload "$folder" --project "$pid" --include-calibration --now ;;
  logs)
    case "${2:-}" in
      server) remote "$server_ssh" 'journalctl --user -u starfront-lab -f -n 50' ;;
      hub) remote "$server_ssh" 'journalctl --user -u collab-hub -f -n 50' ;;
      "") die "lab.sh logs server|hub|WORKER_ID" ;;
      *) say "a worker's log is worker.log in its config folder; on Linux: starfront-lab/worker/, on this Mac: ~/starfront-lab-worker/worker/, on Windows: %USERPROFILE%\\starfront-lab\\worker\\" ;;
    esac ;;
  *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
