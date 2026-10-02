"""Entry point for the studio service — kept so ``pipeline/Dockerfile``'s
``CMD ["uvicorn", "pipeline.webapp:app", ...]`` keeps working.

The one-form paste box that used to live here is now the "New book" action on
the shelf, and the app behind it is a workspace: books persist under
``output/<slug>/`` with a versioned Book Script, director passes, jobs and
exports. See ``pipeline/studio/`` and ``docs/STUDIO-API.md``.
"""

from __future__ import annotations

from .studio.app import app  # noqa: F401  (uvicorn imports this name)
