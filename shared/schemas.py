"""Wire schemas for the TTS engine contract.

Intentionally dependency-light (pydantic only) so both the gateway and the
engine services can import it without pulling in numpy/torch.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Capabilities(BaseModel):
    """What an engine can do. The UI renders controls from these flags."""

    cloning: bool = False
    """Accepts a reference voice clip to clone."""
    emotion_scalar: bool = False
    """Accepts a single 0..1 expressiveness/intensity knob (e.g. Chatterbox)."""
    emotion_tags: bool = False
    """Accepts a discrete emotion label or inline tags (e.g. Orpheus, S1)."""
    emotion_reference: bool = False
    """Accepts a separate reference clip that conveys the target emotion."""
    instruction: bool = False
    """Accepts a free-text performance note ("speak slowly, restrained").
    Unlike ``emotion_tags`` there is no fixed vocabulary — the director writes
    prose. Breeze TTS 2 calls this Voice Direction / Voice Design."""
    multi_speaker: bool = False
    """Ships built-in named voices (see ``EngineInfo.voices``)."""
    streaming: bool = False
    requires_gpu: bool = False
    languages: list[str] = Field(default_factory=lambda: ["en"])


class VoiceInfo(BaseModel):
    id: str
    label: str
    language: str = "en"


class EngineInfo(BaseModel):
    """Self-description an engine returns from ``GET /info``."""

    id: str
    name: str
    description: str
    license: str
    homepage: str = ""
    sample_rate: int
    capabilities: Capabilities
    voices: list[VoiceInfo] = Field(default_factory=list)
    emotion_labels: list[str] = Field(default_factory=list)
    audio_tags: list[str] = Field(default_factory=list)
    """Inline non-speech events this engine understands, written exactly as they
    must appear inside ``text`` (e.g. ``(sigh)``). The director may only insert
    tags from this list; empty means "never inject any"."""


class SynthRequest(BaseModel):
    """A single synthesis request. Every engine accepts this shape; engines
    ignore fields they don't support (declared via ``Capabilities``)."""

    text: str = Field(..., min_length=1, max_length=20_000)
    voice: str | None = None
    reference_audio_b64: str | None = None
    emotion: float | None = Field(default=None, ge=0.0, le=1.0)
    emotion_label: str | None = None
    emotion_reference_b64: str | None = None
    instruction: str | None = None
    """Free-text performance note for engines with ``Capabilities.instruction``."""
    reference_text: str | None = None
    """Exact transcript of ``reference_audio_b64``. Breeze TTS 2 conditions on it
    and refuses to clone without it; engines that don't need it ignore it."""
    language: str = "en"
    speed: float = Field(default=1.0, gt=0.1, le=4.0)
    seed: int | None = None
    unload_after: bool = False
    """Free the model from VRAM after this synthesis (lets the next engine load
    on a constrained GPU). The engine reloads lazily on its next request."""
    extra: dict[str, Any] = Field(default_factory=dict)
    """Escape hatch for engine-specific knobs (e.g. cfg_weight)."""
