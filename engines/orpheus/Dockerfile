# Orpheus runs on vLLM, which bundles its own torch + CUDA. Fighting that on a
# generic pytorch base is what makes this engine miserable — so we start FROM the
# official vLLM image (vllm 0.7.3 + torch 2.5.1 + CUDA, the version orpheus-speech
# 0.1.0 targets) and add Orpheus on top. orpheus-speech requests an unversioned
# vllm, which is already satisfied here, so pip won't upgrade/break it.
FROM vllm/vllm-openai:v0.7.3

# The vLLM image ships an entrypoint for its own API server; clear it so our CMD
# (uvicorn) runs instead.
ENTRYPOINT []
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/cache

COPY engines/orpheus/requirements.txt /app/engine/requirements.txt
RUN pip install --no-cache-dir -r /app/engine/requirements.txt

COPY shared /app/shared
COPY engines/orpheus /app/engine

EXPOSE 8000
CMD ["uvicorn", "engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
