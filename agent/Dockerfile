# meerail-agent — the mail pipeline, containerised.
#
# Only useful with `network_mode: host`, i.e. on Linux: the agent reaches Proton
# Bridge, Postgres and Tika over loopback, so it needs the host's network
# namespace to see any of them. See docker-compose.agent.yml.
FROM python:3.13-slim

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

# main.py bootstraps this itself, but set it so `docker compose run` with an
# overridden entrypoint still resolves `core`.
ENV PYTHONPATH=/app

WORKDIR /app/agent

# config.toml is bind-mounted at runtime and deliberately never baked in — it
# holds Bridge passwords in plaintext. ENTRYPOINT (not CMD) so that flags pass
# through: `docker compose run --rm agent --test`.
ENTRYPOINT ["python", "main.py"]
