from shared.engine_base import create_engine_app

from .tts import Qwen3TTSEngine

app = create_engine_app(Qwen3TTSEngine())
