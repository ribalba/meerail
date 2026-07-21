# meerail-agent

The small connector that runs **on the machine with Proton Mail Bridge**. It
speaks IMAP/SMTP to Bridge over `localhost` and syncs into the meerail server
over HTTP. Your Bridge credentials stay here on the host — they never go to the
server.

## Setup

1. Make sure Proton Bridge is running and note its **IMAP/SMTP ports and the
   Bridge username/password** (Bridge app → your account → *Mailbox details*).
   These are Bridge-specific, not your Proton password.

2. In the meerail web UI (the server), **add an account** with the same email
   address you use here.

3. Configure and run:

   ```bash
   cp config.example.toml config.toml
   $EDITOR config.toml            # fill in Bridge host/ports/username/password
   ./run.sh --once                # one full sync pass (good first run)
   ./run.sh                       # continuous: backfill + live IDLE
   ```

`run.sh` creates a `.venv` and installs `requirements.txt` on first run.

## How it works

Per account, for each IMAP folder:

1. **register** the folder with the server and get back its UID cursor;
2. **scan** UIDs above the cursor — send `(uid, Message-ID, flags)`; the server
   records placements for mail it already has (Proton shows one message under
   several labels/folders) and asks only for what it's missing;
3. **upload** raw bytes for the missing UIDs;
4. **advance** the cursor;
5. **reconcile** flags (read/flagged/…) and prune messages that vanished.

Then it holds an IMAP **IDLE** on the inbox for near-real-time new mail. The
server does all parsing, threading, attachment text extraction, and indexing.

The cursor lives on the **server**, so the agent is stateless — stop and restart
it anytime and it resumes where it left off.

## Notes

- `verify_cert = false` trusts Bridge's self-signed TLS cert. Set a real cert
  path story if you harden this later.
- `imap_security` / `smtp_security`: `starttls` (Bridge default), `ssl`, or
  `plain` (e.g. for a local test server).
- Multiple accounts: add more `[[account]]` blocks. Each runs in its own thread.
