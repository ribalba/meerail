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

It splits into two pieces:

- **`meerail-server`** — the whole app in Docker: FastAPI + Postgres (`pg_trgm`) + Apache Tika +
  the Apple-Mail-style web UI. Provider-agnostic; runs locally or on a remote server.
- **`meerail-agent`** — a small process on the machine with Proton Bridge. It speaks IMAP/SMTP
  to Bridge and syncs into the server. Your Bridge credentials never leave the host.

## Quick start

```bash
# 1. Run the server (server + Postgres + Tika)
cp .env.example .env
docker compose up -d

# 2. Open the app, add your account
open http://localhost:8000        # add an account matching your Proton address

# 3. Run the agent next to Proton Bridge
cd agent
cp config.example.toml config.toml   # fill in your Bridge host/ports + credentials
./run.sh --once                       # first full sync; then run ./run.sh to stay live

# 4. (optional) Native desktop app instead of the browser
cd electron && npm install && npm start
```

See [`agent/README.md`](agent/README.md) for the agent, [`electron/README.md`](electron/README.md)
for building desktop installers, and [`tests/README.md`](tests/README.md) for the test suite.

## Architecture

```
 host with Proton Bridge                    Docker (local or remote)
 ┌───────────────────────┐   HTTP+token    ┌──────────────────────────────┐
 │  meerail-agent        │ ──────────────▶ │  meerail-server (FastAPI)     │
 │  IMAP/SMTP ↔ Bridge   │   raw MIME up   │  parse · thread · Tika        │
 │  drains action queue  │ ◀────────────── │  Postgres (pg_trgm) · SPA     │
 └───────────────────────┘   actions down  └──────────────────────────────┘
                                                   ▲ browser / Electron
```

Content is stored once per Message-ID with per-folder placement tracked separately (handling
Proton exposing labels as folders). The agent is stateless — the server holds the sync cursors,
so you can stop and restart it anytime.

## Status

All six build milestones are complete: **M1** server/infra · **M2** agent + ingest · **M3**
Apple-Mail read UI · **M4** regex search + analytics · **M5** two-way sync + compose · **M6**
desktop packaging. Backed by a pytest suite (unit + integration, incl. an end-to-end run against
a GreenMail IMAP server).

Multiple accounts, a unified inbox, and sending/receiving **file attachments** are all supported.
Not yet implemented: saving **drafts** (the data model supports it) and CONDSTORE/QRESYNC
fast-resync (a UID/flag-diff fallback is used).
