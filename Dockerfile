# Build from this directory, which is the repository root:
#
#   docker build -t ragdex-api .
#
# Everything the runtime needs lives here, including coach.md — the coach's
# voice, read at request time. It used to sit in the parent repository; a
# service that cannot be built from its own checkout cannot be deployed from
# one either.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
# Only the manifest: this stage exists to resolve dependencies into a
# virtualenv that is copied wholesale into the runtime image, so the compiler
# and headers never reach production. The source is copied in below instead,
# which also means a code change does not invalidate this layer.
COPY pyproject.toml ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

FROM python:3.12-slim AS runtime

# Run as a non-root user. A process that cannot write to its own filesystem is
# a much smaller problem when something does go wrong inside it.
RUN groupadd --system app && useradd --system --gid app --home /app app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app app ./app
# app/services/coach.py looks for this beside the package, which from
# /app/app/services/ is /app/coach.md.
COPY --chown=app:app coach.md ./coach.md

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/health\").status==200 else 1)"

# Shell form, because the port is the platform's to choose and Render supplies
# it as $PORT. `exec` keeps uvicorn as PID 1 so it receives SIGTERM directly
# and shuts down gracefully instead of being killed after the grace period.
#
# No --reload, and no --workers: process count belongs to the orchestrator,
# which knows how much CPU the container was actually given.
CMD exec uvicorn app.main:app \
      --host 0.0.0.0 \
      --port "${PORT:-8000}" \
      --proxy-headers \
      --forwarded-allow-ips "*"
