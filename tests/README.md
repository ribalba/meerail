# Tests

- **`test_parse.py`** — pure unit tests for the email parser (no server/DB).
- **`test_render.py`** — HTML sanitization / remote-image blocking (no server/DB).
- **`test_agent_sync_unit.py`** — cursor safety in the agent's sync loop, with
  `core.ingest` stubbed out (no server/DB).
- **`test_ingest.py`** — the ingest pipeline the agent owns: threading,
  cross-folder dedup, flag sync, vanished pruning, idempotent re-scan, account
  auto-registration, and Tika attachment extraction. Writes through
  `core.ingest` (exactly as the agent does) and asserts through the read API.
- **`test_search.py` / `test_contacts.py` / `test_actions.py` / `test_compose.py`**
  — read APIs, the action queue, and compose, seeded via `dbfixture`.
- **`test_greenmail.py`** — drives the real `meerail-agent` binary against a live
  GreenMail IMAP server end-to-end (backfill, prune, flag write-back).

Integration tests **skip themselves** when their backing service isn't up, so
`pytest` is safe to run in any state.

## Run

The suite spans both halves of the app, so it needs the union of their deps
(`tests/requirements.txt`) — `core` to ingest the way the agent does, plus the
server's `nh3` for the rendering tests.

```bash
# from the repo root:
python3 -m venv .venv-test
.venv-test/bin/pip install -r tests/requirements.txt

# the integration tests need the server + database:
docker compose up -d

# optional: the GreenMail test needs GreenMail + the agent venv:
docker run -d --name greenmail -p 3143:3143 -p 3025:3025 \
  -e GREENMAIL_OPTS='-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled' \
  greenmail/standalone:2.1.0
(cd agent && ./run.sh --once)   # creates agent/.venv

.venv-test/bin/pytest tests/ -v
```

Because tests write through `core`, they need the database directly: they read
`DATABASE_URL` (default `postgresql+psycopg://meerail:meerail@localhost:5432/meerail`,
which is what compose publishes on loopback). Point the read-side at a non-default
server with `MEERAIL_URL=http://host:8000`.
