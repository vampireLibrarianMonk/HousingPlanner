#!/usr/bin/env bash
set -euo pipefail

# =====================================================
# Configuration
# =====================================================
IDLE_LIMIT=3600        # seconds until shutdown
CHECK_INTERVAL=60      # seconds between checks
NGINX_ACCESS_LOG="/var/log/nginx/access.log"

# =====================================================
# State
# =====================================================
idle_start_ts=""

log() {
    echo "$(date -u +"%Y-%m-%dT%H:%M:%S+00:00") | $*"
}

# =====================================================
# Activity detection (nginx log mtime)
# =====================================================
is_active() {
    [[ ! -f "$NGINX_ACCESS_LOG" ]] && return 1

    local now last_hit
    now=$(date +%s)
    last_hit=$(stat -c %Y "$NGINX_ACCESS_LOG")

    # Active if nginx handled any request in last 5 minutes
    (( now - last_hit < 300 ))
}

# =====================================================
# Main loop
# =====================================================
log "[START] Idle shutdown monitor started"
log "[CONFIG] idle_limit=${IDLE_LIMIT}s interval=${CHECK_INTERVAL}s"

while true; do
    if is_active; then
        idle_start_ts=""
        log "[ACTIVE] Recent nginx traffic detected"
    else
        if [[ -z "$idle_start_ts" ]]; then
            idle_start_ts=$(date +%s)
            log "[IDLE] No recent traffic — idle timer started"
        else
            now=$(date +%s)
            elapsed=$(( now - idle_start_ts ))

            log "[IDLE] ${elapsed}s / ${IDLE_LIMIT}s elapsed"

            if (( elapsed >= IDLE_LIMIT )); then
                log "[SHUTDOWN] Idle limit reached — stopping instance"
                sudo shutdown -h now
                exit 0
            fi
        fi
    fi

    sleep "$CHECK_INTERVAL"
done
