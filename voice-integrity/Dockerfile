# Single-container backend.  One process, one port, no orchestration.
#
# The heavy layer is the model cache: copy it in rather than downloading at
# container start, so the image runs with no network - which is what the demo
# machine needs.
FROM python:3.11-slim AS base

# ffmpeg carries the codec chain.  libopencore-amrnb is the one that is
# usually missing and whose absence silently disables mobile-codec work.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libopencore-amrnb0 \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    HF_HOME=/app/cache/huggingface \
    TORCH_HOME=/app/cache/torch \
    HF_HUB_DISABLE_TELEMETRY=1

COPY requirements-base.txt requirements-serve.txt ./
# silero-vad depends on torch and torchaudio.  Install the CPU wheels first,
# or pip resolves the default CUDA build and the image grows by gigabytes.
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements-base.txt -r requirements-serve.txt

COPY configs/ ./configs/
COPY src/ ./src/
COPY scripts/ ./scripts/

# Weights and caches are mounted, not baked: they are large and they change
# independently of the code.
VOLUME ["/app/models", "/app/cache", "/app/data", "/app/keys"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"

CMD ["python", "-m", "vif.cli", "serve", "--host", "0.0.0.0", "--port", "8000"]
