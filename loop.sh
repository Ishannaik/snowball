#!/usr/bin/env bash
# Persistent supervisor; scheduling and STOP state live in Python, not log text.
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${SNOWBALL_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON=python3
fi
exec "$PYTHON" "$ROOT/snowball.py" supervise --max-topics 1 --json "$@"
