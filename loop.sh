#!/usr/bin/env bash
# Non-stop research loop (systemd user service snowball-loop, Restart=always, MemoryMax=3G).
# One topic per iteration, then a 5 min pause. `touch $ROOT/STOP` pauses it
# (checked before each iteration); `rm` it to resume. run.sh holds run.lock so runs never overlap.
ROOT="$(cd "$(dirname "$0")" && pwd)"
PAUSE=${RW_PAUSE_SEC:-300}
cd "$ROOT" || exit 1
export RW_SCOPED=1   # the service already caps memory; run.sh must not nest another scope
while true; do
  if [[ -e "$ROOT/STOP" ]]; then
    echo "$(date '+%F %T') STOP file present, paused"; sleep 60; continue
  fi
  "$ROOT/run.sh" --max-topics 1
  rc=$?
  # Nothing to run, or the daily cap is reached: wait longer before asking again.
  if tail -3 "$(ls -t "$ROOT"/logs/run-*.log 2>/dev/null | head -1)" 2>/dev/null | grep -qE "nothing to run|daily cap"; then
    sleep 1800
  else
    sleep "$PAUSE"
  fi
done
