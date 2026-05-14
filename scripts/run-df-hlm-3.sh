#!/bin/bash
# DF-HLM-3 K16 Wrapper [CRUX-MK]

set -euo pipefail

LOCK_DIR="${DF_HLM_3_LOCK_DIR:-/tmp/df-hlm-3.lock}"
LOCK_AGE_LIMIT_S="${DF_HLM_3_LOCK_AGE_LIMIT_S:-21600}"
ENGINE_PGREP_CHECK="${DF_HLM_3_ENGINE_PGREP_CHECK:-true}"

if [ -d "$LOCK_DIR" ]; then
  LOCK_MTIME=$(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0)
  NOW=$(date +%s)
  LOCK_AGE_S=$(( NOW - LOCK_MTIME ))
  if [ "$LOCK_AGE_S" -gt "$LOCK_AGE_LIMIT_S" ]; then
    rm -rf "$LOCK_DIR"
  fi
fi

if [ "$ENGINE_PGREP_CHECK" = "true" ] && pgrep -f "src/konkurrenz_tracker.py" >/dev/null 2>&1; then
  echo "[K16-VETO] Existing DF-HLM-3 engine process detected" >&2
  exit 3
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[K16-VETO] Concurrent DF-HLM-3 instance detected" >&2
  exit 3
fi

echo "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

PYTHON="${DF_HLM_3_PYTHON:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
exec "$PYTHON" src/konkurrenz_tracker.py "$@"
