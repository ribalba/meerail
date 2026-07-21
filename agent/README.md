# meerail-agent

Runs **on the machine with Proton Mail Bridge** and owns the entire mail
pipeline: it speaks IMAP/SMTP to Bridge over `localhost`, parses and threads what
it fetches, extracts attachment text through Tika, and writes the result into
Postgres. Your Bridge credentials stay here on the host.

The web app never fetches or parses mail — it only reads the database. So if the
agent isn't running, nothing new arrives.

## Setup

1. Make sure Proton Bridge is running and note its **IMAP/SMTP ports and the
   Bridge username/password** (Bridge app → your account → *Mailbox details*).
   These are Bridge-specific, not your Proton password.

2. Make sure the backing services are up (`docker compose up -d` in the repo
   root). Compose publishes Postgres on `127.0.0.1:5432` and Tika on
   `127.0.0.1:9998` — the agent needs both.

3. Configure and run:

   ```bash
   cp config.example.toml config.toml
   $EDITOR config.toml            # Bridge host/ports/credentials + database_url
   ./run.sh --once                # one full sync pass (good first run)
   ./run.sh                       # continuous: backfill + live IDLE
   ```

`run.sh` creates a `.venv`, installs `requirements.txt`, and puts the repo root
on `PYTHONPATH` so the agent can import the shared `core` package.

## How it works

Per account, for each IMAP folder:

1. **register** the folder, reading back its UID cursor from the database;
2. **scan** UIDs above the cursor — for each, if that Message-ID is already
   stored, just record another placement (Proton shows one message under several
   labels/folders); otherwise fetch the raw bytes;
3. **parse, thread and store** the new messages, raw MIME included;
4. **advance** the cursor;
5. **reconcile** flags (read/flagged/…) and prune messages that vanished;
6. **extract** attachment text via Tika and refresh the search index.

Then it holds an IMAP **IDLE** on the inbox for near-real-time new mail, and
notifies the web app over Postgres `NOTIFY` so open browsers refresh.

Cursors live in the **database**, so the agent is stateless — stop and restart it
anytime and it resumes where it left off.

## Notes

- `verify_cert = false` trusts Bridge's self-signed TLS cert. Set a real cert
  path story if you harden this later.
- `imap_security` / `smtp_security`: `starttls` (Bridge default), `ssl`, or
  `plain` (e.g. for a local test server).
- Multiple accounts: add more `[[account]]` blocks. Each runs in its own thread.
- Multiple sender addresses: Proton lets one account own several addresses. List
  them per account with `addresses = ["alias@…", "you@customdomain.com"]`; the
  agent reports them on sync and they appear in the app's compose **From**
  dropdown. The primary `email` is always sendable and need not be listed.
- Non-Proton providers work too — the agent speaks standard IMAP/SMTP. See the
  commented Gmail example in `config.example.toml` (requires an App Password,
  not your Google password).
- `database_url` uses the psycopg3 driver (`postgresql+psycopg://`). The agent
  runs on your host Python, so its pins are newer than you might expect —
  older ones have no wheels for current interpreters.
