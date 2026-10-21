#!/usr/bin/env bash
# Restarts a Chrome instance when the cluster asks for it.
# The cluster (unprivileged) writes /run/gateway-heal/<N>.req; deleting the file
# acknowledges the request. Only a single digit is accepted, nothing else can be run.
set -u
DIR="${SF_CLUSTER_HEAL_REQ_DIR:-/run/gateway-heal}"
shopt -s nullglob
while true; do
    for f in "$DIR"/*.req; do
        n="$(basename "$f" .req)"
        rm -f "$f"
        case "$n" in
            [0-9]) ;;
            *) echo "[heal] ignoring invalid request: $n"; continue ;;
        esac
        echo "[heal] restarting chrome-$n"
        supervisorctl -c /etc/supervisor/gateway.conf restart "chrome-$n" || true
    done
    sleep 1
done
