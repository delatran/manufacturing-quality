#!/usr/bin/env sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
if [ ! -f .venv/bin/python ] || [ ! -f frontend/dist/index.html ]; then
  printf '%s\n' 'Run sh scripts/setup.sh first.' >&2
  exit 1
fi
exec .venv/bin/python -m analyst
