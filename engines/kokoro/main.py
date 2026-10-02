from shared.engine_base import create_engine_app

from .tts import KokoroEngine

app = create_engine_app(KokoroEngine())
