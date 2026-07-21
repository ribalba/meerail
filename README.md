<p align="center">
  <img src="app/static/img/logo.png" width="150" alt="meerail logo" />
</p>

<h1 align="center">meerail</h1>

<p align="center">The meerkat email program — an Apple&nbsp;Mail–style client for a local Proton&nbsp;Mail&nbsp;Bridge.</p>

---

meerail is a fast, self-hosted email client built for **Proton Mail Bridge** (IMAP + SMTP),
with **regex search over your whole mailbox** (attachment text included), conversation
**threading**, two-way sync, compose/reply/forward, and everything stored in **PostgreSQL**
for analytics. Runs on macOS and Linux.

**Features:** three-pane Apple-Mail-style UI · unified inbox across accounts · conversation
threading · POSIX-regex & keyword search (scope + "last N years" window, searches PDF/Office
attachment text via Tika) · sandboxed HTML rendering with remote-image blocking · read/flag/
archive/delete and compose that **sync back to Proton** over IMAP/SMTP · light + dark.

It splits into three pieces:

- **`meerail-agent`** — runs on the machine with Proton Bridge and owns the whole write path:
  it speaks IMAP/SMTP to Bridge, parses and threads your mail, extracts attachment text via
  Tika, and writes it into Postgres. Your Bridge credentials never leave the host.
- **`meerail-server`** — the web layer in Docker: FastAPI + the Apple-Mail-style UI. It only
  reads from the database and enqueues your actions; it never fetches or parses mail.
- **`core`** — the shared library both import: models, parsing, threading, ingest, Tika.

## Quick start

```bash
# 1. Start the backing services + web app (Postgres, Tika, server)
cp .env.example .env
docker compose up -d

# 2. Run the agent next to Proton Bridge — it does the syncing and the parsing,
#    writing straight into Postgres (published on 127.0.0.1 by compose).
cd agent
cp config.example.toml config.toml   # fill in your Bridge host/ports + credentials
./run.sh --once                       # first full sync; then run ./run.sh to stay live

# 3. Open the app — accounts the agent syncs appear automatically
open http://localhost:8000

# 4. (optional) Native desktop app instead of the browser
cd electron && npm install && npm start
```

See [`agent/README.md`](agent/README.md) for the agent, [`electron/README.md`](electron/README.md)
for building desktop installers, and [`tests/README.md`](tests/README.md) for the test suite.

## Architecture

```
 host with Proton Bridge                         Docker
 ┌────────────────────────────┐             ┌──────────────────────────┐
 │  meerail-agent             │   writes    │  Postgres (pg_trgm)      │
 │  IMAP/SMTP ↔ Bridge        │ ──────────▶ │  mail · blobs · queue    │
 │  parse · thread · index    │             └──────────────────────────┘
 │  Tika ↔ attachment text    │                    ▲ reads   │ NOTIFY
 │  drains the action queue   │             ┌──────┴──────────▼───────┐
 └────────────────────────────┘             │  meerail-server         │
                                            │  FastAPI · SPA          │
                                            └─────────────────────────┘
                                                   ▲ browser / Electron
```

The database is the only thing the two halves share: the agent writes, the app reads, and
neither calls the other. Live UI updates ride Postgres `LISTEN/NOTIFY`, so the browser still
refreshes the moment mail lands even though ingest happens in another process.

Content is stored once per Message-ID with per-folder placement tracked separately (handling
Proton exposing labels as folders). Raw MIME and attachment bytes live in the database, so
there is no shared filesystem between the agent and the app. Sync cursors live in the database
too, so the agent stays stateless — stop and restart it anytime.

## Status

All six build milestones are complete: **M1** server/infra · **M2** agent + ingest · **M3**
Apple-Mail read UI · **M4** regex search + analytics · **M5** two-way sync + compose · **M6**
desktop packaging. Backed by a pytest suite (unit + integration, incl. an end-to-end run against
a GreenMail IMAP server).

Multiple accounts, a unified inbox, and sending/receiving **file attachments** are all supported.
Not yet implemented: saving **drafts** (the data model supports it) and CONDSTORE/QRESYNC
fast-resync (a UID/flag-diff fallback is used).
