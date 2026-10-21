#!/usr/bin/env bash
# AI Gateway installer: asks a few questions, writes .env, builds and starts the
# Docker Compose stack, creates the admin account and the public models.
#
#   ./install.sh                        interactive install; re-run it to reconfigure
#   ./install.sh --reset-admin-password also sets a new admin password (re-run only)
#   ./install.sh --help
#
# Re-running never silently changes billing (free mode), the sign-up policy, the admin
# password, stored provider keys or existing model prices: current values are the defaults
# and risky changes need an explicit confirmation.
#
# Nothing secret is printed except the generated admin password at the very end.
set -euo pipefail
cd "$(dirname "$0")"

# ---------------------------------------------------------------- ui helpers
if [ -t 1 ]; then
    B=$'\e[1m'; D=$'\e[2m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'
else
    B=; D=; G=; Y=; R=; C=; N=
fi
say()  { printf '%s\n' "$*"; }
info() { printf '%s==>%s %s\n' "$C" "$N" "$*"; }
ok()   { printf '%s✔%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s!%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '%s✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }
section() { printf '\n%s%s%s\n%s\n' "$B" "$1" "$N" "${D}$(printf '%.0s─' $(seq 1 ${#1}))${N}"; }

# ask VAR "Question" "default"
# Answers end up in .env as KEY="value", so quotes, $, backticks and backslashes are refused.
ask() {
    local __var=$1 __q=$2 __def=${3:-} __ans
    while true; do
        if [ -n "$__def" ]; then read -r -p "$__q ${D}[$__def]${N}: " __ans || true; else read -r -p "$__q: " __ans || true; fi
        __ans=${__ans:-$__def}
        case "$__ans" in *'"'*|*'$'*|*'`'*|*'\'*) warn 'Please avoid the characters " $ ` \';; *) break;; esac
    done
    printf -v "$__var" '%s' "$__ans"
}
# ask_secret VAR "Question"   (hidden input, may be empty)
ask_secret() {
    local __var=$1 __q=$2 __ans
    read -r -s -p "$__q: " __ans || true; echo
    printf -v "$__var" '%s' "$__ans"
}
# ask_yn VAR "Question" y|n
ask_yn() {
    local __var=$1 __q=$2 __def=${3:-n} __ans
    while true; do
        read -r -p "$__q ${D}[$( [ "$__def" = y ] && echo Y/n || echo y/N )]${N}: " __ans || true
        __ans=$(echo "${__ans:-$__def}" | tr '[:upper:]' '[:lower:]')
        case "$__ans" in y|yes|h|ha|д|да) printf -v "$__var" y; return;; n|no|y\'o\'q|yoq|н|нет) printf -v "$__var" n; return;; esac
    done
}
# choose VAR "Question" default option1 option2 ...   (prints a numbered menu, stores the number)
choose() {
    local __var=$1 __q=$2 __def=$3; shift 3
    local i=1 __ans
    for opt in "$@"; do say "  ${B}$i)${N} $opt"; i=$((i + 1)); done
    while true; do
        read -r -p "$__q ${D}[$__def]${N}: " __ans || true
        __ans=${__ans:-$__def}
        if [[ "$__ans" =~ ^[0-9]+$ ]] && [ "$__ans" -ge 1 ] && [ "$__ans" -le $# ]; then printf -v "$__var" '%s' "$__ans"; return; fi
    done
}
rand() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "${1:-32}"; }
slug() { echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9.]+/-/g; s/^-+|-+$//g'; }

RESET_ADMIN_PW=n
for arg in "$@"; do
    case "$arg" in
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --reset-admin-password) RESET_ADMIN_PW=y ;;
        *) die "Unknown option: $arg (see --help)" ;;
    esac
done

# ---------------------------------------------------------------- preflight
section "AI Gateway installer"
command -v docker >/dev/null || die "Docker is not installed. See https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (the 'docker compose' command)."
docker info >/dev/null 2>&1 || die "Cannot talk to the Docker daemon. Run as root or add your user to the 'docker' group."
command -v openssl >/dev/null || die "openssl is required to generate secrets."
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null), Compose $(docker compose version --short 2>/dev/null)"
[ "$mem_mb" -gt 0 ] && say "  RAM: ${mem_mb} MB ${D}(each Chrome instance needs ~0.6-1 GB)${N}"

