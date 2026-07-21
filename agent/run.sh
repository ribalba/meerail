#!/usr/bin/env bash
# Bootstrap a venv and run the agent. Pass extra args through, e.g. ./run.sh --once
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

# The agent shares the `core` package with the server, which lives at the repo
# root — put it on the path alongside this directory.
export PYTHONPATH="$(cd .. && pwd)${PYTHONPATH:+:$PYTHONPATH}"

exec .venv/bin/python main.py "$@"
