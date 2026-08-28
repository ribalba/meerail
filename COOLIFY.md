# Deploying meerail with Coolify

This deploys the whole stack — Postgres, Tika, the server, the agent **and Proton
Bridge** — as one Coolify resource from [`docker-compose.coolify.yml`](docker-compose.coolify.yml).
Coolify's Traefik terminates TLS in front of the server; nothing else is reachable
from outside the host.

## Read this part first

The layout in [README.md](README.md) keeps your mail credentials on a machine you
physically own: Bridge runs on your laptop, the agent runs beside it, and the
container half of the app only ever reads Postgres. Putting Bridge on a rented VPS
gives that up. Your Proton *password* still never leaves the Bridge container, but
the logged-in Bridge session, its keyring and the Bridge-issued IMAP/SMTP password
now sit on the host's disk, and your entire mailbox sits in a Postgres next to them.
Whoever controls the host controls your mail.

That is a reasonable trade for a machine you administer and trust. It is not one to
make on shared or untrusted infrastructure.

## What you need

| | |
| --- | --- |
| **Host** | 8 GB RAM and a disk sized to your mailbox. The compose file's memory limits total ~9 GB of *ceilings*, which overcommit fine; the real floor is Postgres + Tika + one agent batch. See [Smaller hosts](#smaller-hosts). |
| **Disk** | Tens of GB for a large account. `store_raw_mime = false` roughly halves it; `content_window_months` bounds it properly. |
| **Coolify** | v4.0.0-beta.411 or newer if you want the magic-variable domain wiring; any v4 works if you set the domain in the UI. |
| **Arch** | amd64 or arm64. The Bridge image tag used here (`:build`) is the source-built multi-arch one — `:latest` repacks the amd64-only .deb. |
| **A wildcard domain** | Or at least one A record pointing at the Coolify host, for the server's hostname. |
| **SSH to the host** | Non-negotiable: logging Bridge into Proton is interactive and Coolify has no way to drive it. |

Tika's `-full` image bundles Tesseract for OCR and is a multi-GB pull. If the
host is disk-tight, edit the `tika` service to `image: apache/tika:4.0.0` and drop
the `build:` line — text extraction still works, images just come back empty. Note
that the stock image also drops the settings meerail builds in (tika/README.md):
notably the parse budget and the size of Tika's forked-parser pool, which it
otherwise derives from the host's core count regardless of the memory limit.

One thing to know before you start: **plain `docker compose` rejects this file.**
The agent's config mount uses Coolify's `content:` extension, which is not a
Compose key — Coolify strips it and writes the file out before Docker sees any of
it. That is also what prevents Docker from silently creating a *directory* where
`meerail.toml` should be on a first deploy. `docker-compose.yml` remains the one you
run locally.

## 1. Create the resource

1. **+ New → Docker Compose** (under Applications), pointing at this repository.
2. Set **Docker Compose Location** to `/docker-compose.coolify.yml`.
3. Save. Coolify parses the file and pre-fills the Environment Variables tab with
   everything it references. **Do not deploy yet.**

## 2. Set the environment variables

In the resource's **Environment Variables** tab:

