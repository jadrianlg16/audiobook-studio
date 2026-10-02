from shared.engine_base import create_engine_app

from .tts import BreezeEngine

app = create_engine_app(BreezeEngine())
