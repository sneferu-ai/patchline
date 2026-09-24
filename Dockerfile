# Patchline — single-container deployment (web + mandatory worker, §8).

# ---- build stage: install dependencies with Poetry ------------------------
FROM python:3.11-slim AS builder

ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

RUN pip install --no-cache-dir poetry

WORKDIR /build
COPY pyproject.toml poetry.lock* ./
RUN poetry install --only main --no-root --no-directory

COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
RUN poetry install --only-root

# ---- production stage ------------------------------------------------------
FROM python:3.11-slim

# FR-072: git is required for the shallow-clone fallback on >500MB archives.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /build/src /app/src
COPY --from=builder /build/alembic /app/alembic
COPY --from=builder /build/alembic.ini /app/alembic.ini
COPY tests/fixtures /app/tests/fixtures

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/patchline.db

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /data

EXPOSE 8000
VOLUME ["/data"]

ENTRYPOINT ["/entrypoint.sh"]
