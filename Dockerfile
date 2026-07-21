FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Raw .eml files and attachments live here; mount a volume to persist them.
RUN mkdir -p /data
ENV DATABASE_URL=postgresql+psycopg2://meerail:meerail@db:5432/meerail \
    DATA_DIR=/data \
    TIKA_URL=http://tika:9998

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
