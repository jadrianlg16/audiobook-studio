"""The studio service: the Book Script pipeline as a persistent workspace.

``docs/STUDIO-API.md`` is the contract; this package implements it.

    store.py   books on disk (script versions, lexicon, settings, derived state)
    jobs.py    one worker thread, persisted job records, SSE pub/sub
    api.py     the FastAPI routers, endpoint for endpoint
    app.py     create_app() — routers under /api, book files under /books

Self-contained by design: the studio image is built from ``./pipeline`` alone
(see ``pipeline/Dockerfile``), so nothing here may import ``gateway`` or
``shared``. The gateway is reached over HTTP with stdlib ``urllib``, exactly the
way ``render.http_synth`` and ``direct.http_annotate`` already do it.
"""
