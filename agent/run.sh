#!/usr/bin/env bash
# Bootstrap a venv and run the agent. Pass extra args through, e.g. ./run.sh --once
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
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
