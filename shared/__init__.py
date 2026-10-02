"""Shared TTS engine contract.

This package defines the contract every voice engine in the project speaks, so
the gateway and UI never depend on a specific engine implementation:

- ``schemas``     – pydantic models exchanged over HTTP (no heavy deps).
- ``engine_base`` – the ``TTSEngine`` ABC + a FastAPI app factory (numpy/fastapi).

Engines import ``engine_base``; the gateway only needs ``schemas``.
"""
