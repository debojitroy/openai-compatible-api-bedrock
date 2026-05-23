# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /sbin/nologin app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app/ ./app/

USER app

EXPOSE 8000

ENV WEB_CONCURRENCY=2 \
    UVICORN_HOST=0.0.0.0 \
    UVICORN_PORT=8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "uvicorn app.main:app --host ${UVICORN_HOST} --port ${UVICORN_PORT} --workers ${WEB_CONCURRENCY} --timeout-graceful-shutdown 5"]
