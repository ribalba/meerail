FROM python:3.13-slim

ARG MEERAIL_VERSION=dev
LABEL org.opencontainers.image.title="meerail-journal" \
      org.opencontainers.image.description="Ordered log of sealed records — keeps several meerail installs in agreement" \
      org.opencontainers.image.version="${MEERAIL_VERSION}" \
      org.opencontainers.image.source="https://github.com/ribalba/meerail" \
      org.opencontainers.image.url="https://meerail.eu" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY journal/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Just the service. `core` is not copied and must not be: this image is the one
# that runs on somebody else's machine, and the less of meerail it contains the
# smaller the thing being trusted. journal/keys.py does import core, and is a
# local helper run on your own laptop — not part of this image.
COPY journal/__init__.py journal/server.py ./journal/

# One SQLite file, one volume. Postgres works too (JOURNAL_DATABASE_URL), and is
# overkill for a log three machines append to.
RUN mkdir -p /data
ENV JOURNAL_DATABASE_URL=sqlite:////data/journal.db

EXPOSE 8080

CMD ["uvicorn", "journal.server:app", "--host", "0.0.0.0", "--port", "8080"]
