#!/usr/bin/env bash
# Bootstrap a venv and run the agent. Pass extra args through, e.g. ./run.sh --once
set -euo pipefail
cd "$(dirname "$0")"

# Python 3.11+ is required. Override the interpreter if `python3` is too old:
#   PYTHON_INTERPRETER=/usr/bin/python3.13 ./run.sh
PYTHON_INTERPRETER="${PYTHON_INTERPRETER:-python3}"

if ! command -v "$PYTHON_INTERPRETER" >/dev/null 2>&1; then
  echo "Error: Python interpreter '$PYTHON_INTERPRETER' was not found." >&2
  echo "Set PYTHON_INTERPRETER to a Python 3.11+ interpreter, e.g.:" >&2
  echo "  PYTHON_INTERPRETER=/usr/bin/python3.13 $0 $*" >&2
  exit 1
fi

if ! "$PYTHON_INTERPRETER" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  version="$("$PYTHON_INTERPRETER" --version 2>&1 || echo "unknown")"
  echo "Error: Python 3.11 or newer is required, but '$PYTHON_INTERPRETER' is $version." >&2
  echo "Set PYTHON_INTERPRETER to a Python 3.11+ interpreter, e.g.:" >&2
  echo "  PYTHON_INTERPRETER=/usr/bin/python3.13 $0 $*" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  "$PYTHON_INTERPRETER" -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
fi

# Re-install whenever requirements.txt changes, not just on first run — an
# existing venv would otherwise silently miss deps added by an upgrade.
stamp=.venv/.requirements.sha
if command -v sha256sum >/dev/null 2>&1; then
  want="$(sha256sum requirements.txt | cut -d' ' -f1)"
else
  # macOS ships shasum rather than GNU coreutils' sha256sum.
  want="$(shasum -a 256 requirements.txt | cut -d' ' -f1)"
fi
if [ "$(cat "$stamp" 2>/dev/null || true)" != "$want" ]; then
  .venv/bin/pip install --quiet -r requirements.txt
  echo "$want" > "$stamp"
fi

# The agent shares the `core` package with the server, which lives at the repo
# root — put it on the path alongside this directory.
export PYTHONPATH="$(cd .. && pwd)${PYTHONPATH:+:$PYTHONPATH}"

exec .venv/bin/python main.py "$@"
