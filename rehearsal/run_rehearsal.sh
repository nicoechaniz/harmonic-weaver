#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REHEARSAL_PYTHON="${REHEARSAL_PYTHON:-$REPO_DIR/.venv/bin/python}"

if [[ ! -x "$REHEARSAL_PYTHON" ]]; then
    echo "[FAIL] Missing rehearsal Python: $REHEARSAL_PYTHON" >&2
    echo "Run: uv sync --extra rehearsal --extra test" >&2
    exit 2
fi

if ! PYTHONPATH="$REPO_DIR/src:$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}" "$REHEARSAL_PYTHON" -c \
    'import fastapi, numpy, scipy, soundfile, uvicorn, websockets, pythonosc' \
    >/dev/null 2>&1; then
    echo "[FAIL] Rehearsal dependencies are incomplete in $REHEARSAL_PYTHON" >&2
    echo "Run: uv sync --extra rehearsal --extra test" >&2
    exit 2
fi

export REHEARSAL_PYTHON
export PYTHONPATH="$REPO_DIR/src:$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec "$REHEARSAL_PYTHON" -m rehearsal.runner "$@"
