"""Wire types for the studio API — one model per interface in docs/STUDIO-API.md.

Two rules kept deliberately:

- **Reuse the pipeline's own models where the shapes are already identical.**
  ``Utterance.render`` *is* the contract's ``Line.render``, ``CastEntry`` *is*
  ``CastEntry``, and ``direct.Pass`` *is* ``Pass``. Re-declaring them here would
  create two definitions of one thing and let them drift silently; a rename in
  ``schema.py`` should break this file loudly instead.
- **Derived state is never a stored field.** ``audio_state``, ``duration_ms``,
  ``fresh`` and the counts are computed in ``store.py`` from the render cache
  every time they are asked for. There is no bookkeeping to get out of sync
  because there is no bookkeeping.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, create_model, field_validator
from typing_extensions import TypedDict

from ..direct import Pass
from ..schema import WIRE_SCHEMA, Attribution, AudioSpan, CastEntry, Intent, Render

AudioState = Literal["cached", "stale", "missing"]
BookStatus = Literal["idle", "rendering", "directing", "exporting", "failed"]
JobKind = Literal["render", "direct", "export"]
JobStatus = Literal["queued", "running", "paused", "done", "failed", "cancelled", "interrupted"]
CastingMode = Literal["narrator_only", "dialogue_expressive", "all_expressive"]
ExportFormat = Literal["m4b", "mp3", "wav", "acx"]
Loudness = Literal["acx", "none"]
ChapterKind = Literal["chapter", "front_matter", "back_matter"]
LineKind = Literal["narration", "dialogue", "internal", "letter", "heading", "verse"]
"""``schema.UtteranceKind``'s values — the only thing ``Line.kind`` is ever
built from — as the closed set the contract names."""


class WireModel(BaseModel):
    """Base of every model here.

    ``json_schema_serialization_defaults_required``: a field with a default is
    always *sent*, so the response schema says it is required rather than
    optional (docs/STUDIO-API.md, Amendment v1.4) — the UI's generated types
    then match the wire instead of sprinkling ``| undefined`` over fields that
    are never missing. It only changes the JSON schema: validation, and the JSON
    a response serializes to, are exactly as before. A model that is both a
    request body and part of a response exports twice, ``-Input``/``-Output``."""

    model_config = WIRE_SCHEMA


class AudioCounts(WireModel):
    cached: int = 0
    stale: int = 0
    missing: int = 0


class DirectedCounts(WireModel):
    directed: int = 0
    total: int = 0


class DirectableCounts(WireModel):
    """Lines routed to an engine that declares ``instruction`` — what a director
    pass can actually stamp. ``POST /direct`` is a 409 when ``lines`` is 0."""

    lines: int = 0
    total: int = 0


class NeedsRenderCounts(WireModel):
    """The two different reasons a book still owes you a render: lines whose
    audio is not cached, and chapters whose WAV does not reflect their lines.
    A single-line re-render empties the first and leaves the second — which is
    the dead end the UI could not label before (Amendments v1.1)."""

    lines: int = 0
    chapters: int = 0


# ---- jobs -------------------------------------------------------------------

class JobProgress(WireModel):
    done: int = 0
    total: int = 0
    cache_hits: int = 0
    synth_calls: int = 0
    rtf: float | None = None
    """Synth wall seconds / audio seconds produced. None until a real synth call
    happens — an all-cache-hit render has no RTF to report, and printing 0.0
    would read as "infinitely fast" rather than "nothing was synthesized"."""
    elapsed_s: float = 0.0
    eta_s: float | None = None


class JobChapter(WireModel):
    index: int
    title: str = ""
    done: int = 0
    total: int = 0
    status: Literal["queued", "running", "done", "failed", "skipped"] = "queued"
    seconds: float = 0.0
    engines: dict[str, int] = Field(default_factory=dict)


class JobLogEntry(WireModel):
    ts: str
    line: str


class JobSummary(WireModel):
    id: str
    kind: JobKind
    book: str
    status: JobStatus = "queued"
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    progress: JobProgress = Field(default_factory=JobProgress)
    error: str | None = None
    result: dict[str, Any] | None = None


class Job(JobSummary):
    chapters: list[JobChapter] = Field(default_factory=list)
    log: list[JobLogEntry] = Field(default_factory=list)
    request: dict[str, Any] = Field(default_factory=dict)

    def summary(self) -> JobSummary:
        return JobSummary(**{k: getattr(self, k) for k in JobSummary.model_fields})


# ---- books ------------------------------------------------------------------

QcStatus = Literal["unheard", "approved", "pickups"]


class QcCounts(WireModel):
    """Chapter sign-off tallies over the chapters that are in the audio."""

    approved: int = 0
    pickups: int = 0
    chapters: int = 0


class BookSummary(WireModel):
    slug: str
    title: str = ""
    author: str = ""
    book_type: str = "unknown"
    profile_label: str = ""
    language: str = "en"
    chapters: int = 0
    utterances: int = 0
    words: int = 0
    est_minutes: float = 0.0
    duration_ms: int = 0
    engines: dict[str, int] = Field(default_factory=dict)
    directed: DirectedCounts = Field(default_factory=DirectedCounts)
    audio: AudioCounts = Field(default_factory=AudioCounts)
    cast: list[CastEntry] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    rendered_at: str | None = None
    last_job: JobSummary | None = None
    status: BookStatus = "idle"
    licence_note: str | None = None
    exportable: bool = False
    """Every chapter is fresh — exactly what ``POST /export`` requires. Derived
    here so the shelf can disable the button instead of offering it and then
    answering 409."""
    directable: DirectableCounts = Field(default_factory=DirectableCounts)
    needs_render: NeedsRenderCounts = Field(default_factory=NeedsRenderCounts)
    qc: QcCounts = Field(default_factory=QcCounts)
    synthetic: bool = False
    """The last render used the fake synth (``STUDIO_OFFLINE=1``): test audio,
    not speech. Recorded per render job on ``book.json`` rather than inferred
    from the current flag, so a book rendered offline still says so after the
    flag is flipped back — the shelf must not present a sine-tone stub as a
    finished audiobook."""


class Line(WireModel):
    id: str
    order: int = 0
    chapter_index: int = 0
    scene_index: int = 0
    speaker: str = "Narrator"
    kind: LineKind = "narration"
    text: str = ""
    intent: Intent = Field(default_factory=Intent)
    render: Render = Field(default_factory=Render)
    audio: AudioSpan | None = None
    audio_state: AudioState = "missing"
    duration_ms: int | None = None
    flag: bool = False
    """QC pickup marker. Stored in the book's ``qc.json`` sidecar while
    ``Utterance`` has no such field — see ``store.qc_notes``."""
    note: str = ""
    """Free-text QC note. Never sent to an engine, never part of a cache key."""
    attribution: Attribution = Field(default_factory=Attribution)
    """``{basis, confidence}`` — the pipeline's own ``schema.Attribution``, reused
    rather than re-declared (the rule at the top of this file). ``Utterance``
    always carries one, so the wire always has an object here; narration is
    ``{basis: "none", confidence: 1.0}``."""


class Scene(WireModel):
    id: str
    chapter_index: int
    scene_index: int
    line_ids: list[str] = Field(default_factory=list)


class Chapter(WireModel):
    index: int
    title: str = ""
    scenes: list[Scene] = Field(default_factory=list)
    audio_file: str | None = None
    duration_ms: int = 0
    lines: int = 0
    directed: int = 0
    audio: AudioCounts = Field(default_factory=AudioCounts)
    fresh: bool = False
    kind: ChapterKind = "chapter"
    include_in_audio: bool = True
    qc_status: QcStatus = "unheard"
    """A person's verdict on the chapter's audio. Lives in the book's
    ``qc-chapters.json`` sidecar; any render that rewrites the chapter resets it
    to "unheard" — a verdict is about a take, not a chapter."""
    """False = the render job skips it and the export leaves it out (a copyright
    page nobody wants read aloud). Kept on ``schema.Chapter`` when that model
    carries the field, and in the book's ``chapters.json`` sidecar until then."""


