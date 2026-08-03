FROM python:3.13-slim

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
