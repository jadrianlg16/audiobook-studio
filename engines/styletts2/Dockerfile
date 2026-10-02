# Build context = project root. CUDA runtime base (GPU recommended; CPU works but slow).
FROM pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/cache

# espeak-ng backs phonemizer; libsndfile for soundfile.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git espeak-ng libsndfile1 ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY engines/styletts2/requirements.txt /app/engine/requirements.txt
RUN pip install --no-cache-dir -r /app/engine/requirements.txt \
 && python -c "import nltk; nltk.download('punkt_tab')"

COPY shared /app/shared
COPY engines/styletts2 /app/engine

EXPOSE 8000
CMD ["uvicorn", "engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
