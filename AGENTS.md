# meerail: grounding for coding agents

Read this before changing anything. It covers what meerail is, how the pieces
fit together, the rules that protect the user's mail, and how to test without
touching a real mailbox. [README.md](README.md) is the user manual; this file is
about the code. Where the two disagree, the code wins (known drift is listed at
the end).

## What meerail is

A self-hosted email client for power users (AGPL-3.0, `github.com/ribalba/meerail`,
version in [`VERSION`](VERSION)). It mirrors one or more IMAP accounts into
PostgreSQL: raw MIME, attachment bytes, and the text Apache Tika extracts from
attachments (OCR included). A three-pane web UI, in a browser or the Electron
wrapper, reads that database and offers:

- POSIX-regex and keyword search over everything, attachment text included
- conversation threading and a unified inbox
- two-way sync: read, flag, move, delete and send go back to the mail server
- reminders ("remind me later"), undo, an Outbox with send delay
- analytics, a Cleanup panel for bulk mail, "Add Task" to Meerato
- optional AI helpers (Anthropic, OpenAI, or any OpenAI-compatible endpoint)

Proton Mail Bridge shaped the design, because it only listens on `127.0.0.1`.
Any plain IMAP/SMTP account (Gmail, Fastmail, Dovecot) works the same way.

## Architecture

```
  IMAP/SMTP server (Proton Bridge, Gmail, ...)
          ^
          | IMAP + SMTP (only the agent ever connects)
  +-------+-----------+   writes mail    +------------------+   reads, and writes    +----------------+
  | agent/  + core/   | ---------------> | PostgreSQL       | <--------------------- | app/  + core/  | <-- browser /
  | sync, parse,      | <--------------- | mail, blobs,     |   user actions +       | FastAPI + SPA  |     Electron
  | thread, index,    |  pending_actions | queue, cursors   |   pending_actions      +----------------+
  | drain the queue   |                  +------------------+
  +-------+-----------+
          | HTTP (attachment bytes)
          v
        Tika
```

- **The two processes share nothing but Postgres.** Neither calls the other over
  HTTP, and there is no shared filesystem.
- **`agent/`** is the only code that speaks IMAP, SMTP or Tika. It owns the whole
  write path for mail (fetch, parse, thread, store, extract, prune), and it
  drains the `pending_actions` queue back to the server. It runs next to the
  mail server's loopback, holds the mail passwords, and is stateless: its cursors
  live in the database.
- **`app/`** (the "server") never fetches, parses or sends mail. It reads the
  database, applies a user's action to local rows at once (optimistically), and
  queues a `PendingAction` for the agent to apply to IMAP/SMTP.
- **`core/`** is the shared library: schema, migrations, config, ingest,
  parsing, threading, the Tika client, outbox and undo helpers, events.
- **Postgres LISTEN/NOTIFY** carries the live traffic:
  - `meerail_events`: "something changed" hints. The server holds one LISTEN
    connection and fans the events out to browsers over SSE (`/api/stream`).
    They are cache-invalidation hints only, never message content, and are
    capped at 4000 bytes.
  - `meerail_commands`: server to agent, e.g. `refresh`. Advisory: with no
    agent running it is simply dropped. A request that must survive an agent
    restart is a column instead (`accounts.recheck_requested`).
- **Optional pieces:**
  - `journal/`: a standalone server that keeps several installs in agreement
    about reminders and account presentation. It stores only ciphertext.
  - `electron/`: a thin desktop wrapper.
  - `website/`: the static marketing site.
  - `tika/`: the custom Tika image and its configuration.

## Repository map

