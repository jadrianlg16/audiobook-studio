# The audiobook pipeline as a web service (the studio API).
# Build context = ./pipeline (self-contained; imports nothing from the repo root,
# so the repo-root .dockerignore that excludes pipeline/ doesn't apply here).
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app \
    AUDIOBOOK_OUTPUT=/data/books AUDIOBOOK_GATEWAY=http://gateway:8000

# ffmpeg/ffprobe for pipeline/master.py (mastering + .m4b/.mp3 export).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/pipeline/requirements.txt
RUN pip install --no-cache-dir -r /app/pipeline/requirements.txt

COPY . /app/pipeline

EXPOSE 8000
CMD ["uvicorn", "pipeline.webapp:app", "--host", "0.0.0.0", "--port", "8000"]
