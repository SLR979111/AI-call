FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 APP_ENV=prod
# Build-time mirrors are overridable for restricted networks. Defaults stay
# Debian/PyPI; CI can pass reachable mirrors without editing the Dockerfile.
ARG APT_MIRROR=deb.debian.org
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org
WORKDIR /app
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get install -y --no-install-recommends libsndfile1 libgomp1 sox \
    && rm -rf /var/lib/apt/lists/*
# Dependencies are acquired at build time; model files are read-only mounts.
RUN pip install --no-cache-dir torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128
COPY services/qwen_tts/requirements.txt services/qwen_tts/requirements.lock /app/
# torch/torchaudio are installed above from the cu128 index. The lock is the
# fully resolved set and intentionally omits torch/torchaudio; --no-deps avoids
# pip replacing cu128 torch with a CPU build from PyPI.
RUN pip install --no-cache-dir --no-deps --index-url "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST" -r /app/requirements.lock
COPY services/qwen_tts /app/services/qwen_tts
COPY app/cancel.py /app/app/cancel.py
COPY app/voice/types.py app/voice/streaming.py /app/app/voice/
COPY licenses /app/licenses
COPY THIRD_PARTY.md /app/THIRD_PARTY.md
EXPOSE 8090
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=3)"]
# One worker owns one GPU/model. Scale with separate sidecars, not --workers.
# Weight policy: QWEN_MODEL_DIR is a read-only bind mount; this image does not
# bake the model weights. NVIDIA CUDA/cuDNN components are governed by NVIDIA's
# EULA; see /app/licenses/NVIDIA-CUDA-NOTICE.md.
CMD ["uvicorn", "services.qwen_tts.server:app", "--host", "0.0.0.0", "--port", "8090"]
