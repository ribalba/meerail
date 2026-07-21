# meerail website

The public marketing page for meerail. A single static HTML file served by
nginx — no build step, no JavaScript. The styling is lifted from the
[meerkat](https://github.com/ribalba/meerkat) landing page so the two sites read
as one family.

## Run it

```bash
cd website
docker compose up -d --build
open http://localhost:8080
```

Port 8080 by default, so it can run alongside the meerail server on 8000. Set
`WEBSITE_PORT` to change it:

```bash
WEBSITE_PORT=3000 docker compose up -d
```

While editing, you can skip Docker entirely:

```bash
python3 -m http.server -d public 8080
```

## Layout

```text
website/
├── Dockerfile              nginx:alpine + ./public
├── docker-compose.yml
├── nginx.conf              gzip, cache headers, SKILL.md as a download
└── public/
    ├── index.html          the whole page
    ├── css/site.css        landing styles (from meerkat) + meerail additions
    ├── img/                logo.png, logo-square.png (from app/static/img)
    └── downloads/
        └── SKILL.md        the importable agent skill
```

## SKILL.md

`public/downloads/SKILL.md` documents the meerail Postgres schema and the
default container credentials (`meerail` / `meerail` / `meerail`, reached via
`docker compose exec db psql`) so an agent can query the mail archive read-only.

The page **links to it on GitHub** rather than serving its own copy, so visitors
always get the current version regardless of when the site was last deployed.
nginx still serves it at `/downloads/SKILL.md` as a fallback; the buttons point
at `github.com/ribalba/meerail/blob/main/website/public/downloads/SKILL.md` and
the install snippet curls the `raw.githubusercontent.com` equivalent.

Keep it in sync with [`core/models.py`](../core/models.py) when the schema
changes, and with [`.env.example`](../.env.example) when the credential
defaults change.

## Before you publish

- The GitHub links assume `github.com/ribalba/meerail` on branch `main`. If the
  repo is still private, the SKILL.md links 404 for visitors — either make it
  public or point the buttons back at the locally served `/downloads/SKILL.md`.
- nginx serves plain HTTP; put it behind a TLS terminator (Caddy, Traefik, a
  reverse proxy) when it faces the internet.
