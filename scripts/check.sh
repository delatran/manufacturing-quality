#!/usr/bin/env sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

if [ ! -x .venv/bin/python ]; then
    printf '%s\n' 'Run sh scripts/setup.sh before checking the project.' >&2
    exit 1
fi

if [ "${1:-}" = '--fix' ]; then
    .venv/bin/python -m ruff format
    .venv/bin/python -m ruff check --fix
    (cd frontend && npm run format && npm run lint -- --fix)
elif [ "$#" -ne 0 ]; then
    printf '%s\n' 'Usage: sh scripts/check.sh [--fix]' >&2
    exit 1
fi

.venv/bin/python -m pip check
.venv/bin/python -m ruff format --check
.venv/bin/python -m ruff check
.venv/bin/python -m mypy
.venv/bin/python -m pytest tests -q
.venv/bin/python scripts/generate_contracts.py --check
.venv/bin/python -m scripts.generate_submission --check
(cd frontend && npm run check)
printf '%s\n' 'All local checks passed.'
