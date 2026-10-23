# syntax=docker/dockerfile:1
# stt-hosted (fpn) — the OpenAI-compatible /v1/audio/transcriptions shim over hosted STT providers.
#
# Runtime user: the dedicated unprivileged `app` (uid/gid 10001), set with a numeric USER so
# runAsNonRoot-style checks pass without an /etc/passwd lookup. The shim keeps no state on disk —
# no model cache (inference is remote), no log files (stdout only) — so /app stays root-owned and
# read-only to the process; the only paths `app` owns are $HOME and the default /tmp. Starlette
# spools multipart uploads larger than 1 MiB to $TMPDIR, which is why the compose overlay mounts
# a tmpfs on /tmp alongside read_only. Port 8080 is unprivileged; the compose healthcheck is a
# plain `python -c urllib` probe, so no extra binary or capability is needed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/app \
    XDG_CACHE_HOME=/home/app/.cache

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --home-dir /home/app --create-home \
            --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Deliberately root-owned (no --chown): the code is immutable at runtime and the service user
# must not be able to rewrite it. World-readable is enough for Python to import it.
COPY app.py ./

USER 10001:10001

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