| Path | What it is |
| --- | --- |
| `core/models.py` | **The schema. Read it first.** Every column carries a comment explaining why it exists. |
| `core/database.py` | Engine and per-process pool sizing, and `init_db()`: `create_all` plus the idempotent migration statements. |
| `core/config.py` | `Settings`: reads `meerail.toml` plus environment variables. |
| `core/ingest.py` | The agent's write path: folders, cursors, storing, flags, pruning, Tika extraction, thumbnails. |
| `core/mail/` | `parse.py` (MIME to `ParsedEmail`), `store.py` (rows, dedup, placements), `threading.py`, `rethread.py`, `tika.py`, `thumbs.py`. |
| `core/outbox.py`, `core/undo.py` | Send-queue timing and undo snapshots, shared by agent and server. |
| `core/events.py` | NOTIFY publishing. |
| `core/searchindex.py`, `core/bodysig.py` | Background backfills for `search_tsv` and `body_sig`. |
| `agent/main.py` | Agent entry point: starts the threads, handles `--once`, `--test`, `--requeue-abandoned`, `--backfill-previews`. |
| `agent/sync.py` | The sync pass (`sync_once`), the per-account loop, and the indexer thread. |
| `agent/actions.py` | Drains the action queue: leasing, applying, retrying, settling. |
| `agent/imap.py` | The `Bridge` wrapper: a socket watchdog, IDLE, and server quirks. |
| `agent/smtp.py`, `commands.py`, `preflight.py` | SMTP send, the LISTEN on `meerail_commands`, and the `--test` checks. |
| `agent/imaplib_compat.py` | A patch for Python 3.14+; it must be imported before `imapclient`. |
| `agent/run.sh`, `mac_service.sh` | Native launcher (builds the venv) and the macOS launchd wrapper. |
| `app/main.py` | The FastAPI app: lifespan tasks, middleware, router registration, static files. |
| `app/routers/*.py` | One file per API area (see the table under Server conventions). |
| `app/mailops.py` | Every mail action: its local state change and its queue row. New actions go here. |
| `app/deps.py`, `sessions.py`, `transport.py` | Auth and HTTPS enforcement. |
| `app/limits.py`, `nethost.py`, `security.py` | Request body limits, the SSRF guard, Fernet secrets. |
| `app/workers.py` | Background loops: contacts, reminders, search index, body signatures, journal. |
| `app/mail/render.py` | nh3 HTML sanitizing, remote-content blocking, `cid:` rewriting. |
| `app/searchquery.py` | Search syntax parser. |
| `app/llm.py`, `aiprompts.py`, `threadtext.py` | AI providers, prompts, and conversation flattening. |
| `app/grammar.py` | Grammar and spelling checks: the settings row, UTF-16 segment offsets, the LanguageTool client, and the privacy guard that refuses a public `grammar.url`. |
| `app/static/` | The SPA: `index.html`, `css/mail.css`, `js/app.*.js`. |
| `tests/` | The pytest suite: `conftest.py` (safety guard), `dbfixture.py` (seeding), `helpers.py` (HTTP). |
| `tools/` | `import_mbox.py` (+ `import-mbox.sh`), plus four maintenance scripts that are dry-run unless given `--apply`: `file_sent.py`, `migrate_blobs.py`, `reparse_forwards.py`, `restore_pending.py`. |
| `journal/` | The standalone journal server. `app/journal.py` and `core/journal.py` are its client. |
| `tika/` | Tika image. The reasoning behind `tika-config.json` lives in `tika/README.md`, because the JSON cannot hold comments. |
| `electron/` | Desktop wrapper. `electron/main.js` is the real file; the root `main.js` is a byte-identical stray copy. |
| `website/` | Static site. `screenshots/` holds a demo seeder and a Playwright shooter; `public/downloads/SKILL.md` documents the schema for LLMs querying mail with SQL. |
| `meerail.sh` | End-user installer and manager (no clone needed). Works in `~/.meerail`. |
| `meerail.example.toml` | The annotated reference for every config key. |

## Data model essentials

- **Content is stored once per `(account_id, dedup_key)`.**
  - `dedup_key` is the canonical Message-ID, or a synthesized hash when there
    is none.
  - Identity is decided by the bytes: `content_hash` is the sha256 of the
    CRLF-normalised message.
  - If a new message shares a `dedup_key` but not the content
    (`core/mail/store.py::same_message`), it is stored under its content key
    and gets a thread of its own.
  - Never decide that two messages are the same from headers alone. The sync
    pass fetches every UID for exactly this reason.
- **`message_locations`** holds one row per folder placement: mailbox, IMAP
  UID, and flags. Proton exposes labels as folders, so one message often has
  several placements. Flags live on placements, not on messages.
- **Pending placements.** A move writes the target placement at once, with
  `imap_uid = -message_pk` (`store.pending_uid`). The agent's next sync replaces
  it with the real one once the move has landed.