RECONFIG=n
CUR_FREE=""; CUR_REG=""; CUR_EXT_BASE=""; CUR_MODELS=""
if [ -f .env ]; then
    RECONFIG=y
    set -a; . ./.env; set +a
    warn "Existing installation found - reconfigure mode. Secrets, billing mode, sign-up policy, admin password,"
    warn "provider keys and model prices are kept unless you change them explicitly."
    # Current values from the running database (the admin panel may have changed them since the install)
    if docker compose ps --status running --services 2>/dev/null | grep -qx api; then
        # </dev/null: `docker compose exec` would otherwise swallow the installer's own input
        cur() { docker compose exec -T api python -m app.setup_cli "$@" </dev/null 2>/dev/null | tr -d '\r'; }
        CUR_FREE=$(cur get-setting free_mode); CUR_REG=$(cur get-setting registration_open)
        CUR_EXT_BASE=$(cur get-setting external_base_url); CUR_MODELS=$(cur model-ids | tr '\n' ' ')
    else
        warn "The stack is not running, so current settings cannot be read; settings stored in the database will"
        warn "only be changed where you confirm it."
    fi
fi
[ "$RESET_ADMIN_PW" = y ] && [ "$RECONFIG" = n ] && die "--reset-admin-password is for an existing installation."

# ---------------------------------------------------------------- branding
section "1. Branding"
ask BRAND "Platform name shown on the site and in the API" "${SF_BRAND_NAME:-AI Gateway}"
ask ASSISTANT "Name the assistant uses for itself (\"I am ...\")" "${SF_ASSISTANT_NAME:-$BRAND}"
say "Default interface language:"
LANGS=(uz ru en)
lang_def=1; [ "${SF_DEFAULT_LANG:-uz}" = ru ] && lang_def=2; [ "${SF_DEFAULT_LANG:-uz}" = en ] && lang_def=3
choose LANG_N "Choose" "$lang_def" "O'zbekcha (uz)" "Русский (ru)" "English (en)"
DEFAULT_LANG=${LANGS[$((LANG_N - 1))]}

# ---------------------------------------------------------------- network
section "2. How will people reach it?"
choose NET "Choose" "${NET_MODE:-1}" \
    "Public domain with automatic HTTPS (Caddy on ports 80/443)" \
    "Behind my own reverse proxy (nginx, Traefik...) - expose a local port" \
    "Local test only (http://localhost)"
CADDY=n; COOKIE_SECURE=1; API_BIND=127.0.0.1
case $NET in
1)
    ask SITE_DOMAIN "Site domain (DNS A record must point to this server)" "${SITE_DOMAIN:-}"
    [ -n "$SITE_DOMAIN" ] || die "A domain is required for HTTPS mode."
    ask API_DOMAIN "Separate API domain (empty = https://$SITE_DOMAIN/v1)" "${API_DOMAIN:-}"
    ask ACME_EMAIL "E-mail for Let's Encrypt notices" "${ACME_EMAIL:-}"
    CADDY=y; API_PORT=${API_PORT:-8310}
    SITE_URL="https://$SITE_DOMAIN"
    API_URL=$([ -n "$API_DOMAIN" ] && echo "https://$API_DOMAIN/v1" || echo "https://$SITE_DOMAIN/v1")
    ;;
2)
    ask API_PORT "Local port for your proxy to forward to (127.0.0.1:PORT)" "${API_PORT:-8310}"
    ask SITE_URL "Public site URL (e.g. https://ai.example.com)" "${SF_SITE_URL:-}"
    ask API_URL "Public API URL" "${SF_API_URL:-${SITE_URL:+$SITE_URL/v1}}"
    case "$SITE_URL" in http://*) COOKIE_SECURE=0;; esac
    SITE_DOMAIN=; API_DOMAIN=; ACME_EMAIL=
    ;;
3)
    ask API_PORT "Local port" "${API_PORT:-8310}"
    SITE_URL="http://localhost:$API_PORT"; API_URL="$SITE_URL/v1"; COOKIE_SECURE=0
    SITE_DOMAIN=; API_DOMAIN=; ACME_EMAIL=
    ;;
