FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARCHONTOS_MIGRATIONS_DIR=/app/db/migrations
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY apps ./apps
RUN pip install --no-cache-dir .
COPY db/migrations ./db/migrations

# Run as an unprivileged user. The artifact directory is created here so a fresh named volume
# mounted at /data/artifacts inherits this ownership. A volume created by an older root image
# keeps root ownership: fix it once with
#   docker compose run --rm --user root ingestion chown -R 10001:10001 /data/artifacts
RUN groupadd --system --gid 10001 archontos \
    && useradd --system --uid 10001 --gid archontos --home-dir /app --no-create-home archontos \
    && mkdir -p /data/artifacts \
    && chown -R archontos:archontos /data/artifacts
USER archontos

CMD ["uvicorn", "apps.action:app", "--host", "0.0.0.0", "--port", "8005"]
