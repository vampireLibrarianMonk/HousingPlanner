#!/usr/bin/env bash
set -euo pipefail

# ============================================
# Configuration
# ============================================
NGINX_ACCESS_LOG="/var/log/nginx/access.log"
STATE_DIR="/var/lib/idle-shutdown"
STATE_FILE="${STATE_DIR}/first_idle_ts"

IDLE_THRESHOLD_SECONDS=3600   # 1 hour
LOG_SCAN_LINES=500

# ============================================
# Helpers
# ============================================
log() {
  echo "$(date -Iseconds) | $1"
}

mkdir -p "$STATE_DIR"

# ============================================
# Detect real (human) traffic
# ============================================
is_active() {
  [[ ! -f "$NGINX_ACCESS_LOG" ]] && return 1

  local now ts_raw ts_epoch line
  now=$(date +%s)

  tail -n "$LOG_SCAN_LINES" "$NGINX_ACCESS_LOG" | while IFS= read -r line; do
    # Ignore infrastructure noise
    if [[ "$line" == *"ELB-HealthChecker"* ]] || [[ "$line" == *"CloudFront"* ]]; then
      continue
    fi

    # Extract nginx timestamp: [17/Jan/2026:03:12:34 +0000]
    ts_raw=$(awk '{print $4" "$5}' <<<"$line" | tr -d '[]')
    ts_epoch=$(date -d "$ts_raw" +%s 2>/dev/null || true)
    [[ -z "$ts_epoch" ]] && continue

    if (( now - ts_epoch < IDLE_THRESHOLD_SECONDS )); then
      exit 0
    fi
  done

  return 1
}

# ============================================
# Main logic
# ============================================
NOW=$(date +%s)

if is_active; then
  if [[ -f "$STATE_FILE" ]]; then
    log "[ACTIVE] Traffic detected — clearing idle timer"
    rm -f "$STATE_FILE"
  else
    log "[ACTIVE] Traffic detected"
  fi
  exit 0
fi

# No real traffic
if [[ ! -f "$STATE_FILE" ]]; then
  echo "$NOW" > "$STATE_FILE"
  log "[IDLE] First idle detection — starting 1h timer"
  exit 0
fi

FIRST_IDLE=$(cat "$STATE_FILE")
IDLE_DURATION=$(( NOW - FIRST_IDLE ))

if (( IDLE_DURATION >= IDLE_THRESHOLD_SECONDS )); then
  log "[IDLE] Idle for ${IDLE_DURATION}s (>=1h) — shutting down"
  /sbin/shutdown -h now
else
  REMAINING=$(( IDLE_THRESHOLD_SECONDS - IDLE_DURATION ))
  log "[IDLE] Idle ${IDLE_DURATION}s — shutdown in ${REMAINING}s if no activity"
fi

exit 0
