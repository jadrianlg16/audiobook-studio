"""Audiobook pipeline: turn a book file into a *Book Script* — the normalized,
engine-agnostic JSON artifact that every later stage (casting, rendering,
mastering, the player) reads and writes.

This package intentionally has near-zero deps (pydantic only) so it can run
anywhere and be unit-tested without models or a GPU. v1 scope: text ingestion +
sentence chunking + a single-narrator Book Script. Speaker attribution, LLM
analysis, rendering and mastering are later stages that build on this schema.
"""

from .schema import BookScript, SCHEMA_VERSION

__all__ = ["BookScript", "SCHEMA_VERSION"]
