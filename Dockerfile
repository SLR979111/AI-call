# syntax=docker/dockerfile:1
# CPU-only image for the competition runtime (x86_64, 8C/16GB, no GPU).
# The fake skeleton has no model weights, so this stays small and builds offline
# once the Python wheels are cached.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=prod \
    DATA_DIR=/app/data

WORKDIR /app

# Install dependencies first so source edits do not invalidate this layer.
# All packages ship manylinux wheels for x86_64; no compiler toolchain needed.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# Only the trees the server needs at runtime. tests/, scripts/ and docs/ are
# intentionally not copied; CI runs them on the host.
COPY app ./app
COPY web ./web
COPY contracts ./contracts
COPY knowledge ./knowledge
COPY flowpacks ./flowpacks
COPY eval ./eval

RUN mkdir -p /app/data

EXPOSE 8080

# The slim image has no curl; use the standard library. Any non-2xx raises and
# marks the container unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

# NOTE: the container runs as root on purpose. ./data is a host bind mount in
# docker-compose.yml, and a non-root UID would make writes fail on hosts whose
# uid/gid does not match. Harden with a dedicated user (and a named volume)
# before exposing this image outside the demo environment.
