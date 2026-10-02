"""The book workspace on disk, and every piece of state derived from it.

Folder layout is ``docs/STUDIO-API.md`` "Book folder layout", verbatim.

The one idea this module is built around: **``audio_state`` is derived, never
stored.** A line is ``cached`` when a file exists for the key the *current*
line hashes to, ``stale`` when it was rendered before under a different key,
``missing`` when it never was. Nothing writes a "dirty" flag anywhere, so there
is no flag to forget to clear — editing a note, a voice, an engine, the text, or
a lexicon entry moves the key, and the state follows for free. That is semantic
#1 of the contract ("the cache key is the interface") taken literally: the same
``Renderer`` (and the same ``text_transform``) that renders the book is the one
that answers "is this line rendered?".

Everything that touches a book is a plain function of ``(slug, disk)`` — there
is no in-memory book model to invalidate, and two processes reading the same
``output/`` see the same answers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import uuid
import urllib.request
import wave
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import ValidationError

from ..build import build_book_script
from ..cast import (
    apply_cast_to_script,
    assign_engines,
    licence_note_for,
    non_commercial_engines,
)
from ..lexicon import Lexicon, lexicon_terms_in, load_lexicon, make_text_transform, save_lexicon
from ..direct import Pass, scene_passages
from ..master import export_summary, ffmpeg_available
from ..profiles import BOOK_TYPES
from ..render import DEFAULT_GATEWAY, Renderer, fake_synth, http_synth
from ..schema import Attribution, BookScript, CastEntry
from . import engines as engine_catalog
from .models import (
    AudioCounts,
    BookSummary,
    CastSummary,
    Chapter,
    DirectableCounts,
    DirectedCounts,
    DirectorSettings,
    EngineOption,
    ExportFile,
    ExportSettings,
    JobSummary,
    Line,
    LexiconView,
    NeedsRenderCounts,
    Scene,
    Script,
    Settings,
    VersionEntry,
    QcCounts,
)

# ---- environment ------------------------------------------------------------

_PLAYER_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "player", "player.html")
_REPO_OUTPUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "output"
)

DEFAULT_DIRECTOR_MODEL = "gemma3:12b"
_GATEWAY_TTL = 15.0  # seconds a gateway answer is reused for


def _flag(name: str, default: str = "0") -> bool:
    return (os.environ.get(name) or default).strip().lower() in ("1", "true", "yes", "on")


def offline() -> bool:
    """STUDIO_OFFLINE=1 → the fake synth, no gateway, no engine."""
    return _flag("STUDIO_OFFLINE")


def fake_director() -> bool:
    """STUDIO_FAKE_DIRECTOR=1 → canned direction, no LLM."""
    return _flag("STUDIO_FAKE_DIRECTOR")


def gateway_url() -> str:
    return os.environ.get("AUDIOBOOK_GATEWAY") or DEFAULT_GATEWAY


def public_prefix() -> str:
    """The path prefix a **browser** must use to reach this service.

    The studio is reached two ways and cannot tell them apart from inside a
    request: directly on :5174 (``/api/…``) and through the frontend's nginx,
    which proxies ``/studio/`` and strips the prefix before we see it. Any URL
    the API hands back (a line's audio, an export file) is followed by the
    browser, so it has to carry the prefix. ``app.py`` therefore also mounts
    every route under this prefix, which makes the prefixed URL correct in both
    topologies. Set ``STUDIO_URL_PREFIX=`` (empty) for a deployment that
    exposes the studio at the root with no proxy in front of it.
    """
    value = os.environ.get("STUDIO_URL_PREFIX")
    if value is None:
        value = "/studio"
    value = value.strip().rstrip("/")
    return value if value.startswith("/") or not value else f"/{value}"


def books_root() -> str:
    """Read from the environment on **every** call, not once at import.

    The tests point ``AUDIOBOOK_OUTPUT`` at a tmp dir after this module is
    already imported, and a module-level constant would quietly write the test's
    books into the real ``output/``.
    """
    root = os.environ.get("AUDIOBOOK_OUTPUT") or _REPO_OUTPUT
    os.makedirs(root, exist_ok=True)
    return root


def book_dir(slug: str) -> str:
    return os.path.join(books_root(), slug)


def now() -> str:
    """ISO 8601 UTC, to the millisecond.

    Milliseconds and not seconds because these strings are also the **sort
    key**: the shelf is "newest updated first" and the job list is "newest
    first", and two books created in the same second would otherwise tie and
    come back in whatever order the filesystem felt like.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class NotFound(LookupError):
    """No such book / version / pass. The API turns this into a 404."""


class CorruptScript(ValueError):
    """``script.json`` exists but does not validate as a ``BookScript``.

    Distinct from ``NotFound`` because the answer is different: the book is
    there, its versions are there, and the way out is
    ``POST /books/{slug}/versions/{n}/restore`` — which is exactly what the
    message says. Before this existed the same situation was an unhandled
    pydantic error, i.e. a bare 500 with no hint that the book was recoverable
    at all.
    """


class InvalidScript(ValueError):
    """A save was refused because the resulting script would not load back."""


# ---- small file helpers -----------------------------------------------------

def _write_json(path: str, payload: Any) -> None:
    """Atomic: a reader (or a restart) never sees a half-written record.

    The temp name is unique **per call**, not per process. A job record is
    written by the worker thread and by whichever HTTP thread just paused or
    resumed it; sharing one temp path let those two collide, and on Windows
    ``os.replace`` then fails because the destination is still open elsewhere —
    which killed the worker thread and hung every job queued behind it.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, ensure_ascii=False)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def _wav_duration_ms(path: str) -> int | None:
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate()
            return round(w.getnframes() / rate * 1000) if rate else None
    except (OSError, wave.Error, ZeroDivisionError):
        return None


# ---- slugs ------------------------------------------------------------------

_MAX_SLUG = 60


def slugify(title: str) -> str:
    """Length-capped on purpose: the slug is a directory name, and every path
    under it (``versions/script.v0001.json``, ``.cache-offline/<64 hex>.wav``)
    has to fit inside Windows' 260-character limit. A long title used to fail
    somewhere deep in the layout with an unhandled OSError — a 500 for what is
    really just a long name."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (s[:_MAX_SLUG].rstrip("-") or "book")


def unique_slug(title: str) -> str:
    """``the-lighthouse``, then ``the-lighthouse-2``, ``-3``… per the contract."""
    base = slugify(title)
    root = books_root()
    if not os.path.exists(os.path.join(root, base)):
        return base
    n = 2
    while os.path.exists(os.path.join(root, f"{base}-{n}")):
        n += 1
    return f"{base}-{n}"


# ---- book record (book.json) ------------------------------------------------

def book_path(slug: str) -> str:
    return os.path.join(book_dir(slug), "book.json")


def load_book(slug: str) -> dict:
    record = _read_json(book_path(slug))
    if not isinstance(record, dict):
        raise NotFound(f"no such book: {slug!r}")
    record.setdefault("slug", slug)
    return record


def save_book(slug: str, record: dict) -> dict:
    _write_json(book_path(slug), record)
    return record


def list_slugs() -> list[str]:
    root = books_root()
    out = []
    for name in os.listdir(root):
        if os.path.isfile(os.path.join(root, name, "book.json")):
            out.append(name)
    return out


# ---- script + versions ------------------------------------------------------

def script_path(slug: str) -> str:
    return os.path.join(book_dir(slug), "script.json")


def load_script(slug: str) -> BookScript:
    path = script_path(slug)
    try:
        with open(path, encoding="utf-8") as fh:
            return BookScript.model_validate_json(fh.read())
    except FileNotFoundError as exc:
        raise NotFound(f"no script for book {slug!r}") from exc
    except ValidationError as exc:
        count = len(exc.errors())
        raise CorruptScript(
            f"{path} is not a valid Book Script ({count} problem(s); first: "
            f"{_first_error(exc)}). Restore the last good version with "
            f"POST /api/books/{slug}/versions/{{n}}/restore — "
            f"GET /api/books/{slug}/versions lists them."
        ) from exc


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "unknown"
    first = errors[0]
    where = ".".join(str(x) for x in first.get("loc", ()))
    return f"{where}: {first.get('msg', 'invalid')}" if where else str(first.get("msg", "invalid"))


def _versions_index_path(slug: str) -> str:
    return os.path.join(book_dir(slug), "versions", "index.json")


def _version_file(slug: str, n: int) -> str:
    return os.path.join(book_dir(slug), "versions", f"script.v{n:04d}.json")


def save_script(slug: str, script: BookScript, note: str = "") -> int:
    """Write ``script.json`` **and** an append-only ``versions/script.vNNNN.json``.

    Versions are the whole document, not a diff: a book script is tens of KB and
    a diff chain that can't be replayed independently is worthless exactly when
    you need it (something went wrong three edits ago).
    """
    record = load_book(slug)
    version = int(record.get("version") or 0) + 1
    payload = script.model_dump_json(indent=2)
    # Round-trip before anything is written: whatever lands on disk has to be
    # loadable by `load_script`, or one bad edit bricks every later read of this
    # book. Cheap — a parse of a document we just serialized —
    # and it is the only place that can promise it for *every* field.
    try:
        BookScript.model_validate_json(payload)
    except ValidationError as exc:
        raise InvalidScript(
            f"refusing to save a script the studio could not read back: {_first_error(exc)}"
        ) from exc
    _write_json(_version_file(slug, version), payload)
    _write_json(script_path(slug), payload)

    index = _read_json(_versions_index_path(slug), []) or []
    index.append({"version": version, "saved_at": now(), "note": note})
    _write_json(_versions_index_path(slug), index)

    record["version"] = version
    record["updated_at"] = now()
    if script.meta.title:
        record["title"] = script.meta.title
    save_book(slug, record)
    return version


def versions(slug: str) -> list[VersionEntry]:
    load_book(slug)  # 404 for an unknown book rather than an empty list
    return [VersionEntry(**v) for v in (_read_json(_versions_index_path(slug), []) or [])]


def restore_version(slug: str, n: int) -> int:
    path = _version_file(slug, n)
    if not os.path.exists(path):
        raise NotFound(f"book {slug!r} has no version {n}")
    try:
        with open(path, encoding="utf-8") as fh:
            script = BookScript.model_validate_json(fh.read())
    except ValidationError as exc:
        raise CorruptScript(f"{path} does not validate ({_first_error(exc)})") from exc
    return save_script(slug, script, note=f"restored from v{n}")


# ---- manifest ---------------------------------------------------------------

def manifest_path(slug: str) -> str:
    return os.path.join(book_dir(slug), "manifest.json")


def load_manifest(slug: str) -> BookScript | None:
    """The last *rendered* script. Absent until the first render finishes."""
    try:
        with open(manifest_path(slug), encoding="utf-8") as fh:
            return BookScript.model_validate_json(fh.read())
    except (OSError, ValueError):
        return None


# ---- lexicon ----------------------------------------------------------------

def global_lexicon_path() -> str:
    return os.path.join(books_root(), "lexicon.json")


def book_lexicon_path(slug: str) -> str:
    return os.path.join(book_dir(slug), "lexicon.json")


def merged_lexicon(slug: str) -> Lexicon:
    """Global first, per-book second — the book wins, per ``load_lexicon``."""
    return load_lexicon(global_lexicon_path(), book_lexicon_path(slug))


def lexicon_view(slug: str) -> LexiconView:
    entries = load_lexicon(book_lexicon_path(slug))
    globals_ = load_lexicon(global_lexicon_path())
    return LexiconView(
        entries=entries,
        occurrences=lexicon_occurrences(slug, {**globals_, **entries}),
        **{"global": globals_},
    )


def lexicon_occurrences(slug: str, lexicon: Lexicon) -> dict[str, int]:
    """How many lines each term actually appears in.

    Counted with ``lexicon_terms_in`` — the same matcher the renderer's text
    transform uses — so an entry that reports 0 really does nothing to this
    book, rather than reporting 0 because two different regexes disagree. (It
    is also the fastest way to see a key that can never match from the UI: a ``Dr.`` entry
    that matches nothing shows up as a zero.)
    """
    if not lexicon:
        return {}
    counts = {term: 0 for term in lexicon}
    try:
        script = load_script(slug)
    except (NotFound, CorruptScript):
        return counts
    for u in script.utterances:
        for term in lexicon_terms_in(u.text, lexicon):
            counts[term] = counts.get(term, 0) + 1
    return counts


def save_book_lexicon(slug: str, entries: Lexicon) -> LexiconView:
    save_lexicon(book_lexicon_path(slug), entries)
    record = load_book(slug)
    record["updated_at"] = now()
    save_book(slug, record)
    return lexicon_view(slug)


# ---- sidecars: QC marks, chapter overrides ----------------------------------
#
# Two small JSON files beside the script, for state the *studio* owns but the
# Book Script does not model yet. Both are written the same way and read the
# same way, and both defer to the schema the moment it grows the field: the
# pipeline's `Utterance`/`Chapter` are the source of truth once they model it, and
# duplicating a field that exists in both places is how two sources of truth
# start disagreeing. `_schema_has` is the switch.

def _schema_has(model: Any, field: str) -> bool:
    return field in getattr(type(model), "model_fields", {})


def qc_path(slug: str) -> str:
    return os.path.join(book_dir(slug), "qc.json")


def load_qc(slug: str) -> dict[str, dict]:
    """``{line_id: {"flag": bool, "note": str}}`` — QC marks the engine never sees."""
    data = _read_json(qc_path(slug), {})
    return data if isinstance(data, dict) else {}


def set_qc(slug: str, line_id: str, *, flag: bool | None = None, note: str | None = None) -> dict:
    data = load_qc(slug)
    entry = dict(data.get(line_id) or {})
    if flag is not None:
        entry["flag"] = bool(flag)
    if note is not None:
        entry["note"] = str(note)
    # An empty mark is removed rather than stored: the file is a list of things
    # that need attention, and a row of defaults is not one of them.
    if not entry.get("flag") and not (entry.get("note") or "").strip():
        data.pop(line_id, None)
    else:
        data[line_id] = entry
    _write_json(qc_path(slug), data)
    return entry


def chapter_qc_path(slug: str) -> str:
    return os.path.join(book_dir(slug), "qc-chapters.json")


def load_chapter_qc(slug: str) -> dict[str, str]:
    """``{"0": "approved"}`` — string keys, like the chapter meta sidecar."""
    data = _read_json(chapter_qc_path(slug), {})
    return data if isinstance(data, dict) else {}


def set_chapter_qc(slug: str, index: int, status: str) -> None:
    data = load_chapter_qc(slug)
    if status == "unheard":
        data.pop(str(index), None)  # the default is not worth a row
    else:
        data[str(index)] = status
    _write_json(chapter_qc_path(slug), data)


def reset_chapter_qc(slug: str, indexes) -> None:
    """A rewritten chapter WAV is a new take; whatever was approved was the old one."""
    data = load_chapter_qc(slug)
    changed = False
    for index in indexes:
        if str(index) in data:
            data.pop(str(index))
            changed = True
    if changed:
        _write_json(chapter_qc_path(slug), data)


def chapter_meta_path(slug: str) -> str:
    return os.path.join(book_dir(slug), "chapters.json")


def load_chapter_meta(slug: str) -> dict[str, dict]:
    """``{"0": {"kind": ..., "include_in_audio": ...}}`` — string keys, because
    that is what JSON gives back and a silent int/str mismatch here would drop
    every override."""
    data = _read_json(chapter_meta_path(slug), {})
    return data if isinstance(data, dict) else {}


def save_chapter_meta(slug: str, data: dict[str, dict]) -> None:
    _write_json(chapter_meta_path(slug), data)


def chapter_kind(chapter: Any, meta: dict[str, dict]) -> str:
    if _schema_has(chapter, "kind"):
        return str(getattr(chapter, "kind") or "chapter")
    return str((meta.get(str(chapter.index)) or {}).get("kind") or "chapter")


def chapter_included(chapter: Any, meta: dict[str, dict]) -> bool:
    if _schema_has(chapter, "include_in_audio"):
        return bool(getattr(chapter, "include_in_audio"))
    value = (meta.get(str(chapter.index)) or {}).get("include_in_audio")
    return True if value is None else bool(value)


def set_chapter_meta(
    slug: str,
    script: BookScript,
    index: int,
    *,
    kind: str | None = None,
    include_in_audio: bool | None = None,
) -> None:
    """Write the override where it will actually be honoured.

    On the schema when it has the field (the renderer and the exporter read the
    script, not this sidecar); in ``chapters.json`` until then. Both are read
    back through ``chapter_kind``/``chapter_included``, so the switch-over needs
    no migration.
    """
    chapter = next((c for c in script.chapters if c.index == index), None)
    if chapter is None:
        raise NotFound(f"book {slug!r} has no chapter {index}")
    meta = load_chapter_meta(slug)
    entry = dict(meta.get(str(index)) or {})
    if kind is not None:
        if _schema_has(chapter, "kind"):
            setattr(chapter, "kind", kind)
        else:
            entry["kind"] = kind
    if include_in_audio is not None:
        if _schema_has(chapter, "include_in_audio"):
            setattr(chapter, "include_in_audio", include_in_audio)
        else:
            entry["include_in_audio"] = bool(include_in_audio)
    if entry:
        meta[str(index)] = entry
        save_chapter_meta(slug, meta)


def included_chapters(slug: str, script: BookScript) -> set[int]:
    """Chapter indices the render job and the exporter are allowed to touch."""
    meta = load_chapter_meta(slug)
    return {c.index for c in script.chapters if chapter_included(c, meta)}


# ---- renderers --------------------------------------------------------------

def cache_dir(slug: str) -> str:
    """Where rendered clips live. Offline (fake synth) gets its own directory:
    the cache key describes what the engine was asked for, not who answered, so
    a fake clip in ``.cache/`` would be served as real audio the day
    STUDIO_OFFLINE flips to 0 — every line "cached", every line silent."""
    return os.path.join(book_dir(slug), ".cache-offline" if offline() else ".cache")


def make_renderer(slug: str, *, synth=None, progress: bool = False) -> Renderer:
    """The renderer for this book — cache dir, and the lexicon as a transform.

    Every caller goes through here (the render job, the single-line endpoint,
    and the ``audio_state`` derivation) so that all three compute *the same*
    key. A second place that built a ``Renderer`` with a different transform
    would report lines as cached that the renderer would then re-synthesize.
    """
    if synth is None:
        synth = (
            fake_synth()
            if offline()
            else http_synth(gateway_url(), instruction_engines=instruction_engines())
        )
    lex = merged_lexicon(slug)
    return Renderer(
        synth,
        cache_dir=cache_dir(slug),
        progress=progress,
        text_transform=make_text_transform(lex) if lex else None,
        # Contract rule 2, enforced at the last possible moment as well as at
        # the PATCH: a `spoken_text` whose tags this engine does not advertise
        # is not sent to it. The catalog is merged (gateway + static), so this
        # still works with the gateway down instead of silently disabling the
        # check exactly when engines are least trustworthy.
        engine_tags=engine_catalog.tags_by_engine(engine_options()),
    )


def _key_renderer(slug: str) -> Renderer:
    """A renderer used only for ``cache_key``/``cache_path`` — the synth
    callable is never invoked, so it does not matter which one it is."""
    return make_renderer(slug, synth=fake_synth())


def line_keys(slug: str, script: BookScript, renderer: Renderer | None = None) -> dict[str, str]:
    r = renderer or _key_renderer(slug)
    # `spoken_for`, not `spoken_text or text`: the renderer may decline a tagged
    # spoken_text for an engine that cannot hear the tags, and a key computed
    # from the other string would mark every such line stale forever.
    return {
        u.id: r.key_for(r.spoken_for(u.text, u.render), u.render, script.schema_version)
        for u in script.utterances
    }


# ---- derived state ----------------------------------------------------------

def _attribution_of(utterance: Any) -> dict:
    """``Utterance.attribution`` as a dict — the schema's default when absent.

    Read through ``getattr`` on purpose: the studio has to load scripts written
    before ``pipeline/attribute.py`` added the field, and a hard reference would
    make it refuse either the old schema or the new one. ``Line.attribution`` is always an object on the wire
    (Amendment v1.4), so a script from before the field reads as the schema's
    default (narration: ``basis "none"``, confidence 1) rather than as null.
    """
    value = getattr(utterance, "attribution", None)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return dict(value)
    return Attribution().model_dump()


class _Derived:
    """One pass over a book: keys, cache hits, manifest timings, chapters."""

    def __init__(self, slug: str, script: BookScript | None = None) -> None:
        self.slug = slug
        self.script = script if script is not None else load_script(slug)
        self.manifest = load_manifest(slug)
        self.renderer = _key_renderer(slug)
        self.keys = line_keys(slug, self.script, self.renderer)
        self.qc = load_qc(slug)
        self.chapter_qc = load_chapter_qc(slug)
        self.chapter_meta = load_chapter_meta(slug)

        manifest_lines = {u.id: u for u in (self.manifest.utterances if self.manifest else [])}
        self.manifest_chapters = {c.index: c for c in (self.manifest.chapters if self.manifest else [])}
        self.lines: list[Line] = []
        for u in self.script.utterances:
            key = self.keys[u.id]
            cached = os.path.exists(self.renderer.cache_path(key))
            previous = manifest_lines.get(u.id)
            span = previous.audio if previous is not None else None
            if cached:
                state = "cached"
                duration = _wav_duration_ms(self.renderer.cache_path(key))
                if duration is None and span is not None:
                    duration = span.end_ms - span.start_ms
            elif span is not None:
                state = "stale"
                duration = span.end_ms - span.start_ms
            else:
                state = "missing"
                duration = None
            marks = self.qc.get(u.id) or {}
            self.lines.append(
                Line(
                    id=u.id,
                    order=u.order,
                    chapter_index=u.chapter_index,
                    scene_index=u.scene_index,
                    speaker=u.speaker,
                    kind=getattr(u.kind, "value", str(u.kind)),
                    text=u.text,
                    intent=u.intent,
                    render=u.render,
                    audio=span,
                    audio_state=state,  # type: ignore[arg-type]
                    duration_ms=duration,
                    flag=bool(marks.get("flag")),
                    note=str(marks.get("note") or ""),
                    attribution=_attribution_of(u),
                )
            )
        self.by_id = {line.id: line for line in self.lines}
        self.manifest_keys = {
            u.id: (u.audio.cache_key if u.audio else None) for u in manifest_lines.values()
        }

    # -- chapters ------------------------------------------------------------

    def chapter_fresh(self, index: int) -> bool:
        """The chapter WAV on disk reflects every one of its lines' current keys.

        Three conditions, and all three have bitten a real render: every line is
        cached (so the audio exists at all), the chapter file exists (so it was
        actually stitched), and the manifest's key for each line equals today's
        key (so the stitch used *these* takes and not an earlier set).
        """
        ids = [u.id for u in self.script.utterances if u.chapter_index == index]
        if not ids:
            return False
        if any(self.by_id[i].audio_state != "cached" for i in ids):
            return False
        chapter = self.manifest_chapters.get(index)
        if chapter is None or not chapter.audio_file:
            return False
        if not os.path.exists(os.path.join(book_dir(self.slug), chapter.audio_file)):
            return False
        rendered = {u.id for u in (self.manifest.utterances if self.manifest else []) if u.chapter_index == index}
        if rendered != set(ids):
            return False  # lines were added or removed since the last render
        return all(self.manifest_keys.get(i) == self.keys[i] for i in ids)

    def chapters(self) -> list[Chapter]:
        scenes_by_chapter: dict[int, list[Scene]] = {}
        for passage in scene_passages(self.script):
            scenes_by_chapter.setdefault(passage.chapter_index, []).append(
                Scene(
                    id=passage.scene_id,
                    chapter_index=passage.chapter_index,
                    scene_index=passage.scene_index,
                    line_ids=list(passage.line_ids),
                )
            )
        out: list[Chapter] = []
        for chapter in self.script.chapters:
            ids = [u.id for u in self.script.utterances if u.chapter_index == chapter.index]
            rendered = self.manifest_chapters.get(chapter.index)
            out.append(
                Chapter(
                    index=chapter.index,
                    title=chapter.title,
                    scenes=scenes_by_chapter.get(chapter.index, []),
                    audio_file=rendered.audio_file if rendered else None,
                    duration_ms=rendered.duration_ms if rendered else 0,
                    lines=len(ids),
                    directed=sum(1 for i in ids if self.by_id[i].render.instruction),
                    audio=self._counts(ids),
                    fresh=self.chapter_fresh(chapter.index),
                    kind=chapter_kind(chapter, self.chapter_meta),  # type: ignore[arg-type]
                    include_in_audio=chapter_included(chapter, self.chapter_meta),
                    qc_status=self.chapter_qc.get(str(chapter.index), "unheard"),  # type: ignore[arg-type]
                )
            )
        return out

    def qc_counts(self) -> QcCounts:
        included = self.included()
        statuses = [self.chapter_qc.get(str(i), "unheard") for i in included]
        return QcCounts(
            approved=sum(1 for s in statuses if s == "approved"),
            pickups=sum(1 for s in statuses if s == "pickups"),
            chapters=len(included),
        )

    def included(self) -> set[int]:
        return {
            c.index for c in self.script.chapters if chapter_included(c, self.chapter_meta)
        }

    def stale_chapters(self) -> list[int]:
        """Chapters whose audio does not reflect their lines' current keys.

        The single source of both ``BookSummary.exportable`` and the 409 body of
        ``POST /export`` — two answers to "can this be exported?" that could
        disagree would be a button that is enabled and then refused.

        A chapter marked ``include_in_audio: false`` is not in this list: it is
        deliberately never rendered, so counting it would make such a book
        permanently un-exportable — the same dead end as a chapter that can
        never become fresh, one level up."""
        included = self.included()
        return [
            c.index for c in self.script.chapters
            if c.index in included and not self.chapter_fresh(c.index)
        ]

    def _counts(self, ids: list[str]) -> AudioCounts:
        counts = AudioCounts()
        for i in ids:
            setattr(counts, self.by_id[i].audio_state, getattr(counts, self.by_id[i].audio_state) + 1)
        return counts

    # -- summary -------------------------------------------------------------

    def licence_note(self) -> str | None:
        """The cast sheet's note, widened by any line routed to a
        non-commercial engine even if its character's cast entry disagrees — a
        per-line engine override is exactly how a book acquires a licence
        problem the cast sheet does not show."""
        flagged = {
            u.render.engine for u in self.script.utterances
            if u.render.engine in non_commercial_engines()
        }
        extra = [CastEntry(character=f"line:{e}", engine=e) for e in sorted(flagged)]
        return licence_note_for(list(self.script.cast) + extra)

    def directable(self, instruction_capable: set[str] | None = None) -> DirectableCounts:
        """Lines routed to an engine that takes a written note.

        A Kokoro-only book used to let you run a director pass, watch it
        succeed, apply it, and stamp nothing — because ``apply_pass`` only
        touches instruction-capable engines. This is the number that makes that
        refusable up front."""
        capable = instruction_capable if instruction_capable is not None else instruction_engines()
        return DirectableCounts(
            lines=sum(1 for u in self.script.utterances if u.render.engine in capable),
            total=len(self.script.utterances),
        )

    def needs_render(self) -> NeedsRenderCounts:
        """Work outstanding, in the two units the UI has to label separately."""
        included = self.included()
        return NeedsRenderCounts(
            lines=sum(
                1 for line in self.lines
                if line.audio_state != "cached" and line.chapter_index in included
            ),
            chapters=len(self.stale_chapters()),
        )

    def summary(self, record: dict, last_job: JobSummary | None) -> BookSummary:
        words = sum(len(u.text.split()) for u in self.script.utterances)
        engines: dict[str, int] = {}
        for u in self.script.utterances:
            engines[u.render.engine] = engines.get(u.render.engine, 0) + 1
        profile = self.script.profile
        status = "idle"
        if last_job is not None:
            if last_job.status in ("running", "queued", "paused"):
                status = {"render": "rendering", "direct": "directing", "export": "exporting"}[
                    last_job.kind
                ]
            elif last_job.status == "failed":
                status = "failed"
        return BookSummary(
            slug=self.slug,
            title=record.get("title") or self.script.meta.title or self.slug,
            author=record.get("author") or self.script.meta.author or "",
            book_type=(profile.book_type if profile else self.script.meta.book_type),
            profile_label=(profile.label if profile else ""),
            language=self.script.meta.language,
            chapters=len(self.script.chapters),
            utterances=len(self.script.utterances),
            words=words,
            est_minutes=round(words / 155, 1),
            duration_ms=sum(c.duration_ms for c in (self.manifest.chapters if self.manifest else [])),
            engines=engines,
            directed=DirectedCounts(
                directed=sum(1 for line in self.lines if line.render.instruction),
                total=len(self.lines),
            ),
            audio=self._counts([line.id for line in self.lines]),
            cast=list(self.script.cast),
            created_at=record.get("created_at", ""),
            updated_at=record.get("updated_at", ""),
            rendered_at=record.get("rendered_at"),
            last_job=last_job,
            status=status,  # type: ignore[arg-type]
            licence_note=self.licence_note(),
            exportable=bool(self.included()) and not self.stale_chapters(),
            directable=self.directable(),
            needs_render=self.needs_render(),
            synthetic=recorded_render_mode(self.slug, record) == "offline",
            qc=self.qc_counts(),
        )


# The jobs module registers itself here so a book summary can report
# ``status``/``last_job`` without ``store`` importing ``jobs`` (which imports
# ``store``). A hook, not an import cycle.
_LAST_JOB: Callable[[str], JobSummary | None] | None = None


def register_job_provider(fn: Callable[[str], JobSummary | None] | None) -> None:
    global _LAST_JOB
    _LAST_JOB = fn


def _last_job(slug: str) -> JobSummary | None:
    return _LAST_JOB(slug) if _LAST_JOB is not None else None


def derive(slug: str, script: BookScript | None = None) -> _Derived:
    """One pass over a book's derived state (keys, cache hits, chapter
    freshness). Cheap enough to call per request; nothing is memoized because a
    stale answer here is exactly the bug this design removes."""
    return _Derived(slug, script=script)


def book_summary(slug: str, derived: _Derived | None = None) -> BookSummary:
    record = load_book(slug)
    d = derived or _Derived(slug)
    return d.summary(record, _last_job(slug))


def script_view(slug: str) -> Script:
    record = load_book(slug)
    d = _Derived(slug)
    return Script(
        book=d.summary(record, _last_job(slug)),
        version=int(record.get("version") or 1),
        profile=(d.script.profile.model_dump() if d.script.profile else {}),
        cast=list(d.script.cast),
        chapters=d.chapters(),
        lines=d.lines,
    )


def line_view(slug: str, line_id: str) -> Line:
    d = _Derived(slug)
    line = d.by_id.get(line_id)
    if line is None:
        raise NotFound(f"book {slug!r} has no line {line_id!r}")
    return line


def list_books() -> list[BookSummary]:
    out: list[BookSummary] = []
    for slug in list_slugs():
        try:
            out.append(book_summary(slug))
        except (NotFound, ValueError, OSError):
            continue  # a half-written or hand-edited folder must not 500 the shelf
    out.sort(key=lambda b: b.updated_at, reverse=True)
    return out


# ---- creating a book --------------------------------------------------------

_SUFFIXES = {".txt": "txt", ".md": "md", ".markdown": "md", ".text": "txt", ".epub": "epub"}


def create_book(
    *,
    title: str = "",
    author: str = "",
    text: str | None = None,
    file_bytes: bytes | None = None,
    filename: str = "",
    book_type: str = "auto",
    voices: str = "auto",
    casting_mode: str | None = None,
    narrator_engine: str | None = None,
    expressive_engine: str | None = None,
) -> BookSummary:
    """Ingest a source, build the Book Script, and lay out ``output/<slug>/``.

    The script is built from a **temp copy first** because an EPUB carries its
    own title and the slug is derived from the title: building first is what
    lets an upload with no title land at ``output/the-cold-lantern`` instead of
    ``output/book``.
    """
    suffix = ".txt"
    if filename:
        suffix = os.path.splitext(filename)[1].lower()
        if suffix not in _SUFFIXES:
            raise ValueError(
                f"unsupported file type {suffix or '(none)'!r} — upload a .txt, .md or .epub"
            )
    if text is None and file_bytes is None:
        raise ValueError("a book needs either text or a file")

    core = core_settings()
    narrator_engine = narrator_engine or core.narrator_engine
    expressive_engine = expressive_engine or core.expressive_engine
    mode = casting_mode or core.casting_mode

    tmp_dir = tempfile.mkdtemp(prefix="studio_new_")
    try:
        tmp_src = os.path.join(tmp_dir, f"source{suffix}")
        if file_bytes is not None:
            with open(tmp_src, "wb") as fh:
                fh.write(file_bytes)
        else:
            with open(tmp_src, "w", encoding="utf-8") as fh:
                fh.write(text or "")

        try:
            script = build_book_script(
                tmp_src,
                title=title,
                author=author,
                book_type=book_type,
                multi_voice={"single": False, "multi": True}.get(voices, None),
                engine=narrator_engine,
                casting_mode=mode,
                expressive_engine=expressive_engine,
            )
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad upload is the client's problem
            # A .txt renamed .epub, or a zip with no EPUB structure, surfaced as
            # a bare 500 with a plain-text body. Whatever the
            # parser threw, the honest answer is "this file is not readable".
            raise ValueError(
                f"could not read this {_SUFFIXES.get(suffix, 'text')} file: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not script.utterances:
            raise ValueError("nothing readable in that source — no utterances were produced")

        final_title = script.meta.title or title or "Untitled book"
        script.meta.title = final_title
        slug = unique_slug(final_title)
        target = book_dir(slug)
        os.makedirs(target, exist_ok=True)
        source_name = f"source{suffix}"
        shutil.move(tmp_src, os.path.join(target, source_name))
        script.meta.source_path = os.path.join(target, source_name)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    stamp = now()
    record = {
        "slug": slug,
        "title": final_title,
        "author": script.meta.author or author or "",
        "created_at": stamp,
        "updated_at": stamp,
        "rendered_at": None,
        "source_kind": "text" if file_bytes is None else _SUFFIXES.get(suffix, "txt"),
        "version": 0,
        "settings": {
            "book_type": book_type,
            "voices": voices,
            "casting_mode": mode,
            "narrator_engine": narrator_engine,
            "expressive_engine": expressive_engine,
        },
    }
    save_book(slug, record)
    save_script(slug, script, note="created")
    return book_summary(slug)


def delete_book(slug: str) -> None:
    path = book_dir(slug)
    if not os.path.isdir(path):
        raise NotFound(f"no such book: {slug!r}")
    shutil.rmtree(path, ignore_errors=True)


def render_mode() -> str:
    """What the next synth call will actually be: ``"offline"`` (the fake synth)
    or ``"live"`` (a real engine through the gateway)."""
    return "offline" if offline() else "live"


def mark_rendered(slug: str, mode: str | None = None) -> None:
    record = load_book(slug)
    record["rendered_at"] = now()
    record["updated_at"] = now()
    record["render_mode"] = mode or render_mode()
    save_book(slug, record)


def recorded_render_mode(slug: str, record: dict) -> str:
    """How this book's audio was made — stamped if we know, inferred if not.

    Books rendered before ``render_mode`` existed (older local fixtures, and
    the one the review caught: a fully "exportable" sine-tone stub wearing a
    real licence warning) carry no stamp. Offline renders cache into
    ``.cache-offline/`` and real ones into ``.cache/``, so a book with the fake
    directory and no real one was rendered by the fake synth — evidence on disk,
    not a guess. With both present we cannot tell which stitched the chapters,
    and an unproven "this is fake" is as bad as an unproven "this is real": the
    answer is ``"unknown"`` and ``synthetic`` stays false.
    """
    stamped = str(record.get("render_mode") or "")
    if stamped:
        return stamped
    if not record.get("rendered_at"):
        return "unknown"
    fake = os.path.isdir(os.path.join(book_dir(slug), ".cache-offline"))
    real = os.path.isdir(os.path.join(book_dir(slug), ".cache"))
    if fake and not real:
        return "offline"
    return "unknown"


def record_render_mode(slug: str, mode: str | None = None) -> None:
    """Stamp how a book's audio was produced, without claiming it was a full
    render. Called by the single-line render too: one fake clip in a book is
    still a book you must not hand to a listener, so ``synthetic`` has to flip
    on the small path as well as the big one."""
    try:
        record = load_book(slug)
    except NotFound:
        return
    record["render_mode"] = mode or render_mode()
    record["updated_at"] = now()
    save_book(slug, record)


def copy_player(slug: str) -> None:
    if os.path.exists(_PLAYER_SRC):
        shutil.copyfile(_PLAYER_SRC, os.path.join(book_dir(slug), "player.html"))


# ---- cast + casting ---------------------------------------------------------

def patch_cast(slug: str, entries: list[dict]) -> Script:
    """Merge partial cast rows (matched on ``character``) and re-stamp the lines.

    A name the book does not have is a ``NotFound`` unless the row says
    ``create: true``. Silently adding a phantom character
    is worse than an error: no line references it, so it does nothing, and the
    caller's actual typo is never reported.
    """
    script = load_script(slug)
    by_character = {c.character: c for c in script.cast}
    for raw in entries:
        name = str(raw.get("character") or "")
        current = by_character.get(name)
        if current is None:
            if not raw.get("create"):
                known = ", ".join(sorted(by_character)) or "(none)"
                raise NotFound(
                    f"book {slug!r} has no character {name!r} — its cast is: {known}. "
                    f"Pass \"create\": true to add a new one."
                )
            current = CastEntry(character=name)
            script.cast.append(current)
            by_character[name] = current
        for field in ("voice_id", "engine", "notes", "voice_design"):
            if field not in raw:
                continue
            value = raw[field]
            if value is None and field in ("engine", "notes"):
                continue  # not nullable on CastEntry: null here means "leave it"
            setattr(current, field, value)
    apply_cast_to_script(script, list(script.cast))
    save_script(slug, script, note="cast edited")
    return script_view(slug)


def casting_mode_for(slug: str) -> str:
    """The book's own casting mode, falling back to the studio default."""
    core = core_settings()
    try:
        stored = (load_book(slug).get("settings") or {}).get("casting_mode")
    except NotFound:
        stored = None
    return str(stored or core.casting_mode)


def new_cast_entry(slug: str, script: BookScript, character: str) -> CastEntry:
    """A cast entry for a character the book did not have, via the casting rule.

    Reassigning a misattributed line to a name that is not in the cast has to produce a *real* entry, not a blank one: the same
    ``assign_engines`` the casting endpoint uses picks the engine and either a
    named voice or a voice description, and it is run over the existing cast so
    the new character does not land on a voice somebody else already has.
    """
    core = core_settings()
    fresh = CastEntry(character=character)
    assigned = assign_engines(
        list(script.cast) + [fresh],
        mode=casting_mode_for(slug),
        narrator_engine=core.narrator_engine,
        expressive_engine=core.expressive_engine,
    )
    return assigned[-1]


def set_attribution(utterance: Any, *, basis: str, confidence: float) -> bool:
    """Stamp ``Utterance.attribution`` when the schema has it. Returns whether it did.

    The field belongs to the attribution stage (``pipeline/schema.py``), so
    this writes through whatever type it is — an existing model gets ``model_copy``, an empty one is constructed
    from the annotation — and reports False rather than guessing if the field is
    not there yet.
    """
    fields = getattr(type(utterance), "model_fields", {})
    if "attribution" not in fields:
        return False
    update = {"basis": basis, "confidence": confidence}
    current = getattr(utterance, "attribution", None)
    if hasattr(current, "model_copy"):
        setattr(utterance, "attribution", current.model_copy(update=update))
        return True
    annotation = fields["attribution"].annotation
    for candidate in getattr(annotation, "__args__", (annotation,)):
        if hasattr(candidate, "model_validate"):
            setattr(utterance, "attribution", candidate.model_validate(update))
            return True
    setattr(utterance, "attribution", update)
    return True


def merge_cast(slug: str, source: str, into: str) -> Script:
    """Every line of ``source`` becomes ``into``; ``source`` leaves the cast.

    The repair for an attribution mistake seen from the other end:
    when the heuristic invented "Tomas" for two of Mara's lines, the fix is one
    merge rather than re-assigning every line by hand. Lines are re-stamped from
    the surviving entry, so the merged character's voice follows immediately.
    """
    script = load_script(slug)
    names = {c.character for c in script.cast}
    if source not in names:
        raise NotFound(f"book {slug!r} has no character {source!r}")
    if into not in names:
        raise NotFound(f"book {slug!r} has no character {into!r}")
    if source == into:
        raise ValueError("a character cannot be merged into itself")
    for u in script.utterances:
        if u.speaker == source:
            u.speaker = into
    script.cast = [c for c in script.cast if c.character != source]
    apply_cast_to_script(script, list(script.cast))
    save_script(slug, script, note=f"merged {source} into {into}")
    return script_view(slug)


def cast_summary(slug: str) -> CastSummary:
    """The cast sheet with the three facts that make it reviewable: how many
    lines each character has, where they first speak, and which of them would
    sound identical (same engine + voice) — the thing you cannot hear until the
    render is done and the wrong two characters share a voice."""
    d = _Derived(slug)
    lines: dict[str, int] = {}
    first: dict[str, str] = {}
    for line in d.lines:
        lines[line.speaker] = lines.get(line.speaker, 0) + 1
        first.setdefault(line.speaker, line.text)  # the words, not the id: a cast sheet is read by a person
    grouped: dict[tuple[str, str], list[str]] = {}
    for entry in d.script.cast:
        if entry.voice_id:
            grouped.setdefault((entry.engine, entry.voice_id), []).append(entry.character)
    return CastSummary(
        cast=list(d.script.cast),
        lines={c.character: lines.get(c.character, 0) for c in d.script.cast},
        first_line={c.character: first[c.character] for c in d.script.cast if c.character in first},
        shared_voices=[sorted(names) for names in grouped.values() if len(names) > 1],
    )


def apply_casting(slug: str, mode: str, expressive_engine: str | None = None) -> Script:
    core = core_settings()
    script = load_script(slug)
    new_cast = assign_engines(
        script.cast,
        mode=mode,
        narrator_engine=core.narrator_engine,
        expressive_engine=expressive_engine or core.expressive_engine,
    )
    apply_cast_to_script(script, new_cast)
    save_script(slug, script, note=f"casting: {mode}")
    record = load_book(slug)
    record.setdefault("settings", {})["casting_mode"] = mode
    save_book(slug, record)
    return script_view(slug)


# ---- passes -----------------------------------------------------------------

def passes_dir(slug: str) -> str:
    return os.path.join(book_dir(slug), "passes")


def pass_path(slug: str, pass_id: str) -> str:
    return os.path.join(passes_dir(slug), f"{pass_id}.json")


def save_pass(slug: str, pass_: Pass) -> None:
    _write_json(pass_path(slug, pass_.id), pass_.model_dump_json(indent=2))


def load_pass(slug: str, pass_id: str) -> Pass:
    try:
        with open(pass_path(slug, pass_id), encoding="utf-8") as fh:
            return Pass.model_validate_json(fh.read())
    except (OSError, ValueError) as exc:
        raise NotFound(f"book {slug!r} has no pass {pass_id!r}") from exc


def list_passes(slug: str) -> list[Pass]:
    directory = passes_dir(slug)
    if not os.path.isdir(directory):
        return []
    out: list[Pass] = []
    for name in os.listdir(directory):
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            out.append(load_pass(slug, name[: -len(".json")]))
        except NotFound:
            continue
    out.sort(key=lambda p: p.created_at, reverse=True)
    return out


def scene_cache_path(slug: str) -> str:
    return os.path.join(passes_dir(slug), ".cache.json")


def load_scene_cache(slug: str) -> dict:
    data = _read_json(scene_cache_path(slug), {})
    return data if isinstance(data, dict) else {}


def save_scene_cache(slug: str, cache: dict) -> None:
    _write_json(scene_cache_path(slug), cache)


# ---- exports ----------------------------------------------------------------

def exports_meta_path(slug: str) -> str:
    """Beside ``exports/``, never inside it. ``master.export_summary`` lists
    every file in that folder and ``/books/<slug>/exports/`` serves it, so a
    sidecar in there shows up as a downloadable "audiobook"."""
    return os.path.join(book_dir(slug), "exports.json")


_EXPORT_EXTRA_FIELDS = (
    "loudness", "sample_rate", "bitrate", "chapter", "duration_ms",
    "measured", "checks", "compliance", "format",
)


def record_export(slug: str, file: str, **facts: Any) -> None:
    """Remember what a mastering run *chose*, keyed by the file it produced.

    The bytes on disk cannot answer "was this ACX-checked, and did it pass" —
    ffprobe would have to re-measure, and a `.mp3` says nothing about which
    loudness pass made it. So the export job writes its own answer here and
    ``GET /exports`` reads it back; the file listing on disk stays the source of
    truth for what still exists."""
    meta = _read_json(exports_meta_path(slug), {}) or {}
    entry = dict(meta.get(file) or {})
    entry.update({k: v for k, v in facts.items() if k in _EXPORT_EXTRA_FIELDS and v is not None})
    entry.setdefault("created_at", now())
    meta[file] = entry
    _write_json(exports_meta_path(slug), meta)


def list_exports(slug: str) -> list[ExportFile]:
    """One entry per file in ``exports/``, enriched with what the job recorded.

    Files with no index entry (an older export, or one dropped in by hand) are
    still listed — an export you can download but cannot see in the library is
    confusing."""
    meta = _read_json(exports_meta_path(slug), {}) or {}
    defaults = core_settings().export
    out = []
    for entry in export_summary(book_dir(slug)):
        extra = dict(meta.get(entry["file"], {}))
        payload = dict(entry)
        payload["format"] = extra.get("format") or payload.get("format") or ""
        out.append(
            ExportFile(
                url=f"{public_prefix()}/books/{slug}/exports/{entry['file']}",
                loudness=extra.get("loudness", defaults.loudness),
                sample_rate=int(extra.get("sample_rate", defaults.sample_rate) or defaults.sample_rate),
                bitrate=extra.get("bitrate"),
                chapter=extra.get("chapter"),
                duration_ms=extra.get("duration_ms"),
                measured=extra.get("measured"),
                checks=extra.get("checks"),
                compliance=extra.get("compliance"),
                **{k: v for k, v in payload.items() if k != "format"},
                format=payload["format"],
            )
        )
    out.sort(key=lambda e: (e.created_at, e.file), reverse=True)
    return out


# ---- settings ---------------------------------------------------------------

class CoreSettings:
    """The settings a job needs, with no network call in sight.

    Split from the full ``Settings`` view on purpose: the view lists the
    gateway's engines, and a render job must not depend on the gateway being
    reachable just to learn which engine it is supposed to use.
    """

    def __init__(self, stored: dict) -> None:
        self.narrator_engine: str = stored.get("narrator_engine") or os.environ.get(
            "STUDIO_NARRATOR_ENGINE") or "kokoro"
        self.expressive_engine: str = stored.get("expressive_engine") or os.environ.get(
            "STUDIO_EXPRESSIVE_ENGINE") or "breeze"
        self.casting_mode: str = stored.get("casting_mode") or os.environ.get(
            "STUDIO_CASTING_MODE") or "narrator_only"
        director = stored.get("director") or {}
        self.director = DirectorSettings(
            provider=director.get("provider") or "ollama",
            model=director.get("model") or "",
        )
        try:
            self.export = ExportSettings(**(stored.get("export") or {}))
        except ValidationError:
            # settings.json is hand-editable and survives upgrades; a value this
            # build no longer accepts must not take the whole service down on
            # boot. The PATCH endpoint validates, so this only catches history.
            self.export = ExportSettings()


def settings_path() -> str:
    return os.path.join(books_root(), "settings.json")


def stored_settings() -> dict:
    data = _read_json(settings_path(), {})
    return data if isinstance(data, dict) else {}


def core_settings() -> CoreSettings:
    return CoreSettings(stored_settings())


def resolved_director() -> DirectorSettings:
    """Stored choice, else the gateway's ``/api/director/defaults``, else
    ``DIRECTOR_MODEL``, else a built-in name. Never hardcodes a model as *the*
    model: an 8 GB machine and a 48 GB machine want different
    directors and neither should need a rebuild."""
    core = core_settings()
    if core.director.model:
        return core.director
    remote = _gateway_json("/api/director/defaults", timeout=2.0) or {}
    model = remote.get("model") or os.environ.get("DIRECTOR_MODEL") or DEFAULT_DIRECTOR_MODEL
    provider = remote.get("provider") if remote.get("provider") in ("ollama", "gemini") else "ollama"
    return DirectorSettings(provider=provider, model=str(model))


def remote_prompt_version() -> str:
    return str((_gateway_json("/api/director/defaults", timeout=2.0) or {}).get("prompt_version") or "")


_gateway_cache: dict[str, tuple[float, Any]] = {}


def _gateway_json(path: str, timeout: float = 5.0) -> Any:
    """GET a gateway path, cached briefly. Returns None when unreachable.

    Cached (including the failure) because ``GET /settings`` and every book
    summary would otherwise pay a DNS timeout each, and an unreachable gateway
    is exactly when you least want a page to take ten seconds to say so.
    """
    hit = _gateway_cache.get(path)
    if hit is not None and time.monotonic() - hit[0] < _GATEWAY_TTL:
        return hit[1]
    url = f"{gateway_url().rstrip('/')}{path}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - unreachable/DNS/timeout/bad JSON are all "no"
        payload = None
    _gateway_cache[path] = (time.monotonic(), payload)
    return payload


def gateway_reachable() -> bool:
    # /api/health answers instantly; /api/engines probes every registered engine
    # first (about 3 s when the GPU profile is down), which used to time out
    # right at the boundary and report a healthy gateway as unreachable.
    return _gateway_json("/api/health", timeout=3.0) is not None


def engine_options() -> list[EngineOption]:
    """The engine catalog the UI and the validators both read.

    The gateway's ``/api/engines``, flattened — **merged over** the static
    catalog in ``engines.py``, because the gateway's answer is incomplete in
    exactly the cases this list has to survive: unreachable (empty list) and
    reachable-but-engine-down (``info: null``, so no voices, no tags, no
    licence). Amendments v1.1 make this list, not the gateway's, the source of
    truth; ``available`` still comes only from the gateway.

    ``non_commercial`` comes from ``cast.non_commercial_engines()`` — the same
    hand-maintained fact the pipeline uses to decide whether a book carries a
    licence note — rather than from parsing the licence string, so the badge and
    the note can never disagree.
    """
    raw = _gateway_json("/api/engines", timeout=5.0)
    if not isinstance(raw, list):
        return engine_catalog.merge_options([])
    flagged = non_commercial_engines()
    out: list[EngineOption] = []
    for entry in raw:
        info = entry.get("info") or {}
        caps = info.get("capabilities") or {}
        engine_id = entry.get("id") or info.get("id") or ""
        out.append(
            EngineOption(
                id=engine_id,
                name=info.get("name") or engine_id,
                available=bool(entry.get("available")),
                instruction=bool(caps.get("instruction")),
                audio_tags=list(info.get("audio_tags") or []),
                voices=[
                    {"id": v.get("id", ""), "label": v.get("label", v.get("id", ""))}
                    for v in (info.get("voices") or [])
                ],
                license=info.get("license") or "",
                non_commercial=engine_id in flagged,
            )
        )
    return engine_catalog.merge_options(out)


def instruction_engines() -> set[str]:
    """Engines that accept a written performance note.

    From the merged catalog, so a book on Breeze is still directable when the
    Breeze container is down — otherwise "can this book be directed?" would
    answer differently depending on which containers happen to be up, and
    ``apply_pass`` would quietly stamp nothing."""
    return {e.id for e in engine_options() if e.instruction}


def known_engine_ids() -> set[str]:
    return {e.id for e in engine_options()}


def default_target_engine(script: BookScript, capable: set[str] | None = None) -> str:
    """Which engine a director pass should write for, by default.

    The instruction-capable engine **most lines in this book actually use**,
    falling back to the configured expressive engine. A pass used to be
    written for Breeze on a book with no Breeze line in it: notes in one
    engine's tag vocabulary, aimed at an engine the book does not use, applied
    to nothing.
    """
    capable = capable if capable is not None else instruction_engines()
    counts: dict[str, int] = {}
    for u in script.utterances:
        if u.render.engine in capable:
            counts[u.render.engine] = counts.get(u.render.engine, 0) + 1
    if counts:
        return max(sorted(counts), key=lambda engine: counts[engine])
    return core_settings().expressive_engine


def settings_view() -> Settings:
    core = core_settings()
    return Settings(
        offline=offline(),
        fake_director=fake_director(),
        gateway_url=gateway_url(),
        narrator_engine=core.narrator_engine,
        expressive_engine=core.expressive_engine,
        casting_mode=core.casting_mode,  # type: ignore[arg-type]
        director=resolved_director(),
        export=core.export,
        ffmpeg=ffmpeg_available(),
        book_types=list(BOOK_TYPES),
        engines=engine_options(),
    )


def patch_settings(patch: dict) -> Settings:
    stored = stored_settings()
    for field in ("narrator_engine", "expressive_engine", "casting_mode"):
        if patch.get(field):
            stored[field] = patch[field]
    if patch.get("director"):
        stored["director"] = dict(patch["director"])
    if patch.get("export"):
        stored["export"] = dict(patch["export"])
    _write_json(settings_path(), stored)
    return settings_view()
