from shared.engine_base import create_engine_app

from .tts import MockEngine

app = create_engine_app(MockEngine())
