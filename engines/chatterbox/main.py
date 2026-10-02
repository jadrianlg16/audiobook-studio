from shared.engine_base import create_engine_app

from .tts import ChatterboxEngine

app = create_engine_app(ChatterboxEngine())