- **`mailboxes`** is an IMAP folder plus its sync cursor (`uidvalidity`,
  `last_uid`), and also carries `missing_since`, `local` and `role`.
- **`accounts.local`** marks an imported account that no agent syncs. The server
  applies actions to it directly instead of queueing them.
- **`pending_actions`** is the queue from server to agent.
  - Types: `setflags | move | delete | create_folder | send | save_sent`. The
    last is queued by the agent itself, after a send, on servers that do not
    file a copy of sent mail on their own.
  - Statuses: `pending | leased | held | stale | refused | undone | done`. There
    is also a legacy `error`, which only `--requeue-abandoned` touches.
  - The JSONB `payload` carries the UID together with its `uidvalidity`, the
    undo keys (`op_id`, `undo_from`), and `not_before` for delayed sends.
- **`outbound`** holds composed mail in states `draft | queued | held | sent`.
  The server builds `raw_mime`; a `send` action tells the agent to relay it.
- **`messages.content_status`** is `full | skipped | pruned`, driven by the
  content window.
- **Search columns.** `search_text` has a trigram GIN index and serves regex
  search. `search_tsv` is a suffix tsvector filled by a database trigger, and
  serves keyword search.
  - `raw_mime`, `search_text` and `search_tsv` are deferred. Do not undefer them
    in list or thread queries: `raw_mime` holds attachments base64-encoded, and
    loading it for a thread once stalled the reader for about 17 s.
- **`models.still_filed()`** must be applied by every read path (it appears as
  `_not_deleted` in `messages.py`). That includes AI, Meerato and search.
  Otherwise mail the user deleted stays reachable until the agent collects the
  row.
- **Time is naive UTC everywhere** (`models.utcnow()`); convert at the edges. In
  the browser, parse API dates with `App.utcDate`.
- **Other tables:** `threads`, `recipients`, `attachments` (bytes,
  `extracted_text`, `thumb`), `contacts` and `contact_pairs`, `reminders`,
  `settings` (key/value), `ui_sessions`, `journal_outbox`, `journal_deferred`.

### Changing the schema

There is no Alembic. Both the server and the agent run `init_db()` at startup.

1. Add the column to the model in `core/models.py`. This covers fresh
   databases through `create_all`.
2. Append an idempotent statement to the tuple in `init_db()`
   (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).
   This is how existing volumes upgrade in place.
3. Each statement runs through `_run_migration` in its own transaction, with a
   short `lock_timeout` and jittered retries.
   - `_already_applied` checks the catalog first, so an ordinary boot takes no
     locks. Keep new statements in the shapes its regexes recognise.
   - This machinery exists because the agent holds read locks for seconds at a
     time.
4. Guard one-time data backfills (see the `_column_exists` pattern) so they do
   not run again on every boot.
5. Never drop a column that may hold the only copy of something (see
   `_retire_disk_columns`). There is no downgrade path.
6. If the change affects what `website/public/downloads/SKILL.md` documents,
   update that file too.

## Key flows

### Incoming mail

`agent/sync.py::sync_once` runs one thread per account and IDLEs on INBOX
between passes. One pass:

1. Drain the action queue first.
2. LIST the folders.
3. For each folder:
   1. SELECT it.
   2. `register_folder`. A new UIDVALIDITY resets the cursor and nothing else.
   3. Fetch the UIDs above `last_uid` in batches. Each message comes in whole
      or as headers only, depending on INTERNALDATE against the content window
      and on `max_message_bytes`.
   4. `store_message`, then advance the cursor after each committed chunk and
      publish the event after the commit.
4. Every `reconcile_interval`, reconcile: SEARCH ALL, update flags, and prune
   vanished UIDs only if the UID list is complete.
5. `prune_mailboxes`, which has a one-hour grace period.
6. `delete_orphan_messages`, then `record_sync`.

A separate indexer thread runs Tika extraction, thumbnails and content-window
pruning.

### A user action

1. The route (e.g. `app/routers/actions.py`) calls `app/mailops.py`.
2. `mailops` changes the placements now and writes a `PendingAction` carrying
   the UID and its `uidvalidity`.
3. It commits, calls `wake_agent` (NOTIFY `meerail_commands`), and calls
   `events.publish`.
