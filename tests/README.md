# Tests

- **`test_parse.py`** — pure unit tests for the email parser (no server/DB).
- **`test_render.py`** — HTML sanitization / remote-image blocking (no server/DB).
- **`test_agent_sync_unit.py`** — cursor safety in the agent's sync loop, with
  `core.ingest` stubbed out (no server/DB).
- **`test_tasks_unit.py`** — parsing the Meerato private URL that "Add Task"
  posts to, the create-task request body including the Backlog + move-to-Now
  schedule "Send & Ticket" sends, and which destinations the server will fetch
  from at all (no server/DB/Meerato).
- **`test_proxy_unit.py`** — whose `X-Forwarded-For`/`-Proto` this server
  believes, which decides the session cookie's `Secure` flag and what the login
  rate limiter counts as one client (no server/DB).
- **`test_migration_unit.py`** — what the schema fixups in `init_db` may throw
  away: a column still holding on-disk paths is not one of them (DB, no server).
- **`test_api_bounds.py`** — what the read APIs do with input nobody would type
  on purpose (negative page sizes, a hundred-million-year search window, a PATCH
  field set to null): a 422 that says what to send, never a 500.
- **`test_sessions_db.py`** — who the password gate lets in, against the real
  session table: logging out ends the session for a copied cookie too, and the UI
  password is not an API credential (DB, no server).
- **`test_ingest.py`** — the ingest pipeline the agent owns: threading,
  cross-folder dedup, flag sync, vanished pruning, idempotent re-scan, account
  auto-registration, and Tika attachment extraction. Writes through
  `core.ingest` (exactly as the agent does) and asserts through the read API.
- **`test_search.py` / `test_contacts.py` / `test_actions.py` / `test_compose.py`**
  — read APIs, the action queue, and compose, seeded via `dbfixture`.
- **`test_undo.py`** — the Recent actions panel and Undo. The three outcomes are
  the three tests worth having, and only how far the queued move has got tells
  them apart: not applied yet (a true undo — the queue row goes and the
  placements come back), the agent holding it or the move just landed (refused,
  because there is no UID to address), and applied and synced (a move back).
  Plus what a label server does to all three: a message whose only placement is
  \All has no reverse move, and it is skipped rather than costing the rest of
  the conversation its undo.
- **`test_greenmail.py`** — drives the real `meerail-agent` binary against a live
  GreenMail IMAP server end-to-end (backfill, prune, flag write-back).

Integration tests **skip themselves** when their backing service isn't up, so
`pytest` is safe to run in any state.

## Run

```bash
python3 -m venv .venv-test
.venv-test/bin/pip install -r tests/requirements.txt

make test
```

`make test` is the whole story: it discards the test volume, brings up an
isolated stack (`docker-compose.test.yml`, compose project `meerail-test`) on
shifted ports, runs the suite against it, and tears it down. Every run therefore
starts on a freshly `initdb`'d cluster.

| service | production | test |
| --- | --- | --- |
| database | `meerail` @ 5432 | `meerail_test` @ 55432 |
| server | 8000 | 18000 |
| tika | 9998 | 59998 |

Pass pytest flags through with `PYTEST_ARGS`:

```bash
make PYTEST_ARGS="-v -k search" test
```

To iterate without paying for a stack rebuild each time, leave it up — the
session fixture truncates every table at the start of each run, so repeated runs
still start clean:

```bash
make test-up
DATABASE_URL=postgresql+psycopg://meerail:meerail@127.0.0.1:55432/meerail_test \
MEERAIL_URL=http://127.0.0.1:18000 \
TIKA_URL=http://127.0.0.1:59998 \
.venv-test/bin/pytest tests/ -v
make test-down
```

Optional: the GreenMail test needs GreenMail + the agent venv.

```bash
docker run -d --name greenmail -p 3143:3143 -p 3025:3025 \
  -e GREENMAIL_OPTS='-Dgreenmail.setup.test.all -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled' \
  greenmail/standalone:2.1.0
(cd agent && ./run.sh --once)   # creates agent/.venv
```

### Why it refuses to run bare

The suite writes through `core.ingest` and **truncates every table it can
reach**. Its old defaults (`localhost:5432` / `localhost:8000`) were the
production stack, so a bare `pytest tests/` seeded test accounts into real mail.

`conftest.py` now aborts the run before collection unless *both*:

- `DATABASE_URL` names a database ending in `_test`, and
- the server at `MEERAIL_URL` reports a `_test` database from `/healthz`.

The second check matters on its own — without it the suite would happily
truncate the test database while asserting against production. Override with
`MEERAIL_ALLOW_DIRTY_DB=1` only if you mean it.
