# meerail journal

Keeps several installs of meerail agreeing about the things IMAP has nowhere to
put.

Mail syncs itself. Three machines against the same accounts see the same folders,
the same read flags, the same conversations — that is what IMAP is for. What does
not sync is everything meerail decided: a conversation parked on a reminder until
Monday, the footer the composer prefills, the name and colour an account wears in
the sidebar. There is no header, flag and no folder for any of it, so each install
keeps its own answer and they drift apart.

This is a small server that holds an ordered log of those decisions. Each install
appends what it did and reads what the others did. It is the only piece of meerail
designed to run somewhere else, so it is built to be worth as little as possible
to whoever runs it.

## What the server can and cannot see

Records are sealed on the machine that writes them, with a key derived from a
passphrase the server never receives. What it stores is ciphertext, an integer,
and a timestamp. It cannot read a subject, an address, a folder name or a due
date, and it cannot write a record — Fernet's authentication means a modified
blob simply will not open.

What it *is* trusted with is ordering and availability. A hostile host can
withhold records or stop answering, and the machines then stop agreeing; it
cannot make them agree on something it invented.

It also never gets the passphrase itself. What goes in its configuration is a
hash of the derived token, so reading the server's environment — or its whole
database — yields nothing that can be replayed as a login.

## Setting it up

**1. Make a passphrase.** On your own machine, in a meerail checkout:

```
python -m journal.keys
```

That prints two things: a passphrase and a `JOURNAL_SPACES=` line. Keep them
apart — the passphrase goes to the meerail installs, the hash to the server.

**2. Run the server** on whatever is always on:

```
JOURNAL_SPACES=<the hash> docker compose -f docker-compose.journal.yml up -d
```

It listens on `127.0.0.1:8080`. Put TLS in front before anything else can reach
it: the records stay sealed over a plaintext hop, but the bearer token does not,
and a stolen token is a way to append junk and read ciphertext.

**3. Point each meerail at it.** In `meerail.toml` on every machine:

```toml
[journal]
url        = "https://journal.example.com"
passphrase = "the passphrase from step 1"
instance   = "thinkpad"          # optional; defaults to the hostname
```

Restart the servers. Each logs `journal: syncing with ...` on startup, posts a
snapshot of what it already knows, and starts reading. A reminder set on one
machine appears on the others within about a minute.

An install with no `[journal]` section makes no outbound request and runs no
extra loop — this is off unless you turn it on.

## Configuration

| Variable | Default | |
|---|---|---|
| `JOURNAL_SPACES` | *(required)* | Comma-separated token hashes. More than one lets a single server hold unrelated journals that cannot see each other. |
| `JOURNAL_DATABASE_URL` | `sqlite:////data/journal.db` | Postgres works too, and is overkill for a log three machines append to a few times a day. |
| `JOURNAL_RETAIN_DAYS` | `90` | How long a *superseded* record is kept. Records no snapshot has replaced are never dropped, whatever this says. |
| `JOURNAL_MAX_BLOB_BYTES` | `262144` | Per-record ceiling. A sealed reminder is a few hundred bytes. |

## How much it holds

A record per reminder set, cancelled or fired, per account-presentation change,
and one claim per reminder that comes due — a few hundred bytes each. A daily
snapshot from each install restates the current state, which is what lets the
server delete anything at all: it cannot read a record, so it cannot work out for
itself that a later one covers an earlier one. Nothing is pruned that a snapshot
has not superseded, so a laptop shut in a drawer for a month catches up rather
than silently missing what it was owed.

## What is synced

- **Reminders** — set, cancel, fired, and the claim that decides which install
  brings a conversation back (all three watch the same clock; without it all
  three would move the same mail).
- **Account presentation** — `label`, `color`, `footer`. A field pinned in an
  install's own `meerail.toml` is left alone there: the file is the more specific
  instruction, and the agent rewrites it on every pass anyway.

Adding a third kind is a handler and a publisher in `app/journal.py`. Nothing in
this server changes — it does not know what a reminder is, and should not.
