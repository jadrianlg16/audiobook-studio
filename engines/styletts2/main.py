from shared.engine_base import create_engine_app

from .tts import StyleTTS2Engine

app = create_engine_app(StyleTTS2Engine())
