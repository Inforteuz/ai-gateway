#!/usr/bin/env bash
# Starts Chrome instance N for one web-chat channel.
#   launch-chrome.sh <index 0-9> <channel>
# CDP port = 9222 + N (the cluster relies on this rule), profile = /data/profiles/profile_N.
# Optional per-instance upstream proxy: line N+1 of /data/proxies.txt
# ("host:port" or "host:port:user:password"), applied through a local tinyproxy.
set -euo pipefail
IDX="${1:?instance index required}"
CHANNEL="${2:?channel required}"

case "$CHANNEL" in
    chatgpt)        START_URL='https://chatgpt.com/' ;;
    deepseek)       START_URL='https://chat.deepseek.com/' ;;
    qwen)           START_URL='https://chat.qwen.ai/' ;;
    gemini)         START_URL='https://gemini.google.com/app' ;;
    zai)            START_URL='https://chat.z.ai/' ;;
    grok)           START_URL='https://grok.com/' ;;
    deepai)         START_URL='https://deepai.org/chat' ;;
    genspark)       START_URL='https://www.genspark.ai/' ;;
    chatbotapp)     START_URL='https://chatbotappai.ai/' ;;
    chatbotchatapp) START_URL='https://chatbotchatapp.com/' ;;
    *)              START_URL='about:blank' ;;
esac

PROFILE_DIR="${SF_CLUSTER_PROFILE_DIR:-/data/profiles}/profile_$IDX"
mkdir -p "$PROFILE_DIR"
# Supervisor guarantees a single process per profile; a lock left by a previous
# container (different hostname) would otherwise block the start.
rm -f "$PROFILE_DIR"/Singleton{Lock,Socket,Cookie}

CHROME="$(command -v google-chrome || command -v chromium || true)"
[ -n "$CHROME" ] || { echo "[chrome-$IDX] no Chrome/Chromium binary found" >&2; exit 1; }

PROXY_ARGS=()
PROXY_FILE="${SF_CLUSTER_PROXIES_FILE:-/data/proxies.txt}"
TP_PID=""
if [ -s "$PROXY_FILE" ]; then
    LINE="$(grep -vE '^(#|$)' "$PROXY_FILE" | awk -v n=$((IDX + 1)) 'NR==n' || true)"
    if [ -n "$LINE" ] && [ "$LINE" != "direct" ]; then
        IFS=':' read -ra P <<< "$LINE"
        if [ ${#P[@]} -eq 4 ]; then UP="http ${P[2]}:${P[3]}@${P[0]}:${P[1]}"
        elif [ ${#P[@]} -eq 2 ]; then UP="http ${P[0]}:${P[1]}"
        else UP=""; fi
        if [ -n "$UP" ]; then
            LOCAL_PORT=$((16222 + IDX))
            CONF="/tmp/tinyproxy_$IDX.conf"
            umask 077
            cat > "$CONF" <<EOF
Port $LOCAL_PORT
Listen 127.0.0.1
Timeout 600
LogLevel Critical
DisableViaHeader Yes
upstream $UP
EOF
            /usr/bin/tinyproxy -d -c "$CONF" &
            TP_PID=$!
            PROXY_ARGS+=("--proxy-server=http://127.0.0.1:$LOCAL_PORT")
            echo "[chrome-$IDX] via proxy ${P[0]}:${P[1]}"
        fi
    fi
fi
[ ${#PROXY_ARGS[@]} -eq 0 ] && echo "[chrome-$IDX] direct connection"

# 2x2 grid of 960x540 windows on the 1920x1080 display
POS_X=$(( (IDX % 2) * 960 ))
POS_Y=$(( ((IDX / 2) % 2) * 540 ))

# Memory-saving flags: fewer renderer processes, no site isolation (only trusted
# provider sites are opened), small disk cache, software GL.
# Chrome stderr (dbus noise in containers) is dropped; set SF_CHROME_STDERR=/dev/stderr to debug.
"$CHROME" \
    --no-sandbox \
    --remote-debugging-port=$((9222 + IDX)) \
    --remote-debugging-address=127.0.0.1 \
    --user-data-dir="$PROFILE_DIR" \
    --no-first-run --no-default-browser-check \
    --disable-sync --disable-signin-promo \
    --disable-background-timer-throttling \
    --disable-backgrounding-occluded-windows \
    --disable-renderer-backgrounding \
    --disable-dev-shm-usage \
    --password-store=basic \
    --disable-blink-features=AutomationControlled \
    --process-per-site \
    --renderer-process-limit=2 \
    --disable-features=site-per-process,IsolateOrigins,TranslateUI,ChromeWhatsNewUI,SigninPromoTrigger,SyncConsent,PasswordManagerOnboarding \
    --disable-gpu-shader-disk-cache \
    --disk-cache-size=33554432 \
    --use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader \
    --lang="${SF_CHROME_LANG:-en-US}" \
    --window-size=960,540 \
    --window-position=$POS_X,$POS_Y \
    "${PROXY_ARGS[@]}" \
    "$START_URL" 2>"${SF_CHROME_STDERR:-/dev/null}" &
CHROME_PID=$!

cleanup() {
    kill "$CHROME_PID" 2>/dev/null || true
    [ -n "$TP_PID" ] && kill "$TP_PID" 2>/dev/null || true
}
trap cleanup TERM INT EXIT
wait "$CHROME_PID"
