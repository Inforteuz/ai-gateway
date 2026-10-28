#!/usr/bin/env bash
# Recovers a Chrome instance and failed display dependencies when requested.
# The cluster (unprivileged) writes /run/gateway-heal/<N>.req; deleting the file
# acknowledges the request. Only configured numeric instance IDs are accepted.
set -u
DIR="${SF_CLUSTER_HEAL_REQ_DIR:-/run/gateway-heal}"
MAX_INSTANCES="${SF_MAX_INSTANCES:-10}"
if ! [[ "$MAX_INSTANCES" =~ ^[1-9][0-9]*$ ]] || [ "${#MAX_INSTANCES}" -gt 4 ]; then
    echo "[heal] invalid SF_MAX_INSTANCES" >&2
    exit 1
fi

ctl() { supervisorctl -c /etc/supervisor/gateway.conf "$@"; }
running() { [ "$(ctl status "$1" | awk '{print $2}')" = RUNNING ]; }

recover() {
    local n="$1"
    if ! running "xvfb-$n"; then
        # Chrome cannot recover while its X display is dead. Restart dependents
        # against the repaired display, leaving other instances untouched.
        ctl stop "chrome-$n" "openbox-$n" "x11vnc-$n" || true
        ctl start "xvfb-$n" || return 1
    fi
    local service
    for service in openbox x11vnc; do
        if ! running "$service-$n"; then
            ctl start "$service-$n" || return 1
        fi
    done
    ctl restart "chrome-$n"
}

shopt -s nullglob
while true; do
    for f in "$DIR"/*.req; do
        n="$(basename "$f" .req)"
        rm -f "$f"
        if ! [[ "$n" =~ ^(0|[1-9][0-9]*)$ ]] || [ "${#n}" -gt 4 ] || [ "$n" -ge "$MAX_INSTANCES" ]; then
            echo "[heal] ignoring invalid request: $n"
            continue
        fi
        echo "[heal] recovering instance $n"
        recover "$n" || echo "[heal] instance $n recovery failed" >&2
    done
    [ "${1:-}" = --once ] && break
    sleep 1
done