4. On the agent side, `drain_actions` leases the row: `SELECT ... FOR UPDATE
   SKIP LOCKED`, then commits `status='leased'` before sending any IMAP or SMTP
   command.
5. It checks the live UIDVALIDITY, applies the action, and settles the row.

### Sending mail

`app/routers/compose.py` builds the MIME and writes an `Outbound` row (state
`queued`) plus a `send` action, with `not_before` when a send delay is set. The
draft is deleted in the same commit, and the agent relays the message over SMTP.
The Outbox can retry a send, cancel it (`held`), or discard it, but never while
it is leased.

Once the server has taken the message, the agent queues a `save_sent` action
for it, unless the server files its own copy of sent mail. Proton Bridge and
Gmail do, and are recognised from the session itself (`_server_files_sent`:
the `\Noselect` "Folders" node, the `X-GM-EXT-1` capability); a plain
IMAP/SMTP server keeps nothing. The action APPENDs the outbound MIME into the
folder with role `sent`, `\Seen`, dated when it was sent, after searching the
folder for the Message-ID so a retry never files it twice. It is a row of its
own so that a failed copy can never reopen the send (which would send the mail
twice). `save_sent` on the account config overrides the server reading.
`tools/file_sent.py` queues the same action for mail sent before this existed.

### Search

1. `app/searchquery.py` parses the filters: `:unread`, `:read`,
   `:has-attachment`, `:no-trash`, `:from`, `:to`, `:similar`.
2. `app/routers/search.py` then runs one of two modes, under a 20 s statement
   timeout:
   - Regex mode: `search_text ~* pattern`, served by the trigram index.
   - Keyword mode: `search_tsv @@` a prefix tsquery, with an ILIKE recheck.
     Until the `search_tsv` backfill has finished, everything goes through ILIKE.
3. The syntax exists in several places that must stay in step: `searchquery.py`,
   `routers/search.py`, `aiprompts.SEARCH_SYSTEM`, the in-app help, and
   `keyword_terms`, which is mirrored in `app/static/js/app.highlight.js` and
   tested by `test_highlight_terms_unit.py`.

### Reminders, undo, and the journal

- **Reminders.** The server holds the clock (`app/workers.py` calls
  `app/reminders.py`). Parking a conversation and bringing it back are both
  ordinary queued moves.
- **Undo.** `core/undo.py` and `app/routers/undo.py`, with one `op_id` per
  keypress. The outcome depends on how far the move has got:
  - Not applied yet: the row is marked `undone` and the snapshot is restored.
  - Applied and synced: a reverse move is queued.
  - Anything in between: the undo is refused.
- **Journal.** When configured, installs publish sealed records. The lowest
  journal sequence number wins the right to fire a due reminder. The journal
  never moves mail by itself.

### Imported accounts

`tools/import_mbox.py` creates a `local` account. For these accounts:

- nothing is queued, because no agent exists to drain the queue;
- folders are created, moved into and deleted immediately;
- "Delete permanently" deletes the rows themselves.

## Invariants: do not break these

The whole product rests on the promise that mail is never lost. Every one of
these rules exists because something went wrong without it. The README section
"What meerail deletes, and when" is the behavioural spec.

1. **A failed, short or partial answer never deletes anything.**
   - Vanished UIDs are pruned only when the UID list is at least as long as
     SELECT's EXISTS count.
   - An empty LIST is never evidence of anything.
   - A folder must be missing from LIST for an hour before it is removed.
   - Local folders are never pruned.
2. **A UIDVALIDITY change resets the cursor.** It never deletes mail.
3. **The cursor never steps over mail that has not been fetched and committed.**
4. **Queued work is never dropped because it keeps failing.** A row ends only in
   success, `stale` (the UIDVALIDITY epoch changed), `refused` (a permanent
   server refusal), or a user decision. There is no attempt cap and no expiry.
5. **A UID always travels with its `uidvalidity`.** The agent checks it against
   the live folder before acting, with no fallback.
6. **Only an action that says so destroys mail:** Empty Trash, Delete in Trash,
   Delete permanently, or folder delete (the last two on imported accounts
   only). Each needs `confirm`.
   - Delete, Trash and Archive are moves.
   - A missing Trash folder means a refusal, never an expunge. Imported
     accounts are the exception: meerail creates a Trash folder for them.
