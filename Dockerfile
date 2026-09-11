# Build from the REPOSITORY ROOT, not from backend/:
#
#   docker build -f backend/Dockerfile -t ragdex-api .
#
# The context has to include coach.md, which lives at the root and is the
# coach's voice at runtime. A Dockerfile cannot COPY above its context, so the
# context is the root and every path below is written from there.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY backend/pyproject.toml ./
# Resolve into a virtualenv that is copied wholesale into the runtime image,
# so the compiler and headers never reach production.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

FROM python:3.12-slim AS runtime

# Run as a non-root user. A process that cannot write to its own filesystem is
# a much smaller problem when something does go wrong inside it.
RUN groupadd --system app && useradd --system --gid app --home /app app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app backend/app ./app
# app/services/coach.py resolves this as parents[3]/coach.md, which from
# /app/app/services/ is /coach.md.
COPY --chown=app:app coach.md /coach.md

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# No --reload, and no --workers: process count belongs to the orchestrator,
# which knows how much CPU the container was actually given.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
