#!/bin/bash
set -euo pipefail

LOG_FILE="/var/log/idle-shutdown.log"
IDLE_LIMIT_SECONDS=$((60 * 60))   # 1 hour
CHECK_INTERVAL=300                # 5 minutes

LAST_ACTIVITY_TS=$(date +%s)

log() {
  echo "$(date -Is) | $1" | tee -a "$LOG_FILE"
}

get_network_bytes() {
  awk '/eth0|ens5/ {rx+=$2; tx+=$10} END {print rx+tx}' /proc/net/dev
}

PREV_NET_BYTES=$(get_network_bytes)

log "[START] Idle shutdown monitor started"
log "[CONFIG] idle_limit=${IDLE_LIMIT_SECONDS}s interval=${CHECK_INTERVAL}s"

while true; do
  sleep "$CHECK_INTERVAL"

  NOW=$(date +%s)
  CUR_NET_BYTES=$(get_network_bytes)
  NET_DELTA=$((CUR_NET_BYTES - PREV_NET_BYTES))
  PREV_NET_BYTES=$CUR_NET_BYTES

  SSH_SESSIONS=$(who | wc -l)

  if [[ "$NET_DELTA" -gt 1024 ]]; then
    LAST_ACTIVITY_TS=$NOW
    log "[ACTIVITY] Network traffic detected (${NET_DELTA} bytes). Timer reset."
  elif [[ "$SSH_SESSIONS" -gt 0 ]]; then
    LAST_ACTIVITY_TS=$NOW
    log "[ACTIVITY] SSH session active (${SSH_SESSIONS}). Timer reset."
  else
    IDLE_FOR=$((NOW - LAST_ACTIVITY_TS))
    REMAINING=$((IDLE_LIMIT_SECONDS - IDLE_FOR))

    if [[ "$REMAINING" -le 0 ]]; then
      log "[SHUTDOWN] Idle limit reached. Powering off."
      shutdown -h now
    else
      log "[IDLE] No activity. ${REMAINING}s remaining until shutdown."
    fi
  fi
done
