"""``create_app()`` — the studio ASGI application.

Mounted twice on purpose. The frontend's nginx proxies ``/studio/`` and strips
the prefix, so behind it the studio sees ``/api/…``; opened directly on :5174 a
browser asks for whatever URL the API handed it, which carries the prefix. One
app, both mount points, and every URL the API emits resolves in either topology
(see ``store.public_prefix``).
"""

from __future__ import annotations

import mimetypes
import os
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import store
from .api import build_router, raw_body_schemas
from .jobs import JobManager, manager as default_manager
from .validate import flatten

# Python's mimetype table has no entry for .m4b, so StaticFiles guesses
# text/plain and the browser tries to *render* a 400 MB audiobook as text.
# (Verified: the export served as `content-type: text/plain; charset=utf-8`.)
mimetypes.add_type("audio/mp4", ".m4b")
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/wav", ".wav")

_INDEX = """<!doctype html><meta charset=utf-8><title>Audiobook Studio — API</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 body{{margin:0;background:#0b0b0d;color:#ececed;font:15px/1.65 system-ui,sans-serif}}
 .wrap{{max-width:640px;margin:0 auto;padding:44px 20px}}
 h1{{font-size:22px;margin:0 0 6px}} p{{color:#a5a5ad}}
 a{{color:#b7b1ff}} code{{background:#151518;border:1px solid #2a2a30;border-radius:5px;padding:1px 5px}}
 ul{{padding-left:18px}} li{{margin:6px 0}}
</style>
<div class=wrap>
<h1>&#9834; Audiobook Studio — service</h1>
<p>This is the studio <strong>API</strong>. The Director's Desk UI is served by the
frontend container on its own port (the compose stack's <code>FRONTEND_PORT</code>,
5173 by default) — open the Studio UI there.</p>
<ul>
  <li>API docs: <a href="/docs">/docs</a></li>
  <li>Health: <a href="/api/health">/api/health</a> &middot;
      Books: <a href="/api/books">/api/books</a> &middot;
      Jobs: <a href="/api/jobs">/api/jobs</a></li>
  <li>Book files (players, chapter WAVs, exports): <code>/books/&lt;slug&gt;/</code></li>
  <li>Contract: <code>docs/STUDIO-API.md</code></li>
</ul>
<p>offline: <code>{offline}</code> &middot; fake director: <code>{fake}</code> &middot;
gateway: <code>{gateway}</code></p>
</div>"""


def create_app(manager: JobManager | None = None) -> FastAPI:
    jobs = manager or default_manager

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Rebuilds the job index from disk (marking anything a previous process
        # left `running` as `interrupted`) and starts the single worker thread.
        jobs.start()
        yield
        jobs.stop()

    app = FastAPI(title="Audiobook Studio", version="1.0", lifespan=lifespan)

    # Every error, including the ones nobody predicted, leaves as
    # `{"detail": "<human message>"}` — the shape the contract states and the
    # UI parses. Starlette's default 500 is a plain-text body, so an unhandled
    # exception used to violate the contract *and* tell the user nothing;
    # FastAPI's default 422 is a list of dicts under
    # `detail`, which every client then has to special-case.
    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(_request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": flatten(exc)})

    @app.exception_handler(Exception)
    async def _on_unhandled(_request: Request, exc: Exception):
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        return JSONResponse(
            status_code=500,
            content={"detail": f"the studio hit an unhandled {type(exc).__name__}: {exc}"},
        )

    def openapi() -> dict:
        """FastAPI's schema, plus the request bodies two routes parse by hand.

        ``PATCH /lines/{id}`` and ``PATCH /cast`` read the raw request, so
        FastAPI never meets ``LinePatch``/``CastPatchEntry``; their routes name
        them via ``openapi_extra`` and the components are added here, once, on
        the cached schema. ``scripts/export_studio_openapi.py`` exports this."""
        if app.openapi_schema is None:
            schema = FastAPI.openapi(app)
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            for name, body in raw_body_schemas().items():
                if components.setdefault(name, body) != body:
                    raise RuntimeError(f"OpenAPI component {name!r} is declared twice, differently")
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]

    router = build_router(jobs)

    app.include_router(router, prefix="/api")
    prefix = store.public_prefix()
    if prefix:
        app.include_router(router, prefix=f"{prefix}/api", include_in_schema=False)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX.format(
            offline=store.offline(),
            fake=store.fake_director(),
            gateway=store.gateway_url(),
        )

    # Book files: player.html, chapter WAVs, manifest.json, exports. html=True so
    # /books/<slug>/ serves the read-along player.
    root = store.books_root()
    os.makedirs(root, exist_ok=True)
    app.mount("/books", StaticFiles(directory=root, html=True), name="books")
    if prefix:
        app.mount(f"{prefix}/books", StaticFiles(directory=root, html=True), name="books-prefixed")
    return app


app = create_app()