class Script(WireModel):
    book: BookSummary
    version: int = 1
    profile: dict[str, Any] = Field(default_factory=dict)
    cast: list[CastEntry] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    lines: list[Line] = Field(default_factory=list)


class LinePatchResult(WireModel):
    """``PATCH /books/{slug}/lines/{id}``: the line as it now stands, and the
    script version that edit left (unchanged by a QC-only mark)."""

    line: Line
    version: int


class LineRenderResult(WireModel):
    """``POST /books/{slug}/lines/{id}/render``: the freshly cached line and the
    URL its take plays from."""

    line: Line
    audio_url: str


# ---- director passes ----------------------------------------------------------

PassSummary = create_model(
    "PassSummary",
    __base__=WireModel,
    __doc__="``GET /books/{slug}/passes``: a ``Pass`` without ``scenes``.",
    **{name: (field.annotation, field) for name, field in Pass.model_fields.items() if name != "scenes"},
)
"""Derived from ``direct.Pass`` rather than re-declared, for the reason at the
top of this file: a field added to ``Pass`` reaches the listing too, instead of
being filtered off the wire by a copy that was never updated."""


class PassApplyResult(WireModel):
    """``POST /books/{slug}/passes/{pass_id}/apply``."""

    pass_: Pass = Field(alias="pass")
    script: Script

    model_config = {"populate_by_name": True}


