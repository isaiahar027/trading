#!/usr/bin/env bash
# Cron every minute: if the loop's heartbeat is older than 5 minutes, trip the kill switch.
# A silent loop is treated as a dead loop. Positions keep their venue-side stops.
cd "$(dirname "$0")/.."
HB=state/HEARTBEAT
[[ -f $HB ]] || exit 0
age=$(( $(date +%s) - $(cat $HB) / 1000 ))
if (( age > 300 )); then
  trader kill "watchdog: heartbeat ${age}s old"
fi
