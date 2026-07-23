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

2. Make sure the backing services are up:

   ```bash
   docker compose up -d
   # or, in the repo root: make up
   ```

   That puts Postgres on `127.0.0.1:5432` and Tika on `127.0.0.1:9998` — the
   addresses `config.example.toml` already points at, and the ones a
   natively-run agent needs, since it is not on the compose network.

3. Configure and run:

   ```bash
   cp config.example.toml config.toml
   $EDITOR config.toml            # Bridge host/ports/credentials + database_url
   chmod 600 config.toml          # it holds your password in plaintext
   ./run.sh --test                # check every connection before syncing
   ./run.sh --once                # one full sync pass (good first run)
   ./run.sh                       # continuous: backfill + live IDLE
   ./run.sh --backfill-previews   # render previews for mail synced before
                                  # previews existed, then exit
   ```

4. Once it syncs cleanly, stop babysitting it in a terminal — see [Running it
   as a service](#running-it-as-a-service). On macOS that is
   `./service.sh install`; on Linux, `make agent-docker` or a systemd user unit.

Previews are precomputed at ingest, so new mail needs nothing extra. Attachments
that were already in the database when you upgraded are left alone until you ask
for them with `--backfill-previews` — on a large mailbox that pass renders every
stored PDF, so it is deliberately opt-in rather than something an upgrade starts
on its own. It works in chunks and is safe to interrupt and re-run.

`run.sh` creates a `.venv`, installs `requirements.txt`, and puts the repo root
on `PYTHONPATH` so the agent can import the shared `core` package.

## Checking your setup

`./run.sh --test` verifies everything the agent depends on and exits, without
writing anything: no schema is created, no mail is fetched or sent.

```text
[  OK  ] Config file     that nobody but you can read it (it holds passwords)
[  OK  ] Database        server version, and whether the schema exists yet
[  OK  ] Tika            version — a warning, not a failure, if it's down
[  OK  ] IMAP  <account> login, negotiated TLS version, folder count
[  OK  ] SMTP  <account> login, negotiated TLS version, sendable addresses
```

Exit code is 0 when everything passes and 1 if any check fails, so it works as a
deploy gate. Every account is checked even when an earlier one fails, so one bad
password doesn't hide the state of the rest.

Three things it deliberately reports on rather than trusting your config:

- **`config.toml` must not be readable by anyone but you.** It stores mail
  passwords in plaintext, so any group or other permission bit fails the check.
  `chmod 600 config.toml` (or `400`) fixes it.

- **Tika being unreachable is a warning, not an error.** Mail still syncs; you
  just lose text extraction from attachments, so they won't be searchable.
- **It inspects the live socket to confirm the connection is really encrypted.**
  If `imap_security`/`smtp_security` promise `starttls` or `ssl` but the socket
  came up in the clear, the check says so and tells you the password was sent
  unencrypted. `plain` is treated as a deliberate choice and passes quietly.

## Reading the logs

The agent logs every pass to stdout, which is what `docker compose logs -f
agent` (or `make agent-logs`) shows. Success is logged as well as failure, on
purpose: an agent that is quiet because it is healthy would otherwise look
exactly like one that is wedged.

```text
2026-07-22 08:50:28 [didi@ribalba.de] sync loop started
2026-07-22 08:50:28 [didi@ribalba.de] connected to 127.0.0.1:1143 (starttls)
2026-07-22 08:51:04 [didi@ribalba.de] sync complete in 36.2s — 45 folders, 1832 messages examined, 12 new
```

A failed pass logs the error, an explanation where there is a useful one, and
when it will try again. Nothing here is fatal — the loop retries forever.

**`LoginError('no such user')` on startup is almost always a race, not a bad
config.** Bridge opens its IMAP port before it has finished loading accounts, so
a login in that window is answered with `no such user` even though the address
is right there in the Bridge UI. `docker compose up` hits this every time,
because the agent starts faster than Bridge does. The retry loop rides it out;
run `--test` if you want to confirm the credentials are fine.

Its companion, `LoginError('too many login attempts')`, is Bridge rate-limiting
you after a burst of failed logins. Retries are jittered per account so the
accounts don't retry in lockstep and re-trigger it — that convoy is what used to
turn a few seconds of Bridge startup into minutes of backoff.

## Running it as a service

`run.sh` is fine for a first sync, but the agent is what makes mail arrive — you
want it started at boot/login and restarted if it dies. How you do that depends
on the platform, because the agent must reach Proton Bridge on **loopback**, and
only Linux lets a container share the host's loopback.

### Linux — Docker, host network

```bash
docker compose -f docker-compose.yml -f docker-compose.agent.yml up -d
# or: make agent-docker
```

`network_mode: host` gives the container the host's network namespace, so
`127.0.0.1` inside it *is* your machine: Bridge, Postgres and Tika are all
reachable and `config.toml` needs no changes. `restart: unless-stopped` brings
the agent back after a reboot, which is the thing `run.sh` won't do for you.

The host namespace is also why the base file publishes `db` and `tika` on
loopback. Sharing the host's network means *not* being on the `meerail` network,
so those names do not resolve and their container ports are not reachable;
`127.0.0.1:5432` and `127.0.0.1:9998` are where this container looks. Bridge is
what forces the whole arrangement — it listens on loopback only, so an agent
sitting on the compose network could not reach it at any address. Syncing a
remote mailbox instead (Gmail, Fastmail, plain IMAP)? Then none of this applies:
put the agent on `networks: [meerail]` and point `database_url`/`tika_url` at
`db` and `tika`.

`config.toml` must exist before the first `up` — it is bind-mounted read-only,
and Docker silently creates a *directory* in its place if the file is missing.
It is never copied into the image; credentials stay on the host.

The one-shot modes work the same way, as `run` instead of `up`:

```bash
# Both -f flags every time; export it once to keep the lines short.
export COMPOSE_FILE=docker-compose.yml:docker-compose.agent.yml

docker compose run --rm agent --test               # or: make agent-test
docker compose run --rm agent --once
docker compose run --rm agent --backfill-previews
docker compose logs -f agent                       # or: make agent-logs
```

If you would rather not containerise it, systemd gets you the same restart
behaviour natively — a user unit (`~/.config/systemd/user/meerail-agent.service`)
with `ExecStart=%h/code/meerail/agent/run.sh`, `Restart=always`, enabled with
`systemctl --user enable --now meerail-agent`. Use a *user* unit, not a system
one: Bridge runs in your session and so must the agent.

### macOS — launchd

**Docker host networking does not work here.** On Docker Desktop the container
joins the Desktop VM's network namespace, not macOS's, so `127.0.0.1` reaches
the VM and Bridge is not there. Run the agent natively and let launchd keep it
alive in the background.

`service.sh` does the whole thing — it generates a LaunchAgent plist with this
checkout's paths baked in, loads it, and starts syncing:

```bash
cd agent
./service.sh install     # or: make agent-service, from the repo root
```

From then on the agent starts at login, is restarted if it dies, and needs no
terminal left open. The rest of the commands:

| | |
| --- | --- |
| `./service.sh status` | Running? With what PID? Plus the last few log lines. |
| `./service.sh logs` | `tail -f` the log. |
| `./service.sh restart` | Pick up an edited `config.toml`. |
| `./service.sh stop` | Stop it, but keep it installed. `start` resumes. |
| `./service.sh uninstall` | Stop it and delete the plist. |
| `./service.sh install --config PATH` | Run against a config other than `agent/config.toml`. |

It writes `~/Library/LaunchAgents/de.meerail.agent.plist` and logs to
`~/Library/Logs/meerail-agent.log`. `install` is idempotent — re-run it after
moving the checkout and it rewrites the plist with the new paths.

Four things the generated plist gets right, if you would rather write your own:

- **`KeepAlive`**, so any exit is restarted. The agent is stateless — cursors
  live in Postgres — so a restart just resumes where it left off.
- **`ThrottleInterval 30`**, because Bridge opens its IMAP port before it has
  finished loading accounts. An agent that wins that race at login gets
  `no such user`; 30s between respawns rides it out instead of hot-looping.
- **An explicit `PATH`.** launchd hands jobs a minimal environment, and `run.sh`
  needs `python3` to build the venv — often Homebrew's, not `/usr/bin`'s.
- **No `ProcessType`.** The default is `Standard`; `Background` would let the
  system throttle CPU and I/O, which is the opposite of what a multi-GB first
  sync wants.

It must be a **LaunchAgent** (`~/Library/LaunchAgents`), not a LaunchDaemon.
Daemons run before login, in a different session, where the Bridge app isn't
running — the agent would just fail its IMAP connect forever. `service.sh`
installs it as an agent for that reason and there is no daemon mode.

The log is plain text and is not rotated — launchd appends to it forever. If it
gets unwieldy, `./service.sh stop`, truncate it, `./service.sh start`.

Docker is still how you run the *rest* of the stack on macOS: `docker compose up
-d` for Postgres, Tika and the server, exactly as on Linux. Only the agent is
native.

### Windows — Task Scheduler

Same reasoning as macOS: Docker Desktop's host mode joins the VM, so the agent
runs natively. There is no `run.sh` equivalent, so create the venv once in
PowerShell:

```powershell
cd $HOME\code\meerail\agent
py -3 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = (Resolve-Path ..).Path
.\.venv\Scripts\python main.py --test          # verify before scheduling
```

Then register a scheduled task that starts it at login and restarts it on
failure:

```powershell
$agent  = "$HOME\code\meerail\agent"
$action = New-ScheduledTaskAction -Execute "$agent\.venv\Scripts\pythonw.exe" `
    -Argument "main.py" -WorkingDirectory $agent
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "meerail-agent" -Action $action -Trigger $trigger -Settings $settings
```

Four details that bite:

- **`pythonw.exe`, not `python.exe`** — otherwise every login opens a console
  window. The cost is that stdout goes nowhere; redirect it if you want logs,
  by scheduling `cmd /c ".venv\Scripts\python.exe main.py >> agent.log 2>&1"`.
- **`PYTHONPATH` must be set for the task too.** Scheduled tasks don't inherit
  your shell's environment. Set it as a *user* environment variable
  (`[Environment]::SetEnvironmentVariable('PYTHONPATH', "$HOME\code\meerail", 'User')`),
  or use the `cmd /c` form above and set it inline.
- **`-ExecutionTimeLimit ([TimeSpan]::Zero)`** disables the default 3-day kill.
  Without it the agent is stopped mid-week for no visible reason.
- **At logon, not at startup.** Bridge runs in your desktop session; a task
  running without you logged in has nothing to connect to.

`chmod 600` has no NTFS equivalent, so `--test` reports the config-permission
check as a warning on Windows instead of inspecting it. Keep `config.toml` in
your own profile directory and check its ACLs with `icacls config.toml` if the
machine has other users.

## How it works

Per account, for each IMAP folder:

1. **register** the folder, reading back its UID cursor from the database;
2. **scan** UIDs above the cursor — for each, if that Message-ID is already
   stored, just record another placement (Proton shows one message under several
   labels/folders); otherwise fetch the raw bytes;
3. **parse, thread and store** the new messages, raw MIME included;
4. **advance** the cursor;
5. **reconcile** flags (read/flagged/…) and prune messages that vanished;
6. **extract** attachment text via Tika and refresh the search index;
7. **render** previews for PDF and image attachments (see `core/mail/thumbs.py`).

Then it holds an IMAP **IDLE** on the inbox for near-real-time new mail, and
notifies the web app over Postgres `NOTIFY` so open browsers refresh.

The same channel carries traffic the other way: the refresh button in the web UI
publishes a command on `meerail_commands`, which cuts the agent's IDLE wait short
and starts a sync pass within a few seconds instead of waiting out
`poll_interval`. Commands are advisory — with no agent running the notification
is simply dropped, and the UI still reloads whatever is already in the database.

Cursors live in the **database**, so the agent is stateless — stop and restart it
anytime and it resumes where it left off.

### Full recheck

Because step 2 only ever looks *above* the cursor, a message lost or damaged
below it stays that way however often you refresh. **Recheck all mail**, in the
agent-status modal, is the repair: it rewinds every folder's cursor so the next
pass re-walks the mailbox from the start, and reconciles flags whether or not
reconcile was due. Re-ingesting is idempotent — messages dedupe on
`(account, dedup_key)` and content already stored only gains a placement row — so
it fills gaps without duplicating anything that survived.

It is also how you widen a content window after the fact: `content_window_months`
decides what gets fetched as the agent walks past a UID, so mail already stored
as headers only is filled in the next time the pass re-walks it — which is what
a recheck makes it do.

Unlike the refresh command this is a **column** on `accounts`
(`recheck_requested`), not a notification. It is the button you reach for when
the agent looks unhealthy, so the request has to keep until an agent is actually
there to serve it — across a restart or a retry backoff. The agent clears it only
once a full pass has completed, and only if no newer request arrived meanwhile;
a pass that dies partway leaves the request standing and runs again.

## Notes

- `verify_cert = false` trusts Bridge's self-signed TLS cert. Set a real cert
  path story if you harden this later.
- `imap_security` / `smtp_security`: `starttls` (Bridge default), `ssl`, or
  `plain` (e.g. for a local test server).
- `imap_connect_timeout` / `imap_read_timeout` (default 10s / 60s) bound how
  long a single socket operation may block. They matter more than they look:
  with no read timeout, a Bridge that stops answering mid-pass leaves the sync
  thread blocked in `recv()` forever. Nothing raises, so nothing is logged and
  no `last_error` is recorded — and the app reports the account as **offline**
  once `last_agent_seen` ages out, blaming a dead agent for a live one that is
  merely deaf. With the timeout the stall surfaces as a normal failure: logged,
  recorded, and retried under the usual backoff. The read timeout applies per
  operation, so a large fetch trips it only if the server goes fully silent.
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
