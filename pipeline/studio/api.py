"""The studio's HTTP surface — one route per row of docs/STUDIO-API.md.

Where the contract and convenience disagree, the contract wins; where the
contract is silent, the note in the handler says what was chosen and why.

URL shape: the studio is reached two ways — directly on ``:5174`` (``/api/…``,
``/books/…``) and through the frontend's nginx at ``/studio/…`` (which strips the
prefix before proxying). A URL the studio *hands back* (a line's audio, an
export) has to work in both, and the service cannot tell which one it is behind.
So ``app.py`` mounts every route under both prefixes and the URLs emitted here
carry the prefixed form: it resolves directly on :5174 via the alias mount, and
behind nginx it is exactly the path the browser must ask for.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import time
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import ValidationError

from ..direct import apply_pass, scene_passages
from ..master import ffmpeg_available
from . import store, validate
from .jobs import JobManager
from .models import (
    BookSummary,
    CastMergeRequest,
    CastPatchEntry,
    CastSummary,
    CastingRequest,
    Chapter,
    ChapterPatch,
    DecideRequest,
    DirectRequest,
    ErrorDetail,
    ExportFile,
    ExportRequest,
    Health,
    Job,
    JobSummary,
    LexiconPut,
    LexiconView,
    Line,
    LinePatch,
    LinePatchResult,
    LineRenderResult,
    NewBookRequest,
    Pass,
    PassApplyResult,
    PassSummary,
    RenderRequest,
    RetryRequest,
    Script,
    Settings,
    SettingsPatch,
    VersionEntry,
)

_HEARTBEAT_S = 15.0
_POLL_S = 0.2
_DELETE_CANCEL_TIMEOUT_S = 10.0


def _not_found(exc: store.NotFound) -> HTTPException:
    return HTTPException(404, str(exc))


def _corrupt(exc: store.CorruptScript) -> HTTPException:
    """A book whose script no longer parses is a 500 — but a 500 that says which
    file, what is wrong with it, and which endpoint puts it back. The alternative
    (what shipped) was a bare "Internal Server Error" on every read of that book,
    with the versions that would have fixed it sitting unmentioned on disk."""
    return HTTPException(500, str(exc))


def _load_script(slug: str):
    try:
        return store.load_script(slug)
    except store.NotFound as exc:
        raise _not_found(exc)
    except store.CorruptScript as exc:
        raise _corrupt(exc)


def _save_script(slug: str, script, note: str) -> int:
    try:
        return store.save_script(slug, script, note=note)
    except store.InvalidScript as exc:
        raise HTTPException(422, str(exc))


async def _json_object(request: Request) -> dict:
    """A JSON object body, or a 422 that says so.

    422 rather than 400 because that is what every other malformed body in this
    service answers (FastAPI's own), and a client that has to tell "your JSON is
    broken" apart by status code is a client that will get it wrong.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        raise HTTPException(422, "expected a JSON object body — this is not valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(422, f"expected a JSON object body, got {type(body).__name__}")
    return body


def _view(fn, *args, **kwargs):
    """Call a store view, mapping its two failure modes onto the contract's."""
    try:
        return fn(*args, **kwargs)
    except store.NotFound as exc:
        raise _not_found(exc)
    except store.CorruptScript as exc:
        raise _corrupt(exc)


# ---- what the OpenAPI export says (docs/STUDIO-API.md, Amendment v1.4) ---------

ERRORS: dict = {
    "default": {
        "model": ErrorDetail,
        "description": "Any error, 4xx or 5xx: one human-readable line in `detail`.",
    }
}
"""Declared on the router, so every operation names the one error body this
service sends. It also stops FastAPI advertising its own 422 list
(``HTTPValidationError``), which ``app.py`` never lets reach the wire."""

RAW_BODIES: tuple[type, ...] = (LinePatch, CastPatchEntry)
"""Request models that two routes parse by hand (``_json_object`` + an explicit
``model_validate``), so FastAPI never sees them. ``app.py`` adds them to the
export's components; the routes point at them through ``openapi_extra``."""


def _raw_body(schema: dict, description: str) -> dict:
    """``openapi_extra`` naming a body the handler reads from the raw request.

    Documentation only: FastAPI does not parse or validate from
    ``openapi_extra``, so the handler's own parsing — "absent" vs ``null`` via
    ``model_fields_set``, and its worded 422s — is exactly what it was."""
    return {
        "requestBody": {
            "required": True,
            "description": description,
            "content": {"application/json": {"schema": schema}},
        }
    }


def _ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def raw_body_schemas() -> dict[str, dict]:
    """The component schemas ``RAW_BODIES`` add to the export, as request
    (validation-mode) schemas under their own model names."""
    out: dict[str, dict] = {}
    for model in RAW_BODIES:
        schema = model.model_json_schema(
            mode="validation", ref_template="#/components/schemas/{model}"
        )
        out.update(schema.pop("$defs", {}))
        out[model.__name__] = schema
    return out


def build_router(manager: JobManager) -> APIRouter:
    router = APIRouter(responses=ERRORS)

    # ---- books -------------------------------------------------------------

    @router.get("/books", response_model=list[BookSummary])
    def list_books() -> list[BookSummary]:
        return store.list_books()

    @router.post("/books", response_model=BookSummary, status_code=201)
    def create_book(body: NewBookRequest) -> BookSummary:
        if not (body.text or "").strip():
            raise HTTPException(400, "a book needs some text")
        try:
            return store.create_book(
                title=body.title,
                author=body.author,
                text=body.text,
                book_type=body.book_type,
                voices=body.voices,
                casting_mode=body.casting_mode,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.post("/books/upload", response_model=BookSummary, status_code=201)
    async def upload_book(
        file: UploadFile = File(...),
        title: str = Form(""),
        author: str = Form(""),
        book_type: str = Form("auto"),
        voices: str = Form("auto"),
        casting_mode: str = Form(""),
    ) -> BookSummary:
        payload = await file.read()
        if not payload:
            raise HTTPException(400, "the uploaded file is empty")
        try:
            return store.create_book(
                title=title,
                author=author,
                file_bytes=payload,
                filename=file.filename or "book.txt",
                book_type=book_type,
                voices=voices,
                casting_mode=casting_mode or None,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/books/{slug}", response_model=BookSummary)
    def get_book(slug: str) -> BookSummary:
        return _view(store.book_summary, slug)

    @router.delete("/books/{slug}", status_code=204)
    def delete_book(slug: str) -> Response:
        """Cancel first, then delete, then forget the jobs.

        Deleting a book with a job in flight used to be a race the client never
        heard about: 204, folder gone, and a few seconds later the dying job's
        failure handler re-created ``output/<slug>/jobs/`` — an orphan folder no
        endpoint would ever mention again. Cancellation is
        cooperative, so this waits for the worker to acknowledge, bounded; the
        job records go with the book rather than lingering as ghosts in
        ``GET /jobs``.
        """
        try:
            store.load_book(slug)
        except store.NotFound as exc:
            raise _not_found(exc)
        manager.cancel_book_jobs(slug, timeout=_DELETE_CANCEL_TIMEOUT_S)
        try:
            store.delete_book(slug)
        except store.NotFound as exc:
            raise _not_found(exc)
        manager.forget_book(slug)
        return Response(status_code=204)

    @router.get("/books/{slug}/script", response_model=Script)
    def get_script(slug: str) -> Script:
        return _view(store.script_view, slug)

    @router.get("/books/{slug}/versions", response_model=list[VersionEntry])
    def get_versions(slug: str) -> list[VersionEntry]:
        try:
            return store.versions(slug)
        except store.NotFound as exc:
            raise _not_found(exc)

    @router.post("/books/{slug}/versions/{n}/restore", response_model=Script)
    def restore(slug: str, n: int) -> Script:
        try:
            store.restore_version(slug, n)
        except store.NotFound as exc:
            raise _not_found(exc)
        except store.CorruptScript as exc:
            # The *stored version* is the broken one here, not the working
            # script: say which, or the user restores the same rubble twice.
            raise HTTPException(500, f"version {n} cannot be restored: {exc}")
        return _view(store.script_view, slug)

    @router.patch(
        "/books/{slug}/lines/{line_id}",
        response_model=LinePatchResult,
        openapi_extra=_raw_body(
            _ref("LinePatch"), "Any subset of the fields; an absent key is left alone, null clears."
        ),
    )
    async def patch_line(slug: str, line_id: str, request: Request) -> dict:
        """Edit one line. Absent key = leave alone, ``null`` = clear.

        Two rules from the contract that are easy to get backwards:
        editing ``text`` clears ``spoken_text`` (the tags were annotations of
        the *old* words, so keeping them would send the engine a sentence the
        reader never sees), and touching ``instruction``/``spoken_text`` stamps
        ``direction_source: "manual"`` so a later director pass leaves it alone;
        clearing both puts the line back to ``"none"``.

        Everything is validated **before** anything is assigned. The old handler
        mutated the model field by field and saved: pydantic does not re-validate
        on attribute assignment, so ``{"speed": 99}`` was written out and then
        rejected on the next *load* — every later read of that book a 500, with
        no way back through the API. Here the merged render
        block is re-validated by ``schema.Render`` itself, the engine and voice
        are checked against the catalog, and ``spoken_text`` has to be this
        line's text plus tags the engine actually understands.
        """
        body = await _json_object(request)
        try:
            patch = LinePatch.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, validate.flatten(exc))
        fields = patch.model_fields_set
        script = _load_script(slug)
        utterance = next((u for u in script.utterances if u.id == line_id), None)
        if utterance is None:
            raise HTTPException(404, f"book {slug!r} has no line {line_id!r}")

        catalog = store.engine_options()
        changes: dict[str, Any] = {}
        touches_script = bool(fields - {"flag", "note"})

        try:
            # -- text ---------------------------------------------------------
            new_text = utterance.text
            if "text" in fields:
                if not (patch.text or "").strip():
                    raise validate.Invalid("text: a line cannot be empty — delete it instead")
                new_text = str(patch.text)

            # -- speaker (may bring an engine and a voice with it) ------------
            new_speaker: str | None = None
            if "speaker" in fields:
                name = (patch.speaker or "").strip()
                if not name:
                    raise validate.Invalid("speaker: a line needs a character name")
                entry = next((c for c in script.cast if c.character == name), None)
                if entry is None:
                    entry = store.new_cast_entry(slug, script, name)
                    script.cast.append(entry)
                new_speaker = name
                changes.update(
                    engine=entry.engine, voice_id=entry.voice_id, voice_design=entry.voice_design
                )

            # -- engine + voice ----------------------------------------------
            engine_id = str(changes.get("engine") or utterance.render.engine)
            if "engine" in fields:
                if not patch.engine:
                    raise validate.Invalid("engine: a line always has an engine — name one")
                engine_id = str(patch.engine)
                changes["engine"] = engine_id
            option = validate.engine_option(engine_id, catalog)

            voice_id = changes.get("voice_id", utterance.render.voice_id)
            if "voice_id" in fields:
                voice_id = patch.voice_id
                changes["voice_id"] = voice_id
                validate.check_voice(option, voice_id)
            elif "engine" in fields:
                # The engine moved and the caller said nothing about the voice.
                # A voice from the old engine is meaningless to the new one, so
                # it is dropped rather than 422'd: the user asked for an engine
                # change, not for a voice they never mentioned to be an error.
                ids = [v.get("id") for v in option.voices]
                if voice_id is not None and voice_id not in ids:
                    voice_id = None
                    changes["voice_id"] = None

            # -- speed ---------------------------------------------------------
            if "speed" in fields:
                # null = back to the profile default rather than "no speed",
                # which `Render` would refuse (and rightly).
                changes["speed"] = 1.0 if patch.speed is None else patch.speed

            # -- direction ------------------------------------------------------
            if "instruction" in fields:
                changes["instruction"] = str(patch.instruction) if patch.instruction else None
            spoken = utterance.render.spoken_text
            if "text" in fields and new_text != utterance.text:
                spoken = None
                changes["spoken_text"] = None
            if "spoken_text" in fields:
                spoken = str(patch.spoken_text) if patch.spoken_text else None
                changes["spoken_text"] = spoken
            if spoken:
                validate.check_spoken_text(spoken, new_text, option)

            new_render = validate.validated_render(utterance.render, changes)
        except validate.Invalid as exc:
            raise HTTPException(422, str(exc))

        # Nothing has been written until here: a rejected patch leaves the book
        # exactly as it was, including its version number.
        before = (utterance.render.instruction, utterance.render.spoken_text)
        after = (new_render.instruction, new_render.spoken_text)
        if after != before:
            # A person typed a note: lock it against later director passes.
            # A person cleared both: the line has no direction at all, so it
            # goes back to "none" and the next applied pass may fill it again.
            new_render.direction_source = "manual" if any(after) else "none"
        utterance.render = new_render
        utterance.text = new_text
        if new_speaker is not None:
            utterance.speaker = new_speaker
            store.set_attribution(utterance, basis="manual", confidence=1.0)

        if "flag" in fields or "note" in fields:
            store.set_qc(slug, line_id, flag=patch.flag, note=patch.note)

        if touches_script:
            version = _save_script(slug, script, note=f"edited {line_id}")
        else:
            # A QC mark changes nothing an engine can hear, so it does not earn
            # a script version — the version list is for edits you might restore.
            version = int(store.load_book(slug).get("version") or 1)
        return {"line": _view(store.line_view, slug, line_id).model_dump(), "version": version}

    @router.patch("/books/{slug}/chapters/{index}", response_model=Chapter)
    def patch_chapter(slug: str, index: int, body: ChapterPatch) -> Chapter:
        """Rename a chapter, or take it out of the audiobook.

        ``include_in_audio: false`` is the front-matter answer: the render skips
        it and the export leaves it out, so a copyright page stops being a
        chapter of read-aloud legalese *and* stops blocking export by never
        being fresh."""
        script = _load_script(slug)
        chapter = next((c for c in script.chapters if c.index == index), None)
        if chapter is None:
            raise HTTPException(404, f"book {slug!r} has no chapter {index}")
        fields = body.model_fields_set
        if "qc_status" in fields and body.qc_status is not None:
            store.set_chapter_qc(slug, index, body.qc_status)
        if "title" in fields and body.title is not None:
            chapter.title = body.title
        store.set_chapter_meta(
            slug,
            script,
            index,
            kind=body.kind if "kind" in fields else None,
            include_in_audio=body.include_in_audio if "include_in_audio" in fields else None,
        )
        if fields & {"title", "kind", "include_in_audio"}:
            _save_script(slug, script, note=f"chapter {index} edited")
        # A sign-off alone is a QC mark: no engine hears it, so no version.
        view = _view(store.script_view, slug)
        return next(c for c in view.chapters if c.index == index)

    @router.post("/books/{slug}/lines/{line_id}/render", response_model=LineRenderResult)
    def render_line(slug: str, line_id: str) -> dict:
        """Synchronous single-line render — the "hear it back in seconds" loop.

        503 while a render job is running for this book: the job owns the cache
        directory's write path, and two writers racing on one key would be a
        torn file that hashes as a valid take.
        """
        if manager.active_for(slug, "render") is not None:
            raise HTTPException(503, "a render job is running for this book — try again when it finishes")
        script = _load_script(slug)
        utterance = next((u for u in script.utterances if u.id == line_id), None)
        if utterance is None:
            raise HTTPException(404, f"book {slug!r} has no line {line_id!r}")
        renderer = store.make_renderer(slug)
        try:
            renderer.render_line(
                renderer.spoken_for(utterance.text, utterance.render),
                utterance.render,
                script.schema_version,
            )
        except Exception as exc:  # noqa: BLE001 - a dead engine is a 502, not a 500
            raise HTTPException(502, f"synthesis failed: {type(exc).__name__}: {exc}")
        # One fake clip is enough to make the book's audio synthetic — the
        # single-line loop is the *most* used path, so it stamps the mode too.
        store.record_render_mode(slug)
        return {
            "line": _view(store.line_view, slug, line_id).model_dump(),
            "audio_url": f"{store.public_prefix()}/api/books/{slug}/lines/{line_id}/audio",
        }

    @router.get(
        "/books/{slug}/lines/{line_id}/audio",
        response_class=FileResponse,
        responses={
            200: {
                "description": "The cached take at the line's current settings.",
                "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}},
            }
        },
    )
    def line_audio(slug: str, line_id: str) -> FileResponse:
        script = _load_script(slug)
        utterance = next((u for u in script.utterances if u.id == line_id), None)
        if utterance is None:
            raise HTTPException(404, f"book {slug!r} has no line {line_id!r}")
        renderer = store.make_renderer(slug)
        key = renderer.key_for(
            renderer.spoken_for(utterance.text, utterance.render),
            utterance.render,
            script.schema_version,
        )
        path = renderer.cache_path(key)
        if not os.path.exists(path):
            raise HTTPException(404, "this line has not been rendered at its current settings")
        return FileResponse(path, media_type="audio/wav", filename=f"{line_id}.wav")

    @router.get("/books/{slug}/cast", response_model=CastSummary)
    def get_cast(slug: str) -> CastSummary:
        return _view(store.cast_summary, slug)

    @router.patch(
        "/books/{slug}/cast",
        response_model=Script,
        openapi_extra=_raw_body(
            {"type": "array", "items": _ref("CastPatchEntry")},
            "Cast rows matched on `character`; absent fields are left alone.",
        ),
    )
    async def patch_cast(slug: str, request: Request) -> Script:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            raise HTTPException(422, "expected a list of cast entries — this is not valid JSON")
        if not isinstance(body, list):
            raise HTTPException(422, "expected a list of cast entries")
        try:
            entries = [CastPatchEntry.model_validate(row) for row in body]
        except ValidationError as exc:
            raise HTTPException(422, validate.flatten(exc))
        catalog = store.engine_options()
        rows: list[dict] = []
        for entry in entries:
            fields = entry.model_fields_set
            try:
                option = validate.engine_option(
                    entry.engine or _cast_engine(slug, entry.character), catalog
                )
                if "voice_id" in fields:
                    validate.check_voice(option, entry.voice_id)
            except validate.Invalid as exc:
                raise HTTPException(422, f"{entry.character}: {exc}")
            except store.CorruptScript as exc:
                raise _corrupt(exc)
            except store.NotFound as exc:
                raise _not_found(exc)
            rows.append(entry.model_dump(exclude_unset=True))
        try:
            return store.patch_cast(slug, rows)
        except store.NotFound as exc:
            raise _not_found(exc)
        except store.InvalidScript as exc:
            raise HTTPException(422, str(exc))
        except store.CorruptScript as exc:
            raise _corrupt(exc)

    def _cast_engine(slug: str, character: str) -> str:
        """The engine a cast row will end up on when the patch does not say."""
        script = store.load_script(slug)
        for entry in script.cast:
            if entry.character == character:
                return entry.engine
        return store.core_settings().narrator_engine

    @router.post("/books/{slug}/cast/merge", response_model=Script)
    def merge_cast(slug: str, body: CastMergeRequest) -> Script:
        """Fold one character into another — the repair for a misattribution.

        Every line of ``from`` becomes ``into`` and ``from`` leaves the cast, so
        an invented speaker (the attribution heuristic's favourite mistake) is
        one request to undo instead of one PATCH per line."""
        try:
            return store.merge_cast(slug, body.from_, body.into)
        except store.NotFound as exc:
            raise _not_found(exc)
        except store.CorruptScript as exc:
            raise _corrupt(exc)
        except store.InvalidScript as exc:
            raise HTTPException(422, str(exc))
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @router.post("/books/{slug}/casting", response_model=Script)
    def casting(slug: str, body: CastingRequest) -> Script:
        if body.expressive_engine:
            try:
                validate.engine_option(body.expressive_engine, store.engine_options())
            except validate.Invalid as exc:
                raise HTTPException(422, str(exc))
        try:
            return store.apply_casting(slug, body.mode, body.expressive_engine)
        except store.NotFound as exc:
            raise _not_found(exc)
        except store.CorruptScript as exc:
            raise _corrupt(exc)
        except store.InvalidScript as exc:
            raise HTTPException(422, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/books/{slug}/lexicon", response_model=LexiconView, response_model_by_alias=True)
    def get_lexicon(slug: str) -> LexiconView:
        try:
            store.load_book(slug)
        except store.NotFound as exc:
            raise _not_found(exc)
        return store.lexicon_view(slug)

    @router.put("/books/{slug}/lexicon", response_model=LexiconView, response_model_by_alias=True)
    def put_lexicon(slug: str, body: LexiconPut) -> LexiconView:
        try:
            return store.save_book_lexicon(slug, body.entries)
        except store.NotFound as exc:
            raise _not_found(exc)

    # ---- jobs on a book ----------------------------------------------------

    @router.post("/books/{slug}/render", response_model=Job, status_code=202)
    def render_book(slug: str, body: RenderRequest) -> Job:
        try:
            store.load_book(slug)
        except store.NotFound as exc:
            raise _not_found(exc)
        derived = _view(store.derive, slug)
        known = {c.index for c in derived.script.chapters}
        if body.scope == "chapters":
            unknown = sorted(set(body.chapters or []) - known)
            if not body.chapters:
                raise HTTPException(422, "scope 'chapters' needs at least one chapter index")
            if unknown:
                raise HTTPException(
                    422,
                    f"unknown chapter(s) {', '.join(str(i) for i in unknown)} — this book has "
                    f"{', '.join(str(i) for i in sorted(known))}",
                )
        if body.scope == "lines":
            ids = {line.id for line in derived.lines}
            unknown_lines = [i for i in (body.line_ids or []) if i not in ids]
            if not body.line_ids:
                raise HTTPException(422, "scope 'lines' needs at least one line id")
            if unknown_lines:
                raise HTTPException(
                    422, f"unknown line id(s): {', '.join(unknown_lines[:10])}"
                )
        return manager.submit("render", slug, body.model_dump(exclude_none=True))

    @router.post("/books/{slug}/direct", response_model=Job, status_code=202)
    def direct_book(slug: str, body: DirectRequest) -> Job:
        """409 when the book has no line a written note could reach.

        A pass over a Kokoro-only book ran, reported success, was applied, and
        stamped exactly nothing — because ``apply_pass`` only touches
        instruction-capable engines. Refusing up front costs a
        request; the old behaviour cost an LLM pass over the whole book and told
        you nothing.
        """
        try:
            store.load_book(slug)
        except store.NotFound as exc:
            raise _not_found(exc)
        derived = _view(store.derive, slug)
        capable = store.instruction_engines()
        directable = derived.directable(capable)
        if directable.lines == 0:
            in_book = sorted({u.render.engine for u in derived.script.utterances})
            raise HTTPException(
                409,
                f"nothing in this book is routed to an engine that takes a written note — "
                f"its lines are on: {', '.join(in_book) or '(none)'}; engines that take "
                f"direction: {', '.join(sorted(capable)) or '(none)'}. Recast some lines "
                f"(POST /casting with dialogue_expressive) first.",
            )
        request = body.model_dump(exclude_none=True)
        if body.scope == "scenes":
            known = {p.scene_id for p in scene_passages(derived.script)}
            if not body.scenes:
                raise HTTPException(422, "scope 'scenes' needs at least one scene id")
            unknown = [s for s in body.scenes if s not in known]
            if unknown:
                # A pass over zero scenes used to finish "done" with counts of
                # zero — success, for a request that asked about nothing that
                # exists.
                raise HTTPException(
                    422,
                    f"unknown scene id(s): {', '.join(unknown)} — this book has "
                    f"{len(known)} scene(s), e.g. {', '.join(sorted(known)[:3])}",
                )
        if body.scope == "chapters":
            known_chapters = {c.index for c in derived.script.chapters}
            unknown_chapters = sorted(set(body.chapters or []) - known_chapters)
            if not body.chapters:
                raise HTTPException(422, "scope 'chapters' needs at least one chapter index")
            if unknown_chapters:
                raise HTTPException(
                    422, f"unknown chapter(s): {', '.join(str(i) for i in unknown_chapters)}"
                )
        if body.target_engine:
            try:
                validate.engine_option(body.target_engine, store.engine_options())
            except validate.Invalid as exc:
                raise HTTPException(422, str(exc))
        else:
            request["target_engine"] = store.default_target_engine(derived.script, capable)
        return manager.submit("direct", slug, request)

    @router.post("/books/{slug}/export", response_model=Job, status_code=202)
    def export_book(slug: str, body: ExportRequest) -> Job:
        """409 unless every chapter is fresh.

        Mastering a book with a stale chapter produces a finished-looking .m4b
        that contains a take the user already replaced — the kind of wrong that
        is only discovered by listening to the whole thing."""
        derived = _view(store.derive, slug)
        if not derived.included():
            raise HTTPException(
                409,
                "every chapter in this book is marked include_in_audio: false — there is "
                "nothing to export",
            )
        stale = derived.stale_chapters()
        if stale:
            raise HTTPException(
                409,
                f"chapter(s) {', '.join(str(i) for i in stale)} are not rendered at their current "
                f"settings — render the book before exporting",
            )
        if not ffmpeg_available():
            raise HTTPException(503, "ffmpeg/ffprobe are not available in this deployment")
        return manager.submit("export", slug, body.model_dump(exclude_none=True))

    @router.get("/books/{slug}/exports", response_model=list[ExportFile])
    def list_exports(slug: str) -> list[ExportFile]:
        try:
            store.load_book(slug)
        except store.NotFound as exc:
            raise _not_found(exc)
        return store.list_exports(slug)

    # ---- passes ------------------------------------------------------------

    @router.get("/books/{slug}/passes", response_model=list[PassSummary])
    def list_passes(slug: str) -> list[dict]:
        try:
            store.load_book(slug)
        except store.NotFound as exc:
            raise _not_found(exc)
        return [p.model_dump(exclude={"scenes"}) for p in store.list_passes(slug)]

    @router.get("/books/{slug}/passes/{pass_id}", response_model=Pass)
    def get_pass(slug: str, pass_id: str) -> dict:
        try:
            return store.load_pass(slug, pass_id).model_dump()
        except store.NotFound as exc:
            raise _not_found(exc)

    @router.post("/books/{slug}/passes/{pass_id}/decide", response_model=Pass)
    def decide(slug: str, pass_id: str, body: DecideRequest) -> dict:
        """Record decisions only. Nothing reaches the book until ``apply``."""
        try:
            pass_ = store.load_pass(slug, pass_id)
        except store.NotFound as exc:
            raise _not_found(exc)
        for decision in body.decisions:
            scene = pass_.scene(decision.scene_id)
            if scene is None:
                raise HTTPException(404, f"pass {pass_id!r} has no scene {decision.scene_id!r}")
            scene.decision = decision.decision
            if decision.instruction is not None:
                scene.final_instruction = decision.instruction.strip() or None
            elif decision.decision in ("accepted", "kept_text"):
                scene.final_instruction = scene.instruction
            elif decision.decision == "rejected":
                scene.final_instruction = None
        store.save_pass(slug, pass_)
        return pass_.model_dump()

    @router.post("/books/{slug}/passes/{pass_id}/apply", response_model=PassApplyResult)
    def apply(slug: str, pass_id: str) -> dict:
        """Commit the decided scenes. Idempotent, and manual lines are safe.

        ``instruction_engines`` is the set of engines the *gateway* says accept a
        written note, so a hybrid book stamps only the lines that can hear one;
        with no gateway it falls back to the pass's own target engine rather
        than stamping every line in the book."""
        try:
            pass_ = store.load_pass(slug, pass_id)
        except store.NotFound as exc:
            raise _not_found(exc)
        script = _load_script(slug)
        engines = store.instruction_engines() or {pass_.target_engine}
        # "set once decisions are committed" — the first time, and only if
        # something was committed. `apply_pass` stamps it unconditionally, so a
        # no-op re-apply used to move the timestamp of an application that
        # happened hours earlier. Restored here rather
        # than in `direct.py`, so `apply_pass` stays a pure function of its inputs.
        first_applied = pass_.applied_at
        decided = any(s.decision in ("accepted", "edited", "kept_text") for s in pass_.scenes)
        changed = apply_pass(script, pass_, instruction_engines=engines)
        if first_applied:
            pass_.applied_at = first_applied
        elif not decided:
            pass_.applied_at = None
        store.save_pass(slug, pass_)
        if changed:
            _save_script(slug, script, note=f"applied pass {pass_id} ({len(changed)} lines)")
        return {
            "pass": pass_.model_dump(),
            "script": _view(store.script_view, slug).model_dump(),
        }

    @router.post("/books/{slug}/passes/{pass_id}/retry", response_model=Job, status_code=202)
    def retry(slug: str, pass_id: str, body: RetryRequest) -> Job:
        try:
            pass_ = store.load_pass(slug, pass_id)
        except store.NotFound as exc:
            raise _not_found(exc)
        if not body.scenes:
            raise HTTPException(400, "no scenes to retry")
        return manager.submit(
            "direct",
            slug,
            {
                "retry": True,
                "pass_id": pass_id,
                "scenes": body.scenes,
                "target_engine": pass_.target_engine,
                "provider": pass_.provider,
                "model": pass_.model,
            },
        )

    # ---- jobs --------------------------------------------------------------

    @router.get("/jobs", response_model=list[JobSummary])
    def list_jobs() -> list[JobSummary]:
        return manager.list()

    @router.get("/jobs/{job_id}", response_model=Job)
    def get_job(job_id: str) -> Job:
        try:
            return manager.get(job_id)
        except store.NotFound as exc:
            raise _not_found(exc)

    @router.get(
        "/jobs/{job_id}/events",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": (
                    "Server-sent events: `progress` (a Job without `log`) first on connect, "
                    "`log` (one JobLogEntry), `done` (the final Job) and then the stream "
                    "closes; a heartbeat comment every 15 s."
                ),
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            }
        },
    )
    def job_events(job_id: str) -> StreamingResponse:
        try:
            job = manager.get(job_id)
        except store.NotFound as exc:
            raise _not_found(exc)

        listener = manager.subscribe(job_id)
        first = manager._progress_payload(job)

        async def stream():
            """Bridge the worker thread's queue into the event loop.

            A thread-safe ``queue.Queue`` drained non-blockingly on a short
            sleep, rather than a blocking ``get`` in a thread per subscriber:
            the events are tiny and infrequent, and this way a client that walks
            away costs nothing but a cancelled task.
            """
            try:
                yield _sse("progress", json.dumps(first, default=str))
                last_beat = time.monotonic()
                while True:
                    drained = False
                    while True:
                        try:
                            event, payload = listener.get_nowait()
                        except queue.Empty:
                            break
                        drained = True
                        yield _sse(event, payload)
                        if event == "done":
                            return
                    if not drained:
                        current = manager.get(job_id)
                        if current.status in ("done", "failed", "cancelled"):
                            # Terminal before we subscribed: say so and close,
                            # instead of holding a stream open forever.
                            yield _sse("done", json.dumps(current.model_dump(), default=str))
                            return
                        if time.monotonic() - last_beat >= _HEARTBEAT_S:
                            last_beat = time.monotonic()
                            yield ": heartbeat\n\n"
                    await asyncio.sleep(_POLL_S)
            finally:
                manager.unsubscribe(job_id, listener)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # nginx would otherwise sit on the stream
            },
        )

    @router.post("/jobs/{job_id}/pause", response_model=Job)
    def pause_job(job_id: str) -> Job:
        try:
            return manager.pause(job_id)
        except store.NotFound as exc:
            raise _not_found(exc)

    @router.post("/jobs/{job_id}/resume", response_model=Job)
    def resume_job(job_id: str) -> Job:
        try:
            return manager.resume(job_id)
        except store.NotFound as exc:
            raise _not_found(exc)

    @router.post("/jobs/{job_id}/cancel", response_model=Job)
    def cancel_job(job_id: str) -> Job:
        try:
            return manager.cancel(job_id)
        except store.NotFound as exc:
            raise _not_found(exc)

    # ---- settings & meta ---------------------------------------------------

    @router.get("/settings", response_model=Settings)
    def get_settings() -> Settings:
        return store.settings_view()

    @router.patch("/settings", response_model=Settings)
    def patch_settings(body: SettingsPatch) -> Settings:
        """Global settings, so the blast radius of a bad value is every book
        created afterwards — ``narrator_engine: "nonexistent-engine-xyz"`` was
        accepted, persisted, and silently used to cast the next book's whole
        cast. Checked against the same catalog the line-level
        PATCH uses."""
        patch = body.model_dump(exclude_none=True)
        try:
            validate.check_settings_patch(patch, store.engine_options())
        except validate.Invalid as exc:
            raise HTTPException(422, str(exc))
        return store.patch_settings(patch)

    @router.get("/health", response_model=Health)
    def health() -> Health:
        return Health(
            status="ok",
            gateway="ok" if store.gateway_reachable() else "unreachable",
            offline=store.offline(),
            fake_director=store.fake_director(),
        )

    return router


def _sse(event: str, data: str) -> str:
    body = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event}\n{body}\n"
