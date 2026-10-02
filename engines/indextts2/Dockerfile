# Build context = project root. Needs a CUDA GPU + downloaded checkpoints.
#
# Download weights once on the host (kept out of the image), e.g.:
#   huggingface-cli download IndexTeam/IndexTTS-2 --local-dir ./models/IndexTTS-2
# and bind-mount ./models -> /models (see docker-compose.yml).
FROM pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/cache \
    INDEXTTS_MODEL_DIR=/models/IndexTTS-2

RUN apt-get update \
 && apt-get install -y --no-install-recommends git libsndfile1 ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY engines/indextts2/requirements.txt /app/engine/requirements.txt
# index-tts upgrades torch to 2.8.0 but leaves the base image's torchvision
# (0.19.1, built for torch 2.4) in place — which breaks at runtime with
# "operator torchvision::nms does not exist". Install the matching torchvision.
RUN pip install --no-cache-dir -r /app/engine/requirements.txt \
 && pip install --no-cache-dir "git+https://github.com/index-tts/index-tts.git" \
 && pip install --no-cache-dir torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128

COPY shared /app/shared
COPY engines/indextts2 /app/engine

EXPOSE 8000
CMD ["uvicorn", "engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
