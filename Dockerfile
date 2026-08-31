# One image, two roles: the web service and the worker run the same code with
# different commands. Built here rather than on a buildpack because ffmpeg is
# not optional — QC measurement and MP3 encoding both depend on it, and no
# Python buildpack ships it.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Fonts are a hard dependency of the product, not a nicety, because the lyric
# video burns text into pixels. A script with no font on this image renders as
# a row of tofu boxes in a delivered file, and nothing in the pipeline can tell:
# ffmpeg exits zero, the file decodes, the duration is right.
#
# DejaVu was already here by accident — `playwright install --with-deps` pulls
# it in as a Chromium dependency — which meant every Latin lyric this studio has
# ever shipped depended on a browser's package list. Pinned explicitly now.
#
# nanum is Korean. noto-core carries Arabic (and Hebrew, Thai, Devanagari and
# the rest) properly, where DejaVu covers Arabic only crudely. Between them they cover every
# language in dailyfive.languages, and a test asserts exactly that — a language
# the studio cannot render is a language it does not offer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      postgresql-client \
      ca-certificates \
      fonts-dejavu-core \
      fonts-nanum \
      fonts-noto-core \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so a code change does not reinstall the world.
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY migrations ./migrations

RUN pip install --upgrade pip wheel \
 && pip install -e ".[postgres,video]"

# Chromium for the lyric video. --with-deps pulls the shared libraries a
# headless browser needs that a slim image does not carry; without them it
# starts and dies with a linker error rather than a useful message.
RUN playwright install --with-deps chromium

# Transient audio only — the delivered bytes live in Postgres.
ENV WORK_DIR=/tmp/dailyfive
RUN mkdir -p /tmp/dailyfive

# App Platform's health check hits this; it reports 503 when the database is
# unreachable or no run has shipped in over a day.
EXPOSE 8080

CMD ["dailyfive", "serve", "--host", "0.0.0.0", "--port", "8080"]
