FROM python:3.13-slim

# Stamped onto the image labels by `make images` / the CI workflow, both of
# which pass `--build-arg MEERAIL_VERSION=$(cat VERSION)` — so the label and the
# `:x.y.z` tag can never disagree.
#
# Only the label. Deliberately NOT `ENV MEERAIL_VERSION`: the copied VERSION
# file below is what the running server reports, and an ENV with a default here
# would outrank it (core/version.py reads the environment first) and make a
# plain `docker compose build` claim to be "dev". That variable stays free for
# an operator who genuinely wants to override the number.
ARG MEERAIL_VERSION=dev
LABEL org.opencontainers.image.title="meerail-server" \
      org.opencontainers.image.description="meerail web app — reads the mail the agent has ingested" \
      org.opencontainers.image.version="${MEERAIL_VERSION}" \
      org.opencontainers.image.source="https://github.com/ribalba/meerail" \
      org.opencontainers.image.url="https://meerail.eu" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# `core` is shared with the agent (models, parsing, ingest); `app` is the web layer.
COPY core ./core
COPY app ./app

# The version this image is. core/version.py reads it from here (BASE_DIR is
# /app), which is what /api/version reports and what the update check compares
# against the copy on main.
COPY VERSION ./VERSION

# Staging for outgoing attachments; mail bytes live in Postgres. /data is where
# the volume lands, so this is container topology rather than configuration —
# which is why it is the one setting still baked in here.
#
# DATABASE_URL deliberately is NOT: the environment outranks meerail.toml, so a
# default baked into the image would override the mounted file for everyone
# instead of only filling a gap. The compose files set it explicitly.
RUN mkdir -p /data
ENV DATA_DIR=/data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
