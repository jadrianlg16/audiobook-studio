# Build context = project root (see docker-compose.yml). CPU-only image.
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/cache

# espeak-ng is Kokoro's g2p fallback; git is needed by some misaki deps.
RUN apt-get update \
 && apt-get install -y --no-install-recommends espeak-ng git \
 && rm -rf /var/lib/apt/lists/*

COPY engines/kokoro/requirements.txt /app/engine/requirements.txt
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r /app/engine/requirements.txt

COPY shared /app/shared
COPY engines/kokoro /app/engine

EXPOSE 8000
CMD ["uvicorn", "engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