esac

# ---------------------------------------------------------------- mode
section "3. Where do the answers come from?"
say "  ${B}1)${N} Web-chat automation - Chrome instances on this server; you log in to the"
say "     providers (Google account, etc.) through the built-in VNC viewer in the admin panel."
say "  ${B}2)${N} Session import - same Chrome cluster, but instead of logging in through VNC you"
say "     import your own session (exported cookies / JWT token from localStorage)."
say "  ${B}3)${N} External API - resell any OpenAI-compatible API under your own model names."
say "  ${D}Local models via Ollama can be added to any of them in the next step.${N}"
choose MODE "Choose" "${INSTALL_MODE:-1}" "Web-chat automation" "Session import (cookies / JWT)" "External API"
[ "$MODE" = 1 ] || [ "$MODE" = 2 ] && warn "Automating consumer chat sites may violate their Terms of Service. Use your own accounts, keep the load low, and check the rules of each provider."

MODELS=()          # "id|provider|upstream|display|price_in|price_out|context"
CHANNELS=""
EXT_BASE=""; EXT_KEY=""
ASK_MODELS=y
if [ "$RECONFIG" = y ]; then
    [ -n "$CUR_MODELS" ] && say "  ${D}Existing models: $CUR_MODELS${N}"
    ask_yn ASK_MODELS "Add or redefine public models now? (no = keep the existing ones and their prices)" n
fi
# Asks for one public model; an id that already exists is only overwritten after confirmation.
ask_model() {   # ask_model PROVIDER UPSTREAM DEFAULT_ID DEFAULT_NAME DEFAULT_CTX
    local mid mname pin pout ctx="$5" over
    ask mid "  Public model id clients will use" "$3"
    if [ -n "$CUR_MODELS" ] && [[ " $CUR_MODELS " == *" $mid "* ]]; then
        ask_yn over "  Model '$mid' already exists. Overwrite its name, prices and settings?" n
        [ "$over" = y ] || { say "  ${D}kept '$mid' as it is${N}"; return; }
    fi
    ask mname "  Display name" "$4"
    ask pin "  Price per 1M input tokens (0 = free)" "0"
    ask pout "  Price per 1M output tokens (0 = free)" "0"
    [ "$1" = cluster ] || ask ctx "  Context window (tokens)" "$5"
    MODELS+=("$mid|$1|$2|$mname|$pin|$pout|$ctx")
}
if [ "$MODE" = 1 ] || [ "$MODE" = 2 ]; then
    say ""
    say "Available channels: ${B}gemini qwen deepseek chatgpt zai grok deepai${N}"
    say "${D}Each channel runs in its own Chrome instance (~0.6-1 GB RAM each). Gemini and Qwen are the most reliable.${N}"
    ask CHANNELS "Channels to run (space or comma separated)" "${SF_CHROME_CHANNELS:-gemini}"
    CHANNELS=$(echo "${CHANNELS//,/ }" | tr -s ' ' | tr '[:upper:]' '[:lower:]' | sed 's/^ //; s/ $//')
    n=0
    for ch in $CHANNELS; do
        case " gemini qwen deepseek chatgpt zai grok deepai genspark chatbotapp chatbotchatapp " in *" $ch "*) ;; *) die "Unknown channel: $ch";; esac
        n=$((n + 1))
        [ "$ASK_MODELS" = y ] || continue
        say ""
        say "${B}Model for channel '$ch'${N}"
        ask_model cluster "$ch" "$(slug "$ASSISTANT")-$n" "$ASSISTANT $n" 128000
    done
fi
if [ "$MODE" = 3 ]; then
    say ""
    ask EXT_BASE "Upstream base URL (OpenAI-compatible, ends with /v1)" "${CUR_EXT_BASE:-https://api.openai.com/v1}"
    if [ "$RECONFIG" = y ]; then
        ask_secret EXT_KEY "Upstream API key (hidden, empty = keep the stored key)"
    else
        ask_secret EXT_KEY "Upstream API key (hidden)"
        [ -n "$EXT_KEY" ] || die "An API key for the upstream is required."
    fi