class VersionEntry(WireModel):
    version: int
    saved_at: str
    note: str = ""


class LexiconView(WireModel):
    entries: dict[str, str] = Field(default_factory=dict)
    global_: dict[str, str] = Field(default_factory=dict, alias="global")
    occurrences: dict[str, int] = Field(default_factory=dict)
    """Lines whose text contains the term, per term (v1.1). An entry with 0
    occurrences does nothing to this book — which is the one thing a
    pronunciation editor cannot tell you by looking at it."""

    model_config = {"populate_by_name": True}


class CastSummary(WireModel):
    cast: list[CastEntry] = Field(default_factory=list)
    lines: dict[str, int] = Field(default_factory=dict)
    first_line: dict[str, str] = Field(default_factory=dict)
    shared_voices: list[list[str]] = Field(default_factory=list)
    """Groups of characters that would sound identical (same engine + voice)."""


class ExportFile(WireModel):
    file: str
    url: str
    bytes: int
    created_at: str
    format: str
    loudness: str = "acx"
    sample_rate: int = 44100
    # ---- ACX exports (v1.1): one entry per chapter MP3 ----------------------
    chapter: int | None = None
    duration_ms: int | None = None
    bitrate: str | None = None
    measured: dict[str, float] | None = None
    checks: dict[str, bool] | None = None
    compliance: Literal["pass", "fail"] | None = None


# ---- settings ---------------------------------------------------------------

class VoiceOption(TypedDict):
    """One named voice of an engine. A ``TypedDict`` rather than a model so the
    catalog stays plain dicts (``validate.py`` reads ``v.get("id")``) while the
    export still names the two keys every voice has."""

    id: str
    label: str


class EngineOption(WireModel):
    id: str
    name: str = ""
    available: bool = False
    instruction: bool = False
    audio_tags: list[str] = Field(default_factory=list)
    voices: list[VoiceOption] = Field(default_factory=list)
    license: str = ""
    non_commercial: bool = False


class DirectorSettings(WireModel):
    provider: Literal["ollama", "gemini"] = "ollama"
    model: str = ""


class ExportSettings(WireModel):
    format: ExportFormat = "m4b"
    loudness: Loudness = "acx"
    sample_rate: int = Field(default=44100, ge=8000, le=192000)
    bitrate: str | None = Field(default=None, pattern=r"^\d{1,4}k$")


class Settings(WireModel):
    offline: bool = False
    fake_director: bool = False
    gateway_url: str = ""
    narrator_engine: str = "kokoro"
    expressive_engine: str = "breeze"
    casting_mode: CastingMode = "narrator_only"
    director: DirectorSettings = Field(default_factory=DirectorSettings)
    export: ExportSettings = Field(default_factory=ExportSettings)
    ffmpeg: bool = False
    book_types: list[str] = Field(default_factory=list)
    engines: list[EngineOption] = Field(default_factory=list)


class SettingsPatch(WireModel):
    narrator_engine: str | None = None
    expressive_engine: str | None = None
    casting_mode: CastingMode | None = None
    director: DirectorSettings | None = None
    export: ExportSettings | None = None


class ErrorDetail(WireModel):
    """Every error the studio answers with, 4xx or 5xx: one human-readable line.

    ``app.py`` flattens FastAPI's 422 list and wraps unhandled exceptions into
    this same shape, so it is the only error body a client ever has to parse."""

    detail: str


