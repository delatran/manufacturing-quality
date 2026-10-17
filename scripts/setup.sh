#!/usr/bin/env sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
requirements_lock='requirements-dev.lock'
if [ "${1:-}" = '--runtime-only' ]; then
    requirements_lock='requirements.lock'
elif [ "$#" -ne 0 ]; then
    printf '%s\n' 'Usage: sh scripts/setup.sh [--runtime-only]' >&2
    exit 1
fi
"${PYTHON:-python3}" -c "import sys; assert sys.version_info[:2] == (3, 12), 'Use Python 3.12 for the committed dependency locks.'"
[ -f .env ] || cp .env.example .env
"${PYTHON:-python3}" -m venv .venv
.venv/bin/python -m pip install --require-hashes -r "$requirements_lock"
.venv/bin/python -m pip check
(cd frontend && npm ci && npm run build)
printf '%s\n' 'Setup complete. Run sh scripts/start.sh.'
