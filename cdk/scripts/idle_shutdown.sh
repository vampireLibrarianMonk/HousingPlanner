#!/usr/bin/env bash
set -euo pipefail

PORT=8501
IDLE_LIMIT_MINUTES=60
idle_minutes=0

while true; do
    ACTIVE=$(ss -tn state established "( sport = :$PORT )" || true)

    if [[ -z "$ACTIVE" ]]; then
        idle_minutes=$((idle_minutes + 1))
    else
        idle_minutes=0
    fi

    if [[ "$idle_minutes" -ge "$IDLE_LIMIT_MINUTES" ]]; then
        logger "Idle limit reached — shutting down"
        shutdown -h now
    fi

    sleep 60
done
