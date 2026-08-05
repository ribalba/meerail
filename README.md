<p align="center">
  <img src="app/static/img/logo.png" width="150" alt="meerail logo" />
</p>

<h1 align="center">meerail</h1>

<p align="center">The meerail email program — an email client for power users</p>

---

meerail is a fast, self-hosted email client built for all IMAP and SMTP gateways (including the Proton Mail Bridge),
with **regex search over your whole mailbox** (attachment text included), conversation
**threading**, two-way sync, compose/reply/forward, and everything stored in **PostgreSQL**
for analytics. Runs on Linux, macOS and Windows.

**Features:** three-pane Apple-Mail-style UI · unified inbox across accounts · conversation
threading · POSIX-regex & keyword search (scope + "last N years" window, `:unread` / `:read` /
`:has-attachment` / `:from` / `:to` filters, searches PDF/Office attachment text via Tika) ·
sandboxed HTML rendering with remote-image blocking · read/flag/
archive/delete and compose that **sync back to your mail server** over IMAP/SMTP · file a mail
as a **Meerato task**, attachments and all · light + dark, following the system or pinned to
either in Settings.

<p align="center">
  <img src="website/public/img/screenshots/inbox.png" width="820" alt="meerail inbox" />
</p>

It splits into three pieces:

- **`meerail-agent`** — runs on the machine with Proton Bridge and owns the whole write path:
  it speaks IMAP/SMTP to Bridge, parses and threads your mail, extracts attachment text via
  Tika, and writes it into Postgres. Your Bridge credentials never leave the host.
- **`meerail-server`** — the web layer in Docker: FastAPI + the Apple-Mail-style UI. It only
  reads from the database and enqueues your actions; it never fetches or parses mail.
- **`core`** — the shared library both import: models, parsing, threading, ingest, Tika.

## Background

meerail started from two things web mail cannot do: search your mail the way you search
code, and keep it somewhere you can query.

**Regex, not "search".** Every mail client offers substring matching over headers and, if
you are lucky, bodies. meerail pushes POSIX regular expressions straight down into
Postgres, over a corpus that already includes the extracted text of your PDF and Office
attachments — so `invoice.*2024` finds the invoice inside the attachment, not just mails
that happen to say "invoice". A trigram index keeps that honest on a mailbox of tens of
thousands of messages.

**Postgres as the store, not a cache.** Mail lives in a real database — raw MIME,
attachment bytes, parsed headers, threading, the search corpus. That means you can point
psql (`make psql`) at years of correspondence and ask questions no email client exposes.
It also means there is no shared filesystem to keep in sync and no proprietary on-disk
format to reverse-engineer later.

**A split that keeps credentials on your machine.** Proton Mail Bridge only listens on
`127.0.0.1`, which forces the design in a useful direction: the agent runs natively beside
Bridge and owns the entire write path, while the server runs in Docker and only ever reads
from the database. Your mail password never enters a container, and the web layer has no
code path that could send it anywhere. The two halves share nothing but Postgres — the
agent writes, the app reads, neither calls the other.

It is not Proton-specific. The agent speaks plain IMAP and SMTP, so Gmail (with an App
Password), Fastmail, or any ordinary mail host works the same way; Bridge is just the case
that shaped the architecture.

## Requirements