7. **Expunge only with `UID EXPUNGE`, never a bare EXPUNGE.**
   - Prefer IMAP MOVE.
   - Fall back to COPY plus removal only after verifying that the copy landed.
   - When in doubt, leave a copy behind.
8. **A send's lease is committed before the first SMTP command.** A leased send
   cannot be cancelled or re-queued, because either would risk sending it twice.
9. **Never queue actions for `local` accounts.** Apply them directly.
10. **Deleted mail is invisible.** Filter every read path with `still_filed()`.
11. **Keep the architecture split.** The server never speaks IMAP or SMTP.
    Events are hints, published after the commit.
12. **JSONB columns (`payload`, `sync_progress`) are replaced, not mutated in
    place.** An in-place edit is committed as no change at all.
13. **Agent write-back goes through `bridge.ops()`, never `bridge.client`.**
    Using the client directly bypasses the socket watchdog.
14. **Secrets never reach the browser.** AI keys and the Meerato token are
    Fernet-encrypted with `secret_key` and are never returned by the API.
15. **Server-side fetches of user-typed URLs go through `app/nethost.py`.**
    That module is the SSRF guard: it blocks private addresses and pins DNS.
16. **Keep the HTML safety layers.**
    - The CSP forbids inline script: no `<script>` blocks, no `on*=` attributes.
    - Mail HTML is sanitised by nh3 on the server.
    - It is rendered in an iframe sandboxed without `allow-scripts`.
    - Remote content is blocked by default.
17. **With `server.password` set, plaintext requests from non-loopback
    addresses are refused.**
    - Body limits (`app/limits.py`) apply before authentication.
    - `api_token` is deliberately not the UI password.

## Server conventions

- **New router.** Create it with
  `APIRouter(prefix="/api/x", tags=["x"], dependencies=[Depends(require_ui_auth)])`
  and register it in `app/main.py`.
  - Handlers are mostly plain `def` (they run in the threadpool) and take
    `db: Session = Depends(get_db)`.
  - Nothing auto-commits: call `db.commit()` yourself.
- **Errors.** Raise `HTTPException` with a human sentence as `detail`:
  - 400: refused or invalid operation
  - 404: missing, including deleted mail
  - 409: conflict, in flight, or revision mismatch
  - 422: validation. Validate at the edge; odd input must give a 422, never a 500.
- **Route order.** Declare fixed paths before `/{id}` paths. For example, bulk
  routes come before `/{message_id}/...` in `actions.py`.
- **Structure.** Put logic in FastAPI-free modules where you can (like
  `staging`, `syncstate`, `transport`), so it can be unit-tested without the
  server.
- **Operator setting.**
  1. Add a field to `Settings` in `core/config.py`.
  2. Map it in `_SECTION_KEYS`.
  3. Document it in `meerail.example.toml` and in the README configuration table.

  Precedence is `env > .env > meerail.toml > default`, and the environment
  variable is the upper-cased field name. `MEERAIL_CONFIG=""` means
  environment only.
- **UI setting.** Store it as a row in the `settings` table, owned and
  validated by its router. Encrypt credentials with `encrypt_secret`.
- **Background work.** Add a loop in `app/workers.py`, spawned through `_spawn`
  in the `main.py` lifespan. Loops catch, log and continue.

| Router | Prefix | Owns |
| --- | --- | --- |
| `auth` | `/api/auth` | status, login, logout (ungated) |
| `accounts` | `/api/accounts` | label, colour, footer; delete. The agent creates accounts. |
| `mailboxes` | `/api/mailboxes` | sidebar tree, create/favorite/delete folder |
| `messages` | `/api` | list, detail, thread, source, attachments, thumbs |
| `actions` | `/api/messages` | mark, flag, move, trash, archive, bulk, empty trash, purge |
| `compose` | `/api/compose` | staging attachments, send, drafts |
| `grammar` | `/api/grammar` | config, languages, check, ignore |
| `outbox` | `/api/outbox` | unsent mail, send delay, retry/cancel/discard |
| `reminders` | (none) | `/api/messages/{id}/remind`, `/api/reminders` |
| `search` | `/api` | `/api/search` |
| `contacts`, `analytics`, `cleanup` | `/api/...` | autocomplete, stats, bulk-mail clusters |
| `sync` | `/api/sync` | refresh, recheck, agent status |
| `tasks`, `ai` | `/api/tasks`, `/api/ai` | Meerato, LLM features |
| `undo` | `/api/actions` | recent operations, undo |
| `stream`, `version` | `/api/stream`, `/api/version` | SSE, update check |

