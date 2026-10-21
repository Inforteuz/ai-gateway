#!/usr/bin/env bash
# Exit 0 if Chrome's sandbox can start in this container, 1 otherwise.
# Used by launch-chrome.sh and by install.sh (docker compose run ... sandbox-probe.sh).
CHROME="$(command -v google-chrome || command -v chromium || true)"
[ -n "$CHROME" ] || { echo "no Chrome/Chromium binary" >&2; exit 1; }
dir="$(mktemp -d)"
trap 'rm -rf "$dir"' EXIT
if timeout 30 "$CHROME" --headless=new --no-first-run --user-data-dir="$dir" --dump-dom about:blank >/dev/null 2>"$dir.err"; then
    rm -f "$dir.err"; exit 0
fi
# The reason, e.g. "No usable sandbox!" when user namespaces are blocked by the host
grep -m1 -iE "sandbox|namespace|permission" "$dir.err" >&2 || true
rm -f "$dir.err"
exit 1