| | |
| --- | --- |
| **Docker** | Engine 24+ with the Compose v2 plugin (`docker compose`, not `docker-compose`). Docker Desktop on macOS/Windows. The only hard requirement for the [`meerail.sh`](#install-the-quick-way) install. |
| **Python** | 3.11 or newer on the host — only for the agent, and only when running it outside Docker (the clone-and-build path). 3.11 is the floor (`tomllib`); 3.13/3.14 are tested. |
| **Node** | 20+, only if you want the Electron desktop app rather than the browser. |
| **RAM** | ~6 GB free for the stack as shipped. Postgres is capped at 10 GB and Tika at 3 GB in `docker-compose.yml`, tuned for a ~32 GB host — lower `shared_buffers` and the `deploy.resources.limits` if your machine is smaller. |
| **Disk** | Sized to your mailbox. Raw MIME plus attachment bytes plus the trigram index runs to tens of GB for a large account — [the content window](#the-content-window) and `agent.store_raw_mime` are the two knobs that bound it. |
| **Mail access** | Proton Mail Bridge running and unlocked, **or** any IMAP+SMTP account. Gmail needs 2-Step Verification, an App Password and IMAP enabled — your normal password will not work. |

Tika's `latest-full` image bundles Tesseract and is a multi-GB pull; it is what OCRs
scanned PDFs and image attachments. Switch to `apache/tika:latest` in
[`docker-compose.yml`](docker-compose.yml) if you want a much smaller image and can live
without OCR — text extraction still works, images just come back empty.

## Install: the quick way

No clone, no build, no Python on your machine — one script that asks what it needs, writes
a configuration, and runs the published containers.

```bash
curl -fsSL https://raw.githubusercontent.com/ribalba/meerail/main/meerail.sh -o meerail.sh
bash meerail.sh
```

It checks Docker is there, asks where your mail lives, sizes Postgres and Tika to the
memory Docker actually has, pulls `ribalba/meerail-{server,agent,tika}` from Docker Hub and
starts them. Everything it writes lives in `~/.meerail` (override with `MEERAIL_HOME`);
your mail lives in Docker volumes.

**Proton Mail works here too, on every OS.** Choose Proton and the installer runs
[Bridge as a container](docker-compose.hub.yml) beside the rest of the stack and walks you
through its one-time interactive login. The agent then reaches it at `bridge:143` over the
compose network — so the "Docker Desktop can't see Bridge on your loopback" problem that
shapes the clone-based install below simply does not arise. Any other IMAP/SMTP account
works the same way, with Gmail, Fastmail and iCloud servers filled in automatically from
your address.

Afterwards:

```bash
bash meerail.sh status      # containers, version, URL
bash meerail.sh logs agent  # watch the first sync work through your mailbox
bash meerail.sh test        # check every connection: database, Tika, IMAP, SMTP
bash meerail.sh requeue     # re-queue anything an older agent gave up on
bash meerail.sh update      # pull the newest release and restart
bash meerail.sh config      # edit meerail.toml, then restart
bash meerail.sh help        # everything else
```

Windows: run it inside WSL2 or Git Bash, with Docker Desktop running.

The running app tells you when a new version is out — a strip in the sidebar, from a
once-a-day check the server makes against this repository; the strip links to
[How to update](#how-to-update). It sends nothing about you or your mail, and
`update_check = false` under `[server]` turns the request off entirely.

## Quick start from a clone

The developer path: build the images from this checkout and run the agent on your own
Python. Use this if you are changing meerail, not just running it.
**[Running it on your platform](#running-it-on-your-platform)** below has the per-OS
detail, including PowerShell commands for Windows and how to keep the agent running at
boot.

```bash
# 1. One config file for the whole system — server and agent both read it.
#    Fill in your Bridge host/ports + credentials under [[agent.account]].
cp meerail.example.toml meerail.toml
chmod 600 meerail.toml               # it holds your mail password in plaintext
cp .env.example .env                 # just the Postgres container's credentials

# 2. Start the backing services + web app (Postgres, Tika, server). Postgres and
#    Tika are published on 127.0.0.1, which is where the agent looks for them.
docker compose up -d

# 3. Run the agent next to Proton Bridge — it does the syncing and the parsing,
#    writing straight into Postgres.
cd agent
./run.sh --once                       # first full sync; then run ./run.sh to stay live

# 4. Open the app — accounts the agent syncs appear automatically
open http://localhost:8000

# 5. (optional) Native desktop app instead of the browser
cd electron && npm install && npm start
```

See [`agent/README.md`](agent/README.md) for the agent, [`electron/README.md`](electron/README.md)
for building desktop installers, and [`tests/README.md`](tests/README.md) for the test suite.

## Running it on your platform

This section is about the **clone-and-build** install. If you used
[`meerail.sh`](#install-the-quick-way) none of it applies: Bridge runs as a container
there, everything is on one Docker network, and the three platforms are the same install.

From a clone the split is: **the stack runs in Docker, and Proton Bridge runs natively** —
as a desktop app it listens on `127.0.0.1`, and only a container sharing that loopback can
reach it. The only question each platform answers differently is *where the agent runs*.

|             | Postgres · Tika · server | agent                               | desktop app         |
| ----------- | ------------------------ | ----------------------------------- | ------------------- |
| **Linux**   | Docker                   | Docker (host network) **or** native | Electron or browser |
| **macOS**   | Docker Desktop           | native (launchd) — Docker can't see Bridge | Electron or browser |
| **Windows** | Docker Desktop (WSL2)    | native — Docker can't see Bridge    | Electron or browser |

On macOS and Windows, Docker Desktop runs containers inside a Linux VM, so a container's
`127.0.0.1` is the VM's loopback — not the one Bridge is listening on. `network_mode:
host` does not change that; it joins the VM's namespace. Hence: native agent on those two.

### Linux

Everything can be containerised, including the agent.

```bash
cp meerail.example.toml meerail.toml   # Bridge host/ports + credentials
chmod 600 meerail.toml                 # it holds your password in plaintext
cp .env.example .env                   # Postgres container credentials only

make agent-docker                      # whole stack + agent, host network
make agent-test                        # verify every connection
```

`make agent-docker` brings up Postgres, Tika, the server *and* the agent with the
[`docker-compose.agent.yml`](docker-compose.agent.yml) overlay: host networking, so
`127.0.0.1` inside the container is your machine, and `restart: unless-stopped` so the
agent comes back after a reboot. Sharing the host's network means the agent is *not* on
the compose network, which is why the base file publishes Postgres and Tika on loopback —
the container reaches them there rather than at `db:5432` / `tika:9998`.

Prefer it native? `cd agent && ./run.sh` — and see [`agent/README.md`](agent/README.md)
for the systemd user unit that keeps it alive.

### macOS

Stack in Docker Desktop, agent on your host Python.

```bash
brew install --cask docker                # if you don't have Docker Desktop
cp meerail.example.toml meerail.toml      # Bridge host/ports + credentials
chmod 600 meerail.toml
cp .env.example .env
make up                                   # Postgres, Tika, server — the first two
                                          # published on 127.0.0.1 for the agent
cd agent
./run.sh --test                           # check every connection first
./run.sh --once                           # first full sync
./run.sh                                  # then stay live
open http://localhost:8000
```

`run.sh` builds the venv and puts the repo root on `PYTHONPATH` for you.

Once that works, hand it to launchd so it runs in the background instead of tying up a
terminal:

```bash
./service.sh install                      # or: make agent-service
./service.sh status                       # running? plus the last few log lines
./service.sh logs                         # tail -f
```

That generates a **LaunchAgent** with this checkout's paths, starts the agent at login and
restarts it if it dies. It has to be a LaunchAgent rather than a LaunchDaemon, since Bridge
only runs inside your logged-in session — see
[`agent/README.md`](agent/README.md#macos--launchd) for the rest of the commands and what
the plist sets.

### Windows

Stack in Docker Desktop (WSL2 backend), agent natively in PowerShell. There is no
`run.sh` equivalent, so the venv is built by hand once:

```powershell
copy meerail.example.toml meerail.toml    # then edit: Bridge host/ports + credentials
copy .env.example .env
docker compose up -d

cd agent
py -3 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = (Resolve-Path ..).Path  # the agent imports the shared `core` package
.\.venv\Scripts\python main.py --test     # check every connection first
.\.venv\Scripts\python main.py --once     # first full sync
.\.venv\Scripts\python main.py            # then stay live
start http://localhost:8000
```

Everything lives on Windows' own loopback — Bridge, and the Postgres/Tika ports Docker
Desktop publishes there — so the default `127.0.0.1` addresses in `config.toml` work as
written.

Two Windows-specific notes: `PYTHONPATH` must be set for whatever launches the agent
(a new shell won't have it — make it a persistent user variable), and `chmod 600` has no
NTFS equivalent, so `--test` reports the config-permission check as a warning instead of
enforcing it; check the ACLs with `icacls config.toml` if others use the machine. For
start-at-login, [`agent/README.md`](agent/README.md#windows--task-scheduler) has a
**Task Scheduler** recipe.

The Electron wrapper works here too — `cd electron && npm install && npm start`, and
`npm run dist` produces an NSIS installer (build it *on* Windows; electron-builder needs
the native toolchain for that target).

## How to update

The app tells you when a newer version is out — a strip in the sidebar, from a once-a-day
check the server makes against [`VERSION`](VERSION) on this repository's default branch.
This is what to do about it. Which of the two paths applies depends on how you installed;
[Versions and releases](#versions-and-releases) explains what a version *is*.

**Your mail is not at risk either way.** Everything lives in Docker volumes (or your own
Postgres), not in the images, and the server runs the schema migrations itself on first
boot. Updating is: pull new images, restart, done. There is no export/import step, and
downgrading is only safe back to a version whose schema you have not yet migrated past —
so if you want a way back, snapshot the database first (`docker compose exec db pg_dump …`).

### If you installed with `meerail.sh`

```bash
bash meerail.sh update      # reads VERSION, pins ~/.meerail/.env to it, pulls, restarts
bash meerail.sh status      # installed vs. latest, and the containers
```

`update` also refreshes `~/.meerail/docker-compose.yml` from the release, so a new service
or a renamed variable comes along with it — this is why you should not update by running
`docker compose pull` in `~/.meerail` by hand. The script itself is the one thing it does
not replace; re-download it if a release note says to:

```bash
curl -fsSL https://raw.githubusercontent.com/ribalba/meerail/main/meerail.sh -o meerail.sh
```

Your configuration (`~/.meerail/meerail.toml`) is never touched. New settings take their
defaults; [Configuration](#configuration) lists them, and `meerail.example.toml` in the
repository is the annotated reference.

### If you installed from a clone

The images are built from your checkout, so updating is a pull and a rebuild:

```bash
git pull
docker compose up -d --build      # rebuild server + tika, restart
```

Then update the agent to match — it shares the `core` package with the server, and running
a stale agent against a migrated database is the one combination worth avoiding:

* **native agent** (macOS, Windows, or by choice on Linux): stop it, `cd agent && ./run.sh
  --once` — `run.sh` reinstalls `requirements.txt` into the venv before it starts — then
  `./run.sh` to stay live. Under launchd or systemd: `./mac_service.sh restart` (macOS) or
  `systemctl --user restart meerail-agent` (Linux), after the venv has been refreshed once.
* **containerised agent** (Linux, `make agent-docker`): `make agent-docker` again rebuilds
  and restarts it with the rest of the stack.

Check `git log --oneline` for anything that touches `meerail.toml` — if
`meerail.example.toml` grew a key you care about, copy it across; nothing breaks if you
don't, since every setting has a default. Migrating from a pre-`meerail.toml` layout is
[its own section](#upgrading-from-the-two-file-layout).

Then `make agent-test` (or `./run.sh --test`) to confirm database, Tika, IMAP and SMTP all
still answer, and reload the browser tab — the frontend is served fresh, but a long-open
tab is still running the old JavaScript.

### Running a specific version, or none

`MEERAIL_VERSION` in `~/.meerail/.env` is what the compose file resolves images against.
Set it to any published tag to move deliberately rather than to whatever is newest:

```bash
MEERAIL_VERSION=0.5.0            # a release
MEERAIL_VERSION=0.5.0-a1b2c3d    # one exact build, immutable
```

then `docker compose --env-file ~/.meerail/.env -f ~/.meerail/docker-compose.yml up -d`.

To stop being told about updates at all, set `update_check = false` under `[server]` in
`meerail.toml` and restart — the server then makes no outbound request whatsoever. The
notice can also just be dismissed in the sidebar, which silences that one version and lets
the next one speak up.

### Desktop app

The Electron wrapper is a window onto the server, so updating the stack updates what you
see. The wrapper itself only changes when you rebuild it — `cd electron && npm install &&
npm run dist` from an updated clone, or reinstall the installer from a newer release.

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

### What is exposed

Every container sits on one Docker network, `meerail`, and addresses the others by service
name. **Only the server is published on all interfaces**; Postgres and Tika are bound to
`127.0.0.1` explicitly, so they are reachable from this machine and not from the LAN.

| Service | Address on the `meerail` network | Published on the host |
| --- | --- | --- |
| `server` | `server:8000` | **`8000`** — browser / Electron |
| `db` | `db:5432` | `127.0.0.1:5432` — loopback only |
| `tika` | `tika:9998` | `127.0.0.1:9998` — loopback only |

Those two loopback bindings are what a **natively-run agent** needs: it is not on the
compose network, so `db` and `tika` do not resolve for it. Same for the host-network agent
container and for `make dev`. Neither service authenticates a caller worth the name (the
shipped Postgres password is `meerail`; Tika will extract whatever anyone POSTs it), so if
you edit those port lines, keep the `127.0.0.1:` prefix — without it Docker publishes on
`0.0.0.0` and punches straight through the host firewall.

Nothing here puts the server behind TLS or asks for a password by default, which is right
for a localhost app and wrong the moment 8000 is reachable from elsewhere — set
`SERVER_PASSWORD` and put a TLS terminator in front if you expose it.

If that terminator is Coolify, [`COOLIFY.md`](COOLIFY.md) and
[`docker-compose.coolify.yml`](docker-compose.coolify.yml) deploy the whole stack —
Bridge included, in a container — as one resource, with Traefik in front and nothing
else published. It moves your mail credentials onto the server, which is the one thing
the layout above is built to avoid; that file opens by saying so.

## Configuration

**One file: `meerail.toml`.** The server and the agent both read it — the server takes
`[database]` and `[server]`, the agent takes `[database]` and `[agent]`, and each ignores
the other's section. Copy from
[`meerail.example.toml`](meerail.example.toml) and `chmod 600` it: it holds your mail
password in plaintext, and the agent's `--test` warns you if the permissions are loose. It
is gitignored, so credentials cannot be pushed by accident.

Every setting can also be given as an environment variable of the same name in upper case
— `server.password` is `SERVER_PASSWORD`, `database.url` is `DATABASE_URL`,
`agent.store_raw_mime` is `STORE_RAW_MIME`. The order is:

```
environment  >  .env  >  meerail.toml  >  built-in default
```

The environment winning is what lets a remote server run with **no file at all**: drop the
bind mount from [`docker-compose.yml`](docker-compose.yml) and pass `DATABASE_URL` and
`SECRET_KEY` in the environment instead. It is also why the compose files set only the
handful of values that are genuinely container topology — a variable with a
`${VAR:-default}` fallback is *always* set, so listing one there would silently override
whatever you wrote in the file.

`.env` still exists, but only for the part that cannot live in TOML: `docker compose` reads
it to expand `${...}`, and the Postgres image takes its credentials from the environment and
nowhere else. Copy [`.env.example`](.env.example) and leave it at the three `POSTGRES_*`
keys unless you specifically want a per-machine override.

### `[database]`

| Key | Env | Default | What it does |
| --- | --- | --- | --- |
| `url` | `DATABASE_URL` | local Postgres | The only channel between the agent and the web app. Containers on the compose network are handed `db:5432` in their environment, which overrides this. If you change `POSTGRES_PASSWORD` in `.env`, change it here too — the Postgres image only takes credentials from the environment, so that one value genuinely does live in two places. |

### `[server]`

| Key | Env | Default | What it does |
| --- | --- | --- | --- |
| `secret_key` | `SECRET_KEY` | `dev-insecure-…` | Signs tokens and encrypts any server-side stored credentials. **Change it** before exposing the app: `python -c "import secrets;print(secrets.token_urlsafe(48))"`. |
| `password` | `SERVER_PASSWORD` | *(empty)* | Empty means no auth — correct for a localhost app. Set it (**with TLS**) if the server is reachable from anywhere else: the UI then shows a password screen, and a successful login holds a signed session cookie for `session_max_age_days`. Scripted clients send it as `Authorization: Bearer <password>`. Failed logins are rate-limited per address (5 per 15 minutes). |
| `session_max_age_days` | `SESSION_MAX_AGE_DAYS` | `30` | How long a browser login lasts before the password is asked again. Changing `password` or `secret_key` logs every browser out immediately. |
| `default_search_years` | `DEFAULT_SEARCH_YEARS` | `0` | Default search window; `0` searches everything. The UI can override per query. |
| `contacts_scan_years` | `CONTACTS_SCAN_YEARS` | `1` | How far back to scan addresses for compose autocomplete; `0` is all time. |
| `max_attachment_bytes` | `MAX_ATTACHMENT_BYTES` | `104857600` | Per-attachment cap for outgoing uploads. |
| `data_dir` | `DATA_DIR` | `./data` | Scratch space for staging outgoing attachments. Mail bytes live in Postgres. Every container overrides it to `/data`. |
| `update_check` | `UPDATE_CHECK` | `true` | Once a day, fetch [`VERSION`](VERSION) from this repository's default branch and let the UI say so if it is newer than the running build. The only outbound request the server makes, and it carries nothing but the request — no identifier, no version, no statistics. `false` makes no request at all. See [How to update](#how-to-update) and [Versions and releases](#versions-and-releases). |

### `[agent]`

| Key | Env | Default | What it does |
| --- | --- | --- | --- |
| `tika_url` | `TIKA_URL` | `http://127.0.0.1:9998` | Attachment text extraction endpoint, called by the agent. |
| `poll_interval` | `POLL_INTERVAL` | `30` | Seconds between IDLE cycles. |
| `reconcile_interval` | `RECONCILE_INTERVAL` | `900` | Seconds between full flag/prune sweeps. |
| `batch_size` | `BATCH_SIZE` | `200` | UIDs per fetch/ingest batch. An account may override it. |
| `store_raw_mime` | `STORE_RAW_MIME` | `true` | Keep each message's original RFC822 bytes in `messages.raw_mime` — held for future features, and roughly half the database. `false` ingests without them. Takes effect for mail synced from then on; existing rows keep their copy. |
| `content_window_months` | `CONTENT_WINDOW_MONTHS` | `0` | Keep the *content* of mail sent within this many months; `0` keeps everything. See [The content window](#the-content-window). |

Then one `[[agent.account]]` block per address — IMAP and SMTP host/port/security, username
(defaults to `email`), password, `verify_cert` (`false` for Bridge's self-signed cert, `true`
for a real one), an optional `batch_size` that overrides the global one for that account
(Gmail answers a 200-message fetch with UIDs missing or by dropping the connection — `25` is
a good starting point there), and an optional `addresses = [...]` list of aliases to offer in
the composer's *From*. Accounts register themselves in the app on first sync; there is
nothing to add in the UI. The example file carries a commented-out Gmail block alongside the
Proton one.

`name = "Your Name"` sets the display name recipients see in front of the address; without it
mail goes out as the bare address. It applies to every address the account sends from, and an
entry in `addresses` written as `Name <alias@example.com>` overrides it for that one — listing
the primary `email` there is how it gets a name of its own. This is not the account *label* in
Settings, which names the account in the sidebar and never leaves the UI.

`label`, `color` and `footer` pin that UI-side presentation from the file: the account's name
in the sidebar, the colour of its dot, and the footer the composer prefills. All three are
normally edited in Settings → Accounts and left out of the file. Set one here and the file
owns it — the agent writes it onto the account on every pass, and Settings shows it as
configured rather than editable, since it would otherwise take a change that the next sync
silently undid. Delete the key again and Settings takes it back, keeping the value the file
last gave it. `footer = ""` is an answer, not an omission: it means this account has no
footer. Handy when accounts are provisioned from a file rather than set up by hand.

### Upgrading from the two-file layout

Before this, settings lived in `.env` *and* `agent/config.toml`, with `STORE_RAW_MIME` and
`CONTENT_WINDOW_MONTHS` in both ([issue #1](https://github.com/ribalba/meerail/issues/1)).
`agent/config.toml` is no longer read. Fold an existing install into one file with:

```bash
python -m core.config migrate
```

It reads your `.env` and `agent/config.toml`, writes `meerail.toml` at mode 0600 with the
values that install was actually running with, and tells you what to do next. The agent
refuses to start until you have run it.

### The content window

A full mailbox is tens of GB, and most of it is mail nobody will open again. Set
`agent.content_window_months` to keep the *content* of recent mail only:

```toml
[agent]
content_window_months = 24   # bodies and attachments for the last two years
```

Older mail is still synced — it just arrives as headers alone. It lists, sorts, threads,
counts towards the folder totals and turns up in a search for its subject or correspondent;
it has no body to open, and the reader says so instead of showing a blank message. The body
never crosses the wire: the agent reads each message's date in the header pass it already
makes, and never asks for the rest.

The window **slides**, which is what makes it a ceiling rather than a one-off tidy-up. Mail
already stored is stripped back to headers once it falls out of the window — body, HTML,
attachment payloads, previews and extracted text all go, and the attachment names and sizes
stay. That runs on the agent's indexer thread, so it keeps happening on a mailbox where no
new mail is arriving.

Nothing is deleted from your mail server. Widening the window applies to new mail
immediately; to pull back content for mail that was already skipped, widen it and then run a
full recheck from the UI's agent-status panel, which re-walks every folder.

### What meerail deletes, and when

Short version: your mail is deleted when you delete it, when your mail server says it is
gone, or when you delete the whole account. Nothing else removes a message, and nothing
removes one because a connection failed.

| What | When | |
| --- | --- | --- |
| A message you trash or archive | You pressed it | Leaves the folder locally at once and is applied to IMAP on the next pass. With no Trash folder on the account, "delete" is an IMAP expunge — permanent, on the server. |
| Mail deleted on your phone or in webmail | The server no longer lists its UID | meerail mirrors the server; a message deleted elsewhere goes here too. Only ever on a UID list the server has confirmed in full — see below. |
| A folder | It is gone from the server's `LIST` | Its messages go with it, unless they are also filed elsewhere. Never on an empty `LIST`. |
| Bodies and attachments of old mail | `content_window_months` is set | Headers stay; the mail still lists, threads and searches. Off by default. See [The content window](#the-content-window). |
| Everything for an account | `DELETE /api/accounts/{id}` | The one command that removes an account's mail wholesale. No button in the UI calls it. |

Being offline is never a reason to delete anything, and the agent is written for machines
that are: a laptop that is opened twice a week, a Bridge that has not signed in yet, a mail
server that has been down since Friday.

- **A connection that fails deletes nothing** — the pass ends and the next one picks up.
- **A connection that answers *short* also deletes nothing.** This is the one that bites:
  Proton Bridge keeps serving while it cannot reach Proton, so it will answer `LIST` with no
  folders, or a folder's `SEARCH` with a fraction of what it holds. The agent checks every
  UID list against the message count `SELECT` reported before it removes anything, and an
  empty `LIST` is never read as "every folder was deleted". It logs what it saw and leaves
  your mail alone.
- **A new `UIDVALIDITY` does not empty the folder.** Bridge changes it for its own reasons —
  a re-login, a rebuilt cache. The agent re-walks the folder instead; mail it already holds
  is matched by `Message-ID` and keeps its content.
- **Filing works offline.** Archive and trash move the message in the app the moment you
  press them, not when the agent gets round to applying it. The copy you see in Archive is
  written locally and replaced by the server's own once the move lands; file the same
  message twice before that and it is still one move, to wherever it ended up.
- **Queued work is never dropped.** Marks, moves and above all messages you have sent sit in
  the queue until they succeed, retried on a backoff for as long as it takes. Nothing
  expires them.
- **The outbox is a folder you can open.** A strip under the sidebar toolbar counts what is
  written but not yet sent — normally for the second it takes, and for as long as it takes
  when the agent is away — and an **Outbox** folder appears above Favorites while anything
  is in it (`g o` any time). Both turn red once an attempt has actually failed. The folder
  lists each waiting message with who it is for, how many attempts it has cost and what the
  last one said; opening one shows the full error, when the next attempt is due, and two
  buttons: **Try now**, which skips the backoff after you have fixed the cause, and
  **Delete**, the only thing anywhere that takes a message back out of the queue.
- **The agent says the same thing in its log.** It prints every send that goes out and every
  one that fails, and — because a pass that dies at connect never gets as far as trying —
  lists what is still sitting in the outbox at startup and after a failed pass. A mailbox
  full of unsent mail can no longer be silent on both sides.

### Running the agent

`agent/run.sh` builds the venv, keeps it in step with `requirements.txt`, puts the repo root
on `PYTHONPATH` (the agent imports `core`) and passes arguments through:

| Flag | |
| --- | --- |
| *(none)* | Stay live: IDLE, sync, drain the action queue. |
| `--once` | One full sync pass, then exit. Use this first. |
| `--test` | Check Postgres, Tika, IMAP and SMTP for every account, report, exit. Run it before anything else. |
| `--config PATH` | Use a config file other than the repository's `meerail.toml`. |
| `--backfill-previews` | Render previews for attachments already stored, then exit. |
| `--requeue-abandoned` | Put back anything an older agent gave up on — including mail it queued to send and never did. Earlier versions stopped after five failed attempts; nothing does now. The agent names what it finds on every start; this is the command that sends it. |

On Linux the same thing runs containerised with `make agent-docker` / `make agent-test` /
`make agent-logs`; on macOS, `agent/service.sh install` (`make agent-service`) puts it in
the background under launchd. [`agent/README.md`](agent/README.md) covers the logs, the
full-recheck path, and start-at-boot setups for all three platforms.

### Importing an mbox

Old mail that is not on a server any more — a Thunderbird folder, a Gmail Takeout export,
an archive from a mail host you have left — goes in through `tools/import-mbox.sh`:

```bash
tools/import-mbox.sh ~/Downloads/archive.mbox
tools/import-mbox.sh archive.mbox --account old@example.com --folder Archive
```

It creates an account of its own for the file (`<filename>@imported.local` unless you name
one), stores every message as a placement in one folder, and then runs the same indexing
pass the agent's indexer thread runs: Tika over the attachments, previews, `search_text`.
What lands is ordinary mail — threaded, searchable down to the text inside a PDF, with
attachments you can open. It runs on the host and talks to Postgres and Tika over the
loopback ports compose publishes, so the stack has to be up; it reuses `agent/.venv`.

| Flag | |
| --- | --- |
| `--account EMAIL` | Import into this account, creating it if it does not exist. Default: derived from the filename. |
| `--folder NAME` | Which folder the mail lands in (default `INBOX`). A name like `Sent` or `Archive` takes that folder's role in the sidebar. |
| `--keep-unread` | Take read/unread from the mbox `Status` headers. Most exports carry none, so this marks the whole import unread; by default everything is imported as read. |
| `--no-index` | Import only, leaving attachment text and previews queued for a running agent. |
| `--config PATH` | Use a config file other than the repository's `meerail.toml`. |

Re-running the same file imports nothing twice: a message already placed in that folder is
skipped, so an interrupted import continues where it stopped, and an mbox that has grown
since last time adds only what is new.

Import into an account **no agent syncs** — the default, and the tool refuses anything else
without `--force`. The agent deletes folders its IMAP server does not list ([What meerail
deletes, and when](#what-meerail-deletes-and-when)), and an imported folder exists nowhere
but here, so its next pass would take the imported mail with it.

### Putting back mail whose move never landed

Filing a message writes the placement here straight away and queues the move for the agent
— see [What meerail deletes, and when](#what-meerail-deletes-and-when). If that move never
lands, the placement stays behind with nothing on the server under it: the message shows in
the folder you filed it into, and every key you press on it is refused, because there is no
UID to address it by.

Versions up to 0.3.1 could get there by deleting the message. A move was applied as COPY,
then `\Deleted`, then EXPUNGE — and on a server where folders are labels the COPY has
already done the move, so the EXPUNGE landed on a message sitting in Trash, which Proton
reads as "delete it for good". The agent now uses IMAP `MOVE` where the server offers it
(Proton Bridge and Gmail both do) and, where it does not, removes the source copy only
after confirming there is still one there.

`tools/restore_pending.py` repairs what that left behind, from the raw MIME meerail still
holds:

```bash
tools/restore_pending.py                     # what it would put back
tools/restore_pending.py --apply
```

It appends each message back into the folder its placement claims, and the next sync pass
ingests it under a real UID and retires the placeholder. Nothing is written to the database
and nothing is deleted; a message already on the server is left alone, so re-running is
safe. It reuses `agent/.venv` and talks to Bridge and Postgres over the loopback ports, so
the stack has to be up.

### Backing up and restoring

Everything meerail knows is in Postgres — the messages, their raw MIME, the attachment
bytes, the extracted text, the accounts and the per-folder sync cursors. One command puts
all of it in one file:

```bash
./meerail.sh backup                      # ~/.meerail/backups/meerail-20260804-120000.dump
./meerail.sh backup /Volumes/backup      # a directory: same name, your path
./meerail.sh backup mail.dump            # or name the file yourself
```

It runs against a live install. `pg_dump` reads a snapshot of its own, so the server keeps
serving and the agent keeps syncing while it works; what you get is the mailbox as of the
second the dump started, not a smear of the hour it took.

`backup` and `restore` are the two commands that do not need an install to have been set up
by `meerail.sh setup`. Run them from a clone whose stack you started yourself — `make up`,
or plain `docker compose up` — and they fall back to the checkout's own
`docker-compose.yml`, writing to `backups/` beside it. Nothing else does that: `update`,
`uninstall` and `config` are about an install and stay that way.

Compression happens **while the dump is produced, not after it** — `pg_dump`'s custom
format hands each block to zstd as it comes off the socket, so there is never an
uncompressed copy on disk and a backup costs only the space of the backup. The default is
zstd level 19 with long-distance matching, which is the smallest thing that is still
sensible; measured on a 232MB slice of a real 36GB mailbox:

| | size | time |
| --- | --- | --- |
| `gzip -9` | 102MB | 11s |
| `zstd -12 --long` | 72MB | 14s |
| `zstd -19` (no `--long`) | 87MB | 86s |
| **`zstd -19 --long`** (default) | **64MB** | 197s |
| `xz -9` | 63MB | 320s |

Long-distance matching is the interesting half: mail repeats itself a long way apart — one
attachment on twenty messages, one quoted thread down a hundred replies — and level 19's
ordinary 8MB window cannot see that far. Level 19 then buys another 11% for roughly sixty
times the CPU, which is the right way round for something written once and kept for years,
but not everyone's trade. To take the time back:

```bash
MEERAIL_BACKUP_COMPRESS=zstd:level=12,long ./meerail.sh backup
```

Anything `pg_dump -Z` accepts goes there. The archive records what it used, so restoring
never needs to be told.

Putting one back **replaces the database entirely** — it stops the server and the agent,
drops the database, recreates it and restores into it:

```bash
./meerail.sh restore ~/.meerail/backups/meerail-20260804-120000.dump
```

It asks first, and refuses anything that is not a `pg_dump` archive before it drops
anything. Expect the restore to take longer than the backup did; most of that is rebuilding
the trigram search index.

Two things are deliberately *not* in the dump. Your configuration — `~/.meerail/meerail.toml`,
which holds your Bridge credentials — is a separate file; copy it somewhere safe yourself,
and keep it as private as it already is. And attachments staged for a message you have
written but not yet sent live in a Docker volume rather than the database; they survive
everything except a restore taken before you wrote it, which is the same as never having
written it.

From a clone there are `make` targets for the same two operations, reading and writing the
same files as the commands above:

```bash
make backup                                    # backups/meerail-<timestamp>.dump
make backup BACKUP_COMPRESS=zstd:level=12,long
make restore FILE=backups/meerail-20260804-120000.dump
```

`make restore` stops the containerised server for you, but an agent you run natively is
yours to stop first.

## Development

```bash
make venv            # .venv with the server deps
make infra           # just Postgres + Tika in Docker
make dev             # uvicorn --reload on :8000, natively
```

Or keep everything in Docker and live-mount the source instead:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

`make help` lists every target. `make psql` opens a shell on the bundled database — worth
doing, since the schema is the point.

### Tests

```bash
python3 -m venv .venv-test
.venv-test/bin/pip install -r tests/requirements.txt
make test            # full suite on a throwaway stack, torn down after
```

The suite refuses to run against your real database — `conftest.py` aborts before collection
unless both `DATABASE_URL` and the server's `/healthz` report a `_test` database, because the
tests truncate every table they can reach. It builds a separate compose project
on shifted ports (55432 / 18000), runs unit and integration tests — including an end-to-end
pass against a GreenMail IMAP server — and discards the volume whichever way pytest went.
`make test-up` / `make test-down` / `make test-psql` drive that stack by hand. See
[`tests/README.md`](tests/README.md).

### Versions and releases

This is what a version *is* and how one gets published; moving an install onto a new one is
[How to update](#how-to-update).

One number, in one file: [`VERSION`](VERSION) at the repository root. Nothing else declares
it — `core/version.py` reads that file, the images are tagged with it, their
`org.opencontainers.image.version` label carries it, `/api/version` reports it, and
`meerail.sh` pins to it.

Pushing to main is the release. [`.github/workflows/images.yml`](.github/workflows/images.yml)
runs the test suite, and only if it passes builds all three images for `linux/amd64` and
`linux/arm64` and pushes them to Docker Hub:

| Image | What it is |
| --- | --- |
| `ribalba/meerail-server` | the web app |
| `ribalba/meerail-agent` | the mail pipeline |
| `ribalba/meerail-tika` | Apache Tika (full, with OCR) plus the JPEG2000 jars |

Each gets three tags: `:<version>`, `:<version>-<sha>` (immutable, for pinning to one exact
build) and `:latest`. So `:<version>` moves with main until `VERSION` is bumped, and cutting
a release is editing that one file — CI republishes under the new number, and every running
install notices within a day because `app/updates.py` compares itself against `VERSION` on
main. Publishing needs `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repository secrets; the
job is skipped on forks.

By hand — for a fork, or to check a build before it ships:

```bash
make version         # what everything will be tagged with
make images          # build all three for this machine, no push
make images-push     # buildx amd64+arm64 and push (needs docker login)
```

`DOCKER_ORG=you make images-push` publishes to your own namespace instead.

