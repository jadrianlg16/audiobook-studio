# Build context = project root. Needs a CUDA GPU; the weights are NOT in the image.
#
# Weights (~4.5 GB for the default 1.7B CustomVoice) land in the shared HF cache
# volume (/cache, bind-mounted from HF_CACHE_DIR) on the first synthesis, or once
# ahead of time with:
#   docker compose --profile gpu run --rm --no-deps --entrypoint hf qwen3tts \
#     download Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice
#
# LICENSE: Apache-2.0 weights (Hugging Face model card metadata) and code. See
# engines/qwen3tts/README.md.
#
# Why this base. qwen-tts 0.1.1 pins transformers 4.57.3 / accelerate 1.12.0 and
# asks for an unpinned torchaudio; the Breeze image runs it on torch 2.9.1+cu128.
# Starting from the 2.4.1 base that breeze/indextts2 use would let pip upgrade
# torch and strand the base's torchvision ("operator torchvision::nms does not
# exist" — see engines/breeze/Dockerfile). So: start from the torch we
# want, and pin torch/torchvision/torchaudio to exactly what the base shipped via
# a constraint file, so no dependency can ever swap them.
FROM pytorch/pytorch:2.9.1-cuda12.8-cudnn9-runtime

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/cache

# sox: the `sox` Python wrapper that qwen-tts imports shells out to the binary and
# prints "SoX could not be found!" on every import without it. Only the 25 Hz
# tokenizer uses it — the 12 Hz checkpoints here never call it — so the warning is
# harmless; the small binary is cheaper than explaining the warning in every log.
# libsndfile1: for soundfile, which librosa imports.
RUN apt-get update \
 && apt-get install -y --no-install-recommends sox libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

COPY engines/qwen3tts/requirements.txt /app/engine/requirements.txt
# One resolver pass for qwen-tts + the web server, so gradio (a qwen-tts
# dependency) and our fastapi/pydantic pins are solved together.
RUN pip list --format=freeze | grep -iE '^(torch|torchvision|torchaudio)==' > /tmp/torch-pins.txt \
 && cat /tmp/torch-pins.txt \
 && test "$(wc -l < /tmp/torch-pins.txt)" -ge 2 \
 && pip install --no-cache-dir -c /tmp/torch-pins.txt \
      qwen-tts==0.1.1 -r /app/engine/requirements.txt \
 && python -c "import torch, torchaudio, transformers, qwen_tts; print('torch', torch.__version__, 'cuda', torch.version.cuda, '| torchaudio', torchaudio.__version__, '| transformers', transformers.__version__)"

COPY shared /app/shared
COPY engines/qwen3tts /app/engine

EXPOSE 8000
CMD ["uvicorn", "engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
