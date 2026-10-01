#!/usr/bin/env bash
# Create/reuse a virtualenv with GOHAN's dependencies.
#
#   scripts/bootstrap.sh          # install runtime deps
#   scripts/bootstrap.sh dev      # + test/lint tools
#
# Safe to re-run; it only installs when something is missing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="${VENV:-.venv}"
PYTHON="${PYTHON:-python3}"
EXTRA="${1:-}"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "→ creating $VENV with $PYTHON"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip setuptools wheel
fi

if ! "$VENV/bin/python" -c "import aiogram, aiosqlite, aiohttp" 2>/dev/null; then
  echo "→ installing runtime dependencies"
  "$VENV/bin/pip" install -q -r requirements.txt
fi

if [[ "$EXTRA" == "dev" ]] && ! "$VENV/bin/python" -c "import pytest" 2>/dev/null; then
  echo "→ installing dev dependencies"
  "$VENV/bin/pip" install -q pytest pytest-asyncio ruff
fi

"$VENV/bin/python" - <<'PY'
import importlib
missing = []
for name in ("aiogram", "aiosqlite", "aiohttp", "pydantic_settings", "richgram"):
    try:
        importlib.import_module(name)
    except Exception:
        missing.append(name)
try:
    importlib.import_module("pyrogram")
except Exception:
    missing.append("kurigram")
print("ready" if not missing else f"missing: {', '.join(missing)}")
PY
