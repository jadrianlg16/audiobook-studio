from shared.engine_base import create_engine_app

from .tts import IndexTTS2Engine

app = create_engine_app(IndexTTS2Engine())