fi
if [ "$MODE" = 3 ] && [ "$ASK_MODELS" = y ]; then
    remote_models=""
    if [ -n "$EXT_KEY" ]; then
        info "Fetching the upstream model list..."
        remote_models=$(curl -fsS -m 15 -H "Authorization: Bearer $EXT_KEY" "${EXT_BASE%/}/models" 2>/dev/null \
                    | python3 -c 'import json,sys; print(" ".join(m["id"] for m in json.load(sys.stdin).get("data", [])[:40]))' 2>/dev/null || true)
    fi
    if [ -n "$remote_models" ]; then say "  ${D}Upstream offers: $remote_models${N}"; else warn "Upstream models not listed (that is fine, type the names yourself)."; fi
    ask ext_list "Upstream model names to resell (space separated)" "$(echo "$remote_models" | awk '{print $1}')"
    [ -n "$ext_list" ] || die "At least one model is required."
    n=0
    for um in $ext_list; do
        n=$((n + 1))
        say ""
        say "${B}Public name for upstream model '$um'${N}"
        ask_model external "$um" "$(slug "$ASSISTANT")-$n" "$ASSISTANT $n" 128000
    done
fi

# ---------------------------------------------------------------- ollama
section "4. Local models (Ollama)"
choose OLL "Use local models?" "${OLLAMA_CHOICE:-1}" \
    "No" \
    "Yes - run Ollama in Docker on this server (CPU)" \
    "Yes - run Ollama in Docker with an NVIDIA GPU" \
    "Yes - I already have Ollama somewhere (URL)"
UPSTREAM_URL="http://ollama:11434"
OLLAMA_PULL=""
if [ "$OLL" = 4 ]; then
    say "${D}Ollama on this host: http://host.docker.internal:11434 (make it listen on 0.0.0.0 or the docker bridge).${N}"
    ask UPSTREAM_URL "Ollama URL" "${SF_UPSTREAM_URL:-http://host.docker.internal:11434}"
fi
if [ "$OLL" != 1 ] && [ "$ASK_MODELS" = y ]; then
    ask local_list "Ollama model names to offer (space separated, e.g. qwen3:8b llama3.2:3b)" "qwen3:8b"
    n=0
    for lm in $local_list; do
        n=$((n + 1))
        say ""
        say "${B}Public name for local model '$lm'${N}"
        ask_model local "$lm" "$(slug "$ASSISTANT")-local-$n" "$ASSISTANT Local $n" 16384
    done
    [ "$OLL" = 2 ] || [ "$OLL" = 3 ] && OLLAMA_PULL="$local_list"
