#!/usr/bin/env bash
# stack_poller.sh [--once]: run the stacks asked for with the files page's "Start new stack" button.
#
# Every POLL seconds, for each "<project> <command...>" line of stack_projects.conf, read
# <releases>/<project>/STACK_REQUEST.json on the server over ssh. A "queued" request is claimed
# (state claimed -> running -> done|failed, written back over ssh), the progress bar is started with
# jobstatus.py, and the command runs with STARFRONT_JOB_PROJECT / STARFRONT_STACK_REQUEST set.
# One stack at a time per project (flock), and the server refuses a second request while one is active.
# Publishing is the command's business (e.g. a set marked .DONE goes live through watch_sets.sh).
#
# Env for testing: STACK_LOCAL=1 (run "remote" commands here instead of over ssh), STACK_RELDIR,
# STACK_CONF, STACK_LOG, POLL, JOBSTATUS, STARFRONT_COLLAB_URL, STARFRONT_JOB_TOKEN_FILE.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CONF=${STACK_CONF:-$HERE/stack_projects.conf}
LOG=${STACK_LOG:-$HERE/stack_poller.log}
RELDIR=${STACK_RELDIR:-/var/lib/starfront/files/_releases}
POLL=${POLL:-60}
JOBSTATUS=${JOBSTATUS:-/workspace/collab/stack/auto/jobstatus.py}
SSH=(ssh -n -i ~/.ssh/starfront_do -o BatchMode=yes -o ConnectTimeout=15 root@137.184.219.128)
ONCE=""; [ "${1:-}" = --once ] && ONCE=1
log(){ echo "$(date '+%F %T %Z') $*" >>"$LOG"; }
remote(){ if [ -n "${STACK_LOCAL:-}" ]; then bash -c "$1"; else "${SSH[@]}" "$1"; fi; }
# set_state <project> <id> <state> <message>: update the request only if it is still ours (same id)
set_state(){
  local f="$RELDIR/$1/STACK_REQUEST.json"
  remote "python3 -c 'import json,os,sys,time
f,i,st,msg=sys.argv[1:5]
d=json.load(open(f))
if d.get(\"id\")!=i: sys.exit(3)
d.update(state=st,updated=time.time(),message=msg[:300])
if st==\"claimed\": d[\"claimed\"]=time.time()
t=f+\".box.tmp\"; json.dump(d,open(t,\"w\"),indent=1)
try: os.chown(t,*[__import__(\"pwd\").getpwnam(\"starfront\")[k] for k in (2,3)])
except KeyError: pass
os.replace(t,f)' '$f' '$2' '$3' '$(printf %s "$4" | tr -d "'")'"
}
J(){ STARFRONT_JOB_PROJECT=$PID timeout 30 python3 "$JOBSTATUS" "$@" </dev/null >/dev/null 2>>"$LOG.jobstatus" || true; }

exec 8>"$LOG.lock"; flock -n 8 || { log "poller already running"; exit 0; }
log "poller start pid $$ conf $CONF"
while :; do
  while read -r PID CMD; do
    [[ -z "$PID" || "$PID" == \#* ]] && continue
    [[ "$PID" =~ ^[A-Za-z0-9]{6,40}$ ]] || { log "bad project id $PID"; continue; }
    REQ=$(remote "cat '$RELDIR/$PID/STACK_REQUEST.json' 2>/dev/null") || continue
    read -r ST ID < <(printf %s "$REQ" | python3 -c 'import json,sys
try: d=json.load(sys.stdin); print(d.get("state",""), d.get("id",""))
except Exception: print("", "")')
    [ "$ST" = queued ] && [ -n "$ID" ] || continue
    exec 7>"$LOG.$PID.lock"; flock -n 7 || { log "[$PID] a stack is already running here"; continue; }
    set_state "$PID" "$ID" claimed "picked up by the stacking machine" || { log "[$PID] $ID changed under us (cancelled?)"; exec 7>&-; continue; }
    log "[$PID] $ID claimed: $CMD"
    J start --version "" --steps "Starting stack" --title "Stack requested from the page" --message "starting"
    set_state "$PID" "$ID" running "stacking"
    STARFRONT_JOB_PROJECT=$PID STARFRONT_STACK_REQUEST=$ID bash -c "$CMD" 7>&- 8>&- </dev/null >>"$LOG" 2>&1; rc=$?
    if [ $rc = 0 ]; then
      set_state "$PID" "$ID" done "finished"; log "[$PID] $ID done"
      # if the command left the bar at our placeholder step, clear it
      STARFRONT_JOB_PROJECT=$PID python3 "$JOBSTATUS" show 2>/dev/null | grep -q '"Starting stack"' && J idle
    else
      set_state "$PID" "$ID" failed "stack command failed (exit $rc)"; log "[$PID] $ID FAILED exit $rc"
      J fail --message "stack failed (exit $rc)"
    fi
    exec 7>&-
  done < "$CONF"
  [ -n "$ONCE" ] && break; sleep "$POLL" 7>&- 8>&-
done
