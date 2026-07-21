# Tests

- **`test_parse.py`** — pure unit tests for the email parser (no server/DB).
- **`test_agent_protocol.py`** — black-box integration over the `/api/agent/*`
  HTTP protocol: threading, cross-folder dedup, flag sync, vanished pruning,
  idempotent re-scan. Uses throwaway accounts.
- **`test_greenmail.py`** — drives the real `meerail-agent` against a live
  GreenMail IMAP server end-to-end (backfill + prune).

Integration tests **skip themselves** when their backing service isn't up, so
`pytest` is safe to run in any state.

## Run

```bash
# from the repo root, in a venv with the server deps + pytest:
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r tests/requirements.txt

# the protocol tests need the server:
docker compose up -d

# optional: the GreenMail test needs GreenMail + the agent venv:
docker run -d --name greenmail -p 3143:3143 -p 3025:3025 \
  -e GREENMAIL_OPTS='-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled' \
  greenmail/standalone:2.1.0
(cd agent && ./run.sh --once)   # creates agent/.venv

.venv/bin/pytest tests/ -v
```

Point the integration tests at a non-default server with `MEERAIL_URL=http://host:8000`.