fi
[ ${#MODELS[@]} -gt 0 ] || [ "$RECONFIG" = y ] || die "No models configured - choose at least one source."

# ---------------------------------------------------------------- accounts
section "5. Admin account and access"
GENERATED_PW=n; SET_ADMIN=n; ADMIN_PASSWORD=""
if [ "$RECONFIG" = n ] || [ "$RESET_ADMIN_PW" = y ]; then
    SET_ADMIN=y
    ask ADMIN_EMAIL "Admin e-mail (used to log in)" "${ADMIN_EMAIL:-admin@${SITE_DOMAIN:-example.com}}"
    ask_secret ADMIN_PASSWORD "Admin password (empty = generate one)"
    if [ -z "$ADMIN_PASSWORD" ]; then ADMIN_PASSWORD="$(rand 20)"; GENERATED_PW=y; fi
    [ ${#ADMIN_PASSWORD} -ge 10 ] || die "Password must be at least 10 characters."
else
    say "  Admin account ${B}${ADMIN_EMAIL:-?}${N} is kept (new password: ./install.sh --reset-admin-password)."
fi

yn_of() { [ "$1" = 1 ] && echo y || echo n; }
if [ "$RECONFIG" = y ] && [ -z "$CUR_FREE" ]; then
    # Database not readable: leave both settings untouched
    REG=keep; FREE=keep
    say "  ${D}Sign-up policy and free mode are left as they are (change them in the admin panel).${N}"
else
    ask_yn REG "Allow anyone to sign up on the site?" "$( [ -n "$CUR_REG" ] && yn_of "$CUR_REG" || echo n )"
    ask_yn FREE "Free mode (no balances or charging, everything costs 0)?" "$( [ -n "$CUR_FREE" ] && yn_of "$CUR_FREE" || echo y )"
    if [ "$CUR_FREE" = 0 ] && [ "$FREE" = y ]; then
        warn "This switches a PAID service to free mode: every request will cost 0 from now on."
        read -r -p "Type FREE to confirm: " confirm_free || true
        [ "$confirm_free" = FREE ] || { FREE=n; say "  ${D}kept paid mode${N}"; }
    fi
fi

# ---------------------------------------------------------------- write config
section "Writing configuration"
DB_MIGRATOR_PASSWORD=${DB_MIGRATOR_PASSWORD:-$(rand 32)}
DB_APP_PASSWORD=${DB_APP_PASSWORD:-$(rand 32)}
CLUSTER_API_KEY=${CLUSTER_API_KEY:-$(rand 40)}
SF_VNC_PASSWORD=${SF_VNC_PASSWORD:-$(rand 8)}

PROFILES=()
[ "$MODE" = 1 ] || [ "$MODE" = 2 ] && PROFILES+=(cluster)
[ "$OLL" = 2 ] || [ "$OLL" = 3 ] && PROFILES+=(ollama)
[ "$CADDY" = y ] && PROFILES+=(caddy)
COMPOSE_FILES="docker-compose.yml"
[ "$OLL" = 3 ] && COMPOSE_FILES="docker-compose.yml:docker-compose.gpu.yml"

umask 077
cat > .env <<EOF
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Keep this file private (chmod 600).
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ai-gateway}"
COMPOSE_PROFILES="$(IFS=,; echo "${PROFILES[*]:-}")"
COMPOSE_FILE="$COMPOSE_FILES"
INSTALL_MODE="$MODE"
NET_MODE="$NET"
OLLAMA_CHOICE="$OLL"

SF_BRAND_NAME="$BRAND"
SF_ASSISTANT_NAME="$ASSISTANT"
SF_DEFAULT_LANG="$DEFAULT_LANG"
SF_SITE_URL="$SITE_URL"
SF_API_URL="$API_URL"
SF_COOKIE_SECURE="$COOKIE_SECURE"
SITE_DOMAIN="$SITE_DOMAIN"
API_DOMAIN="$API_DOMAIN"
ACME_EMAIL="$ACME_EMAIL"
API_BIND="$API_BIND"
API_PORT="$API_PORT"
VNC_PORT="${VNC_PORT:-15900}"
ADMIN_EMAIL="$ADMIN_EMAIL"

SF_UPSTREAM_URL="$UPSTREAM_URL"
SF_CHROME_CHANNELS="$CHANNELS"

DB_MIGRATOR_PASSWORD="$DB_MIGRATOR_PASSWORD"
DB_APP_PASSWORD="$DB_APP_PASSWORD"
CLUSTER_API_KEY="$CLUSTER_API_KEY"
SF_VNC_PASSWORD="$SF_VNC_PASSWORD"
EOF
chmod 600 .env
ok ".env written"

if [ "$CADDY" = y ]; then
    hosts="$SITE_DOMAIN"; [ -n "$API_DOMAIN" ] && hosts="$hosts, $API_DOMAIN"
    { [ -n "$ACME_EMAIL" ] && printf '{\n\temail %s\n}\n\n' "$ACME_EMAIL"
      cat <<EOF
$hosts {
	encode gzip
	request_body {
		max_size 30MB
	}
	reverse_proxy api:8310 {
		flush_interval -1
	}
}
EOF
    } > Caddyfile
    ok "Caddyfile written for: $hosts"
fi
umask 022

# ---------------------------------------------------------------- start
section "Building and starting (first build takes a few minutes)"
docker compose build
docker compose up -d
info "Waiting for the API..."
for i in $(seq 1 90); do
    if docker compose exec -T api python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8310/portal/health', timeout=3)" </dev/null >/dev/null 2>&1; then
        ok "API is up"; break
    fi
    [ "$i" = 90 ] && { docker compose logs --tail 50 migrate api; die "API did not start, see the logs above."; }
    sleep 2
done

run_api() { docker compose exec -T api "$@" </dev/null; }          # never reads the installer's stdin
run_api_in() { docker compose exec -T api "$@"; }                # value piped in (secrets)

section "Configuring"
if [ "$SET_ADMIN" = y ]; then
    if run_api python -m app.cli create-admin --email "$ADMIN_EMAIL" --password "$ADMIN_PASSWORD" >/dev/null 2>&1; then
        ok "Admin account created"
    elif [ "$RESET_ADMIN_PW" = y ]; then
        run_api python -m app.cli set-password --email "$ADMIN_EMAIL" --password "$ADMIN_PASSWORD" >/dev/null && ok "Admin password updated"
    else
        die "Could not create the admin account $ADMIN_EMAIL (it may already exist: use --reset-admin-password)."
    fi
fi
set_flag() {   # set_flag KEY y|n|keep CURRENT
    [ "$2" = keep ] && return
    local v; v=$([ "$2" = y ] && echo 1 || echo 0)
    [ "$v" = "$3" ] && return
    run_api python -m app.setup_cli set-setting "$1" "$v" >/dev/null && ok "$1 = $v"
}
set_flag registration_open "$REG" "$CUR_REG"
set_flag free_mode "$FREE" "$CUR_FREE"
if [ "$MODE" = 1 ] || [ "$MODE" = 2 ]; then
    run_api python -m app.setup_cli set-setting cluster_base_url "http://cluster:8800/v1" >/dev/null
    printf '%s' "$CLUSTER_API_KEY" | run_api_in python -m app.setup_cli set-setting cluster_api_key >/dev/null
    ok "Cluster connected"
fi
if [ "$MODE" = 3 ]; then
    run_api python -m app.setup_cli set-setting external_base_url "$EXT_BASE" >/dev/null
    printf '%s' "$EXT_KEY" | run_api_in python -m app.setup_cli set-setting external_api_key >/dev/null   # empty keeps the stored key
    ok "External API configured"
fi
for m in "${MODELS[@]+"${MODELS[@]}"}"; do
    IFS='|' read -r mid prov up mname pin pout ctx <<< "$m"
    run_api python -m app.setup_cli add-model --id "$mid" --provider "$prov" --upstream "$up" \
        --display "$mname" --price-in "$pin" --price-out "$pout" --context "$ctx"
done
if [ -n "$OLLAMA_PULL" ]; then
    for lm in $OLLAMA_PULL; do
        info "Pulling $lm into Ollama (can take a while)..."
        docker compose exec -T ollama ollama pull "$lm" </dev/null || warn "Pull failed for $lm; retry later: ./manage.sh ollama pull $lm"
    done
fi

# ---------------------------------------------------------------- summary
section "Done"
say "  Site:    ${B}$SITE_URL${N}"
say "  API:     ${B}$API_URL${N}  (OpenAI-compatible)"
say "  Admin:   ${B}$ADMIN_EMAIL${N}"
[ "$GENERATED_PW" = y ] && say "  Password: ${B}$ADMIN_PASSWORD${N}   ${Y}<- save it now, it is not stored anywhere in plain text${N}"
[ "$NET" = 2 ] && say "  Point your reverse proxy to ${B}http://127.0.0.1:$API_PORT${N} (keep Host and X-Forwarded-* headers, allow WebSocket upgrade, disable response buffering)."
if [ "$MODE" = 1 ]; then
    say ""
    say "  Next: log in to the providers. Open the admin panel -> ${B}VNC${N}, then sign in to each"
    say "  Chrome window (one per channel: $CHANNELS). Logins are kept in the cluster-data volume."
fi
if [ "$MODE" = 2 ]; then
    say ""
    say "  Next: import your session for each channel, e.g.:"
    i=0; for ch in $CHANNELS; do say "    ${B}./manage.sh import-session $i $ch cookies-$ch.json${N}"; i=$((i + 1)); done
    say "  Export cookies from your own browser with a cookie-export extension (JSON) or as cookies.txt."
    say "  Tokens kept in localStorage: ./manage.sh import-session 0 deepseek --token userToken='...'"
fi
say ""
say "  Manage: ${B}./manage.sh help${N}   (status, logs, add-model, backup, update...)"
