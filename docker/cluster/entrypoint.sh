#!/usr/bin/env bash
# Builds the supervisor config from SF_CHROME_CHANNELS and starts everything:
#   per channel: Xvfb -> openbox -> x11vnc -> Chrome; then the cluster API and heal watcher.
#
# Every instance gets a screen of its own: instance N runs on display :(99+N) and
# its x11vnc listens on 5900+N. Several providers look alike once they are open
# (four Gemini windows, two DeepSeek), and on one shared screen there is no way to
# tell which window belongs to which account while signing in.
set -euo pipefail

KNOWN="chatgpt deepseek qwen gemini zai grok deepai genspark chatbotapp chatbotchatapp venice"
CHANNELS="${SF_CHROME_CHANNELS:-}"
CHANNELS="${CHANNELS//,/ }"

if [ -z "${SF_CLUSTER_API_KEY:-}" ]; then
    echo "[cluster] SF_CLUSTER_API_KEY is empty; refusing to expose the cluster API without a key" >&2
    exit 1
fi
if [ -z "${SF_VNC_PASSWORD:-}" ]; then
    echo "[cluster] SF_VNC_PASSWORD is empty; set it in .env (used for logging in to providers via VNC)" >&2
    exit 1
fi

mkdir -p /data/state /data/profiles /data/backups /data/attachments /data/.vnc /run/gateway-heal
chown -R gateway:gateway /data /run/gateway-heal
chmod 700 /data/.vnc

# VNC password file (VNC protocol only uses the first 8 characters)
su -s /bin/sh gateway -c 'x11vnc -storepasswd "$SF_VNC_PASSWORD" /data/.vnc/passwd' >/dev/null 2>&1

CONF=/etc/supervisor/gateway.conf
cat > "$CONF" <<'EOF'
[supervisord]
nodaemon=true
user=root
logfile=/dev/null
logfile_maxbytes=0
pidfile=/run/supervisord.pid

[unix_http_server]
file=/run/supervisor.sock
chmod=0700

[supervisorctl]
serverurl=unix:///run/supervisor.sock

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[program:heal-watch]
command=/opt/gateway/bin/heal-watch.sh
priority=40
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
redirect_stderr=true
EOF

ports=()
gemini_instances=()
idx=0
for ch in $CHANNELS; do
    ch="$(echo "$ch" | tr '[:upper:]' '[:lower:]')"
    case " $KNOWN " in
        *" $ch "*) ;;
        *) echo "[cluster] unknown channel '$ch' (known: $KNOWN)" >&2; exit 1 ;;
    esac
    # The cap exists because each instance needs a published VNC port
    # (5900+N in docker-compose). Raise both together, never just one.
    if [ "$idx" -ge "${SF_MAX_INSTANCES:-10}" ]; then
        echo "[cluster] at most ${SF_MAX_INSTANCES:-10} Chrome instances are supported" >&2; exit 1
    fi
    ports+=("$((9222 + idx))")
    [ "$ch" = "gemini" ] && gemini_instances+=("$idx")
    disp=":$((99 + idx))"
    rfb="$((5900 + idx))"
    cat >> "$CONF" <<EOF

[program:xvfb-$idx]
command=/usr/local/bin/python -m cluster.display $disp -screen 0 1920x1080x24 -ac +extension GLX +render -noreset
directory=/opt/gateway
user=gateway
priority=10
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
redirect_stderr=true

[program:openbox-$idx]
command=/usr/bin/openbox
user=gateway
environment=HOME="/home/gateway",DISPLAY="$disp"
priority=20
autorestart=true
startsecs=2
stdout_logfile=/dev/null
redirect_stderr=true

[program:x11vnc-$idx]
command=/usr/bin/x11vnc -display $disp -rfbauth /data/.vnc/passwd -listen 0.0.0.0 -rfbport $rfb -forever -shared -noxdamage -repeat -quiet
user=gateway
priority=30
autorestart=true
startsecs=3
stdout_logfile=/dev/null
redirect_stderr=true

[program:chrome-$idx]
command=/opt/gateway/bin/launch-chrome.sh $idx $ch
user=gateway
environment=HOME="/home/gateway",DISPLAY="$disp"
priority=50
autorestart=true
startsecs=5
stopasgroup=true
killasgroup=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
redirect_stderr=true
EOF
    idx=$((idx + 1))
done

if [ "${#ports[@]}" -eq 0 ]; then
    echo "[cluster] SF_CHROME_CHANNELS is empty; starting one display and VNC only" >&2
    cat >> "$CONF" <<'EOF'

[program:xvfb-0]
command=/usr/local/bin/python -m cluster.display :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset
directory=/opt/gateway
user=gateway
priority=10
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
redirect_stderr=true

[program:openbox-0]
command=/usr/bin/openbox
user=gateway
environment=HOME="/home/gateway",DISPLAY=":99"
priority=20
autorestart=true
startsecs=2
stdout_logfile=/dev/null
redirect_stderr=true

[program:x11vnc-0]
command=/usr/bin/x11vnc -display :99 -rfbauth /data/.vnc/passwd -listen 0.0.0.0 -rfbport 5900 -forever -shared -noxdamage -repeat -quiet
user=gateway
priority=30
autorestart=true
startsecs=3
stdout_logfile=/dev/null
redirect_stderr=true
EOF
fi

CDP_PORTS="$(IFS=,; echo "${ports[*]:-}")"
export SF_CLUSTER_CDP_PORTS="${SF_CLUSTER_CDP_PORTS:-$CDP_PORTS}"
# The memory guard may restart only Gemini instances (their login survives a restart)
export SF_CLUSTER_MEM_GUARD_RESTART="${SF_CLUSTER_MEM_GUARD_RESTART:-$(IFS=,; echo "${gemini_instances[*]:-}")}"

cat >> "$CONF" <<'EOF'

[program:cluster]
command=/usr/local/bin/python -m cluster.server
directory=/opt/gateway
user=gateway
environment=HOME="/home/gateway"
priority=60
autorestart=true
startsecs=3
stopwaitsecs=20
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
redirect_stderr=true
EOF

echo "[cluster] channels: ${CHANNELS:-none}; CDP ports: ${SF_CLUSTER_CDP_PORTS:-none}"
exec /usr/bin/supervisord -c "$CONF"
