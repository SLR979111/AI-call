FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 APP_ENV=prod
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 libgomp1 sox \
    && rm -rf /var/lib/apt/lists/*
# Dependencies are acquired at build time; model files are read-only mounts.
RUN pip install --no-cache-dir torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128
COPY services/qwen_tts/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY services/qwen_tts /app/services/qwen_tts
COPY app/cancel.py /app/app/cancel.py
COPY app/voice/types.py app/voice/streaming.py /app/app/voice/
EXPOSE 8090
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=3)"]
# One worker owns one GPU/model. Scale with separate sidecars, not --workers.
CMD ["uvicorn", "services.qwen_tts.server:app", "--host", "0.0.0.0", "--port", "8090"]