class Health(WireModel):
    status: Literal["ok"] = "ok"
    gateway: Literal["ok", "unreachable"] = "unreachable"
    offline: bool = False
    fake_director: bool = False


# ---- request bodies ---------------------------------------------------------

class NewBookRequest(WireModel):
    title: str = Field(min_length=1, max_length=200)
    """Required and non-empty: the slug, the folder name and
    the export's filename all come from it, and "Untitled book" as a silent
    default is how a shelf ends up with four of them. ``author`` stays optional —
    a book with no author is a normal book, and the contract is amended to say
    so rather than inventing a required field nobody has."""
    author: str = Field(default="", max_length=200)
    text: str = ""
    book_type: str = "auto"
    voices: Literal["auto", "single", "multi"] = "auto"
    casting_mode: CastingMode | None = None

    @field_validator("title")
    @classmethod
    def _title_is_not_blank(cls, value: str) -> str:
        # `min_length` counts characters, and "   " has three of them — which
        # would name a folder nothing can be typed into.
        if not value.strip():
            raise ValueError("a book needs a title")
        return value.strip()


class RenderRequest(WireModel):
    scope: Literal["all", "stale", "chapters", "lines"] = "stale"
    chapters: list[int] | None = None
    line_ids: list[str] | None = None


class DirectRequest(WireModel):
    scope: Literal["all", "undirected", "chapters", "scenes"] = "all"
    chapters: list[int] | None = None
    scenes: list[str] | None = None
    provider: Literal["ollama", "gemini"] | None = None
    model: str | None = None
    target_engine: str | None = None


class ExportRequest(WireModel):
    format: ExportFormat | None = None
    loudness: Loudness | None = None
    sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    bitrate: str | None = Field(default=None, pattern=r"^\d{1,4}k$")
    """e.g. ``"192k"``. ACX wants 192 kbps CBR; the shape is checked here so a
    typo fails at the request instead of inside an ffmpeg command line."""


class CastingRequest(WireModel):
    mode: CastingMode
    expressive_engine: str | None = None


class CastPatchEntry(WireModel):
    """One row of ``PATCH /cast``. Absent fields are left alone.

    ``create`` exists because a name the book
    does not have is a typo far more often than it is a new character, so it is
    a 404 unless the caller says it meant it."""

    character: str = Field(min_length=1, max_length=120)
    voice_id: str | None = None
    engine: str | None = None
    notes: str | None = None
    voice_design: str | None = None
    create: bool = False

    model_config = {"extra": "forbid"}


class CastMergeRequest(WireModel):
    from_: str = Field(min_length=1, alias="from")
    into: str = Field(min_length=1)

    model_config = {"populate_by_name": True}


class LinePatch(WireModel):
    """``PATCH /books/{slug}/lines/{id}``.

    Every field is optional and ``None`` means "clear"; "absent" and "null" are
    told apart by ``model_fields_set``, never by a falsy check — clearing an
    instruction and not mentioning it are different requests. Range/type checks
    live here so a bad value is a 422 *before* anything touches the script;
    cross-field checks (engine ↔ voice, spoken_text ↔ text ↔ tags) need the
    line and the engine catalog, so they live in ``validate.py``."""

    instruction: str | None = Field(default=None, max_length=2000)
    spoken_text: str | None = Field(default=None, max_length=20000)
    engine: str | None = None
    voice_id: str | None = None
    speed: float | None = None
    text: str | None = Field(default=None, max_length=20000)
    speaker: str | None = Field(default=None, max_length=120)
    flag: bool | None = None
    note: str | None = Field(default=None, max_length=2000)

    model_config = {"extra": "forbid"}


class ChapterPatch(WireModel):
    title: str | None = Field(default=None, max_length=300)
    kind: ChapterKind | None = None
    include_in_audio: bool | None = None
    qc_status: QcStatus | None = None

    model_config = {"extra": "forbid"}


class Decision(WireModel):
    scene_id: str
    decision: Literal["accepted", "edited", "rejected", "kept_text"]
    instruction: str | None = None


class DecideRequest(WireModel):
    decisions: list[Decision] = Field(default_factory=list)


class RetryRequest(WireModel):
    scenes: list[str] = Field(default_factory=list)


class LexiconPut(WireModel):
    entries: dict[str, str] = Field(default_factory=dict)
