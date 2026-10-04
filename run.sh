#!/usr/bin/env bash
# One research iteration: pick topic(s) -> collect sources + tool-less model write
# (tools/research.py) -> llmwiki ingest + compile -> quarantine scan -> git commit.
# Called in a loop by loop.sh (systemd user service snowball-loop). The daily digest is a
# separate timer (08:00 IST) that writes digests/YYYY-MM-DD.md; it posts to Discord only if
# DIGEST_CHANNEL_ID is set in config.env (empty).
#
# usage: run.sh [--test] [--topic SLUG] [--max-topics N] [--topic-min M] [--wall-min W] [--digest]
#
# Security (2 Oct 2026): the model never gets tools. It plans keywords from our topic text and
# writes the report from sources our code collected, wrapped as untrusted data. Report URLs are
# checked against the collected sources. Flagged pages go to QUARANTINE.md, not into git.
#
# Models: Ornith A3B for everything, one request in flight (ornith.lock, tools/llm.py).
# Gemma 4 31B only as plan-step fallback for public topics (its free tier allows 16K input
# tokens/min/key, too small for the write step). 27B last, only 00:30-07:00 IST. Never qwen-best.
# Caps per day (state/budget.json): 40 topic runs, 5 new suggestions. Exa has no daily cap: the
# loop stops using it when the rotator has 3 or fewer live keys (tools/exa_budget.py), and logs
# each key that goes spent to EXA_BURN.md.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1

if [[ -z "${RW_SCOPED:-}" ]]; then
  export RW_SCOPED=1
  exec systemd-run --user --scope --quiet -p MemoryMax=3G -p MemorySwapMax=0 "$0" "$@"
fi

exec 9>"$ROOT/run.lock"
flock -n 9 || { echo "another run holds run.lock, exiting"; exit 0; }

TEST=0; ONLY=""; MAX_TOPICS=2; TOPIC_MIN=60; WALL_MIN=110; DIGEST=0
MAX_RUNS_PER_DAY=40; EXA_PER_TOPIC=6   # no daily Exa cap; Exa stops when the pool is down to the reserve
while [[ $# -gt 0 ]]; do
  case "$1" in
    --test) TEST=1 ;;
    --topic) ONLY="$2"; shift ;;
    --max-topics) MAX_TOPICS="$2"; shift ;;
    --topic-min) TOPIC_MIN="$2"; shift ;;
    --wall-min) WALL_MIN="$2"; shift ;;
    --digest) DIGEST=1 ;;
    *) echo "unknown arg $1"; exit 2 ;;
  esac
  shift
done

export TZ=Asia/Kolkata
DATE=$(date +%F)
STAMP=$(date +%Y%m%d-%H%M)
mkdir -p logs state "raw/$DATE"
LOG="$ROOT/logs/run-$STAMP.log"
exec > >(tee -a "$LOG") 2>&1
log() { echo "$(date '+%F %T') $*"; }

set -a; . "$ROOT/.env"; set +a   # QWEN_API_KEY etc. Never echoed.
export LLMWIKI_PROVIDER=openai LLMWIKI_EMBEDDINGS=off
ORN=ornith-1.5-35b-a3b-abliterated
GEM=gemma-4-31b-it          # plan step only (small prompt), public topics only
Q27=qwen3.8-27b-abliterated
LLMWIKI="$ROOT/node_modules/.bin/llmwiki"

