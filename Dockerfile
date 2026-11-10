# CPU application: sherpa ASR and optional Melo. Qwen runs in a separate GPU image.
FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org
ARG APT_MIRROR=deb.debian.org

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    APP_ENV=prod \
    DATA_DIR=/app/data

WORKDIR /app

RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first so source edits do not invalidate this layer.
# All packages ship manylinux wheels for x86_64; no compiler toolchain needed.
COPY requirements.txt requirements.lock ./
RUN python -m pip install --no-cache-dir \
    --index-url "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST" \
    -r requirements.lock

# Only the trees the server needs at runtime. tests/, scripts/ and docs/ are
# intentionally not copied; CI runs them on the host.
COPY app ./app
COPY web ./web
COPY contracts ./contracts
COPY knowledge ./knowledge
COPY flowpacks ./flowpacks
COPY eval ./eval
# License texts/notices are part of the deliverable, not build-only material.
COPY licenses ./licenses
COPY THIRD_PARTY.md ./THIRD_PARTY.md

RUN mkdir -p /app/data

EXPOSE 8080

# The slim image has no curl; use the standard library. Any non-2xx raises and
# marks the container unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

# Weight policy: models/asr and models/melo are NOT copied into this image.
# docker-compose mounts them read-only. The sherpa ASR weight license is still
# unresolved, so baking it into a redistributable image is intentionally disabled.

# NOTE: the container runs as root on purpose. ./data is a host bind mount in
# docker-compose.yml, and a non-root UID would make writes fail on hosts whose
# uid/gid does not match. Harden with a dedicated user (and a named volume)
# before exposing this image outside the demo environment.
