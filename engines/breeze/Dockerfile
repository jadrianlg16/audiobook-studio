# Build context = project root. Needs a CUDA GPU + downloaded checkpoints.
#
# Download the weights once on the host (kept out of the image), e.g.:
#   huggingface-cli download BreezeBlue/Breeze-TTS-2 --local-dir ./models/breeze-tts-2
# and bind-mount ./models -> /models (see docker-compose.yml).
#
# VRAM: ~7.7 GiB eager, ~14.4 GiB with BREEZE_FAST=1 (BreezeBlue's figures).
# An 8 GB card is below their recommended 12 GB minimum — expect OOM.
#
# LICENSE: the weights are BreezeBlue Research & Non-Commercial, and that covers
# self-hosted output. Evaluation only. See engines/breeze/README.md.
FROM pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/cache \
    BREEZE_MODEL_DIR=/models/breeze-tts-2 \
    BREEZE_REPO_DIR=/opt/breeze-tts

RUN apt-get update \
 && apt-get install -y --no-install-recommends git libsndfile1 ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Our thin server layer first, then upstream — so BreezeBlue's own pins win any
# overlap (the model is the fragile half; fastapi/httpx are not).
COPY engines/breeze/requirements.txt /app/engine/requirements.txt
RUN pip install --no-cache-dir -r /app/engine/requirements.txt

# Pinned to the upstream commit the adapter and docs/BREEZE-AUDIT-2026-09-05.md
# were checked against (committed 2026-09-03), so a rebuild can't silently pick
# up a breaking change. Bump deliberately:
#   docker compose build --build-arg BREEZE_REF=<sha> breeze
ARG BREEZE_REF=a6d1a2faa2880cd690267cd12d4f6724abbefdc1
# Upstream's requirements upgrade torch (2.4.1 -> 2.9.1 as of Sep 2026), which
# strands the base image's torchvision 0.19.1 — importing it then fails with
# "operator torchvision::nms does not exist" rather than anything readable.
# indextts2 hits the same trap and pins a matching torchvision because it needs
# one; Breeze references torchvision nowhere (checked requirements.txt,
# breeze_infer/ and infer.py), so drop it instead. Absent gives a clean
# ImportError if some future dependency wants it; broken gives that riddle.
# Uninstalled in the SAME layer as the upgrade so no unusable copy is committed.
RUN git clone https://github.com/breezeblue-ai/breeze-tts.git "$BREEZE_REPO_DIR" \
 && git -C "$BREEZE_REPO_DIR" checkout "$BREEZE_REF" \
 && pip install --no-cache-dir -r "$BREEZE_REPO_DIR/requirements.txt" \
 && pip uninstall -y torchvision

COPY shared /app/shared
COPY engines/breeze /app/engine

EXPOSE 8000
CMD ["uvicorn", "engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