START=$(date +%s)
DEADLINE=$((START + WALL_MIN * 60))
HM=$((10#$(date +%H%M)))
NIGHT=0; (( HM >= 30 && HM < 700 )) && NIGHT=1
log "run start test=$TEST night=$NIGHT deadline=$(date -d @$DEADLINE '+%T')"

BUDGET=$(python3 tools/queue.py budget get)
RUNS_DONE=$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["topic_runs"])' "$BUDGET")
RUNS_LEFT=$((MAX_RUNS_PER_DAY - RUNS_DONE))
if (( TEST == 0 && RUNS_LEFT <= 0 )); then log "daily cap of $MAX_RUNS_PER_DAY topic runs reached"; exit 0; fi
N=$MAX_TOPICS; (( TEST == 0 && N > RUNS_LEFT )) && N=$RUNS_LEFT

if [[ -n "$ONLY" ]]; then
  PICKS=$(python3 -c 'import sys,yaml;t=[x for x in yaml.safe_load(open("topics.yml"))["topics"] if x["slug"]==sys.argv[1]];print(sys.argv[1],"private" if t and t[0].get("private") else "public") if t else None' "$ONLY")
else
  PICKS=$(python3 tools/queue.py pick "$N")
fi
[[ -z "$PICKS" || "$PICKS" == None ]] && { log "nothing to run"; exit 0; }

EXA_STATUS=$(python3 tools/exa_budget.py status)
if python3 -c 'import json,sys;sys.exit(0 if json.loads(sys.argv[1])["use_exa"] else 1)' "$EXA_STATUS"; then
  EXA_EACH=$EXA_PER_TOPIC
else
  EXA_EACH=0; log "Exa pool at or below the 3-key reserve ($EXA_STATUS): no Exa this run"
fi
GEMMA_UP=0
curl -s -m 5 http://127.0.0.1:8810/health | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d["keys"] and d["requests"]<d["daily_cap"] else 1)' 2>/dev/null && GEMMA_UP=1

RUN_JSON="$ROOT/logs/run-$STAMP.json"
python3 -c 'import json,sys;json.dump({"date":sys.argv[2],"stamp":sys.argv[3],"test":sys.argv[4]=="1","topics":[],"created":[],"updated":[],"notes":[]},open(sys.argv[1],"w"))' \
  "$RUN_JSON" "$DATE" "$STAMP" "$TEST"
jadd() {  # field value-json
  python3 -c 'import json,sys;p=sys.argv[1];d=json.load(open(p));d[sys.argv[2]].append(json.loads(sys.argv[3]));json.dump(d,open(p,"w"))' "$RUN_JSON" "$1" "$2"
}

# Launch one research worker per topic. The model has no tools in any step (tools/research.py).
# Topics run in parallel only for collection; their model calls queue on ornith.lock.
i=0; PIDS=()
while read -r SLUG KIND; do
  [[ -z "$SLUG" ]] && continue
  if [[ "$KIND" == private ]]; then CHAIN=$ORN          # private: never Gemma
  elif (( GEMMA_UP )); then CHAIN=$ORN,$GEM
  else CHAIN=$ORN; fi
  (( NIGHT )) && CHAIN=$CHAIN,$Q27
  (( TEST == 0 )) && python3 tools/queue.py budget add topic_runs 1 >/dev/null
  log "topic $SLUG ($KIND) models=$CHAIN exa=$EXA_EACH"
  EXA_LEFT=$EXA_EACH tools/research_one.sh "$SLUG" "$CHAIN" "$STAMP" \
    >"$ROOT/logs/topic-$STAMP-$SLUG.log" 2>&1 </dev/null &
  PIDS+=($!); i=$((i + 1))
  sleep 5
done <<<"$PICKS"
wait "${PIDS[@]}"

NEW=0; PRIVATE_BATCH=0
for f in "$ROOT"/logs/topic-"$STAMP"-*.log; do
  cat "$f"
  read -r _ SLUG OK MODEL SEARCHES < <(grep '^RESULT ' "$f" | tail -1) || continue
  OUT="$ROOT/raw/$DATE/$SLUG.md"
  (( TEST == 0 )) && python3 tools/queue.py budget add exa "${SEARCHES:-0}" >/dev/null
  (( TEST == 0 )) && python3 tools/exa_budget.py track "${SEARCHES:-0}" "$([[ $OK == 1 ]] && echo 1 || echo 0)" >/dev/null
  jadd topics "{\"slug\":\"$SLUG\",\"report\":\"$OUT\",\"ok\":$([[ $OK == 1 ]] && echo true || echo false),\"model\":\"$MODEL\",\"web_search\":${SEARCHES:-0}}"
  if [[ "$OK" == 1 ]]; then
    cp "$OUT" "$ROOT/logs/$DATE-$SLUG.md"   # dated name: a re-run adds a source instead of replacing one
    "$LLMWIKI" ingest "$ROOT/logs/$DATE-$SLUG.md" >/dev/null && NEW=$((NEW + 1))
    rm -f "$ROOT/logs/$DATE-$SLUG.md"
    (( TEST == 0 )) && python3 tools/queue.py done "$SLUG" "$OUT"
  else
    jadd notes "\"$SLUG produced no usable report\""
  fi
done

# Compile every run: it is incremental (no model calls when nothing changed) and it retries
# sources an earlier compile left as "no concepts". A compile that leaves any source with
# "no concepts" counts as failed, so the next model in the chain gets it.
# Gemma may compile only while no private topic has a source in the wiki: page generation
# re-reads older sources that share a concept.
PRIV_IN_WIKI=$(python3 -c '
import yaml, pathlib
priv = [t["slug"] for t in yaml.safe_load(open("topics.yml"))["topics"] if t.get("private")]
src = [p.name for p in pathlib.Path("sources").glob("*.md")]
print(int(any(n.endswith("-" + s + ".md") for n in src for s in priv)))')
if true; then
  # llmwiki caps replies at 4096 tokens; reasoning Qwen models spend that thinking and never
  # reach the tool call. The adapter lifts the cap. Started here, stopped by PID below.
  python3 "$ROOT/tools/llm_adapter.py" 8812 >"$ROOT/logs/adapter-$STAMP.log" 2>&1 &
  ADAPTER=$!
  sleep 1
  COMPILED=0
  for M in ornith-1.5-35b-a3b-abliterated gemma-4-31b-it qwen3.8-27b-abliterated; do
    [[ $M == gemma* && ( $PRIV_IN_WIKI == 1 || $GEMMA_UP == 0 ) ]] && continue
    [[ $M == qwen3.8-27b* && $NIGHT == 0 ]] && continue
    WAIT=$((DEADLINE - $(date +%s) + 1800)); (( WAIT < 5400 )) && WAIT=5400   # pages are written only at the end
    CLOG="$ROOT/logs/compile-$STAMP-$M.log"
    log "llmwiki compile model=$M"
    if [[ $M == ornith* ]]; then
      OPENAI_BASE_URL=http://127.0.0.1:8812/v1 LLMWIKI_MODEL=$M flock -w "$WAIT" "$ROOT/ornith.lock" timeout -k 30 "$WAIT" \
        "$LLMWIKI" compile --concurrency 1 --instructions "$ROOT/SOUL.md" >"$CLOG" 2>&1
    elif [[ $M == gemma* ]]; then
      OPENAI_BASE_URL=http://127.0.0.1:8810/v1 OPENAI_API_KEY=local LLMWIKI_MODEL=$M timeout -k 30 "$WAIT" \
        "$LLMWIKI" compile --concurrency 1 --instructions "$ROOT/SOUL.md" >"$CLOG" 2>&1
    else
      OPENAI_BASE_URL=http://127.0.0.1:8812/v1 LLMWIKI_MODEL=$M timeout -k 30 "$WAIT" \
        "$LLMWIKI" compile --concurrency 1 --instructions "$ROOT/SOUL.md" >"$CLOG" 2>&1
    fi
    RC=$?
    grep -E "Found|no concepts|compiled,|Contradiction" "$CLOG" | sed 's/\x1b\[[0-9;]*m//g'
    if (( RC == 0 )) && ! grep -q "no concepts" "$CLOG"; then COMPILED=1; break; fi
    log "compile with $M incomplete (exit $RC)"
  done
  (( COMPILED )) || jadd notes '"llmwiki compile incomplete, it will retry next run"'
  kill "$ADAPTER" 2>/dev/null
  "$LLMWIKI" lint >"$ROOT/logs/lint-$STAMP.log" 2>&1 || log "lint reported issues (logs/lint-$STAMP.log)"
fi

git add -A  # .gitignore keeps out .env, logs/, state/, node_modules/, collected/
# Wiki and reports come from untrusted forum text: flagged files are not committed.
QHITS=$(python3 tools/quarantine.py wiki --record; python3 tools/quarantine.py raw --record)
if [[ -n "$QHITS" ]]; then
  while IFS=$'\t' read -r QF QWHY; do
    [[ -z "$QF" ]] && continue
    git reset -q -- "$QF" 2>/dev/null
    log "QUARANTINED $QF ($QWHY)"
    jadd notes "\"quarantined ${QF#$ROOT/} ($QWHY)\""
  done <<<"$QHITS"
  git add QUARANTINE.md 2>/dev/null
fi
python3 - "$RUN_JSON" <<'EOF'
import json, subprocess, sys
p = sys.argv[1]; d = json.load(open(p))
out = subprocess.run(["git", "diff", "--cached", "--name-status", "--", "wiki"], capture_output=True, text=True).stdout
for line in out.splitlines():
    st, _, path = line.partition("\t")
    if path.endswith(".md") and not path.endswith(("index.md", "MOC.md")):
        (d["created"] if st.startswith("A") else d["updated"]).append(path)
json.dump(d, open(p, "w"))
EOF
if ! git diff --cached --quiet; then
  git -c user.name=research-loop -c user.email=research-loop@localhost commit -q \
    -m "research run $STAMP$( ((TEST)) && echo ' TEST')" && log "committed $(git rev-parse --short HEAD)"
  python3 -c 'import json,sys,subprocess;p=sys.argv[1];d=json.load(open(p));d["commit"]=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip();json.dump(d,open(p,"w"))' "$RUN_JSON"
fi

if (( DIGEST )); then
  if (( TEST )); then python3 tools/digest.py "$RUN_JSON" --prefix "TEST:"; else python3 tools/digest.py "$RUN_JSON"; fi
fi
log "run end, $(( ($(date +%s) - START) / 60 )) min"
