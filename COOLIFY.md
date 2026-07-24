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
| **Disk** | Tens of GB for a large account. `store_raw_mime = false` (the default in this file) roughly halves it; `content_window_months` bounds it properly. |
| **Coolify** | v4.0.0-beta.411 or newer if you want the magic-variable domain wiring; any v4 works if you set the domain in the UI. |
| **Arch** | amd64 or arm64. The Bridge image tag used here (`:build`) is the source-built multi-arch one — `:latest` repacks the amd64-only .deb. |
| **A wildcard domain** | Or at least one A record pointing at the Coolify host, for the server's hostname. |
| **SSH to the host** | Non-negotiable: logging Bridge into Proton is interactive and Coolify has no way to drive it. |

Tika's `latest-full` image bundles Tesseract for OCR and is a multi-GB pull. If the
host is disk-tight, edit the `tika` service to `image: apache/tika:latest` and drop
the `build:` line — text extraction still works, images just come back empty.

One thing to know before you start: **plain `docker compose` rejects this file.**
The agent's config mount uses Coolify's `content:` extension, which is not a
Compose key — Coolify strips it and writes the file out before Docker sees any of
it. That is also what prevents Docker from silently creating a *directory* where
`config.toml` should be on a first deploy. `docker-compose.yml` remains the one you
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
| `POSTGRES_PASSWORD` | **Required.** Generate a long random one. Coolify refuses the deploy while it is empty — the compose file marks it `:?` because the shipped laptop default (`meerail`) is public knowledge. |
| `SERVER_PASSWORD` | **Required.** This is the password the web UI asks for. Also marked `:?`: an empty one means no auth at all, which is correct for localhost and wrong for a public hostname. |
| `SERVICE_PASSWORD_64_MEERAIL` | Leave it alone — Coolify generates it once and reuses it forever. It signs session cookies, so changing it logs every browser out. |
| `POSTGRES_USER`, `POSTGRES_DB` | Optional, default `meerail`. |
| `SESSION_MAX_AGE_DAYS` | Optional, default 30. |
| `DEFAULT_SEARCH_YEARS`, `CONTACTS_SCAN_YEARS` | Optional; see [README § Configuration](README.md#configuration). |

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

## 4. First deploy

Deploy. Expect it to take a while — Tika's image is large and both meerail images
build from source.

Four services come up healthy and **the agent will not**: it has a placeholder
`config.toml` at this point and no Bridge account to talk to. Its logs saying it
cannot authenticate are the expected state until step 6.

## 5. Log Bridge in

The one step Coolify cannot do. Bridge's login is an interactive CLI session with a
2FA prompt, so it happens over SSH, against the same volume the running container
uses.

```bash
ssh root@your-coolify-host

# Coolify prefixes volume names with the resource UUID.
docker volume ls | grep bridge-data

docker run --rm -it -v <that-volume-name>:/root shenxn/protonmail-bridge:build init
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

Restart the `bridge` service in Coolify afterwards so the running container picks
up the now-populated volume.

## 6. Fill in the agent config

The compose file declares `/app/agent/config.toml` as a Coolify file mount with a
placeholder body, so after the first deploy it appears under the resource's
**Storages** tab. Edit it there — not in the repository, where the password would
be committed.

Three things to change:

- `database_url` — replace `CHANGE-ME-SAME-AS-POSTGRES_PASSWORD` with the
  `POSTGRES_PASSWORD` you set in step 2. (Keep the rest: `db:5432` is the service
  name on Coolify's project network.)
- `email` / `username` — your address and the Bridge username from `info`.
- `password` — the Bridge password from `info`.

Everything else is already pointed at the right places: `imap_host = "bridge"` on
port 143, `smtp_host = "bridge"` on port 25, `tika_url = "http://tika:9998"`, and
`verify_cert = false` because Bridge's self-signed certificate is issued for
127.0.0.1 while we reach it as `bridge`. That hop is container-to-container on the
host's own bridge network and never touches the wire.

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

1. `tika` → `image: apache/tika:latest`, drop `build:`, limit `1g`. Loses OCR of
   scanned PDFs and images; ordinary text extraction is unaffected.
2. `agent` → `batch_size = 25` in `config.toml`, limit `1g`. The peak is one batch
   of complete raw MIME messages held in memory at once, so this scales roughly
   linearly and costs only extra round trips.
3. `db` → `shared_buffers=256MB`, `effective_cache_size=1GB`,
   `maintenance_work_mem=256MB`, limit `1500m`.

Disk is the other axis: `content_window_months = 24` in `config.toml` keeps only the
last two years of message *content*. Older mail stays listed, threaded and
searchable by subject and correspondent, and the window slides — already-stored mail
is stripped back to headers as it passes out of it. Nothing is deleted from Proton.

## Operating it

| | |
| --- | --- |
| **Bridge session expired** | Restart the `bridge` service first. If it still cannot authenticate, re-run the `init` command from step 5 and `login` again; the Bridge password does not change, so the agent config stays valid. |
| **Backups** | The `pg-data` volume is the mailbox. `bridge-data` is only a login you can recreate; `mail-data` is scratch space for outgoing attachments. |
| **Upgrades** | Redeploy. The agent is stateless — its cursors are rows in Postgres — so it resumes mid-backfill without repeating work. |
| **Postgres major upgrade** | The volume is mounted at `/var/lib/postgresql`, not `.../data`, which is what keeps `pg_upgrade --link` available later. |
| **Adding an account** | Another `[[account]]` block in the Storages config, and another `init`/`login` against the Bridge volume. |

## Local development is unaffected

`docker-compose.yml` and `docker-compose.agent.yml` are untouched; `make up` /
`make agent` work exactly as [README.md](README.md) describes. This file is
additive.