| Variable | |
| --- | --- |
| `POSTGRES_PASSWORD` | **Required.** Coolify refuses the deploy while it is empty — the compose file marks it `:?` because the shipped laptop default (`meerail`) is public knowledge. Generate it with `python -c "import secrets;print(secrets.token_urlsafe(32))"` and **use nothing but letters, digits and `- _ . ~`**: this value is interpolated into `DATABASE_URL`, which is a URL, so an `@` or a `%` in it is read as URL structure and the stack fails to start in a way that does not mention passwords at all. See [`failed to resolve host '...@db'`](#failed-to-resolve-host-db). |
| `SERVER_PASSWORD` | **Required.** This is the password the web UI asks for. Also marked `:?`: an empty one means no auth at all, which is correct for localhost and wrong for a public hostname. Setting it also turns on the HTTPS requirement — with a password configured, meerail serves *nothing* over a plaintext connection, not just the login route. On this stack that means Traefik plus `TRUSTED_PROXIES` below; without them the app cannot tell an encrypted request from a plaintext one and answers every request with a 421 saying so. |
| `API_TOKEN` | Optional, empty by default. Set it only if something other than a browser needs the API (`Authorization: Bearer <token>`); the UI password is not accepted for that. |
| `SERVICE_PASSWORD_64_MEERAIL` | Leave it alone — Coolify generates it once and reuses it forever. It signs session cookies, so changing it logs every browser out. |
| `POSTGRES_USER`, `POSTGRES_DB` | Optional, default `meerail`. |
| `SESSION_MAX_AGE_DAYS` | Optional, default 30. |
| `TRUSTED_PROXIES` | Optional, and already set to the private ranges Docker hands out — which is where Coolify's Traefik reaches this container from. It is what makes the app see the *browser* rather than the proxy: without it the session cookie is issued without `Secure` (TLS ends at Traefik, so every request looks like plain HTTP) and the login rate limiter counts one attacker's five wrong passwords against everyone behind the proxy. Leave the field empty and take the default; see [What to set `TRUSTED_PROXIES` to](#what-to-set-trusted_proxies-to) for narrowing it, and for why `meerail.toml` cannot set it on this stack. |
| `HSTS_MAX_AGE_DAYS` | Optional, default 365. How long a browser remembers to reach this hostname over HTTPS only, which is what removes the *first* plaintext request rather than turning it away. It is a promise with a duration — a browser that has been here will refuse plain HTTP to this name, and refuse to let you click past a bad certificate, for this long. Set it to 0 while you are still moving the install between hostnames. |
| `MAX_REQUEST_BYTES` | Optional, default 8 MB. Ceiling on ordinary (JSON) request bodies, applied before the body is read and before it is authenticated. Uploads to the composer get `max_attachment_bytes` instead. Leave it unless something legitimate is being refused with a 413. |
| `DEFAULT_SEARCH_YEARS`, `CONTACTS_SCAN_YEARS` | Optional; see [README § Configuration](README.md#configuration). |

### What to set `TRUSTED_PROXIES` to

**Leave it empty in the Coolify UI.** The compose file already gives it
`10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` — the private ranges Docker hands out
on its own networks, which is where Coolify's Traefik reaches this container
from. There is no address to look up and nothing to type; an empty field in the
Environment Variables tab means the compose default applies.

Note that this is one of the settings that cannot be set in `meerail.toml` on
this stack. The compose file puts `TRUSTED_PROXIES` in the server's environment,
and the environment wins over the file — so a value written into the agent's
`coolify/meerail.toml` is read and then overridden. The Coolify UI is the only
place to change it here.

Those three ranges are wide on purpose, because Docker picks the project
network's subnet itself and renumbers it when the resource is recreated. They
are safe here because nothing else can reach the server: it uses `expose`, not
`ports`, so port 8000 exists only on the project network, and the only thing on
that network besides meerail's own services is the proxy. What the setting
grants is the right to rewrite a request's apparent source and scheme with
`X-Forwarded-For` / `X-Forwarded-Proto`, and only something that can already
open a connection to port 8000 can send those headers at all.

If you would rather be exact, read the subnet off the project network and use
that one CIDR:

```bash
ssh root@your-coolify-host

# The project network is the one the server container is attached to.
docker inspect <server-container> \
  --format '{{range $net, $_ := .NetworkSettings.Networks}}{{$net}}{{println}}{{end}}'
docker network inspect <that-network> \
  --format '{{range .IPAM.Config}}{{.Subnet}}{{println}}{{end}}'
```

Set `TRUSTED_PROXIES` to the subnet it prints (something like
`10.0.1.0/24`) and redeploy. Expect to revisit it: Coolify creates a fresh
network when the resource is destroyed and recreated, and the new one may not
have the same subnet — at which point the exact value is wrong and the wide
default would still have been right.

Never set it to `*`. That trusts whatever opens the connection, on a host where
you may later run something you did not write on the same Docker bridge.

**Whether it is working**, once the domain is up: the server's log lines carry
the client address, and with the setting live that address is your browser's
public IP rather than a `10.` or `172.` one. Getting a 421 from every request —
the response body names this variable — means the opposite: the request arrived
over HTTPS, the app could not confirm it, and it will not hand out the UI or a
session until it can.

## 3. Give the server a domain

On the **`server`** service, set the domain to `https://mail.example.com:8000` —
the `:8000` suffix tells Coolify which container port to route to. Coolify writes
the Traefik labels and gets the certificate.

Declarative alternative: add `SERVICE_FQDN_SERVER_8000: ${SERVICE_FQDN_SERVER_8000}`
to the `server` environment block in the compose file and let Coolify generate a
hostname off your wildcard domain. The UI field is the reliable path; use it if the
magic variable does not take.

Do **not** add `ports:` to any service. `db`, `tika` and `bridge` have no
authentication worth the name between them — Postgres has whatever password you
set, Tika extracts whatever anyone POSTs it, and Bridge hands your mail to anyone
who reaches port 143.

### Cap request bodies at Traefik too

meerail enforces its own ceiling (`MAX_REQUEST_BYTES`, and
`max_attachment_bytes` for the composer's uploads), and it does so before the
body is read or authenticated. Traefik should have one as well: it can refuse
the connection without waking Python at all, which is the only thing that helps
a server already busy with the previous request.

Add a buffering middleware to the `server` service's labels — the ceiling wants
to sit just above the largest attachment you intend to send, not at it, because
MIME and the multipart envelope add to what the browser puts on the wire:

```yaml
    labels:
      - traefik.http.middlewares.meerail-body.buffering.maxRequestBodyBytes=110000000
      - traefik.http.middlewares.meerail-body.buffering.memRequestBodyBytes=1048576
```

then name `meerail-body` in the router's `middlewares` list. Coolify writes the
router labels itself, so add the middleware to the list it generates rather than
replacing it. Lower `maxRequestBodyBytes` to a few megabytes if this install
never sends large attachments — nothing else here posts a large body.

## 4. First deploy

Deploy. Expect it to take a while — Tika's image is large and both meerail images
build from source.

Four services come up healthy and **the agent will not**: it has a placeholder
`meerail.toml` at this point and no Bridge account to talk to. Its logs saying it
cannot authenticate are the expected state until step 6.

## 5. Log Bridge in

The one step Coolify cannot do. Bridge's login is an interactive CLI session with a
2FA prompt, so it happens over SSH — and it must run against **the same volume the
`bridge` container has mounted**, which is the part that usually goes wrong. Coolify
prefixes volume names with the resource UUID, and the upstream image's README uses a
volume called `protonmail`, so it is easy to initialise one volume and run another.

Read the name off the container rather than guessing it:

```bash
ssh root@your-coolify-host

docker ps -a --filter name=bridge --format '{{.Names}}\t{{.Status}}'
docker inspect <bridge-container> \
  --format '{{range .Mounts}}{{.Name}} -> {{.Destination}}{{println}}{{end}}'
```

Stop the container first — Bridge holds an exclusive lock on the vault, and a
crash-looping one will fight the init:

```bash
docker stop <bridge-container>
docker run --rm -it -v <that-volume-name>:/root dancwilliams/protonmail-bridge init
```

That entrypoint generates the GPG key, initialises the `pass` store and drops you
into the Bridge CLI. Then:

```
>>> login          # email, password, 2FA — your real Proton credentials
>>> info           # prints the Bridge-issued IMAP/SMTP settings
>>> exit
```

**Copy what `info` prints.** The username and password there are issued by Bridge
and are not your Proton password — they are what goes into the agent's config in
the next step. The ports it reports (1143/1025) are Bridge's own; the container's
socat republishes them as 143 and 25, which is what the agent connects to.

Confirm the store landed in the volume before starting anything back up — this is
the check that distinguishes "logged in" from "logged into the wrong volume":

```bash
docker run --rm --entrypoint bash -v <that-volume-name>:/root \
  dancwilliams/protonmail-bridge -c 'ls /root/.password-store /root/.gnupg'
```

Then start the `bridge` service again in Coolify. Its log should reach
`Listening on 127.0.0.1:1143` with no keychain errors.

## 6. Fill in the agent config

The compose file declares `/app/meerail.toml` as a Coolify file mount with a
placeholder body, so after the first deploy it appears under the resource's
**Storages** tab. Edit it there — not in the repository, where the password would
be committed.

It carries only an `[agent]` section. On this stack the `server` service is
configured entirely from its environment block (the variables you set in step 2)
and mounts no file at all — the precedence in
[README § Configuration](README.md#configuration) is built for exactly that — so
`[database]` and `[server]` here would be read by nothing.

Only two things to change, both from what `info` printed:

- `email` / `username` — your address and the Bridge username.
- `password` — the Bridge password.

Everything else already points at the right places: `imap_host = "bridge"` on port
143, `smtp_host = "bridge"` on port 25, and `verify_cert = false` because Bridge's
self-signed certificate is issued for 127.0.0.1 while we reach it as `bridge` — a
hop that is container-to-container on the host's own bridge network and never
touches the wire.

Note what is *absent* from that file: `[database].url` and `agent.tika_url`. The
compose file passes both as environment variables instead, and the environment
outranks `meerail.toml`. That is deliberate — it keeps the Postgres password in one
place (Coolify's `POSTGRES_PASSWORD`) rather than also hand-copied into a file that
then drifts. Adding either key back to the Storages file achieves nothing: the
environment still wins.

Redeploy the agent. Watch its logs: it creates the schema, backfills, and the
account appears in the UI on its own after the first successful sync.

To check its wiring without syncing anything:

```bash
docker exec -it <agent-container> python /app/agent/main.py --test
```

It reports Postgres, Tika, IMAP and SMTP per account and exits without writing.
It will also warn that the config file is world-readable — Coolify creates file
mounts that way. The warning is accurate (the file holds a mail password in
plaintext on the host) and does not block anything.

## Smaller hosts

The limits in the compose file assume ~8 GB. On 4 GB, in this order:

1. `tika` → `image: apache/tika:4.0.0`, drop `build:`, limit `1g`. Loses OCR of
   scanned PDFs and images; ordinary text extraction is unaffected. It also
   loses the config the built image carries, and on a many-core host Tika then
   starts a forked parser per pair of cores and sizes each one as a share of
   memory it does not know is capped at 1g — so if this is the step you take,
   raise the limit rather than trusting it. Dropping `build:` is the saving
   worth having here; the 1g is not.
2. `agent` → `batch_size = 25` in the Storages file, limit `1g`. The peak is one batch
   of complete raw MIME messages held in memory at once, so this scales roughly
   linearly and costs only extra round trips.
3. `db` → `shared_buffers=256MB`, `effective_cache_size=1GB`,
   `maintenance_work_mem=256MB`, limit `1500m`.

Disk is the other axis: `content_window_months = 24` in the Storages file keeps only the
last two years of message *content*. Older mail stays listed, threaded and
searchable by subject and correspondent, and the window slides — already-stored mail
is stripped back to headers as it passes out of it. Nothing is deleted from Proton.

## Troubleshooting Bridge

### `pass not initialized` / `Could not load/create vault key`

```
WARN Failed to add test credentials to keychain  error="failed to open dbus connection: exec: \"dbus-launch\": executable file not found in $PATH"
WARN Failed to add test credentials to keychain  error="pass not initialized: exit status 1: Error: password store is empty. Try \"pass init\"."
ERRO Could not load/create vault key             error="could not create keychain: no keychain"
     Proton Mail Bridge is not able to detect a supported password manager
```

Bridge has no keychain to store its vault key in, so it exits, and
`restart: unless-stopped` loops it. It means **step 5 never ran against this
volume** — either it was skipped, or `init` initialised a different volume than the
one the container mounts (`protonmail` from the upstream README instead of Coolify's
UUID-prefixed `bridge-data` is the usual mix-up).

Only the third line matters. The `dbus-launch` one is Bridge trying the
secret-service backend first and falling back to `pass`, which is the intended path
in a container; the `unleash_startup_flags.json` one is a cold cache on first start.
Both are noise even on a working Bridge.

Fix: run step 5 against the volume name you read off the container with
`docker inspect`, then verify `/root/.password-store` exists before restarting.

If `init` itself fails partway — `set -ex` in the entrypoint aborts it if
`gpg --generate-key` finds a half-written keyring from an earlier attempt — clear
the two directories and start over. This throws away the Bridge login only; nothing
in Postgres is touched:

```bash
docker run --rm --entrypoint bash -v <volume>:/root \
  dancwilliams/protonmail-bridge -c 'rm -rf /root/.gnupg /root/.password-store'
```

## Troubleshooting Postgres

### `failed to resolve host '...@db'`

```
sqlalchemy.exc.OperationalError: (psycopg.OperationalError)
failed to resolve host 'U7…FhXWG@db': [Errno -2] Name or service not known
ERROR:    Application startup failed. Exiting.
```

Not DNS, and nothing to do with the compose network. `POSTGRES_PASSWORD`
contains an `@`, and `DATABASE_URL` is a URL: the parser splits on the *first*
`@` it finds, so the tail of your password gets read as the hostname. The
password is in the error message — that is the giveaway, and it is also a reason
to scrub the log line before pasting it anywhere.

`%` does the same damage more quietly. It introduces a percent-escape, so a
password containing `%0f` arrives at Postgres with a literal `0x0F` byte in place
of those three characters, and the connection is refused for a reason that names
nothing.

**Do not percent-encode `POSTGRES_PASSWORD` in Coolify.** That variable is used
twice on this stack — as the literal password `initdb` gives the `meerail` role,
and interpolated into the two `DATABASE_URL` lines — so encoding it changes the
first and cancels out in the second, leaving the two sides disagreeing about what
the password even is.

Set it to a password of letters, digits and `- _ . ~` only:

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

`token_urlsafe` emits exactly that alphabet, which is why it is the generator
named here.

Then deal with the database that already exists, because `POSTGRES_PASSWORD` is
applied by `initdb` and only on the first start against an empty volume —
changing it in Coolify changes what the clients send and nothing about what the
server expects. If the deploy never got as far as syncing mail, deleting the
`pg-data` volume and redeploying is the shortest way out:

```bash
ssh root@your-coolify-host
docker volume ls --filter name=pg-data
docker volume rm <the one for this resource>   # the stack must be stopped
```

If there is mail in it already, change the role instead — no dump, no data loss:

```bash
docker exec -it <db-container> \
  psql -U meerail -d meerail -c "ALTER USER meerail WITH PASSWORD '<the new one>';"
```

### `password authentication failed for user "meerail"`

```
FATAL:  password authentication failed for user "meerail"
DETAIL: Connection matched file ".../pg_hba.conf" line 128: "host all all all scram-sha-256"
```

Look one line further up in the `db` log for this:

```
PostgreSQL Database directory appears to contain a database; Skipping initialization
```

`POSTGRES_PASSWORD` is only ever applied by `initdb`, on the very first start
against an empty volume. Once `pg-data` exists, changing that variable in Coolify
changes what the *clients* send and nothing about what the server expects. A
password you edited after the first deploy is therefore ignored by the database and
obeyed by everything connecting to it.

Which side is wrong is worth establishing before changing anything. Test the
password the way a client does — over TCP, so it goes through the `scram-sha-256`
rule rather than the container's `local ... trust` one:

```bash
docker exec -e PGPASSWORD='<what Coolify has>' <db-container> \
  psql -h 127.0.0.1 -U meerail -d meerail -c 'select 1'
```

If that fails, the database holds an older password. Set it to the current one —
no dump, no data loss, and the local socket is `trust` so it needs no password:

```bash
docker exec -it <db-container> \
  psql -U meerail -d meerail -c "ALTER USER meerail WITH PASSWORD '<what Coolify has>';"
```

If it succeeds and only the *agent* is failing, check `DATABASE_URL` on the agent
service itself — the environment is what it uses, and the Storages file cannot
override it (see step 6).

Third possibility, if both look right: the password contains a URL
metacharacter. `DATABASE_URL` is a URL, so `@`, `:`, `/`, `?`, `#`, `[`, `]` and
`%` inside the password are read as structure rather than as password. An `@`
usually fails loudly and elsewhere — see [`failed to resolve host
'...@db'`](#failed-to-resolve-host-db) above — but a `%` lands here instead: the
client sends a password that is not the one you typed, and Postgres says only
that it was wrong. Use a password of letters, digits and `- _ . ~` only; that
section explains why percent-encoding is not the fix on this stack.

### The agent logs authentication failures

Expected between the first deploy and the end of step 6. The agent restarts on a
backoff and picks up on its own once Bridge is logged in and the config has the real
Bridge password — no redeploy needed beyond the one that reloads `meerail.toml`.

A crash-looping `bridge` does **not** restart the rest of the stack: the agent's
`depends_on` uses `condition: service_started`, which is satisfied once and not
re-evaluated. What you see in Coolify is the resource reported unhealthy because one
container is looping.

## Operating it

| | |
| --- | --- |
| **Bridge session expired** | Restart the `bridge` service first. If it still cannot authenticate, re-run the `init` command from step 5 and `login` again; the Bridge password does not change, so the agent config stays valid. |
| **Backups** | The `pg-data` volume is the mailbox. `bridge-data` is only a login you can recreate; `mail-data` is scratch space for outgoing attachments. |
| **Upgrades** | Redeploy. The agent is stateless — its cursors are rows in Postgres — so it resumes mid-backfill without repeating work. |
| **Postgres major upgrade** | The volume is mounted at `/var/lib/postgresql`, not `.../data`, which is what keeps `pg_upgrade --link` available later. |
| **Adding an account** | Another `[[agent.account]]` block in the Storages config, and another `init`/`login` against the Bridge volume. |

## Local development is unaffected

`docker-compose.yml` and `docker-compose.agent.yml` are untouched; `make up` /
`make agent` work exactly as [README.md](README.md) describes. This file is
additive.
