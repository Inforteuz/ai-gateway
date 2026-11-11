#!/bin/zsh
# usage: run.sh <agent> <model> <job-name> <n-concurrent> <timeout-mult> task1 task2 ...
B=${BENCH_DIR:-$(cd "$(dirname "$0")" && pwd)}
agent=$1; model=$2; job=$3; conc=$4; mult=$5; shift 5
export BENCH_TOKEN=$(cat $B/token)
inc=(); for t in "$@"; do inc+=(-i "$t"); done
case $agent in
  claude-code) base=http://host.docker.internal:${BENCH_PORT:-18788}; m="anthropic/$model"; fmt=anthropic_messages; extra=(--ae ANTHROPIC_DEFAULT_HAIKU_MODEL=${HAIKU:-$model});;
  codex) base=http://host.docker.internal:${BENCH_PORT:-18788}/v1; m="openai/$model"; fmt=openai_responses; extra=();;
  opencode) base=http://host.docker.internal:${BENCH_PORT:-18788}/v1; m="gateway/$model"; fmt=openai_chat_completions; extra=();;
  *) base=http://host.docker.internal:${BENCH_PORT:-18788}/v1; m="openai/$model"; fmt=openai_chat_completions; extra=();;
esac
cd $B
exec harbor run -p ${BENCH_DATASET:?path to downloaded terminal-bench@2.0 tasks} -a $agent -m $m --base-url $base --api-key-name BENCH_TOKEN --api-format $fmt \
  "${extra[@]}" -n $conc --agent-timeout-multiplier $mult -o $B/jobs --job-name $job -y -q "${inc[@]}"
