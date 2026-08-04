#!/usr/bin/env bash
# Import an mbox file into meerail. Pass the file, plus anything import_mbox.py
# takes:
#
#   tools/import-mbox.sh ~/Downloads/archive.mbox
#   tools/import-mbox.sh archive.mbox --account old@example.com --folder Archive
#   tools/import-mbox.sh --help
#
# This runs on the host and talks to Postgres and Tika over the ports compose
# publishes on loopback, exactly as the agent does — so the stack has to be up
# (./meerail.sh start). It reuses the agent's venv, since it needs the same
# dependencies: core's database/parsing stack plus the preview renderer.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
AGENT="$REPO/agent"

# Python 3.11+ is required. Override the interpreter if `python3` is too old:
#   PYTHON_INTERPRETER=/usr/bin/python3.13 tools/import-mbox.sh mail.mbox
PYTHON_INTERPRETER="${PYTHON_INTERPRETER:-python3}"

if [ ! -d "$AGENT/.venv" ]; then
  if ! command -v "$PYTHON_INTERPRETER" >/dev/null 2>&1; then
    echo "Error: Python interpreter '$PYTHON_INTERPRETER' was not found." >&2
    echo "Set PYTHON_INTERPRETER to a Python 3.11+ interpreter, e.g.:" >&2
    echo "  PYTHON_INTERPRETER=/usr/bin/python3.13 $0 $*" >&2
    exit 1
  fi
  if ! "$PYTHON_INTERPRETER" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    version="$("$PYTHON_INTERPRETER" --version 2>&1 || echo "unknown")"
    echo "Error: Python 3.11 or newer is required, but '$PYTHON_INTERPRETER' is $version." >&2
    exit 1
  fi
  "$PYTHON_INTERPRETER" -m venv "$AGENT/.venv"
  "$AGENT/.venv/bin/pip" install --quiet --upgrade pip
fi

# Re-install whenever requirements.txt changes, not just on first run — an
# existing venv would otherwise silently miss deps added by an upgrade. Same
# stamp file agent/run.sh uses, so the two never fight over the venv.
stamp="$AGENT/.venv/.requirements.sha"
if command -v sha256sum >/dev/null 2>&1; then
  want="$(sha256sum "$AGENT/requirements.txt" | cut -d' ' -f1)"
else
  # macOS ships shasum rather than GNU coreutils' sha256sum.
  want="$(shasum -a 256 "$AGENT/requirements.txt" | cut -d' ' -f1)"
fi
if [ "$(cat "$stamp" 2>/dev/null || true)" != "$want" ]; then
  "$AGENT/.venv/bin/pip" install --quiet -r "$AGENT/requirements.txt"
  echo "$want" > "$stamp"
fi

# `core` (and `app`, for the address-book rebuild) live at the repository root.
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

exec "$AGENT/.venv/bin/python" "$REPO/tools/import_mbox.py" "$@"
