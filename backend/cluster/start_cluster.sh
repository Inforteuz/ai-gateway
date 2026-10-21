#!/usr/bin/env bash
# 4 ta Chromium instansiyasini CDP ochiq holda ishga tushiradi.
# Har bir instansiya alohida profil papkasiga ega: foydalanuvchi bir marta tizimga kiradi va keyingi ishga tushishlarda sessiyalar saqlanadi.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILES_DIR="${PROFILES_DIR:-$BASE_DIR/../data/browser_profiles}"
mkdir -p "$PROFILES_DIR"

CHROME_BIN="${CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
if [[ ! -x "$CHROME_BIN" ]]; then
    for cand in "/Applications/Chromium.app/Contents/MacOS/Chromium" "google-chrome" "chromium"; do
        if command -v "$cand" >/dev/null 2>&1; then CHROME_BIN="$cand"; break; fi
    done
fi

PORTS=(9222 9223 9224 9225)
WINDOW_WIDTH=800
WINDOW_HEIGHT=600

for i in "${!PORTS[@]}"; do
    port=${PORTS[$i]}
    profile="$PROFILES_DIR/profile_$i"
    offset_x=$((i * 60))
    offset_y=$((i * 60))

    "$CHROME_BIN" \
        --remote-debugging-port=$port \
        --user-data-dir="$profile" \
        --window-size=${WINDOW_WIDTH},${WINDOW_HEIGHT} \
        --window-position=${offset_x},${offset_y} \
        --no-first-run --no-default-browser-check \
        >/dev/null 2>&1 &

    echo "[$i] Chromium port=$port profile=$profile pid=$!"
    sleep 0.5
done

echo
echo "Barcha 4 ta brauzer ishga tushdi."
echo "Har bir oynada kerakli AI saytlarini oching (chatgpt.com, chat.qwen.ai, grok.com, …) va tizimga kiring."
echo "So'ngra: python -m cluster.server"
