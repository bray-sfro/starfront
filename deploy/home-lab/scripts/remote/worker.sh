# Runs on a macOS or Linux worker machine (locally on this Mac, piped over SSH
# to the server host) after the binary is in LAB/bin/collab-worker.
#   bash worker.sh ID HUB_URL LAB CODE|- MODE SCRATCH_GB XPSD PAUSE_NINA
# CODE "-" means already enrolled: the token is kept, only settings change.
set -euo pipefail
id="$1" hub="$2" lab="$3" code="$4" mode="$5" scratch_gb="$6" xpsd="$7" pause_nina="$8"
lab="${lab/#\$HOME/$HOME}"
bin="$lab/bin/collab-worker" dir="$lab/worker"

mkdir -p "$lab/scratch" "$lab/no-xpsd"
if [ "$code" != - ]; then
  # enrol checks the configuration it writes. The catalogue settings are given
  # in the environment so it checks what this worker will run with (a hub with
  # no catalogue, in synthetic mode, hands out no xpsd_dir); the file gets
  # them below.
  if [ "$mode" = synthetic ]; then
    COLLAB_LOCAL_CATALOG=synthetic COLLAB_XPSD_DIR="$lab/no-xpsd" \
      "$bin" enrol "$hub" --code "$code" --worker-id "$id" --dir "$dir"
  else
    COLLAB_XPSD_DIR="$xpsd" "$bin" enrol "$hub" --code "$code" --worker-id "$id" --dir "$dir"
  fi
fi

# The lab's settings in collab-worker.toml (lower-case names without COLLAB_).
# Synthetic mode: the synthetic sky, and an empty catalogue folder only so the
# configuration check has one. Real mode: the folder this OS reaches the
# catalogue at (the hub also hands it out at enrolment, but not to a worker
# enrolled while the hub had none).
if [ "$mode" = synthetic ]; then
  catalogue="local_catalog=synthetic" xpsd_dir="$lab/no-xpsd"
else
  catalogue="local_catalog=" xpsd_dir="$xpsd"
fi
pause=""
[ "$pause_nina" = 1 ] && pause="pause_while_nina=true"
python3 - "$dir/collab-worker.toml" "scratch_dir=$lab/scratch" "scratch_cap_gb=$scratch_gb" \
  "$catalogue" "xpsd_dir=$xpsd_dir" $pause <<'PY'
import json, re, sys
path, pairs = sys.argv[1], [a.split("=", 1) for a in sys.argv[2:]]
def value(v):
    return v if re.fullmatch(r"-?[0-9]+(\.[0-9]+)?|true|false", v) else json.dumps(v)
names = {k for k, _ in pairs}
kept = [l for l in open(path, encoding="utf-8").read().splitlines() if l.split("=", 1)[0].strip() not in names]
first = next((i for i, l in enumerate(kept) if l.lstrip().startswith("[")), len(kept))
added = [f"{k} = {value(v)}" for k, v in pairs if v != ""]   # an empty value removes the key
open(path, "w", encoding="utf-8").write("\n".join(kept[:first] + added + kept[first:]) + "\n")
PY

"$bin" service uninstall --dir "$dir" >/dev/null 2>&1 || true
"$bin" service install --dir "$dir"
echo "worker $id: installed ($mode catalogue), hub $hub"
