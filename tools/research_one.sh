#!/usr/bin/env bash
# Research one topic. Writes raw/<DATE>/<SLUG>.md.
# usage: research_one.sh SLUG MODELS STAMP     (MODELS: comma list of model ids, tried in order)
# env EXA_LEFT: max Exa searches for this topic (0 = none).
# The model has no tools (tools/research.py + tools/llm.py): it plans keywords and writes the
# report from sources this script's code collected. Nothing it says is executed.
# Prints one final line: RESULT <slug> <ok 0|1> <model> <exa searches>
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SLUG=$1; MODELS=$2; STAMP=$3
cd "$ROOT" || exit 1
DATE=$(TZ=Asia/Kolkata date +%F)
OUT="$ROOT/raw/$DATE/$SLUG.md"
report_ok() {
  [[ -s "$1" ]] && [[ $(wc -c <"$1") -gt 1500 ]] && [[ $(grep -oE 'https?://[^ )>]+' "$1" | sort -u | wc -l) -ge 3 ]]
}
# Resume: a good report from the last 6 h (a run that died before compile) is reused.
if report_ok "$OUT" && (( $(date +%s) - $(stat -c %Y "$OUT") < 6 * 3600 )); then
  echo "$(date '+%F %T') [$SLUG] reusing existing report $OUT"
  echo "RESULT $SLUG 1 cached 0"; exit 0
fi
timeout -k 30 3600 python3 "$ROOT/tools/research.py" "$SLUG" "$OUT" --models "$MODELS" --exa "${EXA_LEFT:-0}" \
  --log "$ROOT/logs/research-$STAMP-$SLUG.log" </dev/null
RC=$?
(( RC == 124 )) && echo "RESULT $SLUG 0 timeout 0"
exit 0
