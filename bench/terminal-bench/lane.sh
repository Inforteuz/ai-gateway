#!/bin/zsh
# usage: lane.sh <lane-file>; each line: agent model jobname timeout-mult task...
B=${BENCH_DIR:-$(cd "$(dirname "$0")" && pwd)}
while read -r agent model job mult tasks; do
  [[ -z "$agent" || "$agent" == \#* ]] && continue
  [[ -f $B/jobs/$job/result.json && -n "$(ls $B/jobs/$job/*/result.json 2>/dev/null)" ]] && continue
  echo "$(date +%T) start $job"
  HAIKU=$HAIKU $B/run.sh $agent $model $job 1 $mult ${=tasks} > $B/logs/$job.log 2>&1
  echo "$(date +%T) done $job"; python3 $B/summ.py $job
done < $1
