#!/usr/bin/env bash
# Full, encrypted backups of an AI Gateway installation (called by manage.sh).
#
#   backup-full  [--live] [--with-ollama] [FILE]   DB + .env/Caddyfile + Chrome sessions + TLS state
#   verify-backup FILE                              decrypt, check checksums and the DB dump (changes nothing)
#   restore-full FILE [--yes]                       replace DB and volumes from a backup
#   extract-env FILE                                write the backup's .env here (fresh server, before restore-full)
#
# The archive is encrypted with AES-256 (openssl, PBKDF2). Passphrase: $BACKUP_PASSPHRASE or a prompt.
# Without the passphrase the backup cannot be restored - store it outside this server.
# Chrome profiles hold logged-in provider sessions; treat the backup like a password vault.
set -euo pipefail
cd "$(dirname "$0")/.."

dc() { docker compose "$@"; }
die() { echo "error: $*" >&2; exit 1; }
project() { echo "${COMPOSE_PROJECT_NAME:-$(sed -n 's/^COMPOSE_PROJECT_NAME="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' .env 2>/dev/null)}"; }
# A small image that is already present (the database image) runs tar on the volumes
TAR_IMAGE=postgres:16-alpine
VOLUMES=(cluster-data caddy-data caddy-config)

passphrase() {   # passphrase confirm|once
    if [ -n "${BACKUP_PASSPHRASE:-}" ]; then printf '%s' "$BACKUP_PASSPHRASE"; return; fi
    local a b
    read -r -s -p "Backup passphrase: " a </dev/tty; echo >&2
    if [ "$1" = confirm ]; then
        read -r -s -p "Repeat passphrase: " b </dev/tty; echo >&2
        [ "$a" = "$b" ] || die "passphrases differ"
        [ ${#a} -ge 12 ] || die "use at least 12 characters"
    fi
    printf '%s' "$a"
}
encrypt() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass fd:3 3<<<"$1"; }
decrypt() { openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass fd:3 3<<<"$1"; }
volume_exists() { docker volume inspect "$(project)_$1" >/dev/null 2>&1; }

backup_full() {
    local live=n ollama=n out=""
    while [ $# -gt 0 ]; do
        case "$1" in --live) live=y;; --with-ollama) ollama=y;; -*) die "unknown option $1";; *) out=$1;; esac; shift
    done
    [ -f .env ] || die "no .env here"
    out=${out:-backups/gateway-full-$(date +%Y%m%d-%H%M%S).tar.enc}
    mkdir -p "$(dirname "$out")"
    local pass; pass=$(passphrase confirm)
    local tmp; tmp=$(mktemp -d); trap 'rm -rf "$tmp"' RETURN
    umask 077

    echo "== database (pg_dump, custom format)"
    dc exec -T db pg_dump -U gateway_migrator -d gateway -Fc --no-owner > "$tmp/db.dump"
    cp .env "$tmp/env"; [ -f Caddyfile ] && cp Caddyfile "$tmp/Caddyfile"

    local vols=("${VOLUMES[@]}"); [ "$ollama" = y ] && vols+=(ollama-data)
    local paused=n
    if [ "$live" = n ] && dc ps --status running --services 2>/dev/null | grep -qx cluster; then
        # Freezing the processes gives a consistent copy of Chrome's SQLite files without a restart
        dc pause cluster >/dev/null && paused=y
    fi
    mkdir -p "$tmp/volumes"
    for v in "${vols[@]}"; do
        volume_exists "$v" || continue
        echo "== volume $v"
        docker run --rm -v "$(project)_$v:/v:ro" -v "$tmp/volumes:/out" "$TAR_IMAGE" tar czf "/out/$v.tgz" -C /v . \
            || { [ "$paused" = y ] && dc unpause cluster >/dev/null; die "could not archive $v"; }
    done
    [ "$paused" = y ] && dc unpause cluster >/dev/null

    {
        echo "created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "project=$(project)"
        echo "revision=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
        echo "live_browser_copy=$live"
    } > "$tmp/MANIFEST"
    (cd "$tmp" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
    tar cf - -C "$tmp" . | encrypt "$pass" > "$out"
    chmod 600 "$out"
    (cd "$(dirname "$out")" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
    echo "saved $out ($(du -h "$out" | cut -f1)). Copy it and $out.sha256 off this server."
}

unpack() {   # unpack FILE DIR PASS -> verified contents in DIR
    [ -f "$1" ] || die "no such file: $1"
    if [ -f "$1.sha256" ]; then (cd "$(dirname "$1")" && sha256sum -c --quiet "$(basename "$1").sha256") || die "archive checksum mismatch"; fi
    decrypt "$3" < "$1" | tar xf - -C "$2" 2>/dev/null || die "cannot decrypt (wrong passphrase or damaged file)"
    (cd "$2" && sha256sum -c --quiet SHA256SUMS) || die "content checksum mismatch"
}

verify_backup() {
    [ $# -ge 1 ] || die "usage: verify-backup FILE"
    local pass; pass=$(passphrase once)
    local tmp; tmp=$(mktemp -d); trap 'rm -rf "$tmp"' RETURN
    unpack "$1" "$tmp" "$pass"
    cat "$tmp/MANIFEST"
    ls -la "$tmp" "$tmp/volumes" 2>/dev/null | sed 's/^/  /'
    local n
    n=$(dc exec -T db pg_restore --list < "$tmp/db.dump" | grep -c "TABLE DATA" || true)
    [ "$n" -gt 0 ] || die "database dump has no table data"
    echo "OK: checksums match, database dump readable ($n tables with data)"
}

extract_env() {
    [ $# -ge 1 ] || die "usage: extract-env FILE"
    [ -f .env ] && die ".env already exists here; move it away first"
    local pass; pass=$(passphrase once)
    local tmp; tmp=$(mktemp -d); trap 'rm -rf "$tmp"' RETURN
    unpack "$1" "$tmp" "$pass"
    umask 077; cp "$tmp/env" .env; [ -f "$tmp/Caddyfile" ] && [ ! -f Caddyfile ] && cp "$tmp/Caddyfile" Caddyfile
    echo "wrote .env from the backup. Review ports/domains (and COMPOSE_PROJECT_NAME on a shared host),"
    echo "then: docker compose up -d db && ./manage.sh restore-full $1"
}

restore_full() {
    local file="" yes=n
    while [ $# -gt 0 ]; do case "$1" in --yes) yes=y;; -*) die "unknown option $1";; *) file=$1;; esac; shift; done
    [ -n "$file" ] || die "usage: restore-full FILE [--yes]"
    [ -f .env ] || die "no .env here (fresh server: ./manage.sh extract-env FILE first)"
    local pass; pass=$(passphrase once)
    local tmp; tmp=$(mktemp -d); trap 'rm -rf "$tmp"' RETURN
    unpack "$file" "$tmp" "$pass"
    cat "$tmp/MANIFEST"
    echo
    echo "This REPLACES the database and the Chrome/Caddy volumes of project '$(project)'."
    echo "Current data is not kept - make a backup-full first if you may need it."
    if [ "$yes" != y ]; then
        local a; read -r -p "Type 'restore' to continue: " a </dev/tty; [ "$a" = restore ] || die "cancelled"
    fi

    dc stop api cluster caddy >/dev/null 2>&1 || true
    dc up -d db >/dev/null
    for i in $(seq 30); do dc exec -T db pg_isready -U gateway_migrator -d postgres >/dev/null 2>&1 && break; sleep 1; done
    echo "== database"
    dc exec -T db psql -q -v ON_ERROR_STOP=1 -U gateway_migrator -d postgres \
        -c "DROP DATABASE IF EXISTS gateway WITH (FORCE)" -c "CREATE DATABASE gateway"
    dc exec -T db pg_restore -U gateway_migrator -d gateway --no-owner --exit-on-error < "$tmp/db.dump"
    for f in "$tmp"/volumes/*.tgz; do
        [ -f "$f" ] || continue
        local v; v=$(basename "$f" .tgz)
        echo "== volume $v"
        docker volume create "$(project)_$v" >/dev/null
        docker run --rm -v "$(project)_$v:/v" -v "$tmp/volumes:/in:ro" "$TAR_IMAGE" \
            sh -c 'find /v -mindepth 1 -delete && tar xzf "/in/$0.tgz" -C /v' "$v"
    done
    dc up -d >/dev/null   # the migrate service re-applies grants for the app role
    echo "restored. Check: ./manage.sh status"
}

cmd=${1:-}; shift || true
case "$cmd" in
    backup-full) backup_full "$@" ;;
    verify-backup) verify_backup "$@" ;;
    restore-full) restore_full "$@" ;;
    extract-env) extract_env "$@" ;;
    *) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
