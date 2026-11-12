#!/bin/zsh
# Pacing/logging proxy for the Terminal-Bench pilot. Needs BENCH_UPSTREAM (gateway origin).
B=${BENCH_DIR:-$(cd "$(dirname "$0")" && pwd)}
cd $B
[ -f token ] || { python3 -c "import secrets;print('bp-'+secrets.token_hex(16))" > token; chmod 600 token; }
BENCH_SHIMS=${BENCH_SHIMS:-} BENCH_PORT=18788 BENCH_PROXY_TOKEN=$(cat token) BENCH_LOG_DIR=$B/proxylog nohup uv run --with aiohttp python proxy.py >> proxy.out 2>&1 &