## Frontend conventions

- **Plain browser JavaScript.** No build step, no framework, no third-party
  script, no npm. Classic `<script>` tags in `app/static/index.html`, whose
  order matters.
- **Module shape.** Each module is an IIFE assigned to `window.App.<name>`.
  - `app.core.js` provides `App.api` (one method per endpoint, with 401 replay
    and undo recording), `App.esc`, `App.icon`, `App.utcDate` and the
    formatters.
  - `app.shell.js::boot()` calls every `init()` and opens the SSE stream.
- **Adding a feature:**
  1. Put the markup in `index.html`, hidden.
  2. Write `app.<name>.js` with an `init()`.
  3. Add its `<script>` tag in dependency order.
  4. Call its `init()` from `boot()`.
  5. Add an `App.api` method for each endpoint it uses.
  6. Add any icon to `ICON_PATHS`.
- **Escaping.** Everything written into `innerHTML` goes through `App.esc`.
- **Power save.** Timers and streams register with `App.power`
  (`whenSuspended` / `whenResumed`).
- **`[hidden]` has no global rule.** A class that sets `display` needs its own
  `.x[hidden]{display:none}`.
- **Theming.** Colour tokens live on `:root` as `light-dark()` pairs. Mail
  bodies always render light.
- **Keyboard.** The `SHORTCUTS` table in `app.keys.js` is the single source; it
  also renders the cheat sheet.
- **Mobile.** The 900px breakpoint appears in three places that must agree:
  `mail.css`, `app.mobile.js` and `app.swipe.js`.
- **No safety net.** There are no JS tests and no linter, and a syntax error in
  any module takes down the whole app.
- **Static files are baked into the server image.** Only
  `docker-compose.dev.yml` bind-mounts `./app`. The server sends `no-cache`,
  but a long-open tab still runs the old code.

## Running and testing

### The test guard: read this

The suite TRUNCATEs every table in the database at `DATABASE_URL` when a session
starts. The fixture is autouse, so this happens even for pure unit tests.

- **Run the tests with `make test`.** It brings up the throwaway compose project
  `meerail-test`, runs the suite, and tears the project down afterwards. Pass
  arguments with `make PYTEST_ARGS="-v -k search" test`.

  | Service | Test stack |
  | --- | --- |
  | database `meerail_test` | 55432 |
  | server | 18000 |
  | Tika | 59998 |

- **To iterate:**
  1. `make test-up`
  2. Run `.venv-test/bin/pytest` with the variables the Makefile's `test`
     target sets: `DATABASE_URL`, `MEERAIL_URL`, `TIKA_URL`,
     `SECRET_KEY=test-insecure`, `MEERAIL_CONFIG=`.
  3. `make test-down`
- **Never set `MEERAIL_ALLOW_DIRTY_DB=1`.** It disables the guard, and it has
  already wiped a real mailbox once.
- **Never run `docker compose -f docker-compose.test.yml ...` without
  `-p meerail-test`.** The project name then defaults to `meerail`, which is the
  production stack. Use the `make test-*` targets.
- **`.venv-test` cannot import `app.main` or most routers**, because it lacks
  `python-multipart` and `sse-starlette`. Verify server code over HTTP against
  the test stack, not by importing it.

### Writing tests

- **Seeding.** Seed through `tests/dbfixture.py`, which calls `core.ingest`
  exactly as the agent does.
- **Calling the server.** Use `tests/helpers.py::api(method, path, body)`,
  which returns `(status, json)`.
- **Fixtures.** `account` and `local_account` skip the test when the server is
  not up, and clean up after themselves.
- **Agent tests.** Agent unit tests put `agent/` on `sys.path` themselves (see
  `test_agent_sync_unit.py`).
- **GreenMail.** `test_greenmail.py` drives the real agent end to end against
  GreenMail, when GreenMail is running.
