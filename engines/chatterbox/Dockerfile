# Build context = project root. chatterbox-tts 0.1.7 requires torch==2.6.0, so
# the base image must provide it (a 2.4.1 base would trigger a torch reinstall).
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/cache

RUN apt-get update \
 && apt-get install -y --no-install-recommends git libsndfile1 ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY engines/chatterbox/requirements.txt /app/engine/requirements.txt
RUN pip install --no-cache-dir -r /app/engine/requirements.txt

COPY shared /app/shared
COPY engines/chatterbox /app/engine

EXPOSE 8000
CMD ["uvicorn", "engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
