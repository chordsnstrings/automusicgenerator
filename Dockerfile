# One image, two roles: the web service and the worker run the same code with
# different commands. Built here rather than on a buildpack because ffmpeg is
# not optional — QC measurement and MP3 encoding both depend on it, and no
# Python buildpack ships it.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      postgresql-client \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so a code change does not reinstall the world.
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY migrations ./migrations

RUN pip install --upgrade pip wheel \
 && pip install -e ".[postgres]"

# Transient audio only — the delivered bytes live in Postgres.
ENV WORK_DIR=/tmp/dailyfive
RUN mkdir -p /tmp/dailyfive

# App Platform's health check hits this; it reports 503 when the database is
# unreachable or no run has shipped in over a day.
EXPOSE 8080

CMD ["dailyfive", "serve", "--host", "0.0.0.0", "--port", "8080"]
