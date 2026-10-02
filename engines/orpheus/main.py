from shared.engine_base import create_engine_app

from .tts import OrpheusEngine

app = create_engine_app(OrpheusEngine())
