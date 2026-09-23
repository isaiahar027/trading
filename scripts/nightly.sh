#!/usr/bin/env bash
# Nightly self-improvement loop. Schedule at 23:30 UTC (see trader-nightly.timer).
#  1. refresh research snapshot (regime + screen) from primary sources
#  2. review the day: fills, misses, Brier, calibration refit, optional brain rewrite proposal
#  3. calibration-only refits auto-promote if AUTO_PROMOTE_CALIBRATION=1; wording changes always wait for an operator
set -euo pipefail
cd "$(dirname "$0")/.."
DAY="${1:-$(date -u +%F)}"
trader research --top 15 || echo "research refresh failed; continuing with review" >&2
FLAGS=()
[[ "${AUTO_PROMOTE_CALIBRATION:-0}" == "1" ]] && FLAGS+=(--auto-promote-calibration)
trader review --day "$DAY" "${FLAGS[@]}"
ls schemas/pending/ 2>/dev/null && echo "pending schema proposals above need: trader schema-approve <file>"
# The running loop loads schemas at start; restart it so approved versions go live before the next session.
if systemctl --user is-active --quiet trader.service 2>/dev/null; then
  systemctl --user restart trader.service
fi
