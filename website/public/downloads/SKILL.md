---
name: meerail
description: Query the user's own email archive in the meerail PostgreSQL database — search messages by regex, sender, date or attachment text, read full bodies, and summarise threads. Use whenever the user asks about their email ("what did X say", "find the invoice from", "summarise that thread", "who never replied").
---

# meerail — querying your mail archive

meerail syncs every message from the user's mail accounts into PostgreSQL. This
skill tells you how to reach that database and what is in it, so you can answer
questions about their mail with SQL instead of guessing.

## Connecting

The database runs as the `db` service of meerail's `docker-compose.yml`. It is
**not** published on a host port by default, so go through the container:

```bash
docker compose exec -T db psql -U meerail -d meerail -c "SELECT count(*) FROM messages;"
```

Run this from the meerail checkout (where `docker-compose.yml` lives), or add
`-f /path/to/meerail/docker-compose.yml`. If the compose project was renamed,
find the container with `docker ps --filter name=db`.

Default credentials, from `.env.example`:

| Setting  | Default   |
|----------|-----------|
| User     | `meerail` |
| Password | `meerail` |
| Database | `meerail` |
| Host     | `db` (inside the compose network) |
| Port     | `5432`    |

These are overridden by `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`
in the user's `.env` — read that file first if the defaults are rejected.

The stack also publishes Postgres on the host's loopback, so you can connect
directly instead:

```bash
PGPASSWORD=meerail psql -h localhost -p 5432 -U meerail -d meerail -c "..."
```

Prefer heredocs for multi-line SQL so quoting survives:

```bash
docker compose exec -T db psql -U meerail -d meerail <<'SQL'
SELECT date_sent, from_addr, subject FROM messages ORDER BY date_sent DESC LIMIT 20;
SQL
```

## Schema

`messages` is the table you want almost every time.

**`messages`** — one row per unique message (deduplicated by Message-ID).

| Column | Notes |
|---|---|
| `id` | primary key, referenced as `message_pk` elsewhere |
| `account_id` | → `accounts.id` |
| `message_id` | RFC 5322 Message-ID |
| `thread_id`, `in_reply_to`, `references` | threading |
| `subject`, `subject_norm` | `subject_norm` has `Re:`/`Fwd:` stripped |
| `from_name`, `from_addr` | sender |
| `date_sent`, `date_received` | timestamps (UTC, naive) |
| `snippet` | short preview |
| `body_text`, `body_html` | full body |
| `search_text` | subject + participants + body **plus extracted attachment text** — search this |
| `has_attachments`, `size_bytes` | |
| `raw_mime` | the original `.eml` bytes (`bytea`) — select it only when you need the raw message; `NULL` if raw storage was turned off when the mail was synced |
| `content_status` | `full`, or `skipped`/`pruned` for mail outside the content window — those rows have headers but no body, attachments or search text beyond the headers. Filter on `content_status = 'full'` when a query assumes a body exists |

**`accounts`** — `id`, `email`, `label`, `color`, `active`.

**`mailboxes`** — `id`, `account_id`, `imap_name`, `display_name`, `role`
(`inbox` / `sent` / `archive` / `trash` / `spam` / `drafts` / `custom`),
`unread_count`, `total_count`.

**`message_locations`** — which folders a message sits in, plus its per-folder
flags: `message_pk`, `mailbox_id`, `imap_uid`, `seen`, `flagged`, `answered`,
`draft`, `deleted`. A message can appear in several mailboxes.

**`recipients`** — `message_pk`, `kind` (`from`|`to`|`cc`|`bcc`|`reply_to`),
`name`, `address`. Join here to find mail *to* someone; `from_addr` on
`messages` only covers senders.

**`attachments`** — `message_pk`, `filename`, `content_type`, `size_bytes`,
`is_inline`, `content` (the bytes, `bytea`), `extracted_text` (PDF/Office text
via Tika), `extract_status`. Avoid selecting `content` in exploratory queries —
it is the full attachment.

**`contacts`** — `address`, `name`, `count` (times corresponded).

## Recipes

Regex search across bodies and attachment text (`~*` is case-insensitive POSIX
regex; `pg_trgm` indexes back it up):

```sql
SELECT id, date_sent, from_addr, subject
FROM messages
WHERE search_text ~* 'invoice|rechnung'
ORDER BY date_sent DESC
LIMIT 50;
```

Everything from one sender in a date range:

```sql
SELECT date_sent, subject, snippet
FROM messages
WHERE from_addr ILIKE '%@accountant.example%'
  AND date_sent >= '2025-01-01' AND date_sent < '2026-01-01'
ORDER BY date_sent;
```

A whole thread, oldest first, to summarise:

```sql
SELECT date_sent, from_addr, body_text
FROM messages
WHERE thread_id = (SELECT thread_id FROM messages WHERE id = 12345)
ORDER BY date_sent;
```

Unread mail in the inbox of every account:

```sql
SELECT a.email, m.date_sent, m.from_addr, m.subject
FROM messages m
JOIN message_locations l ON l.message_pk = m.id
JOIN mailboxes b ON b.id = l.mailbox_id
JOIN accounts a ON a.id = m.account_id
WHERE b.role = 'inbox' AND NOT l.seen AND NOT l.deleted
ORDER BY m.date_sent DESC;
```

Mail addressed to a person (not just from them):

```sql
SELECT m.date_sent, m.subject
FROM messages m
JOIN recipients r ON r.message_pk = m.id
WHERE r.kind IN ('to','cc') AND r.address = 'sam@example.com'
ORDER BY m.date_sent DESC;
```

Find a document by its contents:

```sql
SELECT m.date_sent, m.from_addr, m.subject, at.filename
FROM attachments at
JOIN messages m ON m.id = at.message_pk
WHERE at.extracted_text ~* 'termination|kündigung'
ORDER BY m.date_sent DESC;
```

Who writes most and rarely gets answered:

```sql
SELECT from_addr, count(*) AS received
FROM messages
GROUP BY from_addr
ORDER BY received DESC
LIMIT 25;
```

## Working notes

- **Read-only.** Only run `SELECT`. Mail state (read, flagged, archived,
  deleted) syncs back to the user's mail provider through the meerail agent, so
  writing here would push real changes to their real mailbox. Use the app for
  that.
- **Bound your queries.** Bodies are large; always `LIMIT`, and select
  `snippet` rather than `body_text` when you are surveying. Pull full bodies
  only for the handful of messages you actually need.
- **`search_text` beats `body_text`** for finding things: it concatenates the
  subject, participants, body and the text extracted from PDF and Office
  attachments, and is the column the trigram index is built on.
- **Timestamps are naive UTC.** Convert before quoting local times.
- **Deleted mail lingers.** Filter `NOT l.deleted` when the question is about
  what is currently in a mailbox.
- **This is private correspondence.** Read only what the question needs, quote
  sparingly, and don't send its contents anywhere the user didn't ask for.
