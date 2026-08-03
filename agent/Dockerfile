# meerail-agent — the mail pipeline, containerised.
#
# Only useful with `network_mode: host`, i.e. on Linux: the agent reaches Proton
# Bridge, Postgres and Tika over loopback, so it needs the host's network
# namespace to see any of them. See docker-compose.agent.yml.
FROM python:3.13-slim

# See the server Dockerfile: label only, never ENV, so the copied VERSION file
# stays the single runtime source of the number.
ARG MEERAIL_VERSION=dev
LABEL org.opencontainers.image.title="meerail-agent" \
      org.opencontainers.image.description="meerail agent — IMAP/SMTP, parsing and ingest; owns the write path" \
      org.opencontainers.image.version="${MEERAIL_VERSION}" \
      org.opencontainers.image.source="https://github.com/ribalba/meerail" \
      org.opencontainers.image.url="https://meerail.eu" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first for better layer caching. The agent has its own pins —
# they overlap with the server's but are not the same set (IMAPClient, PyMuPDF).
COPY agent/requirements.txt ./agent/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt

# `core` is shared with the server: models, parsing, threading, ingest, previews.
COPY core ./core
COPY agent ./agent

# Read by core/version.py (BASE_DIR is /app); the agent logs it on startup, so a
# support question can start from what is actually running.
COPY VERSION ./VERSION

# main.py bootstraps this itself, but set it so `docker compose run` with an
# overridden entrypoint still resolves `core`.
ENV PYTHONPATH=/app

WORKDIR /app/agent

# meerail.toml is bind-mounted at /app/meerail.toml at runtime and deliberately
# never baked in — it holds Bridge passwords in plaintext. ENTRYPOINT (not CMD)
# so that flags pass through: `docker compose run --rm agent --test`.
ENTRYPOINT ["python", "main.py"]