- **UI changes.** Check them in a real browser: bring up the test stack, seed it
  with `website/screenshots/seed.py`, and drive `http://127.0.0.1:18000` with
  Playwright. Remember that static files are baked into the image, so rebuild
  or `docker cp` changed files first.

### Commands that touch the real install

On a developer's machine the default stack **is** their mailbox:

- `make up`, `down`, `infra`, `dev`, `psql`, `backup`, `agent*`
- `make restore`, which drops the database
- `meerail.sh`
- the `tools/*.py` scripts with `--apply`, and `tools/import-mbox.sh`

All of these resolve to `meerail.toml` and the production database. Do not run
them unless asked.

### Secrets on disk

Never print, copy or commit these (all are gitignored):

- `meerail.toml` (plaintext mail passwords, mode 0600)
- `.env`
- `backups/` (full mail dumps)
- `certs/`

## Releases and CI

- **Versioning.** `VERSION` is the only version number. `core/version.py`, the
  image tags, `/api/version` and `meerail.sh` all read it.
- **A push to `main` is a release.** `.github/workflows/images.yml` runs the
  tests, then builds `ribalba/meerail-{server,agent,tika}` for amd64 and arm64
  and pushes them to Docker Hub. It tags each image `:<version>`,
  `:<version>-<sha>` and `:latest`, and installs notice within a day.
  - Pushes that change only `*.md` or `website/**` do not trigger it.
- **Other branches and PRs** run `tests.yml`, which is `make test`.
- **Commits.** Leave changes uncommitted for the maintainer to review unless you
  are asked to commit. Never push. Commit messages in this repo are one short
  line.

## Style

- **Comments explain why, at length, in prose.** Many record the incident that
  motivated them. Match that density and do not strip existing comments. When
  you change behaviour, update the comment that describes it.
- **Python and libraries.** Python 3.11 or newer (`tomllib`); the images use
  3.13, and the host venv may be 3.14 (hence `imaplib_compat`). Libraries:
  SQLAlchemy 2.0 typed ORM, psycopg3 (`postgresql+psycopg://`), pydantic v2,
  FastAPI.
- **No formatter or linter is configured.** Follow the surrounding code.
- **User-facing wording is plain, specific and honest.** For example, queued
  mail is "not sent yet", never "failed". Refusals say what to do next.
- **Other agents may be editing the tree at the same time.** Re-read a file
  before you edit it.

## Known drift (as of 2026-09-21)

Trust the code over these:

- **`agent/service.sh` does not exist; the file is `agent/mac_service.sh`.**
  README.md, agent/README.md, the script's own help text and the Makefile's
  `agent-service*` targets still say `service.sh`, so those targets fail.
- **The Message-ID shortcut is gone.** agent/README.md ("How it works" step 2,
  "Full recheck"), the `imap.py::fetch_headers` docstring and
  `ingest.register_folder` still describe a shortcut that skipped fetching a
  message whose Message-ID was already known. Every UID is now fetched and the
  bytes decide. The same README places extraction and previews inside the sync
  pass; they run on the indexer thread.
- **The `Message` docstring says "deduplicated by Message-ID".** See Data model
  essentials for how identity really works.
- **Two server docstrings are outdated.** `app/updates.py` calls the update
  check the only outbound request, but the journal, Meerato and LLM calls are
  opt-in exceptions. `app/security.py` says there is no user auth, but there is
  a password gate.
- **`website/public/downloads/SKILL.md` says Postgres is not published on a
  host port.** Compose publishes it on `127.0.0.1:5432`.
- **`tests/conftest.py` and `tests/dbfixture.py` mention `agent/.venv`.** Use
  `.venv-test` through `make test`.
- **The CSP comment in `app/main.py` mentions `blob:` mail bodies.** The reader
  uses `srcdoc`.

## Further reading

- [README.md](README.md): features, configuration tables, and the deletion
  contract.
- [agent/README.md](agent/README.md): running the agent, logs, full recheck.
- [tests/README.md](tests/README.md): the test stack.
- [tika/README.md](tika/README.md), [journal/README.md](journal/README.md),
  [electron/README.md](electron/README.md), [COOLIFY.md](COOLIFY.md),
  [website/README.md](website/README.md).
- [meerail.example.toml](meerail.example.toml): every config key, annotated.
