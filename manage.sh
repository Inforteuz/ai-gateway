#!/usr/bin/env bash
# Day-to-day operations for an installed AI Gateway. Run ./manage.sh help
set -euo pipefail
cd "$(dirname "$0")"
# extract-env runs on a fresh checkout (no .env yet) to start a restore
if [ "${1:-}" = extract-env ]; then shift; exec scripts/backup.sh extract-env "$@"; fi
[ -f .env ] || { echo "No .env found - run ./install.sh first." >&2; exit 1; }

dc() { docker compose "$@"; }
api() { dc exec -T api "$@"; }

usage() {
    cat <<'EOF'
Usage: ./manage.sh <command> [args]

  status                         containers and health
  logs [service]                 follow logs (api, cluster, db, caddy, ollama)
  up | down | restart [service]  start / stop / restart the stack
  update                         git pull, rebuild images, restart (data is kept)

  create-admin EMAIL             create an administrator (prints a generated password)
  set-password EMAIL             reset a password (prints a generated password)
  add-model --id ID --provider local|cluster|external --upstream NAME [--display N]
            [--price-in X] [--price-out Y] [--context TOKENS]
                                 add or update a public model
  list-models                    show configured models
  set-setting KEY [VALUE]        change an admin setting (VALUE from stdin if omitted)

  import-session N CHANNEL [cookies.json|cookies.txt] [--token KEY=VALUE] [--local-storage file.json]
                                 load your own browser session into Chrome instance N
  instances                      show Chrome instances and their channels
  vnc                            how to open the provider browsers

  ollama <args>                  run the ollama CLI in the Ollama container (e.g. ollama pull qwen3:8b)
  backup [file]                  database only (default: backups/gateway-<date>.sql.gz)
  restore file                   restore a database-only dump (stops the API meanwhile)
  backup-full [--live] [--with-ollama] [file]
                                 encrypted full backup: DB, .env, Caddyfile, Chrome sessions, TLS state
  verify-backup file             decrypt and check a full backup without changing anything
  restore-full file [--yes]      restore a full backup (asks for confirmation)
  extract-env file               fresh server: take .env from a full backup before restore-full
EOF
}

cmd=${1:-help}; shift || true
case "$cmd" in
status)
    dc ps
    if api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8310/portal/health', timeout=3)" >/dev/null 2>&1; then
        echo "api: healthy"; else echo "api: NOT responding"; fi
    ;;
logs)      dc logs -f --tail 200 "$@" ;;
up)        dc up -d "$@" ;;
down)      dc down "$@" ;;
restart)   dc restart "$@" ;;
update)
    if [ -d .git ]; then git pull --ff-only; fi
    dc build
    dc up -d
    ;;
create-admin)
    [ $# -ge 1 ] || { echo "usage: create-admin EMAIL" >&2; exit 2; }
    api python -m app.cli create-admin --email "$1"
    ;;
set-password)
    [ $# -ge 1 ] || { echo "usage: set-password EMAIL" >&2; exit 2; }
    api python -m app.cli set-password --email "$1"
    ;;
add-model)    api python -m app.setup_cli add-model "$@" ;;
list-models)  api python -m app.setup_cli list-models ;;
set-setting)
    if [ $# -ge 2 ]; then api python -m app.setup_cli set-setting "$1" "$2"
    else read -r -s -p "Value for $1 (hidden): " v; echo; printf '%s' "$v" | api python -m app.setup_cli set-setting "$1"; fi
    ;;
import-session)
    [ $# -ge 2 ] || { echo "usage: import-session N CHANNEL [cookies-file] [--token K=V] [--local-storage f.json]" >&2; exit 2; }
    n=$1 ch=$2; shift 2
    args=(--instance "$n" --channel "$ch")
    tmp=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --token) args+=(--token "$2"); shift 2 ;;
            --local-storage)
                dc cp "$2" "cluster:/tmp/ls-$n.json" >/dev/null 2>&1; tmp+=("/tmp/ls-$n.json")
                args+=(--local-storage "/tmp/ls-$n.json"); shift 2 ;;
            --all-domains) args+=(--all-domains); shift ;;
            *)
                [ -f "$1" ] || { echo "file not found: $1" >&2; exit 2; }
                dc cp "$1" "cluster:/tmp/cookies-$n" >/dev/null 2>&1; tmp+=("/tmp/cookies-$n")
                args+=("/tmp/cookies-$n"); shift ;;
        esac
    done
    status=0
    dc exec -T cluster python /opt/gateway/bin/import_cookies.py "${args[@]}" || status=$?
    for f in "${tmp[@]:-}"; do [ -n "$f" ] && dc exec -T cluster rm -f "$f"; done
    exit $status
    ;;
instances)
    . ./.env
    i=0; for ch in ${SF_CHROME_CHANNELS//,/ }; do echo "instance $i: $ch (CDP port $((9222 + i)))"; i=$((i + 1)); done
    [ $i -gt 0 ] || echo "no Chrome instances configured (SF_CHROME_CHANNELS is empty)"
    ;;
vnc)
    . ./.env
    cat <<EOF
Built-in viewer: log in to the admin panel and open "VNC" - all Chrome windows share one screen.

Raw VNC via an SSH tunnel (from your computer):
  ssh -L ${VNC_PORT:-15900}:127.0.0.1:${VNC_PORT:-15900} <user>@<this-server>
  then connect a VNC client to localhost:${VNC_PORT:-15900}
  The VNC password is SF_VNC_PASSWORD in .env.
EOF
    ;;
ollama)   dc exec ollama ollama "$@" ;;
backup)
    mkdir -p backups
    out=${1:-backups/gateway-$(date +%Y%m%d-%H%M%S).sql.gz}
    dc exec -T db pg_dump -U gateway_migrator -d gateway --no-owner | gzip > "$out"
    chmod 600 "$out"
    echo "saved $out"
    ;;
restore)
    [ $# -ge 1 ] && [ -f "$1" ] || { echo "usage: restore FILE.sql.gz" >&2; exit 2; }
    read -r -p "This replaces the current database. Type 'yes' to continue: " a
    [ "$a" = yes ] || exit 1
    dc stop api
    dc exec -T db psql -U gateway_migrator -d postgres -c "DROP DATABASE IF EXISTS gateway WITH (FORCE)" -c "CREATE DATABASE gateway"
    gunzip -c "$1" | dc exec -T db psql -q -U gateway_migrator -d gateway
    dc up -d migrate api
    ;;
backup-full|verify-backup|restore-full) exec scripts/backup.sh "$cmd" "$@" ;;
help|-h|--help) usage ;;
*) usage; exit 2 ;;
esac
